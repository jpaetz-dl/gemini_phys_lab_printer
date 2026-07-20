#!/usr/bin/env python3
"""
reflect_and_print.py -- record a reflection, send it to the receipt API, and
print the image it returns on the thermal printer.

Flow:
  1. Record `RECORD_DURATION` seconds from the ReSpeaker mic to a WebM file
     (reuses audio_io.record_and_send) and POST it to the generate-receipt
     API -- equivalent to:
       curl -X POST <url> -F "audio=@reflection.webm;type=audio/webm"
  2. The API's response carries the receipt image. Decode/print it using the
     same logic as pi_printer_wifi.py: JSON with a base64 "image" field
     (optionally prefixed "data:image/png;base64," or
     "data:image/jpeg;base64,"), or a raw image/* response body.
  3. Print via python-escpos, same connection settings and image/feed/cut
     sequence as pi_printer_wifi.py.

Requires: ffmpeg, requests, pillow, python-escpos (see pi_printer_wifi.py)
"""

import base64
import io
import sys

from PIL import Image
from escpos.printer import Usb, Serial
import requests

from audio_io import record_and_send, RESPEAKER_DEVICE, DEFAULT_API_URL

# ==========================================
# CONFIGURATION
# ==========================================
AUDIO_OUTPUT = "reflection.webm"
RECORD_DURATION = 10  # seconds
API_URL = DEFAULT_API_URL  # http://10.18.44.99:5005/api/generate-receipt

# Printer connection (same as pi_printer_wifi.py)
USE_USB = True
VENDOR_ID = 0x0483    # From `lsusb` on the Pi
PRODUCT_ID = 0x5743
BT_PORT = "/dev/rfcomm0"  # only used if USE_USB is False


def extract_image(resp):
    """Pull a PIL Image out of the API response.

    /api/generate-receipt returns JSON like:
        {"date": ..., "overallTone": ..., "reflectionSentence": ...,
         "highlights": [...], "illustrationSubject": ...,
         "illustrationUrl": "data:image/jpeg;base64,<...>"}

    Also tolerates a raw image/* response body, or an "image" field (the
    shape pi_printer_wifi.py's /api/print/latest uses), in case the API
    changes shape later.
    """
    content_type = resp.headers.get("Content-Type", "")

    if content_type.startswith("image/"):
        return Image.open(io.BytesIO(resp.content))

    data = resp.json()

    for field in ("reflectionSentence", "overallTone", "illustrationSubject", "date"):
        if field in data:
            print(f"  {field}: {data[field]}")

    base64_image = data.get("illustrationUrl") or data.get("image", "")
    if not base64_image:
        raise ValueError(f"No image found in response: {data}")

    # Strip any "data:image/...;base64," prefix, whatever the mime type.
    if base64_image.startswith("data:"):
        base64_image = base64_image.split(",", 1)[1]

    image_bytes = base64.b64decode(base64_image)
    return Image.open(io.BytesIO(image_bytes))


def print_image(image_obj):
    """Print a PIL image on the thermal printer (same process as pi_printer_wifi.py)."""
    print(image_obj.size)
    print("Connecting to ETprin M860 Thermal Printer...")
    if USE_USB:
        p = Usb(VENDOR_ID, PRODUCT_ID)
    else:
        p = Serial(BT_PORT, baudrate=9600)

    try:
        print("Printing dithered receipt raster...")
        # Automatically scales and prints the image onto the 58mm paper roll perfectly
        p.image(image_obj, impl="bitImageColumn")

        # Feed and cut paper
        print("Feeding and slicing paper...")
        p.text("\n\n\n\n")
        p.cut()

        print("[-] Print job completed successfully!")
    finally:
        # Release the USB/serial handle so the next job can reconnect
        p.close()


def main():
    print("==================================================")
    print("Recording reflection and sending to receipt API...")
    print("==================================================")
    try:
        resp = record_and_send(
            output_path=AUDIO_OUTPUT,
            duration=RECORD_DURATION,
            device=RESPEAKER_DEVICE,
            url=API_URL,
        )
        image_obj = extract_image(resp)
        print_image(image_obj)
    except requests.exceptions.RequestException as e:
        print(f"[-] Request to API failed: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[-] Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
