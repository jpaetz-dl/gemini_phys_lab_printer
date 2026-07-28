#!/usr/bin/env bash
#
# stop_services.sh -- stop all three systemd services for this project, so
# you can run a script by hand (e.g. i2c_diag.py) without the real
# button-printer/dashboard services fighting over the same I2C bus/GPIO/
# NeoPixel strip in the background.
#
# Stops (in this order):
#   1. button-printer.service   - the main button/NeoPixel/printer script
#   2. status-display.service   - the HDMI console dashboard (tty1)
#   3. status-web.service       - the LAN web page
#
# This does NOT disable the services - they'll come back on the next reboot
# (or the next `systemctl start`/`./reload_services.sh`) same as always.
# Only used to free things up for manual testing right now.
#
# Usage:
#   ./stop_services.sh              # stop all three
#   ./stop_services.sh web          # just status-web.service
#   ./stop_services.sh main display # just those two
#
# Needs sudo (systemctl stop on these unit files requires root) - run it
# with sudo directly, or it'll re-exec itself with sudo for you.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

declare -A SERVICES=(
  [main]="button-printer.service"
  [display]="status-display.service"
  [web]="status-web.service"
)

# Default: stop all three, in a sensible order (main script first, since
# it's the one that actually holds the I2C bus/GPIO/strip, then the two
# read-only dashboards).
ORDER=(main display web)

if [[ $# -gt 0 ]]; then
  TARGETS=("$@")
else
  TARGETS=("${ORDER[@]}")
fi

for name in "${TARGETS[@]}"; do
  unit="${SERVICES[$name]:-}"
  if [[ -z "$unit" ]]; then
    echo "Unknown target '$name' - expected one of: ${!SERVICES[*]}" >&2
    exit 1
  fi

  if ! systemctl cat "$unit" &>/dev/null; then
    echo "Skipping $unit - not installed on this Pi yet (no such unit file)."
    continue
  fi

  echo "Stopping $unit ..."
  systemctl stop "$unit"
done

echo
echo "Status:"
for name in "${TARGETS[@]}"; do
  unit="${SERVICES[$name]}"
  systemctl is-active --quiet "$unit" 2>/dev/null \
    && echo "  $unit: still active (!)" \
    || echo "  $unit: stopped"
done

echo
echo "Nothing is holding the I2C bus/GPIO/NeoPixel strip now (assuming all"
echo "three stopped above) - safe to run a script by hand. Bring everything"
echo "back with: sudo ./reload_services.sh"
