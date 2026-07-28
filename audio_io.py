#!/usr/bin/env python3
"""
audio_io.py -- record and play back audio on the Pi.

Input:  ReSpeaker 2-Mics HAT v2.0 (mic array)
Output: USB speaker

Wraps `arecord` / `aplay` directly rather than a Python audio library (PyAudio/
sounddevice), since these ALSA device names were already confirmed working by hand
during setup (see respeaker_setup_runbook.md) and it avoids extra native deps on
each new Pi.

Device IDs come from `aplay -l` / `arecord -l` and can differ per device or after
plugging things in a different order -- update the constants below (or pass
--device / --output-device) if a given Pi's card names don't match.

Meant to be imported as a module once this feeds into the remote-API pipeline
(e.g. a future `send_to_api(path)` alongside the thermal printer output) -- for
now it also runs standalone as a CLI for testing.
"""

import argparse
import re
import signal
import subprocess
import sys
from pathlib import Path

# --- ALSA device identifiers (confirmed via `aplay -l` / `arecord -l`) ---
RESPEAKER_DEVICE = "plughw:CARD=seeed2micvoicec,DEV=0"  # mic input (2 channels)
SPEAKER_DEVICE = "plughw:CARD=UACDemoV10,DEV=0"  # USB speaker output

# Fallback mic input: a USB lavalier mic, for if the ReSpeaker HAT ever acts
# up again (I2S/driver flakiness, needs a reseat, etc.) and you want to swap
# inputs without editing code. UNCONFIRMED placeholder card name below - the
# lav mic's actual `CARD=` value depends on what `arecord -l` shows it as on
# this Pi (it showed up as card 1 during troubleshooting, but USB card
# numbers/names can shift depending on what's plugged in and in what order).
# Run `python3 audio_io.py diagnose` with the lav mic plugged in, find its
# entry under "Recording devices (arecord -l)", and update this to match -
# e.g. plughw:CARD=Device,DEV=0 or similar.
LAV_MIC_DEVICE = "plughw:CARD=Device,DEV=0"

# Selectable mic inputs, keyed by the config.json "mic_input" value - lets
# the dashboard offer a dropdown instead of requiring a code change to swap
# mics. mic_device_for_input() below is the single place that resolves a
# key to an actual ALSA device string; anything reading mic_input from
# config should go through it rather than re-implementing the lookup.
MIC_INPUTS = {
    "respeaker": RESPEAKER_DEVICE,
    "lav": LAV_MIC_DEVICE,
}
MIC_INPUT_LABELS = {
    "respeaker": "ReSpeaker 2-Mics HAT",
    "lav": "USB lav mic (fallback)",
}
DEFAULT_MIC_INPUT = "respeaker"


def mic_device_for_input(mic_input):
    """Resolve a config.json "mic_input" key ("respeaker"/"lav") to an ALSA
    device string. Falls back to RESPEAKER_DEVICE for an unknown/missing key
    (e.g. an older config.json saved before this setting existed) rather than
    raising, so a stale or malformed value can't stop recording outright."""
    return MIC_INPUTS.get(mic_input, RESPEAKER_DEVICE)

# --- Recording defaults ---
SAMPLE_RATE = 16000
CHANNELS = 2
SAMPLE_FORMAT = "S16_LE"

# --- Mixer levels (set via `amixer`, same tool alsamixer uses under the hood) ---
# Card names here match the CARD= part of RESPEAKER_DEVICE/SPEAKER_DEVICE above,
# not the full device string. Control names come from what alsamixer/amixer
# showed on this Pi at some point (respeaker_setup_runbook.md step 5) -- but
# these can be wrong for a different driver version, a different overlay, or
# even the same HAT after a firmware/driver update, and amixer errors out
# ("Unable to find simple control") if the name doesn't match. If mic/speaker
# levels aren't taking effect, run `python3 audio_io.py diagnose` first - it
# prints every control each card actually has (`amixer scontrols`/`contents`)
# so you can confirm these names against reality instead of guessing.
MIC_CARD = "seeed2micvoicec"
MIC_CAPTURE_CONTROL = "PGA"
MIC_CAPTURE_LEVEL = 40  # percent; matches the level used in the runbook

