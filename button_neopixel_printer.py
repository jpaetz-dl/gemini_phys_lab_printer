#!/usr/bin/env python3
"""
ADS1015 button + pot -> hold-to-record -> NeoPixel pulse + AI receipt print.

The physical button no longer has its own LED (it's a plain momentary switch
wired to A3 on an Adafruit ADS1015 ADC, with a hardware pull-up - reads near
ADC_VCC when open, drops near 0V when pressed). A potentiometer is wired to
A2 on the same ADS1015, for a future brightness control.

The NeoPixel strip's state is driven by the potentiometer on A2 and by the
button/print cycle. Colors, chase/pulse timing, and the pot dim-in range
below all live in config.json (see config_io.py) so they're adjustable live
from the web page, not hardcoded here:
  - pot at/below pot_dim_start_fraction (default 15%): strip off
  - pot at/above pot_full_fraction (default 60%): strip full-brightness
    idle_color (default white) at rest
  - in between: brightness fades linearly rather than snapping on/off
  - button held: a short comet-tail chase animation in chase_color (replaces
    the old Qwiic button's onboard LED as the recording indicator)
  - button released, waiting on the receipt API and then on the print job:
    the strip pulses (breathes between pulse_floor_color and pulse_color),
    and the Jeopardy! "Think Music" theme loops on the USB speaker
  - once the receipt image has actually been printed: pulsing/music stop and
    the strip returns to its normal pot-driven state (off or full brightness)

Press and hold the button while talking:
  - the NeoPixel strip does the comet-tail chase animation
  - audio is recorded from the ReSpeaker mic to an M4A file, for as long as
    the button is held

Release the button:
  - the strip starts pulsing and the Jeopardy! theme loops on the USB speaker
  - concurrently, the recording is POSTed to the receipt-generation API
    (equivalent to:
       curl -X POST -F "audio=@recording.m4a;type=audio/mp4" \
         "https://daily-printer-129172578078.us-central1.run.app/api/generate-receipt?style=computationalHalftone" \
         --output receipt.jpeg
    )
  - once the JPEG comes back AND has been sent to the printer, the
    pulsing/music stop and the strip returns to normal full brightness

Reuses audio_io.py (mic recording/upload) and reflect_and_print.py (response
image extraction + printing) so all three scripts share one implementation.

Dependencies (install with pip3). Note the [usb] extra on python-escpos -
without it, pyusb isn't installed and USB printing fails with
"requires a usb library to be installed". Must be installed for root too
(sudo's Python uses root's own site-packages, separate from your user's):
    sudo pip3 install rpi_ws281x adafruit-circuitpython-ads1x15 pillow requests \
        "python-escpos[usb]" --break-system-packages
    sudo apt install ffmpeg

NeoPixels on GPIO12 use the Pi's PWM0 hardware channel and DMA, so this
script must be run as root (sudo python3 button_neopixel_printer.py).

Find your printer's USB vendor/product IDs with `lsusb`, then set
PRINTER_VENDOR_ID / PRINTER_PRODUCT_ID below. If escpos can't open the
printer as non-root, add a udev rule granting your user access, or just
run this whole script with sudo (needed anyway for the LEDs).
"""

import argparse
import os
import signal
import statistics
import sys
import threading
import time
from collections import deque

from rpi_ws281x import Color, PixelStrip, ws
import board
import busio
import adafruit_ads1x15.ads1015 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

from audio_io import (
    RESPEAKER_DEVICE,
    SAMPLE_RATE,
    CHANNELS,
    SPEAKER_DEVICE,
    start_recording_m4a,
    stop_recording,
    start_looping_playback,
    stop_playback,
    upload_audio,
    set_default_levels,
)
from reflect_and_print import extract_image, print_image, FLIP_180
from status_io import write_status
import config_io

# ---------------------------------------------------------------------------
# Configuration - edit these to match your hardware
# ---------------------------------------------------------------------------

# NeoPixels
LED_COUNT = 38          # number of pixels on the strip
LED_PIN = 12            # GPIO12 (PWM0)
LED_FREQ_HZ = 800000    # LED signal frequency (usually 800khz)
LED_DMA = 10            # DMA channel to use for generating signal
LED_INVERT = False      # True to invert the signal (level shifter)
LED_CHANNEL = 0         # PWM channel 0 for GPIO12/18
LED_MAX_BRIGHTNESS = 255

