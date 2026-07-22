#!/usr/bin/env python3
"""
ADS1015 button + pot -> hold-to-record -> NeoPixel pulse + AI receipt print.

The physical button no longer has its own LED (it's a plain momentary switch
wired to A3 on an Adafruit ADS1015 ADC, with a hardware pull-up - reads near
ADC_VCC when open, drops near 0V when pressed). A potentiometer is wired to
A2 on the same ADS1015, for a future brightness control.

Press and hold the button while talking:
  - the NeoPixel strip on GPIO12 turns solid green (recording indicator -
    replaces the old Qwiic button's onboard LED)
  - audio is recorded from the ReSpeaker mic to an M4A file, for as long as
    the button is held

Release the button:
  - the 38-LED NeoPixel strip starts pulsing a soft white (its steady-state
    color is a dim, resting version of the same white) and the Jeopardy!
    "Think Music" theme loops on the USB speaker
  - concurrently, the recording is POSTed to the receipt-generation API
    (equivalent to:
       curl -X POST -F "audio=@recording.m4a;type=audio/mp4" \
         "https://daily-printer-129172578078.us-central1.run.app/api/generate-receipt?style=computationalHalftone" \
         --output receipt.jpeg
    )
  - once the JPEG comes back, the pulsing and music stop (back to the dim
    steady state) right as it's sent to the USB thermal receipt printer

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
import sys
import threading
import time

from rpi_ws281x import Color, PixelStrip
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
)
from reflect_and_print import extract_image, print_image

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
IDLE_COLOR = Color(15, 15, 15)     # soft, dim white - steady-state / resting color
PULSE_COLOR = Color(255, 255, 255)  # same white, pulsed brighter, while working
BUTTON_COLOR = Color(0, 255, 0)    # solid green while the button is held (no LED on the button itself anymore)
PULSE_COUNT = 3            # fallback pulse count when pulse() is run without a stop_event
PULSE_STEP_DELAY = 0.02   # seconds between brightness steps; lower = faster pulse

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

# Potentiometer - just monitored/printed for now. Eventually: boost NeoPixel
# brightness once the pot is turned past this fraction (not wired up yet).
POT_BRIGHTNESS_THRESHOLD_FRACTION = 0.75
POT_POLL_INTERVAL_SECONDS = 0.5

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
# Hardware setup
# ---------------------------------------------------------------------------

strip = PixelStrip(LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA,
                    LED_INVERT, LED_MAX_BRIGHTNESS, LED_CHANNEL)
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
    open, and gets pulled down toward 0V when the button is held."""
    return read_voltage(button_channel) < BUTTON_PRESSED_VOLTAGE_THRESHOLD


def read_pot_fraction():
    """Potentiometer position as a 0.0-1.0 fraction of ADC_VCC."""
    return max(0.0, min(1.0, read_voltage(pot_channel) / ADC_VCC))


def pot_monitor_loop():
    """Print the pot's position periodically - just for wiring/calibration
    right now. This is where the eventual "boost NeoPixel brightness past
    POT_BRIGHTNESS_THRESHOLD_FRACTION" logic will hook in."""
    while not _pot_stop_event.is_set():
        try:
            fraction = read_pot_fraction()
            print(f"Pot: {fraction * ADC_VCC:.2f}V ({fraction * 100:.0f}%)")
        except OSError as exc:
            # Retries in read_voltage() were already exhausted - log and keep
            # the thread alive rather than dying on a transient I2C glitch.
            print(f"Pot read failed, will retry: {exc}", file=sys.stderr)
        _pot_stop_event.wait(POT_POLL_INTERVAL_SECONDS)


def clear_strip():
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, Color(0, 0, 0))
    strip.show()


def set_idle():
    """Soft, dim white - the strip's steady/resting state."""
    set_all(IDLE_COLOR)


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
    """Blend between two Colors: t=0 -> color_a, t=1 -> color_b."""
    r1, g1, b1, _ = _color_channels(color_a)
    r2, g2, b2, _ = _color_channels(color_b)
    return Color(
        int(r1 + (r2 - r1) * t),
        int(g1 + (g2 - g1) * t),
        int(b1 + (b2 - b1) * t),
    )


