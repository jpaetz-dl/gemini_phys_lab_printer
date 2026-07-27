#!/usr/bin/env python3
"""
status_web.py -- LAN-accessible status page + basic controls for the
button/printer rig.

Reads status.json (via status_io.py), same as status_display.py, and
additionally exposes two actions:
  - "Test print" - reprints the last-generated receipt (receipt.jpeg), or
    the static test image if none exists yet, via print_receipt() from
    button_neopixel_printer.py. Safe to import that module here because
    hardware setup (NeoPixels/I2C) is deferred to init_hardware(), which
    this script never calls - it only uses print_receipt(), which just
    talks to the USB printer directly.
  - "Restart service" - runs `systemctl restart button-printer.service`.

Runs as its own systemd service (status-web.service), separate from
button-printer.service, so restarting the main service doesn't take the web
page down too. See respeaker_setup_runbook.md section 11.

Dependencies: sudo pip3 install flask --break-system-packages

Usage:
    python3 status_web.py [--host 0.0.0.0] [--port 8080]

Anyone on the local network who can reach this port can trigger a test
print or restart the service - there's no authentication. Fine for a
trusted home/lab LAN; don't expose this port beyond that without adding some.
"""

import argparse
import os
import subprocess
import time

from flask import Flask, jsonify, redirect, request, url_for

from status_io import read_status
from button_neopixel_printer import print_receipt, RECEIPT_IMAGE_OUTPUT, RECEIPT_IMAGE_PATH

app = Flask(__name__)

BUTTON_PRINTER_SERVICE = "button-printer.service"

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
  <meta http-equiv="refresh" content="5">
  <title>Gemini Phys Lab Printer</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; background: #111; color: #eee; padding: 2rem; }}
    h1 {{ font-size: 1.4rem; }}
    table {{ border-collapse: collapse; margin: 1rem 0; }}
    td {{ padding: 0.25rem 1rem 0.25rem 0; vertical-align: top; }}
    td.label {{ color: #888; }}
    .state {{ font-weight: bold; text-transform: uppercase; }}
    .state-idle {{ color: #4caf50; }}
    .state-recording {{ color: #ffc107; }}
    .state-generating {{ color: #29b6f6; }}
    .state-printing {{ color: #ab47bc; }}
    .state-stopped, .state-starting {{ color: #888; }}
    .error {{ color: #ef5350; }}
    button {{ font-size: 1rem; padding: 0.5rem 1rem; margin-right: 0.5rem; margin-top: 1rem;
              background: #333; color: #eee; border: 1px solid #555; border-radius: 4px; cursor: pointer; }}
    button:hover {{ background: #444; }}
    .message {{ margin: 1rem 0; padding: 0.5rem 1rem; background: #222; border-left: 3px solid #4caf50; }}
  </style>
</head>
<body>
  <h1>Gemini Phys Lab Printer</h1>
  {message_html}
  <table>
    <tr><td class="label">State</td><td class="state {state_class}">{state}</td></tr>
    <tr><td class="label">Uptime</td><td>{uptime}</td></tr>
    <tr><td class="label">Pot level</td><td>{pot}</td></tr>
    <tr><td class="label">Last print</td><td>{last_print}</td></tr>
    <tr><td class="label">Last API response</td><td>{last_api}</td></tr>
    <tr><td class="label">Last error</td><td class="{error_class}">{last_error}</td></tr>
  </table>
  <form method="post" action="{test_print_url}" style="display:inline">
    <button type="submit">Test print</button>
  </form>
  <form method="post" action="{restart_url}" style="display:inline"
        onsubmit="return confirm('Restart the button/printer service?');">
    <button type="submit">Restart service</button>
  </form>
  <p><a href="/status.json" style="color:#888">raw status.json</a></p>
</body>
</html>
"""


def format_timestamp(ts):
    if not ts:
        return "never"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def render_page(message=None):
    status = read_status()
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
        pot = (f"{status['pot_fraction'] * 100:.0f}% "
               f"({status.get('pot_voltage', 0):.2f}V, strip "
               f"{'ON' if status.get('strip_on') else 'off'})")
    else:
        pot = "unknown"

    last_print = format_timestamp(status.get("last_print_time"))
    if status.get("last_print_style"):
        last_print += f" (style: {status['last_print_style']})"

    last_api = (f"{status['last_api_response_seconds']:.2f}s"
                if status.get("last_api_response_seconds") is not None else "-")

    last_error = status.get("last_error") or "none"
    error_class = "error" if status.get("last_error") else ""

    message_html = f'<div class="message">{message}</div>' if message else ""

    return PAGE_TEMPLATE.format(
        message_html=message_html,
        state=state,
        state_class=STATE_CSS_CLASS.get(state, ""),
        uptime=uptime,
        pot=pot,
        last_print=last_print,
        last_api=last_api,
        last_error=last_error,
        error_class=error_class,
        test_print_url=url_for("test_print"),
        restart_url=url_for("restart_service"),
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


@app.route("/restart", methods=["POST"])
def restart_service():
    try:
        subprocess.run(["systemctl", "restart", BUTTON_PRINTER_SERVICE], check=True)
        message = f"Restarting {BUTTON_PRINTER_SERVICE}..."
    except Exception as exc:
        message = f"Restart failed: {exc}"
    return redirect(url_for("index", message=message))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0",
                         help="Host to bind (default: 0.0.0.0, i.e. all interfaces)")
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