# These are SK6812 RGBW pixels (4 bytes/pixel - R, G, B, plus a dedicated
# White LED) - not the plain 3-byte WS281x strips rpi_ws281x defaults to.
# Without strip_type set explicitly, PixelStrip sends 3 bytes/pixel, which
# an RGBW strip doesn't understand: each pixel ends up consuming one byte of
# the *next* pixel's data, so colors shift/smear down the whole strip - this
# was the "lights aren't working very well" symptom after switching to RGBW
# hardware. GRBW is the common wire order for SK6812; if colors still come
# out wrong (e.g. red and green swapped), try ws.SK6812_STRIP_RGBW or
# another ws.SK6812_STRIP_*W constant instead.
LED_STRIP_TYPE = ws.SK6812_STRIP_GRBW

OFF_COLOR = Color(0, 0, 0)
PULSE_COUNT = 3            # fallback pulse count when pulse() is run without a stop_event

# Idle/chase/pulse colors, pulse/chase timing, and the pot on/off threshold
# below all live in config.json now (see config_io.py), not as constants
# here, so they can be changed live from the web page (status_web.py's
# settings form) without restarting this script. config_io.DEFAULTS has the
# out-of-the-box values (all start at full-brightness white / the same
# timing this script always used). Each function that needs one of these
# values calls config_io.read_config() itself, at the point of use - NOT
# once into a function default argument, since Python binds defaults at
# definition time and wouldn't pick up later config.json edits.

# Adafruit ADS1015 ADC - replaces the SparkFun Qwiic button. Button is on A3
# (external pull-up: reads near ADC_VCC when open, drops near 0V when
# pressed); potentiometer wiper is on A2.
ADS1015_I2C_ADDRESS = 0x48
BUTTON_ADC_CHANNEL = 3  # A3 - AnalogIn takes plain channel numbers, not named constants
POT_ADC_CHANNEL = 2     # A2
ADC_VCC = 3.3  # supply voltage feeding the button pull-up / pot, for thresholds
BUTTON_PRESSED_VOLTAGE_THRESHOLD = ADC_VCC / 2  # below this = pressed (pulled toward GND)

# I2C on a breadboard/jumper-wire setup can glitch transiently (a nudged wire,
# noise) and raise OSError ("Input/output error") from the underlying smbus
# call. Retry a couple times before giving up rather than crashing the whole
# script or killing the pot-monitor thread over a one-off blip.
ADC_READ_RETRIES = 3
ADC_READ_RETRY_DELAY = 0.05  # seconds between retries

# Potentiometer dim-in range (config_io: "pot_dim_start_fraction" /
# "pot_full_fraction") - at/below the start fraction the strip is off;
# at/above the full fraction it's full-brightness idle_color; in between,
# brightness fades linearly (see strip_brightness_for_pot()) rather than
# snapping straight from off to on. pot_monitor_loop() checks this
# continuously (whenever the strip isn't busy with a press/pulse), so it
# updates live as you turn the pot or change the range from the web page.
POT_POLL_INTERVAL_SECONDS = 0.05

# Occasional single-sample pot reads spike toward ADC_VCC and then vanish on
# the very next read - a potentiometer wiper momentarily losing contact
# (dirty track/vibration) floats toward the pull-up voltage rather than
# reading its actual position. A rolling median over the last few raw reads
# rejects an isolated spike like that (it's outvoted by the two normal
# readings on either side of it) while still tracking a real, sustained turn
# of the pot within a couple of poll intervals.
POT_MEDIAN_WINDOW = 3

# Software debounce/backstop. The mechanical switch can bounce for a few ms
# right at the press/release transitions; requiring several consecutive
# consistent reads before believing an edge filters that out. There's no
# hardware debounce anymore (the Qwiic button's firmware used to handle
# that), so this is the only debounce now - bump these up if presses still
# look noisy.
STABLE_READS_REQUIRED = 4    # consecutive matching reads needed to confirm an edge
STABLE_READ_INTERVAL = 0.01  # seconds between confirmation reads (~40ms total)
POST_RELEASE_GUARD_SECONDS = 0.3  # brief pause before re-arming for the next press

# Printer (USB thermal, ESC/POS). Default: "bt_large" (80mm), per escpos_test.py.
# (Also used as-is by reflect_and_print.print_image() via its own module constants.)
PRINTER_VENDOR_ID = 0x0483
PRINTER_PRODUCT_ID = 0x5743
RECEIPT_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "testReceipt_01_80mm.png")

