#!/bin/sh
# Generate Caddy's optional LAN site config from JBRAIN_LAN_ADDR.
#
# When set (e.g. https://jbrain.local) we write a site that serves the same app
# over HTTPS using Caddy's internal CA, so the Secure session cookie works on
# the local network even when the Cloudflare tunnel / internet is down. Unset ->
# no file, so Caddy's `import /etc/caddy/lan/*.caddy` glob matches nothing and
# only the public site is served (no behaviour change for stock deploys). The same address
# also gets the GrowBot site on :8444 (growbot.caddy, below).
#
# Split out from the entrypoint so it can be exercised directly in tests against
# a throwaway output dir. See docs/runbooks/LOCAL_ACCESS.md.
set -eu

dir="${1:-/etc/caddy/lan}"
mkdir -p "$dir"
# Start clean so a removed/blanked JBRAIN_LAN_ADDR tears the site back down.
rm -f "$dir"/*.caddy
[ -n "${JBRAIN_LAN_ADDR:-}" ] || exit 0

cat > "$dir/lan.caddy" <<EOF
${JBRAIN_LAN_ADDR} {
	tls internal
	import app lan ""
}
EOF

# The GrowBot site (docs/plans/GROWBOT_PLAN.md §2): the desk robot's creature app, on its own
# origin because its root-absolute paths (/api/..., /v3/...) collide with the PWA's. Same name
# and same internal CA as the LAN site, so it needs no new mDNS alias; a different port is a
# different origin for script and storage. Browsers do not scope cookies by port, though, so
# the owner's `__Host-` session would be sent here too: the Cookie header is rebuilt to hold
# growbot's own cookie alone (or dropped), so the api session never reaches vendored
# code. growbot's /internal routes are the api's (bearer growbot.token); the prefix is
# refused here so none is ever reachable from the LAN.
host="${JBRAIN_LAN_ADDR#*://}"
host="${host%%/*}"
host="${host%%:*}"
cat > "$dir/growbot.caddy" <<EOF
https://${host}:8444 {
	tls internal
	handle /internal* {
		respond 404
	}
	request_body {
		max_size 16MB
	}
	@session header_regexp Cookie (^|;)\s*gb_session=
	handle @session {
		reverse_proxy growbot:8810 {
			flush_interval -1
			header_up Cookie "gb_session={http.request.cookie.gb_session}"
			header_up X-Real-IP {remote_host}
		}
	}
	handle {
		reverse_proxy growbot:8810 {
			flush_interval -1
			header_up -Cookie
			header_up X-Real-IP {remote_host}
		}
	}
}
EOF
