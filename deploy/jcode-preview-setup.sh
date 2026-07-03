#!/bin/sh
# Enable host-mode jcode web preview by writing the .env key that drives it
# (docs/archive/JCODE_PREVIEW_HOST_PLAN.md): JCODE_PREVIEW_BASE_HOST. A non-empty base host
# IS the switch — host is the only preview mode since the Wave P5b cutover, so the
# api fail-closes when it's empty. Compose maps the base host to the api + proxy and
# the sandbox, so this is the whole switch — the operator never hand-edits .env.
# Split out from the `jbrain` helper so it can be exercised against a throwaway
# .env (mirroring proxy-preview-conf.sh).
#
# Usage: jcode-preview-setup.sh <env-file> [base-host]
#   base-host defaults to JBRAIN_DOMAIN from the env file.
#
# Idempotent: re-running (or changing the base host) replaces the keys in place
# rather than stacking duplicates. The one thing this CAN'T do is the Cloudflare
# wildcard published hostname + DNS (a dashboard action) — it prints that step.
set -eu

env_file="${1:?usage: jcode-preview-setup.sh <env-file> [base-host]}"
base="${2:-}"

[ -f "$env_file" ] || { echo "preview: no env file at $env_file" >&2; exit 1; }

# Host preview is a jcode feature and rides the box's own Cloudflare tunnel (TLS at
# the edge; the rendered Caddy site is plain http:// on :80). Refuse on a box that
# can't serve it rather than write a config that silently fails closed.
grep -q '^JCODE_ENABLED=true' "$env_file" || {
	echo "preview: code mode (jcode) is not enabled — nothing to preview" >&2
	exit 1
}
grep -q '^TUNNEL_ENABLED=true' "$env_file" || {
	echo "preview: host mode needs tunnel mode (JBRAIN_SITE_ADDR=http://<domain>)" >&2
	exit 1
}

# Default the base host to the box's domain. For free Universal SSL the base host
# must be the zone APEX — <slug>-preview.<base> is then one label deep and covered
# by Cloudflare's free *.<base> cert; a deeper app subdomain would be two levels
# under the zone and need paid ACM, so pass the apex explicitly in that case.
if [ -z "$base" ]; then
	base="$(grep '^JBRAIN_DOMAIN=' "$env_file" | cut -d= -f2-)"
fi
[ -n "$base" ] || { echo "preview: no base host and JBRAIN_DOMAIN is empty" >&2; exit 1; }

# A hostname is only letters, digits, dots, hyphens. Reject anything else before it
# lands in the rendered Caddy site address / the api's Host check and crash-loops
# the proxy (the same fail-closed guard proxy-preview-conf.sh applies).
case "$base" in
	*[!A-Za-z0-9.-]*) echo "preview: invalid base host '$base'" >&2; exit 1;;
esac

# Replace-or-append a key, preserving the rest of the file. grep -v + rewrite is
# portable to busybox sh (no sed -i in-place semantics to depend on); a replaced
# key moves to the end, which is fine for a generated .env.
set_key() {
	if grep -q "^$1=" "$env_file"; then
		tmp="$env_file.tmp.$$"
		grep -v "^$1=" "$env_file" > "$tmp"
		printf '%s=%s\n' "$1" "$2" >> "$tmp"
		mv "$tmp" "$env_file"
	else
		printf '%s=%s\n' "$1" "$2" >> "$env_file"
	fi
}

set_key JCODE_PREVIEW_BASE_HOST "$base"

cat >&2 <<EOF
preview: host mode enabled — sessions serve at <slug>-preview.$base
preview: ONE manual Cloudflare step (once): in your tunnel's Published application
preview: routes add Subdomain '*', Domain '$base', Service http://proxy:80 (this
preview: creates the wildcard DNS). Free Universal SSL covers *.$base only when
preview: $base is your zone apex. See docs/runbooks/CLOUDFLARE_TUNNEL.md. Takes effect on
preview: the next 'jbrain up' (a .env change isn't picked up by 'restart').
EOF
