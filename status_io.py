#!/usr/bin/env python3
"""
status_io.py -- shared status file used by button_neopixel_printer.py,
status_display.py (terminal dashboard), and status_web.py (LAN status page).

button_neopixel_printer.py calls write_status() at each stage of the
record/upload/print cycle; the other two just read it. Using a plain JSON
file (rather than e.g. a socket or shared memory) keeps the three scripts
decoupled -- the dashboard/web page work fine even if the main script isn't
running (they just show a stale/missing status), and nothing here touches
GPIO/I2C, so importing this module is always safe.
"""

import json
import time
from pathlib import Path

STATUS_PATH = Path(__file__).with_name("status.json")


def write_status(**fields):
    """Merge `fields` into the status file and write it back out atomically
    (write to a temp file, then rename over the real one) so a reader never
    sees a half-written file."""
    current = read_status()
    current.update(fields)
    current["updated_at"] = time.time()

    tmp_path = STATUS_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(current, indent=2))
    tmp_path.replace(STATUS_PATH)  # atomic on the same filesystem


def read_status():
    """Return the current status dict, or {} if it doesn't exist yet or is
    unreadable (e.g. main script hasn't started, or caught mid-write)."""
    try:
        return json.loads(STATUS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
