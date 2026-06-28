#!/bin/sh
# Generate Caddy's optional host-mode web-preview site from
# JBRAIN_JCODE_PREVIEW_BASE_HOST (docs/JCODE_PREVIEW_HOST_PLAN.md).
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
		}
	}
	handle {
		respond 404
	}
}
EOF
