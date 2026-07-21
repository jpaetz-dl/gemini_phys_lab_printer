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
import subprocess
import sys
from pathlib import Path

# --- ALSA device identifiers (confirmed via `aplay -l` / `arecord -l`) ---
RESPEAKER_DEVICE = "plughw:CARD=seeed2micvoicec,DEV=0"  # mic input (2 channels)
SPEAKER_DEVICE = "plughw:CARD=UACDemoV10,DEV=0"  # USB speaker output

# --- Recording defaults ---
SAMPLE_RATE = 16000
CHANNELS = 2
SAMPLE_FORMAT = "S16_LE"

# --- Remote API ---
DEFAULT_API_URL = "http://10.18.44.99:5005/api/generate-receipt"


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


def start_recording_m4a(output_path, device=RESPEAKER_DEVICE, rate=SAMPLE_RATE, channels=CHANNELS):
    """Start an open-ended recording from `device` directly to an AAC/M4A file via ffmpeg.

    Unlike record_audio()/record_webm(), this doesn't take a duration -- it's meant
    for press-and-hold style capture where the caller doesn't know how long the
    recording should be until the button is released. Returns the running Popen;
    stop it with stop_recording() once the hold ends.

    Requires ffmpeg to be installed (`sudo apt install ffmpeg`).
    """
    output_path = Path(output_path)
    cmd = [
        "ffmpeg", "-y",
        "-f", "alsa",
        "-ar", str(rate),
        "-ac", str(channels),
        "-i", device,
        "-c:a", "aac",
        "-b:a", "128k",
        str(output_path),
    ]
    print(f"Recording from {device} -> {output_path} (m4a/aac, until stopped)")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_recording(proc, timeout=10):
    """Gracefully stop a recording started by start_recording_m4a() (or any open-ended
    ffmpeg Popen without a fixed -t duration).

    Sends SIGINT (same as Ctrl+C) rather than killing the process, so ffmpeg finalizes
    the output container's moov atom properly instead of leaving a truncated/corrupt file.
    """
    import signal

    if proc.poll() is not None:
        return  # already exited
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    print("Recording stopped.")


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
    except subprocess.CalledProcessError as e:
        print(f"Command failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
