#!/bin/bash
# Tidal Connect bridge: starts mDNS, then the manager (sidebar page + one Tidal Connect device per chosen player).
set -u
log() { echo "[bridge] $*"; }

if ! /armhf/lib/ld-linux-armhf.so.3 --library-path /armhf/lib/arm-linux-gnueabihf /armhf/bin/true >/dev/null 2>&1; then
  log "ERROR: this kernel cannot run the 32-bit Tidal Connect program."
  sleep infinity
fi

# mDNS: the Tidal app finds devices through it
mkdir -p /run/dbus /run/avahi-daemon
rm -f /run/dbus/pid /run/avahi-daemon/pid
dbus-daemon --system --fork
sed -i -E \
  -e 's/^#?host-name=.*/host-name=tidal-connect-bridge/' \
  -e 's/^#?publish-workstation=.*/publish-workstation=no/' \
  -e 's/^#?publish-hinfo=.*/publish-hinfo=no/' \
  /etc/avahi/avahi-daemon.conf
LAN_IF=$(awk '$2=="00000000"{print $1; exit}' /proc/net/route)
[ -n "$LAN_IF" ] && sed -i -E "s/^#?allow-interfaces=.*/allow-interfaces=$LAN_IF/" /etc/avahi/avahi-daemon.conf
avahi-daemon --daemonize --no-rlimits --no-drop-root || log "WARNING: avahi-daemon failed to start"

exec python3 -u /manager.py