SPEAKER_CARD = "UACDemoV10"
SPEAKER_CONTROL = "PCM"
SPEAKER_LEVEL = 40  # percent

# --- Remote API ---
DEFAULT_API_URL = "http://10.18.44.99:5005/api/generate-receipt"


def set_alsa_volume(card, control, percent):
    """Set an ALSA mixer control's level. Equivalent to:

        amixer -c <card> sset <control> <percent>%

    `card` is the card name from `aplay -l`/`arecord -l` (e.g. 'seeed2micvoicec'),
    not a full device string like RESPEAKER_DEVICE. Raises CalledProcessError
    (with amixer's own error, usually "Unable to find simple control") if
    `control` doesn't exist on that card -- run `amixer -c <card> scontrols`
    to see the actual control names.
    """
    cmd = ["amixer", "-c", card, "sset", control, f"{percent}%"]
    print(f"Setting {card} '{control}' to {percent}%")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)


def get_alsa_volume(card, control):
    """Return an ALSA mixer control's current level as a 0-100 int, or None
    if it can't be read/parsed. Equivalent to reading the `[NN%]` out of:

        amixer -c <card> sget <control>
    """
    try:
        result = subprocess.run(
            ["amixer", "-c", card, "sget", control],
            check=True, capture_output=True, text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    match = re.search(r"\[(\d+)%\]", result.stdout)
    return int(match.group(1)) if match else None


def set_default_levels(mic_percent=MIC_CAPTURE_LEVEL, speaker_percent=SPEAKER_LEVEL):
    """Set the mic capture level and speaker playback level to their
    configured defaults. Meant to be called once at startup (e.g. from
    button_neopixel_printer.py's main(), or this module's own `levels`
    CLI command) so levels don't depend on whatever alsamixer was last
    left at.

    The mic and speaker are set independently, each in its own try/except -
    previously a single subprocess.run(check=True) failure (e.g.
    MIC_CAPTURE_CONTROL not matching this card's actual control name) would
    raise straight out of this function, which meant the *speaker* line
    right after it never even ran. That looked exactly like "both mic and
    speaker levels are wrong" even though only the mic control name was
    actually bad - a single bad control should never be able to take the
    other one down with it.

    Returns a list of (label, exception) pairs for whichever control(s)
    failed to set (empty list if both succeeded), so callers can report
    real per-control status instead of an all-or-nothing outcome.
    """
    errors = []
    try:
        set_alsa_volume(MIC_CARD, MIC_CAPTURE_CONTROL, mic_percent)
    except subprocess.CalledProcessError as exc:
        errors.append(("mic", exc))
    try:
        set_alsa_volume(SPEAKER_CARD, SPEAKER_CONTROL, speaker_percent)
    except subprocess.CalledProcessError as exc:
        errors.append(("speaker", exc))
    return errors


def diagnose_audio():
    """Print ALSA card/mixer diagnostics for the mic and speaker - purely
    informational, doesn't change anything. Run this first when audio isn't
    working (`python3 audio_io.py diagnose`) and share the output: it shows
    whether each card is even still detected under its expected name, and
    every mixer control each one actually has (including mute switches),
    so a wrong/stale MIC_CAPTURE_CONTROL or SPEAKER_CONTROL name - or an
    unexpectedly muted control neither of those constants covers - shows up
    directly instead of being guessed at.
    """

    def run(cmd):
        print(f"$ {' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            output = (result.stdout + result.stderr).strip()
            print(output if output else "(no output)")
        except FileNotFoundError:
            print(f"  '{cmd[0]}' not found - is it installed?")
        except subprocess.TimeoutExpired:
            print(f"  '{cmd[0]}' timed out")
        print()

    print("=== Recording devices (arecord -l) ===")
    run(["arecord", "-l"])
    print("=== Playback devices (aplay -l) ===")
    run(["aplay", "-l"])
    print(f"=== Mixer controls on '{MIC_CARD}' (mic) ===")
    run(["amixer", "-c", MIC_CARD, "scontrols"])
    print(f"=== Full mixer state on '{MIC_CARD}' (mic) - look for mute/off flags ===")
    run(["amixer", "-c", MIC_CARD, "contents"])
    print(f"=== Mixer controls on '{SPEAKER_CARD}' (speaker) ===")
    run(["amixer", "-c", SPEAKER_CARD, "scontrols"])
    print(f"=== Full mixer state on '{SPEAKER_CARD}' (speaker) - look for mute/off flags ===")
    run(["amixer", "-c", SPEAKER_CARD, "contents"])
    print("=== Configured control names (audio_io.py constants) ===")
    print(f"  MIC_CARD={MIC_CARD!r}  MIC_CAPTURE_CONTROL={MIC_CAPTURE_CONTROL!r}")
    print(f"  SPEAKER_CARD={SPEAKER_CARD!r}  SPEAKER_CONTROL={SPEAKER_CONTROL!r}")
    print(f"  RESPEAKER_DEVICE={RESPEAKER_DEVICE!r}")
    print(f"  LAV_MIC_DEVICE={LAV_MIC_DEVICE!r}  <- confirm this against the")
    print("    'Recording devices' list above if you're switching to the lav mic;")
    print("    it's an unconfirmed placeholder until you check it here.")
    print()
    print("If a card is missing entirely from the arecord -l/aplay -l output above,")
    print("that's a driver/overlay/USB problem, not a mixer setting - check")
    print("respeaker_setup_runbook.md sections 3-4. If the card is present but its")
    print("scontrols list doesn't include the control name shown above, that's the")
    print("mismatch to fix (update MIC_CAPTURE_CONTROL/SPEAKER_CONTROL here).")


def record_audio(output_path, duration=5, device=RESPEAKER_DEVICE,
                  channels=CHANNELS, rate=SAMPLE_RATE, fmt=SAMPLE_FORMAT):
    """Record `duration` seconds of audio from `device` to a WAV file."""
    output_path = Path(output_path)
    cmd = [
        "arecord",
        "-D", device,
        "-c", str(channels),
        "-r", str(rate),
        "-f", fmt,
        "-d", str(duration),
        str(output_path),
    ]
    print(f"Recording {duration}s from {device} -> {output_path}")
    subprocess.run(cmd, check=True)
    return output_path


def play_audio(input_path, device=SPEAKER_DEVICE):
    """Play a WAV file through `device`."""
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    cmd = ["aplay", "-D", device, str(input_path)]
    print(f"Playing {input_path} -> {device}")
    subprocess.run(cmd, check=True)


def record_and_playback(output_path, duration=5, in_device=RESPEAKER_DEVICE,
                         out_device=SPEAKER_DEVICE):
    """Record then immediately play back -- quick end-to-end mic/speaker check."""
    path = record_audio(output_path, duration=duration, device=in_device)
    play_audio(path, device=out_device)
    return path


def start_looping_playback(path, device=SPEAKER_DEVICE):
    """Start looping playback of an audio file (mp3, wav, etc.) through `device`,
    repeating indefinitely until stop_playback() is called. Uses ffmpeg rather
    than aplay since aplay can't decode compressed formats like mp3.

    Requires ffmpeg to be installed (`sudo apt install ffmpeg`).
    """
    path = Path(path)
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", str(path),
        "-f", "alsa", device,
    ]
    print(f"Looping playback: {path} -> {device}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def play_audio_file(path, device=SPEAKER_DEVICE, block=True):
    """Play an audio file (m4a, mp3, wav, etc.) through `device` once via
    ffmpeg -- unlike play_audio(), this isn't limited to WAV, so it works
    directly on the m4a recordings from start_recording_m4a().

    If `block` is True (default), waits for playback to finish and raises
    on failure. If False, returns the running Popen immediately (e.g. so a
    web request handler doesn't hang for the length of the recording) --
    pass it to stop_playback() to cut it off early if needed.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    cmd = ["ffmpeg", "-y", "-i", str(path), "-f", "alsa", device]
    print(f"Playing {path} -> {device}")
    if block:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return None
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_playback(proc, timeout=5):
    """Stop playback started by start_looping_playback()."""
    if proc.poll() is not None:
        return  # already exited
    proc.terminate()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def record_webm(output_path, duration=10, device=RESPEAKER_DEVICE,
                 rate=SAMPLE_RATE, channels=CHANNELS):
    """Record `duration` seconds from `device` directly to a WebM/Opus file via ffmpeg.

    Requires ffmpeg to be installed (`sudo apt install ffmpeg`).
    """
    output_path = Path(output_path)
    cmd = [
        "ffmpeg", "-y",
        "-f", "alsa",
        "-ar", str(rate),
        "-ac", str(channels),
        "-i", device,
        "-t", str(duration),
        "-c:a", "libopus",
        str(output_path),
    ]
    print(f"Recording {duration}s from {device} -> {output_path} (webm/opus)")
    subprocess.run(cmd, check=True)
    return output_path


class HoldRecording:
    """Handle for an open-ended recording started by start_recording_m4a().
    Opaque to callers -- just pass it to stop_recording()."""

    def __init__(self, proc, tmp_wav_path, output_path):
        self.proc = proc
        self.tmp_wav_path = tmp_wav_path
        self.output_path = output_path


def start_recording_m4a(output_path, device=RESPEAKER_DEVICE, rate=SAMPLE_RATE,
                         channels=CHANNELS, fmt=SAMPLE_FORMAT):
    """Start an open-ended recording from `device`, for press-and-hold style
    capture where the caller doesn't know how long the recording should be
    until the button is released.

    Captures via `arecord` to a temporary WAV file -- the same tool/settings
    confirmed to give clean audio in respeaker_setup_runbook.md -- rather than
    ffmpeg's live ALSA input, which was found to produce static on the
    ReSpeaker 2-Mics HAT (likely a buffering/format-negotiation quirk specific
    to ffmpeg's alsa demuxer on this hardware). The WAV is transcoded to
    AAC/M4A afterward, as a separate file-to-file ffmpeg pass, once
    stop_recording() is called -- that step isn't real-time-sensitive, so it
    isn't subject to the same issue.

    Returns a HoldRecording handle; pass it to stop_recording().
    """
    output_path = Path(output_path)
    tmp_wav_path = output_path.with_name(output_path.stem + ".tmp.wav")
    cmd = [
        "arecord",
        "-D", device,
        "-c", str(channels),
        "-r", str(rate),
        "-f", fmt,
        str(tmp_wav_path),
    ]
    print(f"Recording from {device} -> {tmp_wav_path} (wav, until stopped)")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return HoldRecording(proc, tmp_wav_path, output_path)


def stop_recording(recording, timeout=10):
    """Stop a recording started by start_recording_m4a(), then transcode the
    captured WAV to the AAC/M4A path that was requested at start time.

    Sends SIGINT (same as Ctrl+C) rather than killing arecord, so it finalizes
    the WAV header properly instead of leaving a truncated/corrupt file.
    """
    proc = recording.proc
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    tmp_wav_path = recording.tmp_wav_path
    output_path = recording.output_path
    print(f"Recording stopped. Encoding {tmp_wav_path} -> {output_path} ...")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(tmp_wav_path), "-c:a", "aac", "-b:a", "128k", str(output_path)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    tmp_wav_path.unlink(missing_ok=True)
    print(f"Saved {output_path}")


def upload_audio(path, url=DEFAULT_API_URL, field_name="audio", content_type="audio/webm", params=None):
    """POST an audio file as multipart/form-data, optionally with query params. Equivalent to:

        curl -X POST "<url>?<query>" -F "audio=@<path>;type=<content_type>"

    Requires the `requests` package (`pip install requests --break-system-packages`).
    """
    import requests

    path = Path(path)
    with open(path, "rb") as f:
        files = {field_name: (path.name, f, content_type)}
        print(f"POSTing {path} -> {url} (params={params})")
        resp = requests.post(url, params=params, files=files)
    resp.raise_for_status()
    return resp


def record_and_send(output_path="reflection.webm", duration=10, device=RESPEAKER_DEVICE,
                     url=DEFAULT_API_URL):
    """Record `duration` seconds to a WebM file and POST it to the receipt API."""
    path = record_webm(output_path, duration=duration, device=device)
    resp = upload_audio(path, url=url)
    print(f"Response: {resp.status_code} {resp.text}")
    return resp


def main():
    parser = argparse.ArgumentParser(
        description="Record/play audio on the Pi (ReSpeaker mic + USB speaker)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="Record audio to a WAV file")
    rec.add_argument("output", help="Output WAV file path")
    rec.add_argument("-d", "--duration", type=int, default=5, help="Duration in seconds (default: 5)")
    rec.add_argument("--device", default=RESPEAKER_DEVICE, help="ALSA input device")

    play = sub.add_parser("play", help="Play a WAV file")
    play.add_argument("input", help="Input WAV file path")
    play.add_argument("--device", default=SPEAKER_DEVICE, help="ALSA output device")

    both = sub.add_parser("test", help="Record then immediately play back (quick mic/speaker check)")
    both.add_argument("-o", "--output", default="test_recording.wav", help="File to save the recording to")
    both.add_argument("-d", "--duration", type=int, default=5, help="Duration in seconds (default: 5)")
    both.add_argument("--in-device", default=RESPEAKER_DEVICE, help="ALSA input device")
    both.add_argument("--out-device", default=SPEAKER_DEVICE, help="ALSA output device")

    reflect = sub.add_parser("reflect", help="Record to WebM and POST it to the receipt API")
    reflect.add_argument("-o", "--output", default="reflection.webm", help="Output WebM file path")
    reflect.add_argument("-d", "--duration", type=int, default=10, help="Duration in seconds (default: 10)")
    reflect.add_argument("--device", default=RESPEAKER_DEVICE, help="ALSA input device")
    reflect.add_argument("--url", default=DEFAULT_API_URL, help="API endpoint to POST to")

    levels = sub.add_parser("levels", help="Set mic capture / speaker playback volume")
    levels.add_argument("--mic-percent", type=int, default=MIC_CAPTURE_LEVEL,
                         help=f"Mic capture level, 0-100 (default: {MIC_CAPTURE_LEVEL})")
    levels.add_argument("--speaker-percent", type=int, default=SPEAKER_LEVEL,
                         help=f"Speaker playback level, 0-100 (default: {SPEAKER_LEVEL})")

    sub.add_parser("diagnose", help="Print ALSA card/mixer diagnostics (read-only) - "
                                     "run this first when audio isn't working")

    args = parser.parse_args()

    try:
        if args.command == "record":
            record_audio(args.output, duration=args.duration, device=args.device)
        elif args.command == "play":
            play_audio(args.input, device=args.device)
        elif args.command == "test":
            record_and_playback(args.output, duration=args.duration,
                                 in_device=args.in_device, out_device=args.out_device)
        elif args.command == "reflect":
            record_and_send(args.output, duration=args.duration,
                             device=args.device, url=args.url)
        elif args.command == "levels":
            errors = set_default_levels(mic_percent=args.mic_percent, speaker_percent=args.speaker_percent)
            if errors:
                for label, exc in errors:
                    print(f"  {label}: FAILED - {exc}", file=sys.stderr)
                print("Run `python3 audio_io.py diagnose` to see the actual control "
                      "names available on each card.", file=sys.stderr)
                sys.exit(1)
        elif args.command == "diagnose":
            diagnose_audio()
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
