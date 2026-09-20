#!/bin/sh
# Proxy container entrypoint: render the optional LAN site from JBRAIN_LAN_ADDR,
# then hand off to Caddy. Generation runs every start so the LAN site tracks the
# current env (enable/disable/rename) without rebuilding the image.
set -eu

/usr/local/bin/proxy-lan-conf.sh /etc/caddy/lan
/usr/local/bin/proxy-preview-conf.sh /etc/caddy/preview

# Publish the internal-CA root where the api (uid 1000) can read it. Backgrounded and
# looping because Caddy mints that CA lazily, when it first serves the `tls internal`
# site — which is after this line. See the script's header.
/usr/local/bin/proxy-publish-ca.sh &

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