def pulse(color=PULSE_COLOR, base_color=IDLE_COLOR, stop_event=None, times=PULSE_COUNT):
    """Breathe the whole strip between `base_color` (the dim idle color) and
    `color` (full brightness) - it never goes fully dark.

    If `stop_event` is given, pulses repeatedly until it's set (this is the
    "working" animation that runs from button release until the receipt
    starts printing). Otherwise pulses a fixed `times` and stops. Either way,
    leaves the strip at the dim white idle/steady state when done.
    """
    steps = 50

    def one_cycle():
        for step in range(steps + 1):          # fade up to full brightness
            set_all(lerp_color(base_color, color, step / steps))
            time.sleep(PULSE_STEP_DELAY)
        for step in range(steps, -1, -1):      # fade back down to the base color
            set_all(lerp_color(base_color, color, step / steps))
            time.sleep(PULSE_STEP_DELAY)

    if stop_event is not None:
        while not stop_event.is_set():
            one_cycle()
    else:
        for _ in range(times):
            one_cycle()

    set_idle()


def print_receipt(image_path=RECEIPT_IMAGE_PATH):
    """Print a local image file directly - handy for testing the printer
    on its own, independent of the record/upload flow (see --test-image)."""
    from escpos.printer import Usb

    if not os.path.isfile(image_path):
        print(f"Receipt image not found: {image_path}", file=sys.stderr)
        return
    try:
        printer = Usb(PRINTER_VENDOR_ID, PRINTER_PRODUCT_ID, profile="default")
        printer.image(image_path)
        printer.cut()
        printer.close()
    except Exception as exc:
        print(f"Print failed: {exc}", file=sys.stderr)


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
    """Button just went down: turn the strip green and start recording."""
    print("Button pressed - recording...")
    set_all(BUTTON_COLOR)
    global _active_recording_proc
    _active_recording_proc = start_recording_m4a(
        audio_path, device=RESPEAKER_DEVICE, rate=SAMPLE_RATE, channels=CHANNELS)


def on_button_up(audio_path, api_url, api_style, receipt_output):
    """Button just released: stop recording, pulse the strip + loop the
    "working" music, send the audio off, and print whatever receipt comes
    back - stopping the pulse/music right as printing starts."""
    print("Button released - stopping recording, pulsing LEDs, and generating receipt.")

    # Start the pulse animation and the Jeopardy loop immediately, so there's
    # instant feedback on release. Both run until stop_working_feedback() is
    # called, right before the receipt is sent to the printer.
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

    request_started = time.monotonic()
    try:
        resp = upload_audio(
            audio_path,
            url=api_url,
            content_type="audio/mp4",
            params={"style": api_style} if api_style else None,
        )
        print(f"Receipt API responded in {time.monotonic() - request_started:.2f}s")
        image_obj = extract_image(resp)
        try:
            image_obj.convert("RGB").save(receipt_output, "JPEG")
        except Exception as exc:
            print(f"Couldn't save a local copy of the receipt: {exc}", file=sys.stderr)

        stop_working_feedback()  # paper's about to start printing
        print_image(image_obj)
    except Exception as exc:
        stop_working_feedback()  # give up cleanly either way
        print(f"Receipt API request failed after {time.monotonic() - request_started:.2f}s: {exc}",
              file=sys.stderr)

    pulse_thread.join()


def cleanup(*_args):
    global _active_recording_proc, _active_playback_proc
    _pot_stop_event.set()
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

    init_adc()
    set_idle()

    pot_thread = threading.Thread(target=pot_monitor_loop, daemon=True)
    pot_thread.start()

    print("Ready. Press and hold the button to record, release to send + print "
          "(Ctrl+C to quit)...")

    while True:
        wait_for_stable_state(True)
        on_button_down(args.audio_output)

        wait_for_stable_state(False)
        on_button_up(args.audio_output, args.url, args.style, args.receipt_output)

        time.sleep(POST_RELEASE_GUARD_SECONDS)


if __name__ == "__main__":
    main()
