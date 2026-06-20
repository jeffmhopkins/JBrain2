# Local-network access (sign in when the internet is down)

The Cloudflare Tunnel (`docs/CLOUDFLARE_TUNNEL.md`) is how you reach JBrain from
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
2. **The Secure session cookie.** Login sets the session cookie with the `Secure`
   flag (`backend/src/jbrain/api/auth.py`), and browsers **refuse to store a
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
  run by `deploy/jbrain-avahi-alias.service`). avahi tracks the box's IP across
  DHCP changes. This host half is provisioned by `deploy/lan-setup.sh`.

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
- a different `*.local` name, then `sudo jbrain enable-lan && sudo jbrain restart`;
- blank it to disable, then `sudo jbrain enable-lan` (tears down the alias
  service) and `sudo jbrain restart` (drops the Caddy site).

## The certificate warning (and how to remove it)

`tls internal` mints the cert from Caddy's own CA, which your devices don't trust
yet, so the **first visit shows a certificate warning**. You can click through —
the connection is still HTTPS, so login works immediately. To remove the warning,
install Caddy's root certificate on your devices once. It lives on the persistent
`caddy_data` volume:

```bash
docker compose -f /opt/jbrain2/docker-compose.yml cp \
  proxy:/data/caddy/pki/authorities/local/root.crt ./jbrain-local-ca.crt
```

Import `jbrain-local-ca.crt` into each device's trust store (OS/browser "trusted
root certificate authorities"). The key is stable across restarts, so this is a
one-time step per device.

## Notes

- **No new exposure.** The LAN site reuses the already-published `443` port; it
  adds no port-forwarding and nothing reachable from the internet. mDNS
  (UDP 5353) is link-local — it does not leave your LAN.
- **Works alongside any access mode.** Tunnel mode (HTTP public site on :80) and
  direct mode (Let's Encrypt on :443) both coexist with the LAN HTTPS site;
  Caddy routes by hostname.
- **Owner key still required.** This changes *how you reach* the box, not auth —
  you still sign in with your owner key.
