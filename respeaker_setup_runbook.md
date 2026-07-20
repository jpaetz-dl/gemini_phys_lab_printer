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

---

### Notes for replicating on other devices
- Card numbers/IDs (`UACDemoV10`, `seeed2micvoicec`) can shift depending on what's plugged in and enumeration order — always re-check with `aplay -l` / `arecord -l` on each new device rather than assuming the same numbers.
- Steps 3–5 (driver install + alsamixer levels) are the one-time setup per device; steps 1–2 and 6–7 are the verification/test steps.
