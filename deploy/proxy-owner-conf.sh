#!/bin/sh
# Render the owner site (docs/plans/OWNER_TUNNEL_PLAN.md §3.2): the owner app's private
# origin, https://owner.jbrain.home.arpa on :8443, served with the box's owner-CA leaf and
# requiring a client certificate from the enrollment CA. Nothing gets past the handshake
# without an enrolled phone's key, which is the gate: the port is on the `edge` network
# only, but cloudflared shares that network.
#
# The files come from the api's `owner-keys ensure` one-shot. Until all three exist no
# site is written, so Caddy's `import /etc/caddy/owner/*.caddy` matches nothing and the
# public and LAN sites run exactly as before: a missing key can never take them down.
#
# Split out from the entrypoint so it can be exercised against a throwaway output dir.
set -eu

dir="${1:-/etc/caddy/owner}"
tls="${2:-/etc/caddy/owner-tls}"
mkdir -p "$dir"
rm -f "$dir"/*.caddy

for f in site.crt site.key enroll-ca.crt; do
	[ -s "$tls/$f" ] || exit 0
done

cat > "$dir/owner.caddy" <<CADDY
https://owner.jbrain.home.arpa:8443 {
	tls $tls/site.crt $tls/site.key {
		protocols tls1.3
		client_auth {
			mode require_and_verify
			trust_pool file $tls/enroll-ca.crt
		}
	}
	import app owner-tunnel {http.request.tls.client.public_key_sha256}
}
CADDY
