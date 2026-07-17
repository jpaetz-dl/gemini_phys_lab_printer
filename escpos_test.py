#!/usr/bin/env python3
"""
Quick test script for printing an image to a USB ESC/POS thermal printer
on macOS, bypassing CUPS/PostScript entirely.

Setup:
    brew install libusb
    pip3 install python-escpos[all] --break-system-packages

Find your printer's Vendor ID / Product ID:
    system_profiler SPUSBDataType
(look for your printer's entry, IDs shown like "Vendor ID: 0x0416")

Usage:
    python3 escpos_test.py --printer bt_small --image test.png
    python3 escpos_test.py --printer bt_large --image test.png
    python3 escpos_test.py --printer rongta --image test.png

    Or specify raw IDs directly instead of --printer:
    python3 escpos_test.py --vendor 0x0416 --product 0x5011 --width 384 --image test.png
"""

import argparse
import tempfile
import os
import time
from PIL import Image
from escpos.printer import Usb

# Known printers: name -> (vendor_id, product_id, width_mm)
PRINTERS = {
    "bt_small": (0x6868, 0x0200, 58),
    "bt_large": (0x0483, 0x5743, 80),
    "rongta": (0x0fe6, 0x811e, 80),
}

# Print width in dots at 203 dpi
WIDTH_MM_TO_DOTS = {58: 384, 80: 576}


def main():
    parser = argparse.ArgumentParser(description="Test ESC/POS image printing over USB on macOS")
    parser.add_argument("--printer", choices=PRINTERS.keys(), help="Named printer profile (see list above)")
    parser.add_argument("--vendor", help="USB Vendor ID, e.g. 0x0416 (overrides --printer if set)")
    parser.add_argument("--product", help="USB Product ID, e.g. 0x5011 (overrides --printer if set)")
    parser.add_argument("--image", required=True, help="Path to image file (png/jpg)")
    parser.add_argument("--width", type=int, help="Print width in dots (384 for 58mm, 576 for 80mm); overrides --printer default")
    parser.add_argument("--in-ep", type=lambda x: int(x, 0), default=0x82, help="USB IN endpoint (default 0x82)")
    parser.add_argument("--out-ep", type=lambda x: int(x, 0), default=0x01, help="USB OUT endpoint (default 0x01)")
    parser.add_argument(
        "--mode",
        default="bitImageRaster",
        choices=["bitImageRaster", "bitImageColumn", "graphics"],
        help="Image command mode. Try bitImageRaster first for generic/clone printers.",
    )
    parser.add_argument("--text-only", action="store_true", help="Skip the image, just print a line of text (diagnostic)")
    parser.add_argument(
        "--flip",
        default="none",
        choices=["none", "vertical", "180"],
        help=(
            "none: normal, top of image prints/exits first. "
            "vertical: reverse row order only, so bottom of image exits first (left/right unchanged). "
            "180: full rotation, use if 'vertical' also comes out mirrored left-right."
        ),
    )
    parser.add_argument("--loop", action="store_true", help="Print repeatedly until cancelled with Ctrl+C")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between prints when --loop is set (default 1.0)")
    parser.add_argument(
        "--heat-time",
        type=int,
        default=None,
        help=(
            "Printhead burn time per dot line, in units of 10us (printer default is usually ~80). "
            "Higher = slower and darker. Range roughly 3-255. Setting this (or --heat-interval) "
            "sends the ESC 7 heating-parameters command before printing; not all printers support it."
        ),
    )
    parser.add_argument(
        "--heat-interval",
        type=int,
        default=None,
        help=(
            "Pause between dot lines, in units of 10us (printer default is usually ~2). "
            "Higher = slower feed, most direct 'slow down' knob. Range roughly 0-255."
        ),
    )
    parser.add_argument(
        "--heat-dots",
        type=int,
        default=None,
        help="Max simultaneous heating dots setting, 0-255 (printer default is usually ~7, meaning 64 dots). Lower = slower, less power draw.",
    )
    args = parser.parse_args()

    if not args.printer and not (args.vendor and args.product):
        parser.error("Specify --printer NAME, or both --vendor and --product")

    if args.printer:
        default_vendor, default_product, width_mm = PRINTERS[args.printer]
        default_width_dots = WIDTH_MM_TO_DOTS[width_mm]
    else:
        default_vendor = default_product = None
        default_width_dots = 384

    vendor_raw = args.vendor if args.vendor else default_vendor
    product_raw = args.product if args.product else default_product
    width = args.width if args.width else default_width_dots

    vendor_id = int(vendor_raw, 16) if isinstance(vendor_raw, str) else vendor_raw
    product_id = int(product_raw, 16) if isinstance(product_raw, str) else product_raw

    print(f"Connecting to USB printer {hex(vendor_id)}:{hex(product_id)} (width={width} dots) ...")
    p = Usb(vendor_id, product_id, in_ep=args.in_ep, out_ep=args.out_ep)

    if args.heat_time is not None or args.heat_interval is not None or args.heat_dots is not None:
        heat_dots = args.heat_dots if args.heat_dots is not None else 7
        heat_time = args.heat_time if args.heat_time is not None else 80
        heat_interval = args.heat_interval if args.heat_interval is not None else 2
        print(f"Setting heating params: dots={heat_dots}, time={heat_time}, interval={heat_interval} (ESC 7) ...")
        p._raw(bytes([0x1B, 0x37, heat_dots & 0xFF, heat_time & 0xFF, heat_interval & 0xFF]))

    # Precompute the (possibly flipped) image once, outside the print loop.
    image_path = args.image
    temp_path = None
    if not args.text_only and args.flip != "none":
        img = Image.open(args.image)
        if args.flip == "vertical":
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        elif args.flip == "180":
            img = img.transpose(Image.ROTATE_180)
        fd, temp_path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        img.save(temp_path)
        image_path = temp_path

    def print_once():
        if args.text_only:
            print("Printing diagnostic text line ...")
            p.text("Hello from escpos_test.py - if this reads cleanly, connection is fine.\n")
        else:
            print(f"Printing {args.image} at {width} dots wide, mode={args.mode}, flip={args.flip} ...")
            p.image(image_path, impl=args.mode, fragment_height=960)
        p.text("\n")
        p.cut()

    count = 0
    try:
        if args.loop:
            print(f"Looping (delay={args.delay}s). Press Ctrl+C to stop.")
            while True:
                print_once()
                count += 1
                print(f"Printed {count} so far.")
                time.sleep(args.delay)
        else:
            print_once()
            count = 1
            print("Done. Check the printer.")
    except KeyboardInterrupt:
        print(f"\nStopped after {count} print(s).")
    finally:
        if temp_path:
            os.remove(temp_path)


if __name__ == "__main__":
    main()
