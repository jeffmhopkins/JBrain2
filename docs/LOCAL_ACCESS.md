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

When `JBRAIN_LAN_ADDR` is set (e.g. `https://jbrain.local`):

- **mDNS** (`avahi-daemon` on the host) advertises the box as `<name>.local`, so
  any device on the LAN resolves it with zero per-client config and no internet.
  Avahi tracks the box's IP across DHCP changes.
- **Caddy** adds a second site for that hostname and serves it over HTTPS using
  its **internal CA** (`tls internal`) — a self-signed cert minted locally, no
  Let's Encrypt and no inbound reachability required. The proxy entrypoint renders
  this site from `JBRAIN_LAN_ADDR` at container start
  (`deploy/proxy-lan-conf.sh`), reusing the same app handlers as the public site.

```
laptop ──HTTPS──> jbrain.local (Caddy, internal CA) ──> api    [all on the LAN]
```

Because it's genuine HTTPS, the `Secure` cookie is stored and login works — tunnel
up or down.

## Enabling it

### Fresh install
`deploy/install.sh` asks **"Enable local network access?"** Accept it and pick a
name (defaults to the box's current hostname). The installer installs
`avahi-daemon`, aligns the system hostname so `<name>.local` is advertised, and
writes `JBRAIN_LAN_ADDR=https://<name>.local` to `/opt/jbrain2/.env`.

### Existing install
1. Install the mDNS responder: `sudo apt-get install -y avahi-daemon`.
   Avahi advertises `<system-hostname>.local`; set the hostname if you want a
   friendlier name (`sudo hostnamectl set-hostname jbrain`).
2. Add to `/opt/jbrain2/.env`:
   ```
   JBRAIN_LAN_ADDR=https://jbrain.local
   ```
   (match the `<name>.local` avahi advertises).
3. `sudo jbrain restart` — the proxy entrypoint picks up the new site.

To disable, blank `JBRAIN_LAN_ADDR` (or remove the line) and restart; the LAN
site is torn down on the next start.

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
