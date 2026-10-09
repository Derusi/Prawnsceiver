#!/bin/bash
# rtl_tcp daemon wrapper: serves one RTL-SDR dongle (pinned by serial)
# on the network so the prawnceiver server can connect from another host.
# Map: dongle serial -> rtl_tcp port (add new dongles here).
case "$1" in
  48263793)          PORT=1234 ;;  # NESDR SMArt v5 (primary, satellites)
  77771111153705700) PORT=1235 ;;  # generic R820T (AIS, when plugged in)
  *) echo "unknown serial $1" >&2; exit 1 ;;
esac
exec /usr/bin/rtl_tcp -a 0.0.0.0 -p "$PORT" -d "$1" -f 137680000 -s 240000 -g 0