# ReSpeaker mic recording
AUDIO_OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "recording.m4a")

# "Working" music - loops on the USB speaker while the strip pulses, i.e.
# from button release until the receipt starts printing.
JEOPARDY_CLIP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "01 - Theme from _Jeopardy!_ (Think Music) (From _Jeopardy!_).mp3",
)

# Receipt-generation API (separate from the escpos printer)
RECEIPT_API_URL = "https://daily-printer-129172578078.us-central1.run.app/api/generate-receipt"
RECEIPT_API_STYLE = "computationalHalftone"
RECEIPT_IMAGE_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "receipt.jpeg")

# ---------------------------------------------------------------------------
# Hardware setup - deferred to init_hardware(), NOT run at import time. This
# lets other scripts (status_web.py, status_display.py) import constants and
# print_receipt() from this module without also grabbing the NeoPixel
# PWM/DMA channel or the I2C bus out from under the main process.
# ---------------------------------------------------------------------------

strip = None
i2c = None
ads = None
button_channel = None
pot_channel = None


def init_hardware():
    """Set up the NeoPixel strip and the ADS1015 ADC. main() calls this
    first thing - must happen before init_adc() or anything touching
    strip/button_channel/pot_channel."""
    global strip, i2c, ads, button_channel, pot_channel
    strip = PixelStrip(LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA,
                        LED_INVERT, LED_MAX_BRIGHTNESS, LED_CHANNEL,
                        strip_type=LED_STRIP_TYPE)
    strip.begin()

    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1015(i2c, address=ADS1015_I2C_ADDRESS)
    button_channel = AnalogIn(ads, BUTTON_ADC_CHANNEL)
    pot_channel = AnalogIn(ads, POT_ADC_CHANNEL)


# Tracks an in-progress recording/playback so cleanup() can stop them if the
# script is interrupted mid-hold or mid-"working" animation.
_active_recording_proc = None
_active_playback_proc = None

# Set once main() starts the pot-monitoring thread; cleared to stop it.
_pot_stop_event = threading.Event()

# True while the button is held or the post-release pulse is running, so
# pot_monitor_loop() knows not to fight over the strip during that window.
_strip_busy = False

# Tracks the in-progress chase animation so cleanup() can stop it if the
# script is interrupted mid-hold.
_chase_thread = None
_chase_stop_event = None


def read_voltage(channel, retries=ADC_READ_RETRIES, retry_delay=ADC_READ_RETRY_DELAY):
    """Read an AnalogIn channel's voltage, retrying briefly on OSError (loose
    wire, I2C noise) instead of letting one bad read take down a thread or
    the whole script."""
    last_exc = None
    for attempt in range(retries):
        try:
            return channel.voltage
        except OSError as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(retry_delay)
    raise last_exc


def init_adc():
    try:
        read_voltage(button_channel)
        read_voltage(pot_channel)
    except OSError as exc:
        sys.exit(f"ADS1015 not found on I2C bus (address {hex(ADS1015_I2C_ADDRESS)}) "
                  f"- check wiring/address: {exc}")


def is_button_pressed():
    """True if the button is pressed - the pull-up reads near ADC_VCC when
    open, and gets pulled down toward 0V when the button is held.

    read_voltage() already retries a few times internally on OSError (I2C
    glitches), but if a glitch outlasts those retries this used to let the
    OSError propagate straight up out of wait_for_stable_state()'s debounce
    loop and main()'s bare `while True:` - completely unhandled, which
    crashed the whole script. Unlike pot_monitor_loop() (its own thread,
    already wrapped in a try/except OSError), this runs in the main thread
    where main()'s button/print loop lives, so that crash took the entire
    program down - LEDs frozen at whatever they last showed, pot no longer
    doing anything, button presses no longer registering, until systemd's
    Restart=on-failure kicked in (and if the bus stayed glitchy, eventually
    gave up after its restart-burst limit). Failing safe here (assume
    "released" - the pull-up's normal resting state - and let the next poll
    try again) keeps a transient glitch from being able to take the process
    down at all.
    """
    try:
        return read_voltage(button_channel) < BUTTON_PRESSED_VOLTAGE_THRESHOLD
    except OSError as exc:
        print(f"Button read failed, assuming released: {exc}", file=sys.stderr)
        return False


