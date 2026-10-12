#!/bin/sh
# Start mosquitto with the api's MQTT token as go-auth's User-Agent.
#
# The api refuses /internal/mqtt-* without mosquitto's own token
# (backend/src/jbrain/api/internal_auth.py), and go-auth's HTTP backend can carry a
# credential in no header but User-Agent. The api mints the token into the shared
# `internal_secrets` volume when it starts, so this waits for it, renders the
# read-only conf with the token in place of the placeholder line, and runs mosquitto
# on the rendered copy. Nothing goes in `.env`, and the update that ships this is
# the update that makes it work.
#
# Runs as root (the image's default) so it can read the api-owned 0600 token;
# mosquitto drops to its own user after reading the conf.
set -eu

CONF_SRC="${JBRAIN_MQTT_CONF:-/etc/jbrain-mosquitto/mosquitto.conf}"
TOKEN_FILE="${JBRAIN_MQTT_TOKEN_FILE:-/run/jbrain-internal/mqtt.token}"
CONF_OUT="${JBRAIN_MQTT_CONF_OUT:-/tmp/mosquitto.conf}"
WAIT_SECONDS="${JBRAIN_MQTT_TOKEN_WAIT:-120}"

waited=0
while [ ! -s "$TOKEN_FILE" ]; do
  if [ "$waited" -ge "$WAIT_SECONDS" ]; then
    # Exit rather than start a broker that would deny every phone; the restart
    # policy tries again once the api is up.
    echo "mqtt: no api token at $TOKEN_FILE after ${WAIT_SECONDS}s" >&2
    exit 1
  fi
  sleep 1
  waited=$((waited + 1))
done

token="$(tr -d ' \t\r\n' < "$TOKEN_FILE")"
case "$token" in
  '' | *[!0-9A-Za-z]*)
    echo "mqtt: the token at $TOKEN_FILE is not alphanumeric" >&2
    exit 1
    ;;
esac

if ! grep -q '^auth_opt_http_user_agent[[:space:]]' "$CONF_SRC"; then
  echo "mqtt: $CONF_SRC has no auth_opt_http_user_agent line to fill in" >&2
  exit 1
fi

# Rewritten in place, not appended: with per_listener_settings an auth option belongs
# to the listener above it, and the conf's own line is in the right section.
umask 077
tmp="$CONF_OUT.$$"
awk -v tok="$token" '
  $1 == "auth_opt_http_user_agent" { print "auth_opt_http_user_agent " tok; next }
  { print }
' "$CONF_SRC" > "$tmp"
# A config reload (SIGHUP) re-reads the conf after the privilege drop.
if id mosquitto > /dev/null 2>&1; then
  chown mosquitto "$tmp"
fi
mv "$tmp" "$CONF_OUT"

if [ "$#" -eq 0 ]; then
  set -- /usr/sbin/mosquitto -c "$CONF_OUT"
fi
exec "$@"
