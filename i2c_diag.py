#!/usr/bin/env python3
"""
i2c_diag.py -- standalone pot/button reader for diagnosing I2C bus flakiness.

Deliberately independent of the rest of this project (doesn't import
config_io.py, status_io.py, or button_neopixel_printer.py) - just the same
underlying hardware libraries (adafruit-blinka, adafruit-circuitpython-ads1x15)
that button_neopixel_printer.py itself uses to talk to the ADS1015. That way
this script can't be affected by any bug in our own code, and it can run at
the same time as (or instead of) the real services without fighting over
which of *our* modules is doing what - it only touches the I2C bus directly.

Run `sudo ./stop_services.sh` first if button-printer.service is running -
two processes both opening /dev/i2c-1 and polling the same ADS1015 will
contend with each other and muddy the results.

What it does: reads the button (ADS1015 channel A3) and potentiometer
(channel A2) voltages in a loop, printing each one with a timestamp, and
prints every I2C error (OSError from the underlying smbus call) as it
happens - with a running count - rather than swallowing or retrying it, so
you can see exactly how often the bus is actually erroring and correlate it
with anything else going on (touching wires, LEDs running, etc).

Deliberately has NO timeout/retry/recovery logic (unlike
button_neopixel_printer.py's read_voltage()) - the goal here is to observe
the bus's raw behavior, not paper over it. If a read hangs, this script
hangs too, and stays hung - that's useful information on its own: whichever
line printed last (with "reading pot..." / "reading button..." just before
each read) tells you which channel's read is the one actually stuck, and
how long it ran cleanly before that happened. Ctrl+C to stop early.

Usage:
    python3 i2c_diag.py
    python3 i2c_diag.py --interval 0.05
    python3 i2c_diag.py --address 0x48
"""

import argparse
import sys
import time

import board
import busio
import adafruit_ads1x15.ads1015 as ADS
from adafruit_ads1x15.analog_in import AnalogIn

BUTTON_ADC_CHANNEL = 3  # A3
POT_ADC_CHANNEL = 2     # A2
ADC_VCC = 3.3
BUTTON_PRESSED_VOLTAGE_THRESHOLD = ADC_VCC / 2  # below this = pressed


def timestamp():
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=0.1,
                         help="Seconds between reads (default: 0.1)")
    parser.add_argument("--address", type=lambda s: int(s, 0), default=0x48,
                         help="ADS1015 I2C address (default: 0x48)")
    args = parser.parse_args()

    print(f"[{timestamp()}] Opening I2C bus and ADS1015 @ {hex(args.address)} ...", flush=True)
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        ads = ADS.ADS1015(i2c, address=args.address)
        button_channel = AnalogIn(ads, BUTTON_ADC_CHANNEL)
        pot_channel = AnalogIn(ads, POT_ADC_CHANNEL)
    except Exception as exc:
        print(f"[{timestamp()}] FAILED to open I2C/ADS1015: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)

    print(f"[{timestamp()}] Ready. Reading every {args.interval}s (Ctrl+C to stop) ...\n", flush=True)

    reads = 0
    errors = 0
    consecutive_errors = 0
    started_at = time.time()

    try:
        while True:
            reads += 1

            # Pot (A2) - read voltage and raw ADC count separately from the
            # button so a failure/hang on one channel is unambiguous about
            # which one it was.
            print(f"[{timestamp()}] reading pot (A{POT_ADC_CHANNEL})...", end=" ", flush=True)
            try:
                pot_v = pot_channel.voltage
                pot_raw = pot_channel.value
                pot_pct = max(0.0, min(1.0, pot_v / ADC_VCC)) * 100
                print(f"{pot_v:.3f}V ({pot_pct:.0f}%, raw={pot_raw})", flush=True)
                consecutive_errors = 0
            except OSError as exc:
                errors += 1
                consecutive_errors += 1
                print(f"I2C ERROR #{errors} (consecutive: {consecutive_errors}): "
                      f"{type(exc).__name__}: {exc}", flush=True)

            # Button (A3)
            print(f"[{timestamp()}] reading button (A{BUTTON_ADC_CHANNEL})...", end=" ", flush=True)
            try:
                button_v = button_channel.voltage
                pressed = button_v < BUTTON_PRESSED_VOLTAGE_THRESHOLD
                print(f"{button_v:.3f}V ({'PRESSED' if pressed else 'released'})", flush=True)
                consecutive_errors = 0
            except OSError as exc:
                errors += 1
                consecutive_errors += 1
                print(f"I2C ERROR #{errors} (consecutive: {consecutive_errors}): "
                      f"{type(exc).__name__}: {exc}", flush=True)

            # Periodic summary so a long run doesn't require scrolling back
            # to see the error rate.
            if reads % 50 == 0:
                elapsed = time.time() - started_at
                rate = (errors / reads) * 100 if reads else 0
                print(f"--- {reads} reads over {elapsed:.0f}s, {errors} errors "
                      f"({rate:.1f}%) ---", flush=True)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        pass

    elapsed = time.time() - started_at
    rate = (errors / reads) * 100 if reads else 0
    print(f"\n[{timestamp()}] Stopped. {reads} reads over {elapsed:.0f}s, "
          f"{errors} I2C errors ({rate:.1f}%).")
    if errors == 0:
        print("No I2C errors seen during this run.")
    else:
        print("If this error rate is high, or errors cluster around specific "
              "events (touching wires, LEDs active, etc), that points to "
              "wiring/power rather than anything in the main script - see "
              "the I2C reliability section in README.md.")


if __name__ == "__main__":
    main()