# Rolling history of raw pot readings for the median filter in
# read_pot_fraction() - module-level since there's only one pot, read from
# both the main thread (set_idle()) and pot_monitor_loop()'s thread.
_pot_reading_history = deque(maxlen=POT_MEDIAN_WINDOW)


def read_pot_fraction():
    """Potentiometer position as a 0.0-1.0 fraction of ADC_VCC, median-
    filtered over the last POT_MEDIAN_WINDOW raw reads.

    Without this, an isolated bad read (wiper losing contact for an
    instant, floating toward ADC_VCC, then re-making contact on the very
    next read) would flash the strip to full brightness for one poll
    interval and then back down - the median throws out that kind of
    single-sample spike (it's outvoted 2-to-1 by the normal readings right
    before and after it) while a real, sustained turn of the pot still
    shows up within a couple of poll intervals.
    """
    raw = max(0.0, min(1.0, read_voltage(pot_channel) / ADC_VCC))
    _pot_reading_history.append(raw)
    return statistics.median(_pot_reading_history)


def _color(rgba):
    """Convert a config.json [r, g, b, w] list into an rpi_ws281x Color.
    `w` is the dedicated White LED on the SK6812 RGBW strip, layered on top
    of the R/G/B mix (not a substitute for it) - dial it up per-phase in
    config.json / the web page's settings form for a warmer glow. Accepts a
    plain 3-element [r, g, b] too (treated as w=0), since config_io.
    read_config() only pads old entries on the way out - callers that build
    a color list by hand don't have to remember the 4th slot.
    """
    w = rgba[3] if len(rgba) > 3 else 0
    return Color(rgba[0], rgba[1], rgba[2], w)


def strip_brightness_for_pot(fraction, dim_start, full):
    """0.0-1.0 brightness for the idle strip given the pot's position and
    the configured dim-in range: 0 at/below dim_start, 1 at/above full,
    fading linearly in between (so the strip dims in rather than snapping
    straight from off to on)."""
    if full <= dim_start:
        # Degenerate/misconfigured range (e.g. someone set both the same,
        # or full < dim_start) - fall back to a hard cutoff at dim_start
        # rather than dividing by zero.
        return 1.0 if fraction >= dim_start else 0.0
    if fraction <= dim_start:
        return 0.0
    if fraction >= full:
        return 1.0
    return (fraction - dim_start) / (full - dim_start)


def idle_color_for_pot(fraction, cfg=None):
    """The strip's resting color for a given pot reading: off at/below the
    configured dim_start fraction, full-brightness idle_color at/above the
    full fraction, and a smooth fade (via lerp_color(), including the white
    channel) in between. `cfg` can be passed in to reuse an already-read
    config (e.g. from pot_monitor_loop's own loop iteration) instead of
    hitting the filesystem again."""
    cfg = cfg or config_io.read_config()
    brightness = strip_brightness_for_pot(fraction, cfg["pot_dim_start_fraction"], cfg["pot_full_fraction"])
    return lerp_color(OFF_COLOR, _color(cfg["idle_color"]), brightness)


def pot_monitor_loop():
    """Print the pot's position periodically, and - whenever the strip isn't
    busy with a press/pulse - keep the idle resting color in sync with it."""
    while not _pot_stop_event.is_set():
        try:
            cfg = config_io.read_config()
            fraction = read_pot_fraction()
            brightness = strip_brightness_for_pot(
                fraction, cfg["pot_dim_start_fraction"], cfg["pot_full_fraction"])
            print(f"Pot: {fraction * ADC_VCC:.2f}V ({fraction * 100:.0f}%) "
                  f"-> strip brightness {brightness * 100:.0f}%")
            write_status(
                pot_fraction=round(fraction, 3),
                pot_voltage=round(fraction * ADC_VCC, 2),
                strip_on=brightness > 0,
                strip_brightness_percent=round(brightness * 100),
            )
            if not _strip_busy:
                set_all(idle_color_for_pot(fraction, cfg=cfg))
        except OSError as exc:
            # Retries in read_voltage() were already exhausted - log and keep
            # the thread alive rather than dying on a transient I2C glitch.
            print(f"Pot read failed, will retry: {exc}", file=sys.stderr)
        _pot_stop_event.wait(POT_POLL_INTERVAL_SECONDS)


def clear_strip():
    if strip is None:
        return  # cleanup() ran before init_hardware() got a chance to - nothing to clear
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, Color(0, 0, 0))
    strip.show()


