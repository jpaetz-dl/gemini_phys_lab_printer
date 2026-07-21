# Pi Audio Setup Runbook: USB Speaker + ReSpeaker 2-Mics HAT v2.0

Target: Raspberry Pi with ReSpeaker 2-Mics Pi HAT v2.0 (mic input) + USB speaker (audio output). Goal of this pass: confirm both devices work independently before wiring up recording → remote API.

## 1. Identify the USB speaker

```bash
aplay -l
```

Find your speaker's card entry, e.g. `card 1: UACDemoV10 [USB Audio Device], device 0: ...`. Note the **ID string** (`UACDemoV10`) — that's what goes after `CARD=`.

## 2. Test the USB speaker (generates tone locally, no file needed)

```bash
speaker-test -D plughw:CARD=UACDemoV10,DEV=0 -c2 -t sine -f 440 -l1
```

Ctrl+C to stop.

**Gotcha hit on this run:** `DEV=` is the device index *under* the card (almost always `0`), not the card number. Setting `DEV=1` (mistaking it for the card index) throws `playback open error: -2, No such file or directory`. Always leave `DEV=0` unless `aplay -l` explicitly lists a device 1+ under that card.

## 3. Install the ReSpeaker 2-Mics v2.0 driver (device tree overlay)

The legacy `seeed-voicecard` install script is deprecated and can corrupt the desktop / fail to detect on current Raspberry Pi OS. Use the overlay method:

```bash
sudo apt update && sudo apt install -y device-tree-compiler

curl -O https://raw.githubusercontent.com/Seeed-Studio/seeed-linux-dtoverlays/refs/heads/master/overlays/rpi/respeaker-2mic-v2_0-overlay.dts
dtc -I dts -O dtb -o respeaker-2mic-v2_0-overlay.dtbo respeaker-2mic-v2_0-overlay.dts

sudo cp respeaker-2mic-v2_0-overlay.dtbo /boot/firmware/overlays/
echo "dtoverlay=respeaker-2mic-v2_0-overlay" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

Note: on older Raspberry Pi OS releases the paths are `/boot/config.txt` and `/boot/overlays/` instead of the `/boot/firmware/...` versions. Check with:

```bash
ls /boot/firmware/config.txt 2>/dev/null || echo "use /boot/config.txt instead"
```

## 4. Confirm the ReSpeaker is detected

```bash
aplay -l
arecord -l
```

Look for a card named `seeed2micvoicec`. Note its card number for later commands.

## 5. Set ALSA levels with alsamixer

```bash
alsamixer
```

- Press **F6**, select `seeed2micvoicecard` as the active sound card.
- On the **Playback** view (default), set the speaker volume — used **40**.
- Press **F4** to switch to the **Capture** view, set the mic capture level — used **40**.
- Esc or Ctrl+C to exit.

Without raising the capture level here, recordings from the mics come in silent/too quiet.

## 6. Record a test clip with the ReSpeaker mics

```bash
arecord -D plughw:CARD=seeed2micvoicec,DEV=0 -c2 -r 16000 -f S16_LE -d 5 respeaker_test.wav
```

## 7. Play the recording back through the USB speaker

```bash
aplay -D plughw:CARD=UACDemoV10,DEV=0 respeaker_test.wav
```

Confirms mic recording quality using the speaker already verified in step 2.

## 8. Install dependencies for the button / NeoPixel / printer scripts

These cover `button_neopixel_printer.py`, `audio_io.py`, `reflect_and_print.py`,
`pi_printer_wifi.py`, and `escpos_test.py`.

### System packages (apt)

```bash
sudo apt update
sudo apt install -y ffmpeg libusb-1.0-0 i2c-tools
```

- `ffmpeg` — used by `audio_io.py` to record straight to WebM/Opus or M4A/AAC.
- `libusb-1.0-0` — backend `pyusb` needs to talk to the USB thermal printer.
- `i2c-tools` — optional, but `i2cdetect -y 1` is the fastest way to confirm the
  Qwiic button is wired correctly (look for it at address `0x6f`).

### Enable I2C (needed for the Qwiic button)

```bash
sudo raspi-config nonint do_i2c 0
sudo reboot
```

Or via the UI: `raspi-config` → *Interface Options* → *I2C* → enable.

### Python packages (pip)

The GPIO/PWM (NeoPixel) and printer scripts need to run as root, and `sudo`
uses root's own site-packages — separate from your user's — so install these
**with sudo** too, or they'll appear "missing" the moment you run the script
with `sudo python3 ...` even though `pip3 list` shows them for your user:

```bash
sudo pip3 install rpi_ws281x sparkfun-qwiic-button pillow requests \
    "python-escpos[usb]" --break-system-packages
