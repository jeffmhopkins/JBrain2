#!/bin/sh
# Start AdGuard Home as the house's DNS (docs/runbooks/HOME_DNS.md).
#
# Everything the box decides is decided here, at each start, so Ops -> Update is the
# whole delivery and nothing goes in `.env` (CLAUDE.md #10):
#   - the DNS listener is the box's LAN address, found from its default route;
#   - the web UI and its API listen on the docker bridge gateway, the address the api
#     reaches as host.docker.internal and no LAN device is told about (a host that adds
#     a route to it could still knock, which is what the password is for);
#   - the one UI user's password is `adguard.token`, which the api mints into the
#     shared secrets volume, so only the api holds it.
set -eu

CONF_DIR=/opt/adguardhome/conf
CONF="$CONF_DIR/AdGuardHome.yaml"
SEED=/etc/jbrain-adguard/AdGuardHome.yaml
PIN=/etc/jbrain-adguard/pin.awk
TOKEN_FILE="${ADGUARD_TOKEN_FILE:-/run/jbrain-internal/adguard.token}"
UI_PORT="${ADGUARD_UI_PORT:-3053}"

say() { echo "[adguard] $*"; }

# The api mints the token at its own start; exiting lets the restart policy retry
# rather than serving a UI with no password.
waited=0
while [ ! -s "$TOKEN_FILE" ]; do
  if [ "$waited" -ge 120 ]; then
    say "no adguard.token after 120s; is the api up?" >&2
    exit 1
  fi
  sleep 1
  waited=$((waited + 1))
done
token="$(tr -d ' \t\r\n' < "$TOKEN_FILE")"
case "$token" in
  '' | *[!0-9A-Za-z]*) say "adguard.token is not alphanumeric" >&2; exit 1 ;;
esac

# The source address of the route to the internet is the LAN address the router's
# devices can reach. Waits out a boot where the network is not up yet.
lan_ip() {
  ip -4 route get 1.1.1.1 2>/dev/null \
    | awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }'
}
dns_addr="${ADGUARD_DNS_ADDR:-}"
waited=0
while [ -z "$dns_addr" ]; do
  dns_addr="$(lan_ip)"
  [ -n "$dns_addr" ] && break
  [ "$waited" -ge 120 ] && { say "no default route after 120s" >&2; exit 1; }
  sleep 2
  waited=$((waited + 2))
done

# A public address would make this an open resolver for the internet (a box on a
# direct uplink, or in the router's DMZ). Refuse rather than serve it.
case "$dns_addr" in
  10.* | 192.168.* | 172.1[6-9].* | 172.2[0-9].* | 172.3[01].*) ;;
  *) say "$dns_addr is not a private address; refusing to serve DNS on it" >&2; exit 1 ;;
esac

# The router, for naming devices by reverse lookup (pin.awk says why not resolved).
gateway="$(ip -4 route get 1.1.1.1 2>/dev/null \
  | awk '{ for (i = 1; i < NF; i++) if ($i == "via") { print $(i + 1); exit } }')"

ui_addr="${ADGUARD_UI_ADDR:-}"
if [ -z "$ui_addr" ]; then
  ui_addr="$(ip -4 -o addr show dev docker0 2>/dev/null \
    | awk '{ split($4, a, "/"); print a[1]; exit }')"
fi
[ -n "$ui_addr" ] || { say "no docker0 address for the UI" >&2; exit 1; }

hash="$(htpasswd -nbBC 10 jbrain "$token" | head -n 1 | cut -d: -f2-)"
case "$hash" in
  '$2'*) ;;
  *) say "could not hash the UI password" >&2; exit 1 ;;
esac

[ -f "$CONF" ] || { say "first start: seeding the config"; cp "$SEED" "$CONF"; }
awk -v dns="$dns_addr" -v ptr="$gateway" -v ui="$ui_addr:$UI_PORT" -v hash="$hash" \
  -f "$PIN" "$CONF" > "$CONF.pinned"
mv "$CONF.pinned" "$CONF"
chmod 600 "$CONF"

say "DNS on $dns_addr:53, UI on $ui_addr:$UI_PORT"
exec /opt/adguardhome/AdGuardHome --no-check-update -c "$CONF" -w /opt/adguardhome/work
