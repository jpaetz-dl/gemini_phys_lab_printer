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
| `audio_io.py` | Standalone mic/speaker CLI (record/play/test/reflect/levels) | No |
| `reflect_and_print.py` | One-shot: record a fixed-length clip, send it, print the result | No |
| `pi_printer_wifi.py` | Polls a laptop's local server for print jobs over Wi-Fi | No |
| `escpos_test.py` | Printer-only diagnostic tool (no mic/button involved) | No |
| `status_io.py` | Shared status-file helper (imported by the three below, not run directly) | No |
| `config_io.py` | Shared settings-file helper (LED colors/timing, pot dim-in range, audio levels) | No |
| `status_display.py` | Full-screen status dashboard, meant for the Pi's HDMI console | Yes |
| `status_web.py` | LAN-accessible status page with test-print/restart/settings controls | Yes |
| `reload_services.sh` | Restarts the three systemd services after a code change | Yes |
| `stop_services.sh` | Stops the three systemd services, to test by hand without contention | Yes |
| `i2c_diag.py` | Standalone pot/button reader for diagnosing I2C bus issues | Yes |

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

The button/pot ADC reads and the main press/release loop are both resilient
to I2C problems now, covering two different failure modes:

- **A quick-raising glitch** (loose wire, electrical noise) - `read_voltage()`
  retries a couple times (`ADC_READ_RETRIES`/`ADC_READ_RETRY_DELAY`), and a
  failed read fails safe (button reads as "released", the loop logs and
  resets to idle) instead of raising and taking the whole process down. This
  used to be able to crash the script entirely - LEDs frozen at whatever
  they last showed, pot/button unresponsive - until systemd restarted it (or
  gave up, if the glitch was persistent enough to exceed the restart-burst
  limit).
- **A genuinely wedged bus** (an electrical fault holding SDA/SCL, a stuck
  device) - this doesn't raise anything, it makes the underlying smbus call
  block *forever*, which no amount of retrying-on-exception can catch since
  nothing ever returns or raises. `read_voltage()` now bounds each attempt
  with a hard timeout (`ADC_READ_TIMEOUT_SECONDS`, 0.2s) instead of calling
  `channel.voltage` directly, so a truly stuck bus now fails the same way a
  quick glitch does (bounded, logged, safe) instead of freezing the
  button-polling loop and the pot-monitor thread forever - both of which
  share the same I2C bus, so a wedge affects both LEDs and the button at
  the same time. If the bus recovers, subsequent reads pick back up
  automatically - no restart needed.

  Getting this right took three attempts, all worth knowing about if you're
  touching this code:
  1. The first version ran every single retry attempt in a brand-new
     disposable thread and just abandoned it on timeout. That bounds any
     *one* read, but not a *sustained* fault: pot_monitor_loop() polls
     forever, so a bus that stays bad (or is just consistently a bit slower
     than the timeout) piled up new threads faster than a stuck-but-not-dead
     bus could drain them - an unbounded leak that got worse the faster the
     poll loop ran, which is exactly backwards from the goal.
  2. The fix for that used `concurrent.futures.ThreadPoolExecutor` with a
     small fixed worker count - bounded thread count, but it introduced a
     *different* serious bug: `ThreadPoolExecutor` registers a global
     `atexit` hook that joins every worker thread before the process is
     allowed to fully exit. A worker stuck forever on a truly dead I2C read
     would then block `sys.exit()`, Ctrl+C, and `systemctl restart
     button-printer.service` from ever completing - the opposite of what a
     timeout is supposed to buy you. The fix for *that* was a hand-rolled
     `_InFlightRead` (`threading.Event` + a plain
     `threading.Thread(daemon=True)`), with at most one outstanding read
     tracked *per channel* (`id(channel)` in a dict) - retries on the same
     channel reused the one in-flight read instead of starting new ones, so
     a sustained fault was bounded at exactly one leaked thread per channel
     (two total, for button + pot), and daemon threads can't block process
     exit either way.
  3. That per-channel version still hung occasionally, and - the key clue -
     `i2c_diag.py` (see below) could run for a long time with **zero**
     errors while the real script still froze. i2c_diag.py is deliberately
     single-threaded, reading one channel at a time; the real script has
     the button-polling loop (main thread, polling continuously via
     `wait_for_stable_state()`) and `pot_monitor_loop()` (its own thread)
     both hitting the *same* ADS1015 constantly, on two different threads,
     with nothing coordinating between them. The adafruit_ads1x15 driver was
     never written to be thread-safe - a "read voltage" call is actually a
     short sequence of I2C transactions (write a config register, then read
     the conversion result), and if the button thread's sequence and the
     pot thread's sequence interleave mid-transaction, the bus can end up
     in a corrupted state where the underlying smbus call never returns -
     which looks exactly like a wedged bus, but is self-inflicted by
     concurrent access, not a wiring fault. This lined up with everything
     observed: no errors from a single-threaded diagnostic, hangs from the
     real (multi-threaded) script, and the hangs getting *worse* as
     `POT_LED_UPDATE_INTERVAL_SECONDS` got turned down - a faster pot loop
     means more chances for its reads to land at the same instant as the
     button loop's.

  What's actually in the code now: a single, permanent, daemon
  `_I2CWorker` thread that owns *all* I2C access - both channels' reads go
  through it via a queue, processed strictly one at a time, so only one
  real transaction is ever on the bus regardless of which channel or thread
  asked for it. That's the actual fix for the interleaving bug (nothing
  about timeouts alone could have fixed it - the earlier versions just
  bounded the *symptom*). It's also simpler and safer than the two
  per-channel versions before it: exactly one thread exists for the entire
  life of the process, not "up to one per channel" - there's no way for it
  to leak further no matter how long a fault lasts or how many times
  callers retry, and the per-channel `_i2c_inflight` dict still dedupes
  repeated calls so the worker's queue never grows past one pending item
  per channel.

  One real trade-off worth knowing: because there's now a single shared
  worker, if a read on *either* channel ever gets permanently and totally
  stuck, reads on *both* channels start timing out (not hanging - each
  still fails safely within its own timeout - just no longer independent).
  The previous per-channel design didn't have that coupling, but it also
  wasn't actually protecting anything in a genuine bus-level wedge - a real
  electrical fault affects every device on the bus regardless of which
  software thread is asking, so both channels' independent threads would
  have hung right along with each other anyway. The coupling only shows up
  in exchange for actually fixing the far more likely cause (the
  concurrent-access race above), so it's a good trade.

