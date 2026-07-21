#!/usr/bin/env python3
"""
Qwiic button -> hold-to-record -> NeoPixel pulse + AI receipt print.

Press and hold the button while talking:
  - the button's own LED lights solid (recording indicator)
  - audio is recorded from the ReSpeaker mic to an M4A file, for as long as
    the button is held

Release the button:
  - the button LED turns off
  - the 38-LED NeoPixel strip on GPIO12 starts its pulse animation
  - concurrently, the recording is POSTed to the receipt-generation API
    (equivalent to:
       curl -X POST -F "audio=@recording.m4a;type=audio/mp4" \
         "https://daily-printer-129172578078.us-central1.run.app/api/generate-receipt?style=computationalHalftone" \
         --output receipt.jpeg
    )
  - the JPEG the API returns is printed on the USB thermal receipt printer

Reuses audio_io.py (mic recording/upload) and reflect_and_print.py (response
image extraction + printing) so all three scripts share one implementation.

Dependencies (install with pip3). Note the [usb] extra on python-escpos -
without it, pyusb isn't installed and USB printing fails with
"requires a usb library to be installed". Must be installed for root too
(sudo's Python uses root's own site-packages, separate from your user's):
    sudo pip3 install rpi_ws281x sparkfun-qwiic-button pillow requests \
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
import qwiic_button

from audio_io import (
    RESPEAKER_DEVICE,
    SAMPLE_RATE,
    CHANNELS,
    start_recording_m4a,
    stop_recording,
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
PULSE_COLOR = Color(0, 128, 255)  # color used while pulsing (G, R, B order for ws281x)
PULSE_COUNT = 3
PULSE_STEP_DELAY = 0.008  # seconds between brightness steps; lower = faster pulse

# Qwiic button (default I2C address is 0x6F on SparkFun Qwiic buttons)
BUTTON_I2C_ADDRESS = 0x6F
DEBOUNCE_MS = 150          # hardware debounce, passed to the button itself
BUTTON_LED_BRIGHTNESS = 255  # brightness of the button's own LED while recording

# Software debounce/backstop. The mechanical switch can bounce for a few ms
# right at the press/release transitions; requiring several consecutive
# consistent reads before believing an edge filters that out.
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

button = qwiic_button.QwiicButton(address=BUTTON_I2C_ADDRESS)

# Tracks an in-progress recording so cleanup() can stop it if the script is
# interrupted mid-hold.
_active_recording_proc = None


def init_button():
    if not button.is_connected():
        sys.exit("Qwiic button not found on I2C bus - check wiring/address.")
    button.begin()
    button.set_debounce_time(DEBOUNCE_MS)
    button.LED_off()
    # Clear any stale event flags left over from before this script started.
    button.clear_event_bits()


def clear_strip():
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, Color(0, 0, 0))
    strip.show()


def set_all(color):
    for i in range(strip.numPixels()):
        strip.setPixelColor(i, color)
    strip.show()


def scale_color(color, brightness_0_to_1):
    # Color is packed as 0xWWRRGGBB-ish int from rpi_ws281x; pull channels back out.
    white = (color >> 24) & 0xFF
    red = (color >> 16) & 0xFF
    green = (color >> 8) & 0xFF
    blue = color & 0xFF
    return Color(
        int(red * brightness_0_to_1),
        int(green * brightness_0_to_1),
        int(blue * brightness_0_to_1),
    )


def pulse(times=PULSE_COUNT, color=PULSE_COLOR):
    """Fade the whole strip up and down `times` times."""
    steps = 50
    for _ in range(times):
        for step in range(steps + 1):          # fade in
            set_all(scale_color(color, step / steps))
            time.sleep(PULSE_STEP_DELAY)
        for step in range(steps, -1, -1):      # fade out
            set_all(scale_color(color, step / steps))
            time.sleep(PULSE_STEP_DELAY)
    clear_strip()


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
        if button.is_button_pressed() == target_pressed:
            consecutive = 1
            for _ in range(STABLE_READS_REQUIRED - 1):
                time.sleep(STABLE_READ_INTERVAL)
                if button.is_button_pressed() == target_pressed:
                    consecutive += 1
                else:
                    break
            if consecutive >= STABLE_READS_REQUIRED:
                return
        time.sleep(0.02)


def on_button_down(audio_path):
    """Button just went down: light it up and start recording."""
    print("Button pressed - recording...")
    button.LED_on(BUTTON_LED_BRIGHTNESS)
    global _active_recording_proc
    _active_recording_proc = start_recording_m4a(
        audio_path, device=RESPEAKER_DEVICE, rate=SAMPLE_RATE, channels=CHANNELS)


def on_button_up(audio_path, api_url, api_style, receipt_output):
    """Button just released: stop recording, pulse the strip, send the
    audio off, and print whatever receipt comes back."""
    print("Button released - stopping recording, pulsing LEDs, and generating receipt.")
    button.LED_off()

    global _active_recording_proc
    proc, _active_recording_proc = _active_recording_proc, None
    if proc is not None:
        stop_recording(proc)

    # Start the pulse animation and the upload/print sequence at the same time.
    pulse_thread = threading.Thread(target=pulse, daemon=True)
    pulse_thread.start()

    try:
        resp = upload_audio(
            audio_path,
            url=api_url,
            content_type="audio/mp4",
            params={"style": api_style} if api_style else None,
        )
        image_obj = extract_image(resp)
        try:
            image_obj.convert("RGB").save(receipt_output, "JPEG")
        except Exception as exc:
            print(f"Couldn't save a local copy of the receipt: {exc}", file=sys.stderr)
        print_image(image_obj)
    except Exception as exc:
        print(f"Receipt generation/print failed: {exc}", file=sys.stderr)

    pulse_thread.join()


def cleanup(*_args):
    global _active_recording_proc
    if _active_recording_proc is not None:
        stop_recording(_active_recording_proc)
        _active_recording_proc = None
    try:
        button.LED_off()
    except Exception:
        pass
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

    init_button()
    clear_strip()
    print("Ready. Press and hold the button to record, release to send + print "
          "(Ctrl+C to quit)...")

    while True:
        wait_for_stable_state(True)
        on_button_down(args.audio_output)

        wait_for_stable_state(False)
        on_button_up(args.audio_output, args.url, args.style, args.receipt_output)

        button.clear_event_bits()
        time.sleep(POST_RELEASE_GUARD_SECONDS)


if __name__ == "__main__":
    main()