def set_idle():
    """The strip's steady/resting state, per the pot's current position and
    the configured dim-in range (see idle_color_for_pot())."""
    try:
        fraction = read_pot_fraction()
    except OSError:
        fraction = 0.0  # bus hiccup - fall back to the default idle look
    set_all(idle_color_for_pot(fraction))


def set_all(color):
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, color)
    strip.show()


def _color_channels(color):
    # Color is packed as 0xWWRRGGBB-ish int from rpi_ws281x; pull channels back out.
    white = (color >> 24) & 0xFF
    red = (color >> 16) & 0xFF
    green = (color >> 8) & 0xFF
    blue = color & 0xFF
    return red, green, blue, white


def lerp_color(color_a, color_b, t):
    """Blend between two Colors, including the white channel: t=0 ->
    color_a, t=1 -> color_b. Interpolating white too means the chase tail
    and pulse breathing fade a configured warm-glow white in and out along
    with the RGB mix, instead of it snapping on/off."""
    r1, g1, b1, w1 = _color_channels(color_a)
    r2, g2, b2, w2 = _color_channels(color_b)
    return Color(
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t),
        int(w1 + (w2 - w1) * t),
    )


def pulse(color=None, base_color=None, stop_event=None, times=PULSE_COUNT, step_delay=None):
    """Breathe the whole strip between `base_color` (a dim floor) and `color`
    (full brightness) - it never goes fully dark.

    If `stop_event` is given, pulses repeatedly until it's set (this is the
    "working" animation that runs from button release through the receipt
    API call and the print job itself). Otherwise pulses a fixed `times` and
    stops. Either way, leaves the strip at its normal pot-driven state (off
    or full brightness) when done.

    `color`/`base_color`/`step_delay` default to None so each call reads the
    current pulse_color/pulse_floor_color/pulse_step_delay from config.json
    at call time - letting the web page's settings change take effect on the
    very next pulse, without needing a default argument (which Python would
    only ever evaluate once, at function-definition time).
    """
    cfg = config_io.read_config()
    if color is None:
        color = _color(cfg["pulse_color"])
    if base_color is None:
        base_color = _color(cfg["pulse_floor_color"])
    if step_delay is None:
        step_delay = cfg["pulse_step_delay"]

    steps = 50

    def one_cycle():
        for step in range(steps + 1):          # fade up to full brightness
            set_all(lerp_color(base_color, color, step / steps))
            time.sleep(step_delay)
        for step in range(steps, -1, -1):      # fade back down to the base color
            set_all(lerp_color(base_color, color, step / steps))
            time.sleep(step_delay)

    if stop_event is not None:
        while not stop_event.is_set():
            one_cycle()
    else:
        for _ in range(times):
            one_cycle()

    set_idle()


def chase(color=None, stop_event=None, cycles=1, tail_length=None, step_delay=None):
    """Comet-tail chase animation: a lit pixel with a fading tail travels
    around the strip in a loop.

    If `stop_event` is given, loops until it's set (this is the "button
    held/recording" animation). Otherwise loops `cycles` full trips around
    the strip and stops. Either way, clears the strip when done - the caller
    is expected to immediately start whatever comes next (pulse or idle).

    `color`/`tail_length`/`step_delay` default to None so each call reads
    the current chase_color/chase_tail_length/chase_step_delay from
    config.json at call time (see pulse()'s docstring for why).
    """
    cfg = config_io.read_config()
    if color is None:
        color = _color(cfg["chase_color"])
    if tail_length is None:
        tail_length = cfg["chase_tail_length"]
    if step_delay is None:
        step_delay = cfg["chase_step_delay"]

    n = strip.numPixels()
    position = 0
    completed_cycles = 0

    while True:
        for i in range(n):
            distance = (position - i) % n
            if distance < tail_length:
                brightness = 1.0 - (distance / tail_length)
                strip.setPixelColor(i, lerp_color(OFF_COLOR, color, brightness))
            else:
                strip.setPixelColor(i, OFF_COLOR)
        strip.show()
        time.sleep(step_delay)

        position += 1
        if position >= n:
            position = 0
            completed_cycles += 1

        if stop_event is not None:
            if stop_event.is_set():
                break
        elif completed_cycles >= cycles:
            break

    clear_strip()