The dashboards themselves can't cause either of these - they're separate
processes that only ever read/write the shared `status.json`/`config.json`
files and never touch I2C - but a frozen or crashed main script does stop
updating `status.json`, which is what makes the dashboards *look* frozen too
(they're accurately showing stale data, not hung themselves).

**If the bus is flaky often enough to notice** (not just an occasional
retried glitch), the code above bounds the damage but doesn't fix the
underlying cause - that's almost always electrical, not software. Worth
checking, roughly in order of how common each one is:

- **Wiring length/quality.** I2C isn't meant for long runs - keep SDA/SCL as
  short as practical, and if they have to run more than ~20-30cm, use a
  twisted pair (SDA twisted with GND, SCL twisted with GND) rather than
  loose jumpers. Loose breadboard/jumper connections are the single most
  common cause of intermittent I2C - reseat them if you haven't recently.
- **Pull-up resistors.** The ADS1015 breakout usually has its own onboard
  pull-ups, but if you've got other I2C devices on the same bus, or long
  wires, the combined bus capacitance can get too high for the default
  pull-up strength to drive a clean signal, especially at the Pi's default
  clock speed. 4.7kΩ pull-ups to 3.3V on both SDA and SCL (only need one set
  per bus, not per device) is the standard fix.
- **Shared/insufficient power.** If the NeoPixel strip and the ADC share the
  Pi's 5V rail (or even just a common ground with a long/thin wire), a
  bright/fast LED update can sag the rail or inject noise right as an I2C
  transaction is happening. Separate power injection for the LED strip
  (with grounds still tied together) is worth trying if the wedges seem to
  line up with LED activity.
- **Bus speed.** The Pi's I2C clock defaults to 100kHz (or sometimes
  400kHz depending on the overlay), which can be too fast for a noisy or
  long bus. Lowering it is a one-line change in `/boot/firmware/config.txt`
  (or `/boot/config.txt` on older OS images):
  `dtparam=i2c_arm_baudrate=50000`, then reboot. Slower but more reliable.
- **`i2cdetect -y 1`** (from `i2c-tools`, `sudo apt install i2c-tools`) is
  the quick sanity check - run it a few times over a minute or two. The
  ADS1015 should show up at the same address (`48`) every time; if it
  sometimes doesn't appear, or the command itself hangs, that's the bus
  being unhappy independent of anything this script is doing, which points
  more toward wiring/power than a driver quirk.

None of this is needed for the script to stay stable now (that's what the
timeout/retry logic above is for) - it's only worth chasing if you want the
*occasional retried glitch* to stop happening at all, rather than just being
handled gracefully when it does.