```

- `rpi_ws281x` — drives the NeoPixel strip on GPIO12 (PWM0/DMA).
- `sparkfun-qwiic-button` — the `qwiic_button` module for the SparkFun Qwiic button.
- `pillow` — image handling for the receipts (`PIL.Image`).
- `requests` — POSTing audio to the receipt-generation API.
- `python-escpos[usb]` — **the `[usb]` extra matters.** Plain `python-escpos`
  doesn't pull in `pyusb`, and printing then fails with "Printing with USB
  connection requires a usb library to be installed" even though escpos itself
  imported fine.

### Gotchas

- **Must run as root:** `sudo python3 button_neopixel_printer.py` — required
  for the NeoPixel PWM/DMA access; the printer and I2C button also end up
  needing root in the same process.
- **`[usb]` extra + sudo, together:** the USB printing error above almost
  always means one of these two was skipped, not that pyusb/libusb are
  actually absent from the system.
- **Onboard audio vs. GPIO12/18:** the Pi's onboard analog audio jack uses the
  same PWM peripheral as GPIO12/18. Since this setup uses a USB speaker
  (`UACDemoV10`, see step 1), this hasn't been an issue — but if a future Pi
  drives audio through the 3.5mm jack instead, add `dtparam=audio=off` to
  `config.txt` before wiring NeoPixels to GPIO12.

## 9. USB permissions for the thermal printer (udev rule)

On a fresh Pi, printing without `sudo` fails with something like:

```
escpos.exceptions.DeviceNotFoundError: Device not found (Unable to open USB printer on (1155, 22339):
[Errno 13] Access denied (insufficient permissions))
```

`(1155, 22339)` is just `(0x0483, 0x5743)` in decimal — the bt_large
printer's vendor/product ID. By default only root can open USB devices, so
either run the print scripts with `sudo`, or add a one-time udev rule so your
regular user can too:

```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="0483", ATTR{idProduct}=="5743", MODE="0666"' | sudo tee /etc/udev/rules.d/99-thermal-printer.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Then unplug and replug the printer's USB cable (or reboot) so the rule takes
effect. If you're using a different printer profile (`bt_small`, `rongta`,
or raw IDs), swap in that printer's `idVendor`/`idProduct` from `lsusb`
instead.

Note this doesn't remove the need for `sudo` on `button_neopixel_printer.py`
— that script still needs root for the NeoPixel GPIO/PWM access regardless.

## 10. Run `button_neopixel_printer.py` on boot (systemd)

The main script needs to run as root (GPIO12 PWM/DMA) and should come back
up automatically after a power cycle or crash, so use a systemd service
rather than a cron `@reboot` line or autostart script.

### Create the unit file

```bash
sudo nano /etc/systemd/system/button-printer.service
```

```ini
[Unit]
Description=Qwiic button / NeoPixel / receipt printer
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/pi/gemini_phys_lab_printer
ExecStart=/usr/bin/python3 /home/pi/gemini_phys_lab_printer/button_neopixel_printer.py
Restart=on-failure
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

`/home/pi/gemini_phys_lab_printer` above is a placeholder — the actual
username varies per device (e.g. `printbot2`), so swap in the real path
both places. **`ExecStart` must include the script filename, not just the
folder** — pointing it at the directory alone (`.../gemini_phys_lab_printer`
with no `/button_neopixel_printer.py` on the end) fails at every boot with:

```
python3: can't find '__main__' module in '/home/printbot2/gemini_phys_lab_printer'
```

because Python treats a bare directory argument as a package to run via
`__main__.py`, which doesn't exist here.

### Enable and start it

```bash
sudo systemctl daemon-reload
sudo systemctl enable button-printer.service   # starts automatically on future boots
sudo systemctl start button-printer.service    # starts it right now, without rebooting
```

### Check on it / make changes

```bash
sudo systemctl status button-printer.service   # is it running?
journalctl -u button-printer.service -f        # live console output (prints, errors, timing)

sudo systemctl restart button-printer.service  # after editing the .py file
sudo systemctl stop button-printer.service      # to stop it (e.g. for manual testing)
sudo systemctl disable button-printer.service   # stop it from running at boot
```

### Gotchas

- **`Restart=on-failure` matters:** at boot, I2C/USB devices may not be
  enumerated the instant the service starts. If `init_button()` fails once,
  systemd just retries a few seconds later instead of leaving it dead.
- **Ctrl+C doesn't apply here:** since it's running under systemd as root,
  not in your terminal, use `sudo systemctl stop button-printer.service`
  to stop it instead.

---

### Notes for replicating on other devices
- Card numbers/IDs (`UACDemoV10`, `seeed2micvoicec`) can shift depending on what's plugged in and enumeration order — always re-check with `aplay -l` / `arecord -l` on each new device rather than assuming the same numbers.
- Steps 3–5 (driver install + alsamixer levels) are the one-time setup per device; steps 1–2 and 6–7 are the verification/test steps.