def print_receipt(image_path=RECEIPT_IMAGE_PATH):
    """Print a local image file directly - handy for testing the printer
    on its own, independent of the record/upload flow (see --test-image and
    status_web.py's "Test print" button). Returns True on success, False on
    a missing file or a print failure (also logged to stderr either way).

    Applies the same FLIP_180 rotation as reflect_and_print.print_image(),
    since this path bypasses that function entirely - without it, test
    prints came out upside-down relative to the normal record/print flow.
    """
    from escpos.printer import Usb
    from PIL import Image

    if not os.path.isfile(image_path):
        print(f"Receipt image not found: {image_path}", file=sys.stderr)
        return False
    try:
        image_obj = Image.open(image_path)
        if FLIP_180:
            image_obj = image_obj.transpose(Image.ROTATE_180)
        printer = Usb(PRINTER_VENDOR_ID, PRINTER_PRODUCT_ID, profile="default")
        printer.image(image_obj)
        printer.cut()
        printer.close()
        return True
    except Exception as exc:
        print(f"Print failed: {exc}", file=sys.stderr)
        return False


def wait_for_stable_state(target_pressed):
    """Block until is_button_pressed() equals `target_pressed` for
    STABLE_READS_REQUIRED consecutive polls in a row. Used to debounce
    both the press edge and the release edge."""
    while True:
        if is_button_pressed() == target_pressed:
            consecutive = 1
            for _ in range(STABLE_READS_REQUIRED - 1):
                time.sleep(STABLE_READ_INTERVAL)
                if is_button_pressed() == target_pressed:
                    consecutive += 1
                else:
                    break
            if consecutive >= STABLE_READS_REQUIRED:
                return
        time.sleep(0.02)


def on_button_down(audio_path):
    """Button just went down: start the comet-tail chase animation and start recording."""
    print("Button pressed - recording...")
    write_status(state="recording", recording_started_at=time.time(), button_pressed=True)

    global _strip_busy
    _strip_busy = True  # pot_monitor_loop() backs off the strip until we're idle again

    global _chase_stop_event, _chase_thread
    _chase_stop_event = threading.Event()
    _chase_thread = threading.Thread(target=chase, kwargs={"stop_event": _chase_stop_event}, daemon=True)
    _chase_thread.start()

    global _active_recording_proc
    _active_recording_proc = start_recording_m4a(
        audio_path, device=RESPEAKER_DEVICE, rate=SAMPLE_RATE, channels=CHANNELS)


def on_button_up(audio_path, api_url, api_style, receipt_output):
    """Button just released: stop the chase + recording, pulse the strip +
    loop the "working" music, send the audio off, and print whatever receipt
    comes back - stopping the pulse/music once printing actually finishes."""
    print("Button released - stopping recording, pulsing LEDs, and generating receipt.")
    write_status(button_pressed=False)

    # Stop the chase animation now that the button's been released.
    global _chase_stop_event, _chase_thread
    if _chase_stop_event is not None:
        _chase_stop_event.set()
    if _chase_thread is not None:
        _chase_thread.join()
        _chase_thread = None

    # Start the pulse animation and the Jeopardy loop immediately, so there's
    # instant feedback on release. Both run until stop_working_feedback() is
    # called, once the receipt has actually been printed.
    stop_pulse_event = threading.Event()
    pulse_thread = threading.Thread(target=pulse, kwargs={"stop_event": stop_pulse_event}, daemon=True)
    pulse_thread.start()

    global _active_playback_proc
    _active_playback_proc = start_looping_playback(JEOPARDY_CLIP_PATH, device=SPEAKER_DEVICE)

    def stop_working_feedback():
        global _active_playback_proc
        stop_pulse_event.set()
        if _active_playback_proc is not None:
            stop_playback(_active_playback_proc)
            _active_playback_proc = None

    global _active_recording_proc
    proc, _active_recording_proc = _active_recording_proc, None
    if proc is not None:
        stop_recording(proc)  # includes the (fast, but blocking) AAC transcode

    write_status(state="generating")

    request_started = time.monotonic()
    try:
        resp = upload_audio(
            audio_path,
            url=api_url,
            content_type="audio/mp4",
            params={"style": api_style} if api_style else None,
        )
        request_seconds = time.monotonic() - request_started
        print(f"Receipt API responded in {request_seconds:.2f}s")
        image_obj = extract_image(resp)
        try:
            image_obj.convert("RGB").save(receipt_output, "JPEG")
        except Exception as exc:
            print(f"Couldn't save a local copy of the receipt: {exc}", file=sys.stderr)

        write_status(state="printing", last_api_response_seconds=round(request_seconds, 2))
        print_image(image_obj)
        stop_working_feedback()  # image is printed - back to normal full brightness
        write_status(
            state="idle",
            last_print_time=time.time(),
            last_print_style=api_style,
            last_error=None,
        )
    except Exception as exc:
        stop_working_feedback()  # give up cleanly either way
        print(f"Receipt API request failed after {time.monotonic() - request_started:.2f}s: {exc}",
              file=sys.stderr)
        write_status(state="idle", last_error=str(exc), last_error_time=time.time())

    pulse_thread.join()  # pulse() already left the strip at set_idle()'s color
    global _strip_busy
    _strip_busy = False


