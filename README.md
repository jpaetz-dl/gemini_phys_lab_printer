# Gemini Phys Lab Printer

Raspberry Pi setup: a button + potentiometer wired to an Adafruit ADS1015
ADC, a NeoPixel strip, a ReSpeaker 2-Mics HAT, and a USB thermal receipt
printer. Hold the button and talk, and it prints an AI-generated receipt of
what you said.

One-time hardware/OS setup (ALSA devices, I2C, driver overlays, system
packages) is in [`respeaker_setup_runbook.md`](respeaker_setup_runbook.md) —
do that first. This README covers how to run each script.

## Scripts at a glance

| Script | What it does | Needs sudo? |
|---|---|---|
| `button_neopixel_printer.py` | Main flow: hold button to record, release to pulse + print an AI receipt | Yes |
| `audio_io.py` | Standalone mic/speaker CLI (record/play/test/reflect) | No |
| `reflect_and_print.py` | One-shot: record a fixed-length clip, send it, print the result | No |
| `pi_printer_wifi.py` | Polls a laptop's local server for print jobs over Wi-Fi | No |
| `escpos_test.py` | Printer-only diagnostic tool (no mic/button involved) | No |

---

## `button_neopixel_printer.py`

The main script. Press and hold the button while talking — the NeoPixel
strip turns solid green (there's no LED on the button itself anymore) and
it records from the ReSpeaker mic. Release it, and the strip pulses a soft
white while the recording is sent to the receipt API; whatever JPEG comes
back gets printed, and the pulsing stops right as it does. A potentiometer
on the same ADS1015 is also read and its position printed to the console
(brightness control planned but not wired up yet).

Requires root (GPIO12's PWM/DMA access needs it):

```bash
sudo python3 button_neopixel_printer.py
```

Flags (all optional):

| Flag | Default | Purpose |
|---|---|---|
| `--audio-output PATH` | `recording.m4a` | Where to save the button-hold recording |
| `--receipt-output PATH` | `receipt.jpeg` | Where to save a local copy of the returned receipt image |
| `--url URL` | the daily-printer Cloud Run endpoint | Override the receipt-generation API endpoint |
| `--style STYLE` | `computationalHalftone` | `style` query param sent to the API |
| `--test-image PATH` | — | Skip the button/mic loop entirely; just print this local image file once and exit (for testing the printer by itself) |

Examples:

```bash
# Normal run
sudo python3 button_neopixel_printer.py

# Try a different receipt style
sudo python3 button_neopixel_printer.py --style pencilSketch

# Just confirm the printer works, no button/mic needed
sudo python3 button_neopixel_printer.py --test-image testReceipt_01_80mm.png
```

Ctrl+C stops it cleanly (clears the NeoPixels and stops the pot-monitor
thread; if you interrupt mid-recording it also stops ffmpeg gracefully
rather than leaving a corrupt file).

Hardware constants worth knowing about if yours differ (edit the top of the
file): `LED_COUNT` (38), `LED_PIN` (GPIO12), `ADS1015_I2C_ADDRESS` (`0x48`),
`BUTTON_ADC_CHANNEL`/`POT_ADC_CHANNEL` (A3/A2), `ADC_VCC` (`3.3`, used to
threshold the button's pull-up voltage and normalize the pot reading),
`PRINTER_VENDOR_ID`/`PRINTER_PRODUCT_ID` (`0x0483`/`0x5743`, the "bt_large"
80mm printer).

---

## `audio_io.py`

Low-level mic/speaker utility — this is what `button_neopixel_printer.py` and
`reflect_and_print.py` build on, and it's also useful standalone for testing
the ReSpeaker/speaker independently. Four subcommands:

```bash
# Record 5s (default) to a WAV file
python3 audio_io.py record output.wav
python3 audio_io.py record output.wav -d 10 --device plughw:CARD=seeed2micvoicec,DEV=0

# Play a WAV file back
python3 audio_io.py play output.wav
python3 audio_io.py play output.wav --device plughw:CARD=UACDemoV10,DEV=0

# Record then immediately play back - quick end-to-end mic/speaker check
python3 audio_io.py test
python3 audio_io.py test -o test_recording.wav -d 5 --in-device <dev> --out-device <dev>

# Record to WebM and POST it to the receipt API
python3 audio_io.py reflect
python3 audio_io.py reflect -o reflection.webm -d 10 --url http://10.18.44.99:5005/api/generate-receipt
```

| Subcommand | Flags |
|---|---|
| `record OUTPUT` | `-d/--duration` (default 5s), `--device` (ALSA input) |
| `play INPUT` | `--device` (ALSA output) |
| `test` | `-o/--output` (default `test_recording.wav`), `-d/--duration`, `--in-device`, `--out-device` |
| `reflect` | `-o/--output` (default `reflection.webm`), `-d/--duration` (default 10s), `--device`, `--url` |

Note: the press-and-hold recording in `button_neopixel_printer.py` uses
`start_recording_m4a()`/`stop_recording()` from this module directly (not
exposed as a CLI subcommand here), since it needs an open-ended recording
rather than a fixed duration.

---

## `reflect_and_print.py`

No flags — everything is a constant at the top of the file. Records a fixed
10-second (`RECORD_DURATION`) clip, POSTs it to `API_URL`, and prints
whatever image comes back:

```bash
python3 reflect_and_print.py
```

Edit the top of the file to change `AUDIO_OUTPUT`, `RECORD_DURATION`,
`API_URL`, or the printer connection (`USE_USB`/`VENDOR_ID`/`PRODUCT_ID`/
`BT_PORT`).

---

## `pi_printer_wifi.py`

No flags. Runs forever, polling a laptop's local Express server once a
second for new print jobs and printing whatever it finds:

```bash
python3 pi_printer_wifi.py
```

Edit the top of the file first: `LAPTOP_IP` (your laptop's local network IP),
`USE_USB`, `VENDOR_ID`/`PRODUCT_ID`. Ctrl+C to stop.

---

## `escpos_test.py`

Printer-only diagnostic — no mic, no button, just "can I print to this
printer at all." Most useful when bringing up a new/unfamiliar printer.

```bash
# Named printer profile
python3 escpos_test.py --printer bt_large --image test.png

# Or raw USB IDs (find with `lsusb` on Linux / `system_profiler SPUSBDataType` on macOS)
python3 escpos_test.py --vendor 0x0416 --product 0x5011 --width 384 --image test.png
```

Named profiles in `PRINTERS`: `bt_small` (58mm), `bt_large` (80mm, the one
`button_neopixel_printer.py` defaults to), `rongta` (80mm).

| Flag | Default | Purpose |
|---|---|---|
| `--printer NAME` | — | Named profile (`bt_small`/`bt_large`/`rongta`) |
| `--vendor` / `--product` | — | Raw USB IDs; overrides `--printer` if both given |
| `--image PATH` | *(required)* | Image to print |
| `--width DOTS` | from printer profile | Print width in dots (384=58mm, 576=80mm) |
| `--mode` | `bitImageRaster` | `bitImageRaster` / `bitImageColumn` / `graphics` — try `bitImageRaster` first for clone printers |
| `--flip` | `none` | `none` / `vertical` / `180` — fix upside-down or mirrored output |
| `--text-only` | off | Skip the image, print a diagnostic text line instead |
| `--loop` | off | Print repeatedly until Ctrl+C |
| `--delay SECONDS` | `1.0` | Delay between prints when `--loop` is set |
| `--heat-time` | printer default (~80) | Printhead burn time per dot line (higher = darker/slower) |
| `--heat-interval` | printer default (~2) | Pause between dot lines (higher = slower feed) |
| `--heat-dots` | printer default (~7) | Max simultaneous heating dots (lower = slower, less power) |
| `--in-ep` / `--out-ep` | `0x82` / `0x01` | USB endpoint overrides, rarely needed |

---

## Dependencies

See [`respeaker_setup_runbook.md`](respeaker_setup_runbook.md#8-install-dependencies-for-the-button--neopixel--printer-scripts)
for the full install list (apt + pip). The short version:

```bash
sudo apt install -y ffmpeg libusb-1.0-0 i2c-tools
sudo pip3 install rpi_ws281x adafruit-circuitpython-ads1x15 pillow requests \
    "python-escpos[usb]" --break-system-packages
```

Install with `sudo` even if you already installed for your own user — the
GPIO/printer scripts run as root, which has its own separate site-packages.
