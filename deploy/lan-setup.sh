#!/usr/bin/env bash
# Provision LAN access on the host: mDNS (avahi) so the box answers to a fixed
# <name>.local, plus a CNAME-alias service that publishes that name pointing at
# the host's own hostname (so we never have to rename the box). The Caddy LAN
# HTTPS site itself is on by default in compose; this is the host half that makes
# the name resolve. Idempotent — safe to re-run from install.sh and `jbrain`.
# See docs/LOCAL_ACCESS.md.
set -euo pipefail

INSTALL_DIR="${JBRAIN_INSTALL_DIR:-/opt/jbrain2}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT=/etc/systemd/system/jbrain-avahi-alias.service

say() { printf '\n[jbrain lan-setup] %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

# The advertised name follows JBRAIN_LAN_ADDR (default jbrain.local, matching the
# compose default). Only .local names use mDNS; a custom DNS name means the
# operator runs their own resolution, so skip the responder + alias for it.
LAN_ADDR="https://jbrain.local"
if [ -f "$INSTALL_DIR/.env" ] && grep -q '^JBRAIN_LAN_ADDR=' "$INSTALL_DIR/.env"; then
  LAN_ADDR="$(grep '^JBRAIN_LAN_ADDR=' "$INSTALL_DIR/.env" | cut -d= -f2-)"
fi
NAME="${LAN_ADDR#http://}"; NAME="${NAME#https://}"; NAME="${NAME%%/*}"
if [ -z "$NAME" ]; then
  say "LAN access disabled (JBRAIN_LAN_ADDR is blank) — removing alias service"
  systemctl disable --now jbrain-avahi-alias.service >/dev/null 2>&1 || true
  rm -f "$UNIT"; systemctl daemon-reload || true
  exit 0
fi
case "$NAME" in
  *.local) ;;
  *) say "JBRAIN_LAN_ADDR ($NAME) is not a .local name — skipping mDNS setup"; exit 0 ;;
esac

if ! command -v avahi-daemon >/dev/null 2>&1; then
  say "Installing avahi-daemon + python bindings (mDNS responder + CNAME alias)"
  apt-get update -qq
  apt-get install -y -qq avahi-daemon python3-dbus python3-gi
elif ! python3 -c 'import dbus, gi' >/dev/null 2>&1; then
  say "Installing python bindings for the mDNS alias"
  apt-get update -qq && apt-get install -y -qq python3-dbus python3-gi
fi

install -m 0755 "$SCRIPT_DIR/avahi_alias.py" "$INSTALL_DIR/avahi_alias.py"

cat > "$UNIT" <<EOF
[Unit]
Description=JBrain mDNS alias ($NAME)
After=avahi-daemon.service
Requires=avahi-daemon.service

[Service]
ExecStart=$INSTALL_DIR/avahi_alias.py $NAME
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
systemctl enable --now jbrain-avahi-alias.service
# Re-exec on every run so an updated avahi_alias.py / changed name takes effect.
systemctl restart jbrain-avahi-alias.service
say "Done — this box answers at $LAN_ADDR on the LAN"