def cleanup(*_args):
    global _active_recording_proc, _active_playback_proc, _chase_stop_event, _chase_thread
    write_status(state="stopped")
    _pot_stop_event.set()
    if _chase_stop_event is not None:
        _chase_stop_event.set()
    if _chase_thread is not None:
        _chase_thread.join(timeout=2)
        _chase_thread = None
    if _active_recording_proc is not None:
        stop_recording(_active_recording_proc)
        _active_recording_proc = None
    if _active_playback_proc is not None:
        stop_playback(_active_playback_proc)
        _active_playback_proc = None
    clear_strip()
    sys.exit(0)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--audio-output", default=AUDIO_OUTPUT_PATH,
        help=f"Where to save the recording (default: {AUDIO_OUTPUT_PATH})",
    )
    parser.add_argument(
        "--receipt-output", default=RECEIPT_IMAGE_OUTPUT,
        help=f"Where to save a local copy of the returned receipt image "
             f"(default: {RECEIPT_IMAGE_OUTPUT})",
    )
    parser.add_argument(
        "--url", default=RECEIPT_API_URL,
        help=f"Receipt-generation API endpoint (default: {RECEIPT_API_URL})",
    )
    parser.add_argument(
        "--style", default=RECEIPT_API_STYLE,
        help=f"'style' query param sent to the API (default: {RECEIPT_API_STYLE})",
    )
    parser.add_argument(
        "--test-image",
        help="Skip the button/mic loop entirely - just print this local image "
             "file once and exit (for testing the printer on its own).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    if args.test_image:
        print_receipt(args.test_image)
        return

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    write_status(state="starting", started_at=time.time(), api_url=args.url, button_pressed=False)

    init_hardware()

    try:
        cfg = config_io.read_config()
        set_default_levels(mic_percent=cfg["mic_percent"], speaker_percent=cfg["speaker_percent"])
    except Exception as exc:
        # Don't let a mixer-control mismatch (see audio_io.py's MIC_CARD /
        # MIC_CAPTURE_CONTROL etc.) stop the whole script from starting.
        print(f"Couldn't set default mic/speaker levels, continuing anyway: {exc}",
              file=sys.stderr)

    init_adc()
    set_idle()
    write_status(state="idle")

    pot_thread = threading.Thread(target=pot_monitor_loop, daemon=True)
    pot_thread.start()

    print("Ready. Press and hold the button to record, release to send + print "
          "(Ctrl+C to quit)...")

    global _strip_busy
    while True:
        try:
            wait_for_stable_state(True)
            on_button_down(args.audio_output)

            wait_for_stable_state(False)
            on_button_up(args.audio_output, args.url, args.style, args.receipt_output)

            time.sleep(POST_RELEASE_GUARD_SECONDS)
        except Exception as exc:
            # Belt-and-suspenders: is_button_pressed() already fails safe on
            # I2C glitches instead of raising, and on_button_up() already
            # catches its own network/print errors, so this is a backstop
            # for anything else unanticipated - it should rarely if ever
            # fire. The goal is simply that NOTHING here can take the whole
            # process down; whatever broke, log it, force everything back to
            # a clean idle state, and keep going rather than crashing (which
            # would freeze the LEDs and stop responding to the button/pot
            # until systemd restarts it).
            print(f"Unexpected error in main loop, recovering: {exc}", file=sys.stderr)
            write_status(state="idle", last_error=str(exc), last_error_time=time.time())
            if _chase_stop_event is not None:
                _chase_stop_event.set()
            if _chase_thread is not None and _chase_thread.is_alive():
                _chase_thread.join(timeout=2)
            _strip_busy = False
            clear_strip()
            set_idle()
            time.sleep(1)


if __name__ == "__main__":
    main()