Ctrl+C stops it cleanly (clears the NeoPixels and stops the pot-monitor
thread; if you interrupt mid-recording it also stops ffmpeg gracefully
rather than leaving a corrupt file).

Hardware constants worth knowing about if yours differ (edit the top of the
file): `LED_COUNT` (38), `LED_PIN` (GPIO12), `ADS1015_I2C_ADDRESS` (`0x48`),
`BUTTON_ADC_CHANNEL`/`POT_ADC_CHANNEL` (A3/A2), `ADC_VCC` (`3.3`, used to
threshold the button's pull-up voltage and normalize the pot reading),
`PRINTER_VENDOR_ID`/`PRINTER_PRODUCT_ID` (`0x0483`/`0x5743`, the "bt_large"
80mm printer).

The NeoPixel strip is SK6812 RGBW (4 bytes/pixel, with a dedicated white
LED) - `LED_STRIP_TYPE` at the top of the file tells rpi_ws281x to talk to
it as such (`ws.SK6812_STRIP_GRBW`). If you swap in a plain 3-byte RGB
strip, or colors come out in the wrong order (e.g. red/green swapped) on a
different RGBW strip, that's the constant to change.

NeoPixel colors, chase/pulse timing, the pot dim-in range, and mic/speaker
levels are *not* hardcoded constants anymore - they live in `config.json`
(via `config_io.py`) and are read fresh on every chase/pulse/idle update, so
changes made from the web page's settings form (see below) take effect
live, without restarting this script. If `config.json` doesn't exist yet,
`config_io.DEFAULTS` supplies the same values this script always shipped
with.

The strip doesn't snap straight from off to full brightness as the pot
turns - at/below `pot_dim_start_fraction` (default 15%) it's off, at/above
`pot_full_fraction` (default 60%) it's full-brightness `idle_color`, and in
between it fades in linearly (including the white channel). Raw pot reads
are also median-filtered over the last few samples (`POT_MEDIAN_WINDOW`,
default 3) before that dimming math runs, to reject the occasional
single-sample spike toward `ADC_VCC` that a potentiometer wiper losing
momentary contact can cause - those spikes vanish on the very next read, so
the median (outvoted by the normal readings on either side) throws them out
without meaningfully slowing down a real, sustained turn of the pot.

The pot read + LED brightness update runs on its own fast loop
(`POT_LED_UPDATE_INTERVAL_SECONDS`, default 0.05s) so turning the pot feels
responsive - but writing `status.json` (for the dashboards) and re-reading
`config.json` (for live settings changes) are throttled to a separate,
slower interval (`POT_STATUS_WRITE_INTERVAL_SECONDS` /
`POT_CONFIG_RELOAD_INTERVAL_SECONDS`, both 0.5s by default) rather than
running at that same fast rate. Neither the dashboards (which poll
`status.json` every 1.5s) nor a settings-form submission (rare, by a
person) need faster than that, so tying them to the fast loop would just be
wasted disk I/O and journal log spam every time someone wants the LEDs to
feel snappier. `write_status()`/`write_config()` are also thread-safe now
(a lock in `status_io.py`/`config_io.py`), so raising the LED update rate
further is safe either way - it just won't make the dashboards update any
faster, since that's governed by the separate, slower interval.

---

## `audio_io.py`

Low-level mic/speaker utility — this is what `button_neopixel_printer.py` and
`reflect_and_print.py` build on, and it's also useful standalone for testing
the ReSpeaker/speaker independently. Six subcommands:

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

# Set mic capture / speaker playback volume (via amixer)
python3 audio_io.py levels
python3 audio_io.py levels --mic-percent 60 --speaker-percent 70

# Print ALSA card/mixer diagnostics (read-only) - run this first if
# recording/playback stops working
python3 audio_io.py diagnose
```

| Subcommand | Flags |
|---|---|
| `record OUTPUT` | `-d/--duration` (default 5s), `--device` (ALSA input) |
| `play INPUT` | `--device` (ALSA output) |
| `test` | `-o/--output` (default `test_recording.wav`), `-d/--duration`, `--in-device`, `--out-device` |
| `reflect` | `-o/--output` (default `reflection.webm`), `-d/--duration` (default 10s), `--device`, `--url` |
| `levels` | `--mic-percent` (default 40), `--speaker-percent` (default 40) |
| `diagnose` | none - prints `arecord -l`/`aplay -l` and full `amixer scontrols`/`contents` for both cards |

Note: the press-and-hold recording in `button_neopixel_printer.py` uses
`start_recording_m4a()`/`stop_recording()` from this module directly (not
exposed as a CLI subcommand here), since it needs an open-ended recording
rather than a fixed duration.

`button_neopixel_printer.py` also calls `set_default_levels()` itself at
startup, so mic/speaker volume gets (re-)set every time that script runs —
including on boot, since it's the one running under systemd (see the runbook's
section 10). Card names/control names (`MIC_CARD`, `MIC_CAPTURE_CONTROL`,
`SPEAKER_CARD`, `SPEAKER_CONTROL`) are constants at the top of `audio_io.py` —
if a Pi's card exposes different control names (or a driver/overlay update
changes them out from under you), `python3 audio_io.py diagnose` prints every
control each card actually has, to check against those constants directly
instead of guessing.

The mic and speaker are always set as two independent calls now, each with
its own error handling - a bad/stale control name on one can't silently
prevent the other from being applied too. Previously they were two calls in
a row with no isolation between them, so a single failing mic control name
(e.g. after a driver update renamed it) would raise and skip the speaker
line right after it - which looked exactly like "both mic and speaker
volume are wrong" even though only one of the two control names was
actually bad. `set_default_levels()` now returns a list of which
control(s), if any, failed, and both `button_neopixel_printer.py`'s startup
and the web page's "Apply audio levels" form report those individually
instead of one all-or-nothing error.

**Switching mic input (ReSpeaker vs. USB lav mic).** The Audio levels card
on the web dashboard has a "Mic input" dropdown - pick "USB lav mic" if the
ReSpeaker HAT ever acts up again and you want a working fallback without
editing code. It's stored as `mic_input` (`"respeaker"` or `"lav"`) in
`config.json`, and `on_button_down()` in `button_neopixel_printer.py` reads
it fresh on every button press (via `audio_io.mic_device_for_input()`), so
switching takes effect on the very next recording, no restart needed. Unlike
the volume sliders this doesn't touch `amixer` at all - it just changes
which ALSA device string gets passed to `arecord`.

The lav mic's ALSA device name (`LAV_MIC_DEVICE` in `audio_io.py`) is an
**unconfirmed placeholder** (`plughw:CARD=Device,DEV=0`) - USB mic card
names/numbers can shift depending on what's plugged in and in what order.
Before relying on it, plug the lav mic in and run `python3 audio_io.py
diagnose` - it prints the lav mic's real `CARD=` name under "Recording
devices" - and update `LAV_MIC_DEVICE` to match if it's different.

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

## `status_display.py` / `status_web.py` (status dashboard + LAN page)

`button_neopixel_printer.py` writes its current state (idle/recording/
generating/printing), pot level, button pressed/released, last print
time/style, and last error to `status.json` (via `status_io.py`) on every
state change. These two scripts just read that file — neither touches
GPIO/I2C, so they're safe to run alongside the main script, or even if it's
not running (they'll just show "no status yet").

```bash
# Full-screen terminal dashboard, refreshes once a second
python3 status_display.py

# LAN web page - visit http://<pi-ip>:8080 from any device on the network
python3 status_web.py
python3 status_web.py --host 0.0.0.0 --port 9000
```

`status_display.py` is meant to run on the Pi's HDMI console via systemd
(no keyboard/login needed) — see the runbook's section 11 for that setup,
plus the `status-web.service` unit for the web page. Both dashboards now
also show the button's live pressed/released state alongside the pot level.

The web page (light theme: white/tan background, yellow accents) refreshes
its Status card every ~1.5s via a small JS polling loop against
`/status.json` - not a full-page reload. That used to be a `<meta refresh>`
every 10s, which had two problems: it wasn't very "live," and it would wipe
out anything you were mid-edit in the settings/audio forms below, since the
whole page (including form inputs) got re-rendered from scratch on every
reload. The JS-only approach only ever touches the Status card's own DOM
nodes, so the forms are untouched unless you actually submit them.

The web page has:

- **Test print** — reprints the last-generated receipt, or the static test
  image if there isn't one yet.
- **Play last recording** — plays `recording.m4a` (the most recent
  button-hold capture) out the USB speaker, in the background so the page
  doesn't hang while it plays.
- **Restart service** — restarts `button-printer.service`.
- **LED colors & timing** — a form to change the idle/chase/pulse/pulse-floor
  colors, each with its own white-channel slider (the SK6812 strip's
  dedicated White LED, layered on top of the color picker's RGB value for a
  warmer glow — defaults to 0/off), the chase and pulse step timing, and the
  potentiometer dim-in range ("Pot dim-in start" / "Pot full-on" — the strip
  is off at/below the first, full brightness at/above the second, and fades
  linearly in between; the form rejects a submission where full-on isn't
  greater than dim-in start). Saves to `config.json` (via `config_io.py`,
  colors stored as `[r, g, b, w]`); `button_neopixel_printer.py` reads it
  live, so changes apply without restarting the main service. "Reset to
  defaults" clears `config.json` back to the built-in values.
- **Audio levels** — sliders for mic record level and speaker volume. Applied
  immediately via `amixer`, and persisted to `config.json` so the level also
  becomes the new default the next time `button_neopixel_printer.py` starts
  (e.g. after a reboot). Same card also has a **Mic input** dropdown
  (ReSpeaker HAT / USB lav mic fallback) — that one's config-only (no
  `amixer` call involved) and takes effect on the next button press; see
  the `audio_io.py` section above for the caveat about confirming the lav
  mic's actual ALSA device name first.

Test print, restart, and settings changes all need root (GPIO/systemctl/USB
access), which is why `status_web.py` also runs as root under systemd.
There's no authentication on the page, so it's meant for a trusted home/lab
LAN only — anyone who can reach the port can change LED colors, audio
levels, or restart the service.

Requires Flask: `sudo pip3 install flask --break-system-packages`

---

## `reload_services.sh`

Restarts the three systemd services (`button-printer.service`,
`status-display.service`, `status-web.service`) without rebooting the whole
Pi — handy while iterating, since editing a `.py` file doesn't take effect
until the service running it restarts.

```bash
./reload_services.sh              # restart all three
./reload_services.sh web          # just status-web.service
./reload_services.sh main display # just those two
```

Needs root (same as the services themselves) — it re-execs itself with
`sudo` automatically if you don't run it with sudo already. Skips any
service that isn't installed yet on that particular Pi rather than failing,
and prints an active/not-active summary at the end.

## `stop_services.sh`

Stops the same three services (doesn't disable them — they come back on
the next reboot or `./reload_services.sh`), so you can run something like
`i2c_diag.py` by hand without the real services also holding the I2C
bus/GPIO/NeoPixel strip in the background and contending with it.

```bash
./stop_services.sh              # stop all three
./stop_services.sh web          # just status-web.service
```

Same sudo/re-exec/skip-if-not-installed behavior as `reload_services.sh`.

## `i2c_diag.py`

Standalone pot/button reader for diagnosing I2C bus issues — deliberately
doesn't import `config_io.py`, `status_io.py`, or `button_neopixel_printer.py`,
only the same underlying hardware libraries (`board`/`busio`/
`adafruit_ads1x15`) those modules use, so it can't be affected by any bug in
our own code and can run independently of (or instead of) the real services.

```bash
sudo ./stop_services.sh     # free up the bus first
python3 i2c_diag.py
python3 i2c_diag.py --interval 0.05
```

Reads both ADC channels in a loop, one at a time, printing each voltage/raw
value with a timestamp and a running count of any I2C errors
(`OSError` from the underlying smbus call) as they happen. Deliberately has
**no** retry/timeout logic, unlike `read_voltage()` in the main script — the
goal is to observe the bus's raw behavior, not paper over it. If a read
hangs, this script hangs too, and whichever line printed last (`reading
pot...` / `reading button...`) tells you exactly which channel froze.

**Why this mattered for the I2C hang investigation:** this script running
clean with zero errors while the real script still froze occasionally was
the key clue that the problem *wasn't* a flaky physical bus after all — see
the three-attempt history under `button_neopixel_printer.py`'s I2C section
above. i2c_diag.py is single-threaded (reads one channel at a time); the
real script has two threads (button polling + `pot_monitor_loop()`) hitting
the same ADS1015 concurrently, which is what was actually causing the
occasional hangs — a software race, not a wiring fault. If you ever need to
confirm a *genuine* electrical fault again in the future (loose wire, bad
pull-ups, etc.) rather than a software race, this script is still the right
tool - it just wasn't what explained this particular round of hangs.

## Dependencies

See [`respeaker_setup_runbook.md`](respeaker_setup_runbook.md#8-install-dependencies-for-the-button--neopixel--printer-scripts)
for the full install list (apt + pip). The short version:

```bash
sudo apt install -y ffmpeg libusb-1.0-0 i2c-tools
sudo pip3 install rpi_ws281x adafruit-circuitpython-ads1x15 pillow requests \
    flask "python-escpos[usb]" --break-system-packages
```

Install with `sudo` even if you already installed for your own user — the
GPIO/printer scripts run as root, which has its own separate site-packages.
