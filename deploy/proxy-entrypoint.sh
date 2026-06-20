#!/bin/sh
# Proxy container entrypoint: render the optional LAN site from JBRAIN_LAN_ADDR,
# then hand off to Caddy. Generation runs every start so the LAN site tracks the
# current env (enable/disable/rename) without rebuilding the image.
set -eu

/usr/local/bin/proxy-lan-conf.sh /etc/caddy/lan

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
