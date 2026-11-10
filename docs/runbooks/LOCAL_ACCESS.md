# Local-network access (sign in when the internet is down)

> **Status:** Living · **Last verified:** 2026-11-10

The Cloudflare Tunnel (`docs/runbooks/CLOUDFLARE_TUNNEL.md`) is how you reach JBrain from
*outside* your house. But it depends on the internet: a power blip that takes the
WAN down while the box and LAN stay up (battery backup) leaves you locked out of
your own server, even from a laptop on the same switch.

This page is the fix — a **local site** that works on the LAN regardless of the
tunnel or internet.

## Why plain `http://<box-ip>` doesn't work

Two things block the obvious "just hit the local IP" approach:

1. **Name + TLS.** Your public hostname resolves through Cloudflare DNS, which is
   unreachable with the WAN down. In tunnel mode Caddy also serves only plain
   **HTTP on :80** (TLS lives at Cloudflare's edge), so there's no local HTTPS.
2. **The Secure session cookie.** Login sets the session cookie
   (`__Host-jbrain_session`) with the `Secure` flag
   (`backend/src/jbrain/api/session_cookie.py`), and browsers **refuse to store a
   `Secure` cookie over plain HTTP**. So even if you reach the box on the LAN over
   HTTP, the login "succeeds" but the cookie never sticks and you stay logged out.

So local access needs **real HTTPS on the LAN** plus a name you can resolve
without the internet. That's exactly what this feature provides.

## How it works

Local access is **on by default** at `https://jbrain.local`
(`JBRAIN_LAN_ADDR`, defaulted in `docker-compose.yml`). Two halves:

- **Caddy** serves a second site for that name over HTTPS using its **internal
  CA** (`tls internal`) — a cert minted locally, no Let's Encrypt and no inbound
  reachability. The proxy entrypoint renders this site from `JBRAIN_LAN_ADDR` at
  container start (`deploy/proxy-lan-conf.sh`), reusing the public site's handlers.
- **mDNS** (`avahi-daemon`) makes `jbrain.local` resolve on the LAN with zero
  per-client config and no internet. avahi only auto-advertises the box's *system*
  hostname, so rather than rename the box, a small service publishes `jbrain.local`
  as a **CNAME alias** pointing at `<hostname>.local` (`deploy/avahi_alias.py`,
  run by the `jbrain-avahi-alias.service` systemd unit — a unit `deploy/lan-setup.sh`
  **generates** into `/etc/systemd/system/`, not a file in the repo). avahi tracks
  the box's IP across DHCP changes. This host half is provisioned by
  `deploy/lan-setup.sh`.

```
laptop ──HTTPS──> jbrain.local (Caddy, internal CA) ──> api    [all on the LAN]
```

Because it's genuine HTTPS, the `Secure` cookie is stored and login works — tunnel
up or down.

## Setup

### Fresh install
Nothing to choose — `deploy/install.sh` enables it: it writes
`JBRAIN_LAN_ADDR=https://jbrain.local`, then runs `lan-setup.sh` to install
`avahi-daemon` + the python bindings and start the alias service.

### Existing install
`sudo jbrain update` turns it on automatically (it backfills `JBRAIN_LAN_ADDR`
and runs `lan-setup.sh`). One caveat from how updates bootstrap: the *first*
update after this change runs with the **old** `jbrain` script, which rebuilds
the stack (so the Caddy site comes up) but doesn't yet run the host setup. Finish
that one time with the now-updated helper:

```bash
sudo jbrain enable-lan
```

Every later `jbrain update` does it for you.

### Renaming or disabling
Edit `JBRAIN_LAN_ADDR` in `/opt/jbrain2/.env`:
- a different `*.local` name, then `sudo jbrain enable-lan && sudo jbrain up`;
- blank it to disable, then `sudo jbrain enable-lan` (tears down the alias
  service) and `sudo jbrain up` (drops the Caddy site).

Use `sudo jbrain up`, not `restart` — a `.env` change is only picked up when the
container is recreated; `restart` reuses the proxy's old environment.

## The certificate warning (and how to remove it)

`tls internal` mints the cert from Caddy's own CA, which your devices don't trust
yet, so the **first visit shows a certificate warning**. You can click through —
the connection is still HTTPS, so login works immediately. To remove the warning,
install Caddy's root certificate on your devices once.

**From the PWA (no terminal).** Signed in as the owner, open
`https://<your box>/api/lan/root.crt` (Settings → Devices → *Box certificate*, and the GrowBot screen, link to it; a link to it opened from another site, such as mail or chat, is refused as cross-site, so tap it in the app or type the URL): it downloads
`jbrain-box-root.crt`, the public root only. On iOS open the downloaded profile, install it
in Settings → General → VPN & Device Management, then turn on full trust in Settings →
General → About → Certificate Trust Settings. On Android install it under Settings →
Security → Encryption & credentials → Install a certificate → CA certificate. On a desktop
import it into the OS or browser's trusted roots. A box with no LAN site (or a root the api
cannot read) answers 404.

