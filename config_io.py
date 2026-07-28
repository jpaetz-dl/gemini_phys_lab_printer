#!/usr/bin/env python3
"""
config_io.py -- shared, live-reloadable settings for the button/printer rig.

Same atomic read/write pattern as status_io.py, but for user-adjustable
settings rather than live status: LED colors, chase/pulse timing, the pot
on/off threshold, and mic/speaker levels. status_web.py's settings form
writes here; button_neopixel_printer.py reads here.

Colors are stored as [r, g, b, w] lists (JSON has no tuple/Color type) -- the
4th value is the dedicated White LED on the SK6812 RGBW strip (separate from
mixing r=g=b for "white"; it's its own diode, good for a warmer glow layered
on top of a color). button_neopixel_printer.py converts these to rpi_ws281x
Color objects at the point of use. Old 3-element [r, g, b] entries (saved
before the white channel was exposed) are padded with w=0 on read, so an
existing config.json from before this change still loads fine.

Important: button_neopixel_printer.py must call read_config() at each place
it needs a color/timing/threshold value (e.g. inside pulse()/chase()/
idle_color_for_pot(), not read it once into a module-level constant or
function default at import time. Function defaults are bound once, when the
function is defined -- changing config.json afterward wouldn't be picked up
if a value were only ever read into a default argument.
"""

import json
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("config.json")

# config.json keys that hold [r, g, b, w] color lists - read_config() pads
# any of these found as an old 3-element [r, g, b] entry with w=0.
COLOR_KEYS = ("idle_color", "chase_color", "pulse_color", "pulse_floor_color")

DEFAULTS = {
    # LED colors, as [r, g, b, w] (0-255 each). w is the dedicated White LED
    # on the SK6812 RGBW strip - defaults to 0 (off) so out-of-the-box
    # behavior is unchanged; dial it up per-phase for a warmer glow.
    "idle_color": [255, 255, 255, 0],       # full-brightness idle-on color
    "chase_color": [255, 255, 255, 0],
    "pulse_color": [255, 255, 255, 0],
    "pulse_floor_color": [15, 15, 15, 0],   # dim floor the pulse breathes down to (never fully dark)

    # Animation timing.
    "chase_step_delay": 0.03,     # seconds between each step of the chase; lower = faster
    "chase_tail_length": 6,       # number of pixels in the comet's fading tail
    "pulse_step_delay": 0.02,     # seconds between brightness steps; lower = faster pulse

    # Potentiometer on/off threshold, as a 0.0-1.0 fraction of ADC_VCC.
    "pot_on_threshold_fraction": 0.40,

    # Mic capture / speaker playback levels, 0-100 percent (amixer).
    "mic_percent": 40,
    "speaker_percent": 40,
}


def read_config():
    """Return the current config, merged over DEFAULTS so a fresh Pi (no
    config.json yet) or a partially-written file both work fine."""
    config = dict(DEFAULTS)
    try:
        saved = json.loads(CONFIG_PATH.read_text())
        config.update(saved)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    for key in COLOR_KEYS:
        value = config.get(key)
        if isinstance(value, list) and len(value) == 3:
            config[key] = value + [0]  # old entry saved before the white channel existed
    return config


def write_config(**fields):
    """Merge `fields` into config.json, writing atomically (tmp file +
    rename) so a reader never sees a half-written file."""
    current = {}
    try:
        current = json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    current.update(fields)
    current["updated_at"] = time.time()
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(current, indent=2))
    tmp_path.replace(CONFIG_PATH)


def reset_config():
    """Delete config.json, reverting everything to DEFAULTS."""
    CONFIG_PATH.unlink(missing_ok=True)
