#!/bin/sh
# Generate Caddy's optional LAN site config from JBRAIN_LAN_ADDR.
#
# When set (e.g. https://jbrain.local) we write a site that serves the same app
# over HTTPS using Caddy's internal CA, so the Secure session cookie works on
# the local network even when the Cloudflare tunnel / internet is down. Unset ->
# no file, so Caddy's `import /etc/caddy/lan/*.caddy` glob matches nothing and
# only the public site is served (no behaviour change for stock deploys).
#
# Split out from the entrypoint so it can be exercised directly in tests against
# a throwaway output dir. See docs/LOCAL_ACCESS.md.
set -eu

dir="${1:-/etc/caddy/lan}"
mkdir -p "$dir"
# Start clean so a removed/blanked JBRAIN_LAN_ADDR tears the site back down.
rm -f "$dir"/*.caddy
[ -n "${JBRAIN_LAN_ADDR:-}" ] || exit 0

cat > "$dir/lan.caddy" <<EOF
${JBRAIN_LAN_ADDR} {
	tls internal
	import app
}
EOF
