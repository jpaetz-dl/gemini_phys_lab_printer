#!/usr/bin/env python3
"""
status_display.py -- full-screen status dashboard for the button/printer rig.

Reads status.json (written by button_neopixel_printer.py via status_io.py)
and redraws a plain-text dashboard once a second. Doesn't touch any
GPIO/I2C hardware itself, so it's safe to run alongside the main script.

Meant to run directly on the Pi's console (tty1) via a systemd service bound
to that tty, so the status is visible on an attached HDMI monitor without
ever needing a keyboard or login -- see respeaker_setup_runbook.md section 11
for the systemd unit (status-display.service) and the getty@tty1 override it
needs.

Can also just be run by hand over SSH for a quick look:
    python3 status_display.py
"""

import socket
import sys
import time
from datetime import datetime

from status_io import read_status

REFRESH_SECONDS = 1.0

# ANSI helpers - safe on any Linux console/terminal.
CLEAR = "\x1b[2J\x1b[H"
BOLD = "\x1b[1m"
RESET = "\x1b[0m"
COLORS = {
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "cyan": "\x1b[36m",
    "magenta": "\x1b[35m",
    "red": "\x1b[31m",
    "gray": "\x1b[90m",
}

STATE_COLOR = {
    "starting": "gray",
    "idle": "green",
    "recording": "yellow",
    "generating": "cyan",
    "printing": "magenta",
    "stopped": "gray",
}


def color(text, name):
    return f"{COLORS[name]}{text}{RESET}"


def get_local_ip():
    """Best-effort local network IP, for pointing people at the web status page."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # doesn't actually send anything (UDP)
        return s.getsockname()[0]
    except OSError:
        return "unknown"
    finally:
        s.close()


def format_timestamp(ts):
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_duration(seconds):
    seconds = int(seconds)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def render(status, ip_address):
    lines = []
    lines.append(BOLD + "=" * 56 + RESET)
    lines.append(BOLD + "  Gemini Phys Lab Printer -- Status".ljust(56) + RESET)
    lines.append(BOLD + "=" * 56 + RESET)
    lines.append("")

    if not status:
        lines.append(color("  No status yet - is button_neopixel_printer.py running?", "red"))
        lines.append("")
        lines.append(f"  IP address: {ip_address}")
        return "\n".join(lines)

    state = status.get("state", "unknown")
    state_color = STATE_COLOR.get(state, "gray")
    lines.append(f"  State:        {color(state.upper(), state_color)}")

    started_at = status.get("started_at")
    if started_at:
        lines.append(f"  Uptime:       {format_duration(time.time() - started_at)}")

    if "pot_fraction" in status:
        pct = status["pot_fraction"] * 100
        voltage = status.get("pot_voltage", 0.0)
        strip_state = "ON" if status.get("strip_on") else "off"
        lines.append(f"  Pot level:    {pct:.0f}%  ({voltage:.2f}V)  -> strip {strip_state}")

    if "button_pressed" in status:
        pressed = status["button_pressed"]
        button_text = color("PRESSED", "yellow") if pressed else color("released", "gray")
        lines.append(f"  Button:       {button_text}")

    lines.append("")
    lines.append(f"  Last print:   {format_timestamp(status.get('last_print_time'))}"
                  + (f"  (style: {status['last_print_style']})" if status.get("last_print_style") else ""))
    if status.get("last_api_response_seconds") is not None:
        lines.append(f"  Last API:     {status['last_api_response_seconds']:.2f}s")

    last_error = status.get("last_error")
    if last_error:
        lines.append(color(f"  Last error:   {last_error}", "red")
                      + f"  ({format_timestamp(status.get('last_error_time'))})")
    else:
        lines.append("  Last error:   none")

    lines.append("")
    lines.append(f"  IP address:   {ip_address}")
    lines.append(f"  Web status:   http://{ip_address}:8080")
    lines.append("")
    lines.append(BOLD + "=" * 56 + RESET)

    return "\n".join(lines)


def main():
    ip_address = get_local_ip()
    try:
        while True:
            status = read_status()
            print(CLEAR + render(status, ip_address))
            time.sleep(REFRESH_SECONDS)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
