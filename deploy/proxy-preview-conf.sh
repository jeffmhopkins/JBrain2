#!/bin/sh
# Generate Caddy's optional host-mode web-preview site from
# JBRAIN_JCODE_PREVIEW_BASE_HOST (docs/archive/JCODE_PREVIEW_HOST_PLAN.md).
#
# When set (e.g. hopkinsbrain.com) we write a wildcard site http://*.<host> that routes
# ONLY <slug>-preview.<host> requests to the api's internal /__jcode_preview/<slug> prefix
# (the HMR WebSocket included), and 404s every other subdomain — so the catch-all wildcard
# can't serve anything but a preview. Unset -> no file, so Caddy's
# `import /etc/caddy/preview/*.caddy` glob matches nothing and nothing changes (stock
# deploys, and tunnel mode without host preview, are unaffected).
#
# Host preview rides the Cloudflare tunnel (TLS terminates at the edge), so the site is
# plain HTTP on :80 like the main site in tunnel mode. The slug is matched loosely here
# ([0-9a-f]+, avoiding a {16} that would clash with Caddy's {…} placeholders); the api
# enforces the exact 16-hex slug AND that the Host is <slug>-preview.<host>.
#
# Split out from the entrypoint so it can be exercised against a throwaway output dir.
set -eu

dir="${1:-/etc/caddy/preview}"
mkdir -p "$dir"
# Clear stale FIRST, so any early exit below leaves the preview site torn down.
rm -f "$dir"/*.caddy

host="${JBRAIN_JCODE_PREVIEW_BASE_HOST:-}"
[ -n "$host" ] || exit 0

# Host preview rides the Cloudflare tunnel (TLS at the edge); the rendered site is plain
# HTTP on :80. In direct / auto-TLS mode (JBRAIN_SITE_ADDR is a bare host) an http://*
# site would serve previews with no certificate — skip rather than misconfigure.
case "${JBRAIN_SITE_ADDR:-}" in
	http://*) : ;;
	*)
		echo "preview: host mode needs tunnel mode (http:// JBRAIN_SITE_ADDR); skipping" >&2
		exit 0
		;;
esac

# Fail closed on a malformed host: a bad value (space, newline, braces) would emit an
# invalid Caddy site address and crash-loop the WHOLE proxy — taking the main site down,
# not just the preview. A hostname is only letters, digits, dots, and hyphens.
case "$host" in
	*[!A-Za-z0-9.-]*)
		echo "preview: invalid JBRAIN_JCODE_PREVIEW_BASE_HOST '$host'; skipping" >&2
		exit 0
		;;
esac

cat > "$dir/preview.caddy" <<EOF
http://*.${host} {
	@preview header_regexp Host ^([0-9a-f]+)-preview\.
	handle @preview {
		rewrite * /__jcode_preview/{re.preview.1}{uri}
		reverse_proxy api:8000 {
			flush_interval -1
			header_up X-Real-IP {remote_host}
			header_up X-JBrain-Ingress preview
			header_up X-JBrain-Device ""
		}
	}
	handle {
		respond 404
	}
}
EOF

# GrowBot away from home: the creature on growbot.<host>, so the PWA opens it from anywhere
# (owner, 2026-11-11). It rides the same wildcard tunnel route as the previews, so it needs no
# Cloudflare change, and a name of its own beats the wildcard above. It is the LAN site's
# :8444 block with three doors shut: the setup page and the box's certificate are for a phone
# on the home network, and a robot dials the relay there too, so a cookieless /d/ (a chip,
# never a phone) has no business arriving through the tunnel. Named matchers, not path
# shorthand: Caddy sorts path-only handles ahead of named ones, which would shut /d/ on the
# phones as well. The cookie is rebuilt as on :8444; the owner's `__Host-` cookie is
# host-only on the apex and never sent here, but any other cookie for the domain would be.
# X-Growbot-Away tells the sidecar the request crossed the tunnel, so it refuses a relay socket
# with no valid session (a cookie named gb_session is all Caddy can check).
cat > "$dir/growbot.caddy" <<EOF
http://growbot.${host} {
	@shut path /internal* /setup.html /setup.js /box-root.crt
	handle @shut {
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
			header_up X-Growbot-Away 1
		}
	}
	@chip path /d/*
	handle @chip {
		respond 404
	}
	handle {
		reverse_proxy growbot:8810 {
			flush_interval -1
			header_up -Cookie
			header_up X-Real-IP {remote_host}
			header_up X-Growbot-Away 1
		}
	}
}
EOF
