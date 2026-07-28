#!/usr/bin/env python3
"""
status_web.py -- LAN-accessible status page + controls for the button/printer rig.

Reads status.json (via status_io.py), same as status_display.py, and
additionally exposes:
  - "Test print" - reprints the last-generated receipt (receipt.jpeg), or
    the static test image if none exists yet, via print_receipt() from
    button_neopixel_printer.py. Safe to import that module here because
    hardware setup (NeoPixels/I2C) is deferred to init_hardware(), which
    this script never calls - it only uses print_receipt(), which just
    talks to the USB printer directly.
  - "Restart service" - runs `systemctl restart button-printer.service`.
  - The Status card refreshes itself every ~1.5s via a small JS polling loop
    against /status.json, rather than a `<meta refresh>` full-page reload.
    That matters for two reasons: a full-page reload only happened every 10s
    (not very "live"), and - worse - it would blow away anything you were
    mid-edit in the settings/audio forms below, since the whole page
    (including form inputs) got re-rendered from scratch on every reload.
    JS-only polling only ever touches the Status card's own DOM nodes, so
    the forms are untouched unless you actually submit them.
  - "Play last recording" - plays AUDIO_OUTPUT_PATH (recording.m4a) out the
    USB speaker, via audio_io.play_audio_file(), in a background thread so
    the request returns immediately instead of blocking for the clip's
    length.
  - A settings form for the NeoPixel colors (idle/chase/pulse/pulse floor),
    each with its own white-channel slider (the SK6812 strip's dedicated
    White LED, layered on top of the RGB color for a warmer glow), plus
    chase/pulse timing and the potentiometer dim-in range (pot_dim_start_
    percent / pot_full_percent - the strip is off below the first, full
    brightness at/above the second, and fades linearly in between rather
    than snapping on) - all written to config.json via config_io.py, which
    button_neopixel_printer.py reads live (no restart needed - the idle/pot
    fields take effect within POT_CONFIG_RELOAD_INTERVAL_SECONDS; chase/
    pulse colors and timing take effect on the very next chase/pulse since
    those are read fresh on every call).
  - An audio levels form for mic capture / speaker playback volume - applies
    immediately via amixer AND persists to config.json, so it also becomes
    the default the next time button_neopixel_printer.py starts (e.g. on
    boot). The same form also has a "Mic input" dropdown (ReSpeaker HAT vs.
    USB lav mic fallback) - that one's config.json-only (no amixer call,
    button_neopixel_printer.py just reads it fresh on the next button press
    via audio_io.mic_device_for_input()).

Runs as its own systemd service (status-web.service), separate from
button-printer.service, so restarting the main service doesn't take the web
page down too. See respeaker_setup_runbook.md section 11.

Dependencies: sudo pip3 install flask --break-system-packages

Usage:
    python3 status_web.py [--host 0.0.0.0] [--port 8080]

Anyone on the local network who can reach this port can trigger a test
print, restart the service, play the last recording, or change LED/audio
settings - there's no authentication. Fine for a trusted home/lab LAN;
don't expose this port beyond that without adding some.
"""

import argparse
import os
import subprocess
import threading
import time

from flask import Flask, jsonify, redirect, request, url_for

import config_io
from status_io import read_status
from button_neopixel_printer import (
    print_receipt,
    RECEIPT_IMAGE_OUTPUT,
    RECEIPT_IMAGE_PATH,
    AUDIO_OUTPUT_PATH,
)
from audio_io import (
    play_audio_file,
    set_default_levels,
    get_alsa_volume,
    MIC_CARD,
    MIC_CAPTURE_CONTROL,
    SPEAKER_CARD,
    SPEAKER_CONTROL,
    SPEAKER_DEVICE,
    MIC_INPUTS,
    MIC_INPUT_LABELS,
)

app = Flask(__name__)

BUTTON_PRINTER_SERVICE = "button-printer.service"