**From a shell**, the same file lives on the persistent `caddy_data` volume:

```bash
docker compose -f /opt/jbrain2/docker-compose.yml cp \
  proxy:/data/caddy/pki/authorities/local/root.crt ./jbrain-local-ca.crt
```

Import `jbrain-local-ca.crt` into each device's trust store (OS/browser "trusted
root certificate authorities").

> The box also publishes this same root to `/data/lan-root.crt` in the `caddy_data`
> volume (`deploy/proxy-publish-ca.sh`), because the api runs as a non-root user and
> cannot read it where Caddy writes it — the directory holds the CA private key. That
> copy is what lets a room-endpoint panel be given the LAN address with a certificate it
> can validate, instead of being sent out through the tunnel
> (`../plans/ROOM_ENDPOINT_PLAN.md` §10.4j). It is the public root only; the key is never
> copied. The key is stable across restarts, so this is a
one-time step per device.

> The `docker compose cp` is a host-shell step and is no longer the way to do it
> (CLAUDE.md non-negotiable #10): `GET /api/lan/root.crt` (`backend/src/jbrain/api/lan_ca.py`,
> owner-only) closed that gap in GrowBot wave G2.

## Chrome on Android says ERR_TOO_MANY_RETRIES

Newer Android asks before an app may reach the home network, and Chrome without that
permission fails every LAN address (even `http://<box-ip>`) with this misleading error, while
panels and robots reach the box fine. Settings → Apps → Chrome → Permissions → **Nearby
devices** (or **Local network**) → Allow. JBrain's GrowBot sheets and Settings → Devices say
so, since a page on the box cannot load to say it.

## Notes

- **No new exposure.** The LAN site reuses the already-published `443` port; it
  adds no port-forwarding and nothing reachable from the internet.
- **The GrowBot site on `:8444`.** The same address also gets
  `https://jbrain.local:8444`, the desk robot's creature app (`growbot/README.md`), on its own
  port because a different port is a different origin and the app's paths collide with the
  PWA's. The proxy publishes `8444` on the box for the LAN (forward it nowhere). Same internal
  CA, so the same certificate warning and the same one-time root install apply. Blanking
  `JBRAIN_LAN_ADDR` drops it with the LAN site. `8443`, the owner site, is never published.
- **The robots' clock on UDP `8123`.** The `growbot` service answers SNTP there: a robot has no
  internet, and its TLS check of the box's certificate dates needs the real time first. One
  48-byte reply per well-formed request, nothing larger, so it amplifies nothing; LAN only,
  forward it nowhere. A robot that cannot set its clock fails its flash as `clock_unset`. mDNS
  (UDP 5353) is link-local — it does not leave your LAN.
- **Works alongside any access mode.** Tunnel mode (HTTP public site on :80) and
  direct mode (Let's Encrypt on :443) both coexist with the LAN HTTPS site;
  Caddy routes by hostname.
- **Owner key still required.** This changes *how you reach* the box, not auth —
  you still sign in with your owner key.
