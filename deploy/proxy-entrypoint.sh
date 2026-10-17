#!/bin/sh
# Proxy container entrypoint: render the optional LAN site from JBRAIN_LAN_ADDR,
# then hand off to Caddy. Generation runs every start so the LAN site tracks the
# current env (enable/disable/rename) without rebuilding the image.
set -eu

/usr/local/bin/proxy-lan-conf.sh /etc/caddy/lan
/usr/local/bin/proxy-preview-conf.sh /etc/caddy/preview
/usr/local/bin/proxy-owner-conf.sh /etc/caddy/owner /etc/caddy/owner-tls

# The owner site's files are only checked for existence above. A key and certificate that
# do not match (a crash between their two writes, a partial restore) would stop Caddy
# loading at all and take the public and LAN sites down with it, so a config that does
# not validate with the owner site in it runs without it. This checks the site certificate
# and key; a bad enroll-ca.crt still validates but fails closed (every phone is refused).
if [ -e /etc/caddy/owner/owner.caddy ] \
	&& ! out=$(caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1); then
	echo "proxy: config does not validate with the owner site; trying without it:" >&2
	printf '%s\n' "$out" | tail -n 1 >&2
	rm -f /etc/caddy/owner/owner.caddy
fi

# Publish the internal-CA root where the api (uid 1000) can read it. Backgrounded and
# looping because Caddy mints that CA lazily, when it first serves the `tls internal`
# site — which is after this line. See the script's header.
/usr/local/bin/proxy-publish-ca.sh &

exec caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