# Color settings exposed on the page: (config.json key, label).
COLOR_FIELDS = [
    ("idle_color", "Idle (pot on, at rest)"),
    ("chase_color", "Chase (button held)"),
    ("pulse_color", "Pulse peak (waiting on receipt)"),
    ("pulse_floor_color", "Pulse floor (dim, never fully dark)"),
]

STATE_CSS_CLASS = {
    "idle": "state-idle",
    "recording": "state-recording",
    "generating": "state-generating",
    "printing": "state-printing",
    "stopped": "state-stopped",
    "starting": "state-starting",
}

PAGE_TEMPLATE = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Gemini Phys Lab Printer</title>
  <style>
    :root {{
      --tan: #f4ead9;
      --tan-dark: #e6d7bd;
      --yellow: #f2b705;
      --yellow-dark: #d99e00;
      --ink: #3a2f22;
      --muted: #8a7a63;
    }}
    body {{
      font-family: -apple-system, "Segoe UI", sans-serif;
      background: var(--tan);
      color: var(--ink);
      padding: 2rem;
      max-width: 720px;
      margin: 0 auto;
    }}
    h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
    h2 {{ font-size: 1.1rem; margin: 0 0 0.75rem 0; color: var(--ink); }}
    a {{ color: var(--yellow-dark); }}
    .card {{
      background: #fffdf8;
      border: 1px solid var(--tan-dark);
      border-radius: 8px;
      padding: 1.25rem 1.5rem;
      margin: 1.25rem 0;
      box-shadow: 0 1px 2px rgba(0,0,0,0.04);
    }}
    table {{ border-collapse: collapse; width: 100%; }}
    td {{ padding: 0.3rem 1rem 0.3rem 0; vertical-align: top; }}
    td.label {{ color: var(--muted); white-space: nowrap; }}
    .state {{ font-weight: bold; text-transform: uppercase; }}
    .state-idle {{ color: #4a8c3f; }}
    .state-recording {{ color: var(--yellow-dark); }}
    .state-generating {{ color: #2f7cab; }}
    .state-printing {{ color: #8a4fae; }}
    .state-stopped, .state-starting {{ color: var(--muted); }}
    .button-pressed {{ color: var(--yellow-dark); font-weight: bold; }}
    .button-released {{ color: var(--muted); }}
    .error {{ color: #b3402a; }}
    button, input[type=submit] {{
      font-size: 0.95rem;
      padding: 0.5rem 1.1rem;
      margin-right: 0.5rem;
      margin-top: 0.5rem;
      background: var(--yellow);
      color: var(--ink);
      border: 1px solid var(--yellow-dark);
      border-radius: 5px;
      cursor: pointer;
      font-weight: 600;
    }}
    button:hover, input[type=submit]:hover {{ background: var(--yellow-dark); color: #fff; }}
    button.secondary {{
      background: #fffdf8;
      color: var(--ink);
      border: 1px solid var(--tan-dark);
    }}
    button.secondary:hover {{ background: var(--tan-dark); }}
    .message {{
      margin: 1rem 0; padding: 0.6rem 1rem;
      background: #fdf3d6; border-left: 4px solid var(--yellow);
      border-radius: 4px;
    }}
    .settings-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 0.75rem 1.5rem;
      margin-bottom: 1rem;
    }}
    .field {{ display: flex; flex-direction: column; gap: 0.25rem; }}
    .field label {{ font-size: 0.85rem; color: var(--muted); }}
    .field .sublabel {{ font-size: 0.75rem; color: var(--muted); margin-top: 0.15rem; }}
    .field input[type=color] {{ height: 2.2rem; width: 100%; border: 1px solid var(--tan-dark); border-radius: 4px; padding: 2px; }}
    .field input[type=number], .field input[type=range] {{
      padding: 0.35rem; border: 1px solid var(--tan-dark); border-radius: 4px; font-size: 0.9rem;
    }}
    .hint {{ color: var(--muted); font-size: 0.8rem; margin-top: -0.5rem; margin-bottom: 0.75rem; }}
    .range-row {{ display: flex; align-items: center; gap: 0.75rem; }}
    .range-row output {{ min-width: 2.5rem; text-align: right; font-variant-numeric: tabular-nums; }}
  </style>
</head>
<body>
  <h1>Gemini Phys Lab Printer</h1>
  {message_html}

  <div class="card">
    <h2>Status <span id="live-indicator" class="sublabel" style="font-weight:normal"></span></h2>
    <table>
      <tr><td class="label">State</td><td id="state-value" class="state {state_class}">{state}</td></tr>
      <tr><td class="label">Button</td><td id="button-value" class="{button_class}">{button_text}</td></tr>
      <tr><td class="label">Uptime</td><td id="uptime-value">{uptime}</td></tr>
      <tr><td class="label">Pot level</td><td id="pot-value">{pot}</td></tr>
      <tr><td class="label">Last print</td><td id="last-print-value">{last_print}</td></tr>
      <tr><td class="label">Last API response</td><td id="last-api-value">{last_api}</td></tr>
      <tr><td class="label">Last error</td><td id="last-error-value" class="{error_class}">{last_error}</td></tr>
    </table>

    <form method="post" action="{test_print_url}" style="display:inline">
      <button type="submit">Test print</button>
    </form>
    <form method="post" action="{play_recording_url}" style="display:inline">
      <button type="submit" class="secondary">Play last recording</button>
    </form>
    <form method="post" action="{restart_url}" style="display:inline"
          onsubmit="return confirm('Restart the button/printer service?');">
      <button type="submit" class="secondary">Restart service</button>
    </form>
    <p><a href="/status.json">raw status.json</a></p>
  </div>

  <div class="card">
    <h2>LED colors &amp; timing</h2>
    <form method="post" action="{settings_url}">
      <div class="settings-grid">
        {color_fields_html}
        <div class="field">
          <label for="chase_step_delay">Chase step delay (s)</label>
          <input type="number" id="chase_step_delay" name="chase_step_delay" step="0.005" min="0.001" max="0.5" value="{chase_step_delay}">
        </div>
        <div class="field">
          <label for="chase_tail_length">Chase tail length (pixels)</label>
          <input type="number" id="chase_tail_length" name="chase_tail_length" step="1" min="1" max="38" value="{chase_tail_length}">
        </div>
        <div class="field">
          <label for="pulse_step_delay">Pulse step delay (s)</label>
          <input type="number" id="pulse_step_delay" name="pulse_step_delay" step="0.005" min="0.001" max="0.5" value="{pulse_step_delay}">
        </div>
        <div class="field">
          <label for="pot_dim_start_percent">Pot dim-in start (%)</label>
          <input type="number" id="pot_dim_start_percent" name="pot_dim_start_percent" step="1" min="0" max="100" value="{pot_dim_start_percent}">
        </div>
        <div class="field">
          <label for="pot_full_percent">Pot full-on (%)</label>
          <input type="number" id="pot_full_percent" name="pot_full_percent" step="1" min="0" max="100" value="{pot_full_percent}">
        </div>
      </div>
      <div class="hint">At/below "dim-in start" the strip is off; at/above "full-on" it's the full idle color; in between it fades in smoothly rather than snapping on.</div>
      <input type="submit" value="Save LED settings">
      <button type="submit" formaction="{reset_settings_url}" class="secondary"
              onclick="return confirm('Reset LED colors/timing/threshold to defaults?');">Reset to defaults</button>
    </form>
  </div>

  <div class="card">
    <h2>Audio levels</h2>
    <form method="post" action="{audio_levels_url}">
      <div class="settings-grid">
        <div class="field">
          <label for="mic_percent">Mic record level ({mic_percent}%)</label>
          <div class="range-row">
            <input type="range" id="mic_percent" name="mic_percent" min="0" max="100" value="{mic_percent}"
                   oninput="mic_out.value = this.value">
            <output id="mic_out" name="mic_out" for="mic_percent">{mic_percent}</output>
          </div>
        </div>
        <div class="field">
          <label for="speaker_percent">Speaker volume ({speaker_percent}%)</label>
          <div class="range-row">
            <input type="range" id="speaker_percent" name="speaker_percent" min="0" max="100" value="{speaker_percent}"
                   oninput="speaker_out.value = this.value">
            <output id="speaker_out" name="speaker_out" for="speaker_percent">{speaker_percent}</output>
          </div>
        </div>
        <div class="field">
          <label for="mic_input">Mic input</label>
          <select id="mic_input" name="mic_input">
            {mic_input_options_html}
          </select>
          <span class="sublabel">Switch to the USB lav mic if the ReSpeaker HAT ever acts up again.</span>
        </div>
      </div>
      <input type="submit" value="Apply audio settings">
    </form>
  </div>

  <script>
    // Polls /status.json and patches only the Status card's own cells - the
    // page never does a full reload, so the settings/audio forms below are
    // never touched (and never wiped out) while you're mid-edit in them.
    // See the module docstring for why a <meta refresh> used to do exactly
    // that.
    const STATE_CSS_CLASS = {{
      idle: "state-idle", recording: "state-recording", generating: "state-generating",
      printing: "state-printing", stopped: "state-stopped", starting: "state-starting",
    }};

    function pad(n) {{ return String(n).padStart(2, "0"); }}

    function formatTimestamp(ts) {{
      if (!ts) return "never";
      const d = new Date(ts * 1000);
      return `${{d.getFullYear()}}-${{pad(d.getMonth() + 1)}}-${{pad(d.getDate())}} `
           + `${{pad(d.getHours())}}:${{pad(d.getMinutes())}}:${{pad(d.getSeconds())}}`;
    }}

    function formatUptime(startedAt) {{
      if (!startedAt) return "unknown";
      let s = Math.floor(Date.now() / 1000 - startedAt);
      const h = Math.floor(s / 3600); s -= h * 3600;
      const m = Math.floor(s / 60); s -= m * 60;
      return h ? `${{h}}h ${{m}}m ${{s}}s` : `${{m}}m ${{s}}s`;
    }}

    function formatPot(status) {{
      if (status.pot_fraction === undefined || status.pot_fraction === null) return "unknown";
      const pct = (status.pot_fraction * 100).toFixed(0);
      const v = (status.pot_voltage || 0).toFixed(2);
      const strip = (status.strip_brightness_percent !== undefined && status.strip_brightness_percent !== null)
        ? `strip ${{status.strip_brightness_percent}}% brightness`
        : (status.strip_on ? "strip ON" : "strip off");
      return `${{pct}}% (${{v}}V, ${{strip}})`;
    }}

    function formatLastPrint(status) {{
      let s = formatTimestamp(status.last_print_time);
      if (status.last_print_style) s += ` (style: ${{status.last_print_style}})`;
      return s;
    }}

    function formatLastApi(status) {{
      const v = status.last_api_response_seconds;
      return (v === undefined || v === null) ? "-" : `${{v.toFixed(2)}}s`;
    }}

    function setText(id, text) {{ document.getElementById(id).textContent = text; }}

    function pollStatus() {{
      fetch("/status.json", {{ cache: "no-store" }})
        .then((r) => r.json())
        .then((status) => {{
          const state = status.state || "unknown";
          const stateEl = document.getElementById("state-value");
          stateEl.textContent = state;
          stateEl.className = "state " + (STATE_CSS_CLASS[state] || "");

          const buttonEl = document.getElementById("button-value");
          if (status.button_pressed === undefined || status.button_pressed === null) {{
            buttonEl.textContent = "unknown"; buttonEl.className = "";
          }} else if (status.button_pressed) {{
            buttonEl.textContent = "PRESSED"; buttonEl.className = "button-pressed";
          }} else {{
            buttonEl.textContent = "released"; buttonEl.className = "button-released";
          }}

          setText("uptime-value", formatUptime(status.started_at));
          setText("pot-value", formatPot(status));
          setText("last-print-value", formatLastPrint(status));
          setText("last-api-value", formatLastApi(status));

          const errorEl = document.getElementById("last-error-value");
          if (status.last_error) {{
            errorEl.textContent = status.last_error; errorEl.className = "error";
          }} else {{
            errorEl.textContent = "none"; errorEl.className = "";
          }}

          document.getElementById("live-indicator").textContent = "";
        }})
        .catch(() => {{
          document.getElementById("live-indicator").textContent = "(connection lost - retrying)";
        }});
    }}

    pollStatus();
    setInterval(pollStatus, 1500);
  </script>
</body>
</html>
"""


def format_timestamp(ts):
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def rgb_to_hex(rgba):
    return "#{:02x}{:02x}{:02x}".format(*rgba[:3])


def hex_to_rgb(hex_str):
    hex_str = (hex_str or "").lstrip("#")
    if len(hex_str) != 6:
        raise ValueError(f"Invalid color: {hex_str!r}")
    return [int(hex_str[i:i + 2], 16) for i in (0, 2, 4)]


def white_channel(rgba):
    """Pull the white value out of a [r, g, b, w] config list (0 if missing -
    old configs saved before the white channel was exposed)."""
    return rgba[3] if len(rgba) > 3 else 0


def render_mic_input_options(cfg):
    selected = cfg.get("mic_input", "respeaker")
    parts = []
    for key in MIC_INPUTS:
        label = MIC_INPUT_LABELS.get(key, key)
        sel = " selected" if key == selected else ""
        parts.append(f'<option value="{key}"{sel}>{label}</option>')
    return "\n            ".join(parts)


def render_color_fields(cfg):
    parts = []
    for key, label in COLOR_FIELDS:
        w_id = f"{key}_w"
        w = white_channel(cfg[key])
        parts.append(
            f'<div class="field">'
            f'<label for="{key}">{label}</label>'
            f'<input type="color" id="{key}" name="{key}" value="{rgb_to_hex(cfg[key])}">'
            f'<div class="range-row">'
            f'<input type="range" id="{w_id}" name="{w_id}" min="0" max="255" value="{w}" '
            f'oninput="{w_id}_out.value = this.value">'
            f'<output id="{w_id}_out" name="{w_id}_out" for="{w_id}">{w}</output>'
            f'</div>'
            f'<span class="sublabel">White channel (warm glow, 0-255)</span>'
            f'</div>'
        )
    return "\n        ".join(parts)


def render_page(message=None):
    status = read_status()
    cfg = config_io.read_config()

    state = status.get("state", "unknown")

    started_at = status.get("started_at")
    if started_at:
        uptime_seconds = int(time.time() - started_at)
        hours, rem = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        uptime = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"
    else:
        uptime = "unknown"

    if "pot_fraction" in status:
        brightness = status.get("strip_brightness_percent")
        strip_text = f"strip {brightness}% brightness" if brightness is not None else \
            ("strip ON" if status.get("strip_on") else "strip off")
        pot = f"{status['pot_fraction'] * 100:.0f}% ({status.get('pot_voltage', 0):.2f}V, {strip_text})"
    else:
        pot = "unknown"

    button_pressed = status.get("button_pressed")
    if button_pressed is None:
        button_text, button_class = "unknown", ""
    elif button_pressed:
        button_text, button_class = "PRESSED", "button-pressed"
    else:
        button_text, button_class = "released", "button-released"

    last_print = format_timestamp(status.get("last_print_time"))
    if status.get("last_print_style"):
        last_print += f" (style: {status['last_print_style']})"

    last_api = (f"{status['last_api_response_seconds']:.2f}s"
                if status.get("last_api_response_seconds") is not None else "-")

    last_error = status.get("last_error") or "none"
    error_class = "error" if status.get("last_error") else ""

    message_html = f'<div class="message">{message}</div>' if message else ""

    # Prefer the live amixer reading; fall back to the persisted config value
    # (e.g. amixer unavailable, or the mixer control name doesn't match).
    mic_percent = get_alsa_volume(MIC_CARD, MIC_CAPTURE_CONTROL)
    if mic_percent is None:
        mic_percent = cfg["mic_percent"]
    speaker_percent = get_alsa_volume(SPEAKER_CARD, SPEAKER_CONTROL)
    if speaker_percent is None:
        speaker_percent = cfg["speaker_percent"]

    return PAGE_TEMPLATE.format(
        message_html=message_html,
        state=state,
        state_class=STATE_CSS_CLASS.get(state, ""),
        button_text=button_text,
        button_class=button_class,
        uptime=uptime,
        pot=pot,
        last_print=last_print,
        last_api=last_api,
        last_error=last_error,
        error_class=error_class,
        test_print_url=url_for("test_print"),
        play_recording_url=url_for("play_recording"),
        restart_url=url_for("restart_service"),
        settings_url=url_for("save_settings"),
        reset_settings_url=url_for("reset_settings"),
        audio_levels_url=url_for("save_audio_levels"),
        color_fields_html=render_color_fields(cfg),
        chase_step_delay=cfg["chase_step_delay"],
        chase_tail_length=cfg["chase_tail_length"],
        pulse_step_delay=cfg["pulse_step_delay"],
        pot_dim_start_percent=round(cfg["pot_dim_start_fraction"] * 100),
        pot_full_percent=round(cfg["pot_full_fraction"] * 100),
        mic_percent=mic_percent,
        speaker_percent=speaker_percent,
        mic_input_options_html=render_mic_input_options(cfg),
    )


@app.route("/")
def index():
    return render_page(message=request.args.get("message"))


@app.route("/status.json")
def status_json():
    return jsonify(read_status())


@app.route("/test-print", methods=["POST"])
def test_print():
    image_path = RECEIPT_IMAGE_OUTPUT if os.path.isfile(RECEIPT_IMAGE_OUTPUT) else RECEIPT_IMAGE_PATH
    ok = print_receipt(image_path)
    message = (f"Test print sent ({os.path.basename(image_path)})." if ok
               else f"Test print failed - check the console/journal for {BUTTON_PRINTER_SERVICE}.")
    return redirect(url_for("index", message=message))


@app.route("/play-recording", methods=["POST"])
def play_recording():
    if not os.path.isfile(AUDIO_OUTPUT_PATH):
        message = f"No recording found yet at {os.path.basename(AUDIO_OUTPUT_PATH)}."
        return redirect(url_for("index", message=message))

    def _play():
        try:
            play_audio_file(AUDIO_OUTPUT_PATH, device=SPEAKER_DEVICE, block=True)
        except Exception as exc:
            print(f"Playback failed: {exc}")

    threading.Thread(target=_play, daemon=True).start()
    message = f"Playing {os.path.basename(AUDIO_OUTPUT_PATH)}..."
    return redirect(url_for("index", message=message))


@app.route("/restart", methods=["POST"])
def restart_service():
    try:
        subprocess.run(["systemctl", "restart", BUTTON_PRINTER_SERVICE], check=True)
        message = f"Restarting {BUTTON_PRINTER_SERVICE}..."
    except Exception as exc:
        message = f"Restart failed: {exc}"
    return redirect(url_for("index", message=message))


@app.route("/settings", methods=["POST"])
def save_settings():
    try:
        fields = {}
        for key, _label in COLOR_FIELDS:
            rgb = hex_to_rgb(request.form.get(key))
            w = max(0, min(255, int(request.form.get(f"{key}_w", 0))))
            fields[key] = rgb + [w]

        fields["chase_step_delay"] = max(0.001, float(request.form["chase_step_delay"]))
        fields["chase_tail_length"] = max(1, int(request.form["chase_tail_length"]))
        fields["pulse_step_delay"] = max(0.001, float(request.form["pulse_step_delay"]))

        dim_start_percent = max(0.0, min(100.0, float(request.form["pot_dim_start_percent"])))
        full_percent = max(0.0, min(100.0, float(request.form["pot_full_percent"])))
        if full_percent <= dim_start_percent:
            raise ValueError('"Pot full-on" must be greater than "Pot dim-in start"')
        fields["pot_dim_start_fraction"] = round(dim_start_percent / 100, 3)
        fields["pot_full_fraction"] = round(full_percent / 100, 3)

        config_io.write_config(**fields)
        message = "LED settings saved."
    except (KeyError, ValueError) as exc:
        message = f"Couldn't save settings: {exc}"
    return redirect(url_for("index", message=message))


@app.route("/settings/reset", methods=["POST"])
def reset_settings():
    config_io.reset_config()
    return redirect(url_for("index", message="LED settings reset to defaults."))


@app.route("/audio-levels", methods=["POST"])
def save_audio_levels():
    try:
        mic_percent = max(0, min(100, int(request.form["mic_percent"])))
        speaker_percent = max(0, min(100, int(request.form["speaker_percent"])))
        mic_input = request.form.get("mic_input", "respeaker")
        if mic_input not in MIC_INPUTS:
            raise ValueError(f"Unknown mic input: {mic_input!r}")
    except (KeyError, ValueError) as exc:
        return redirect(url_for("index", message=f"Couldn't apply audio levels: {exc}"))

    # set_default_levels() sets the mic and speaker independently - a bad
    # control name on one (e.g. MIC_CAPTURE_CONTROL not matching this card's
    # actual controls) can no longer silently prevent the other from being
    # applied, which is what made "both mic and speaker are wrong" such a
    # confusing symptom before. Report per-control results here instead of
    # a single all-or-nothing message.
    errors = set_default_levels(mic_percent=mic_percent, speaker_percent=speaker_percent)
    failed = {label for label, _exc in errors}
    parts = []
    parts.append(f"mic {mic_percent}%" if "mic" not in failed else f"mic FAILED (see journal)")
    parts.append(f"speaker {speaker_percent}%" if "speaker" not in failed else f"speaker FAILED (see journal)")
    for label, exc in errors:
        print(f"Couldn't set {label} level: {exc}")

    # Persist the requested values regardless of whether they applied live -
    # they're what should take effect on the next restart/boot too, and
    # keeping the user's last-requested settings visible is more useful than
    # silently reverting the form to old values after a partial failure.
    # mic_input doesn't go through amixer at all (it's read by
    # button_neopixel_printer.py's on_button_down() at the moment of
    # recording), so it always "applies" here - just persist it.
    config_io.write_config(mic_percent=mic_percent, speaker_percent=speaker_percent, mic_input=mic_input)

    parts.append(f"mic input: {MIC_INPUT_LABELS.get(mic_input, mic_input)}")
    message = "Audio levels: " + ", ".join(parts)
    if errors:
        message += " - run `python3 audio_io.py diagnose` on the Pi to check control names."
    return redirect(url_for("index", message=message))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0",
                         help="Host to bind (default: 0.0.0.0, i.e. all interfaces)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    args = parser.parse_args()
    # threaded=True matters: Flask's dev server is single-threaded by
    # default, so one slow/blocking request (e.g. "Test print" hitting a USB
    # printer that isn't responding) would otherwise freeze every other
    # request too - including the page's own auto-refresh - making the
    # whole dashboard look hung until that one request finally times out or
    # returns. With threading on, a stuck request only blocks itself.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
