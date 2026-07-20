#!/usr/bin/env python3
"""
Qwiic button -> NeoPixel pulse + receipt print.

On button press: pulses a 38-LED NeoPixel strip on GPIO12 three times,
then prints testReceipt_01_80mm.png on a USB thermal receipt printer.

Dependencies (install with pip3). Note the [usb] extra on python-escpos -
without it, pyusb isn't installed and USB printing fails with
"requires a usb library to be installed". Must be installed for root too
(sudo's Python uses root's own site-packages, separate from your user's):
    sudo pip3 install rpi_ws281x sparkfun-qwiic-button "python-escpos[usb]" --break-system-packages

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
import time

from rpi_ws281x import Color, PixelStrip
import qwiic_button
from escpos.printer import Usb

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

# Software debounce/backstop. The mechanical switch can bounce for a few ms
# on both press and release; a few noisy is_pressed() reads right after a
# real press can otherwise look like several extra presses.
STABLE_READS_REQUIRED = 4   # consecutive "pressed" reads needed to confirm a real press
STABLE_READ_INTERVAL = 0.01  # seconds between confirmation reads (~40ms total)
REFRACTORY_SECONDS = 1.0    # ignore the button entirely for this long after a trigger,
                            # which swallows any release-bounce train

# Printer (USB thermal, ESC/POS). Default: "bt_large" (80mm), per escpos_test.py.
PRINTER_VENDOR_ID = 0x0483
PRINTER_PRODUCT_ID = 0x5743
RECEIPT_IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "testReceipt_01_80mm.png")

# ---------------------------------------------------------------------------
# Hardware setup
# ---------------------------------------------------------------------------

strip = PixelStrip(LED_COUNT, LED_PIN, LED_FREQ_HZ, LED_DMA,
                    LED_INVERT, LED_MAX_BRIGHTNESS, LED_CHANNEL)
strip.begin()

button = qwiic_button.QwiicButton(address=BUTTON_I2C_ADDRESS)


def init_button():
    if not button.is_connected():
        sys.exit("Qwiic button not found on I2C bus - check wiring/address.")
    button.begin()
    button.set_debounce_time(DEBOUNCE_MS)
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


def on_button_pressed(image_path=RECEIPT_IMAGE_PATH):
    print("Button pressed - pulsing LEDs and printing receipt.")
    pulse()
    print_receipt(image_path)


def wait_for_stable_press():
    """Block until is_button_pressed() reads True for STABLE_READS_REQUIRED
    consecutive polls in a row, to filter out contact bounce on press."""
    while True:
        if button.is_button_pressed():
            consecutive = 1
            for _ in range(STABLE_READS_REQUIRED - 1):
                time.sleep(STABLE_READ_INTERVAL)
                if button.is_button_pressed():
                    consecutive += 1
                else:
                    break
            if consecutive >= STABLE_READS_REQUIRED:
                return
        time.sleep(0.02)


def cleanup(*_args):
    clear_strip()
    sys.exit(0)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        dest="image_path",
        default=RECEIPT_IMAGE_PATH,
        help="Path to the receipt image to print on button press "
             f"(default: {RECEIPT_IMAGE_PATH})",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)

    init_button()
    clear_strip()
    print(f"Ready. Will print '{args.image_path}' on button press (Ctrl+C to quit)...")

    while True:
        wait_for_stable_press()
        on_button_pressed(args.image_path)
        button.clear_event_bits()

        # Hard-ignore the switch for a bit: this is what actually kills the
        # "3-4 fake presses after a real one" - it swallows the release-bounce
        # train instead of racing it with the outer loop.
        time.sleep(REFRACTORY_SECONDS)

        # Now wait for a clean release (in case the button is somehow still
        # down or still settling) before re-arming for the next press.
        while button.is_button_pressed():
            time.sleep(0.02)
        button.clear_event_bits()


if __name__ == "__main__":
    main()
