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
import os
import threading
import time
from pathlib import Path

STATUS_PATH = Path(__file__).with_name("status.json")

# button_neopixel_printer.py calls write_status() from more than one thread
# at once - the main thread (button press/release, cleanup) and
# pot_monitor_loop()'s background thread (every POT_POLL_INTERVAL_SECONDS).
# Without this lock, two overlapping calls could both write to the *same*
# fixed ".json.tmp" path and then both try to rename it: whichever renames
# first succeeds and the file is gone, so the second call's tmp_path.replace()
# raises FileNotFoundError ("No such file or directory: '...status.json.tmp'
# -> '...status.json'") - not a filesystem problem, a race between threads.
# The lock also prevents a subtler lost-update race, where two threads each
# read the old status, merge their own change into it, and write back -
# whichever finishes last wins and silently discards the other thread's
# update. Serializing the whole read-modify-write-rename sequence per
# process fixes both.
_write_lock = threading.Lock()


def write_status(**fields):
    """Merge `fields` into the status file and write it back out atomically
    (write to a temp file, then rename over the real one) so a reader never
    sees a half-written file. Thread-safe (see _write_lock above)."""
    with _write_lock:
        current = read_status()
        current.update(fields)
        current["updated_at"] = time.time()

        # Unique per call (pid + thread id), not a fixed name, so even a
        # crash mid-write between two calls can't leave a stale tmp file
        # that later collides with a fresh one.
        tmp_path = STATUS_PATH.with_suffix(f".{os.getpid()}.{threading.get_ident()}.json.tmp")
        tmp_path.write_text(json.dumps(current, indent=2))
        tmp_path.replace(STATUS_PATH)  # atomic on the same filesystem


def read_status():
    """Return the current status dict, or {} if it doesn't exist yet or is
    unreadable (e.g. main script hasn't started, or caught mid-write)."""
    try:
        return json.loads(STATUS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
