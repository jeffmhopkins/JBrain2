#!/bin/sh
# Publish Caddy's internal-CA ROOT CERTIFICATE where the api can actually read it.
#
# WHY THIS EXISTS. Caddy mints the LAN site's CA as root, into a directory that also holds
# the CA PRIVATE KEY, so that directory is not world-traversable — correctly. The api runs
# as `appuser` (uid 1000) and simply cannot get to it: measured on the live box,
# `[Errno 13] Permission denied` on the root.crt path, with the parent not even listable.
#
# The api needs that root to hand a room-endpoint panel something it can validate
# `https://jbrain.local` against. Without it `_panel_base` falls back to the public
# hostname and a panel three metres from the box routes its traffic out through Cloudflare
# and back — which is what was happening (ROOM_ENDPOINT_PLAN.md §10.4j).
#
# Copying the ROOT is the safe half of the pair: it is a public certificate, published to
# every device that trusts this box, and `docs/runbooks/LOCAL_ACCESS.md` already tells a
# human to `docker cp` exactly this file. The alternative — loosening the mode on a
# directory containing a private key — trades a real secret for a convenience.
#
# A LOOP, not a one-shot, and that is the whole reason this is its own script: Caddy mints
# the CA lazily when it first serves the `tls internal` site, which is after this
# entrypoint has handed off. There is no moment at startup when the file is reliably
# there. Re-copying also means a regenerated CA (or a renamed LAN site) propagates without
# anyone noticing it had to.
set -eu

src="${1:-/data/caddy/pki/authorities/local/root.crt}"
dst="${2:-/data/lan-root.crt}"
period="${CA_PUBLISH_PERIOD_S:-30}"

while :; do
    if [ -r "$src" ]; then
        # Atomic: the api may read this at any moment, and half a PEM validates nothing.
        tmp="$dst.tmp"
        if cp "$src" "$tmp" 2>/dev/null; then
            chmod 0644 "$tmp" 2>/dev/null || true
            mv "$tmp" "$dst" 2>/dev/null || rm -f "$tmp"
        fi
    fi
    sleep "$period"
done
