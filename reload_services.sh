#!/usr/bin/env bash
#
# reload_services.sh -- restart all three systemd services for this project
# without rebooting the whole Pi. Handy while iterating: edit a .py file,
# run this, and the change is live.
#
# Restarts (in this order):
#   1. button-printer.service   - the main button/NeoPixel/printer script
#   2. status-display.service   - the HDMI console dashboard (tty1)
#   3. status-web.service       - the LAN web page
#
# `daemon-reload` runs first so an edited .service *unit file* (not just a
# .py file) is picked up too - harmless no-op if you only changed Python.
#
# Usage:
#   ./reload_services.sh              # restart all three
#   ./reload_services.sh web          # just status-web.service
#   ./reload_services.sh main display # just those two
#
# Needs sudo (systemctl restart on these unit files requires root) - run it
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

# Default: restart all three, in a sensible order (main script first, then
# the two read-only dashboards).
ORDER=(main display web)

if [[ $# -gt 0 ]]; then
  TARGETS=("$@")
else
  TARGETS=("${ORDER[@]}")
fi

systemctl daemon-reload

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

  echo "Restarting $unit ..."
  systemctl restart "$unit"
done

echo
echo "Status:"
for name in "${TARGETS[@]}"; do
  unit="${SERVICES[$name]}"
  systemctl is-active --quiet "$unit" 2>/dev/null \
    && echo "  $unit: active" \
    || echo "  $unit: NOT active (check: journalctl -u $unit -f)"
done
