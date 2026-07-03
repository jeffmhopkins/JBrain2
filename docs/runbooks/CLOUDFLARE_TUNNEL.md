# Home-network access via Cloudflare Tunnel

> **Status:** Living · **Last verified:** 2026-07-03

This is the recommended way to reach JBrain from outside your house when the box
sits on a home network with a **dynamic IP** and possibly **CGNAT** (carrier-grade
NAT). The tunnel solves both problems: a `cloudflared` connector on the box dials
*out* to Cloudflare and traffic for your domain flows back down that connection,
so you need **no static IP, no port-forwarding, and no inbound reachability**. It
works behind CGNAT precisely because nothing has to reach you from the internet.

You keep your domain registered wherever it is now (e.g. Namecheap) — only the
**DNS hosting** moves to Cloudflare.

## How it fits the stack

In tunnel mode the installer sets `JBRAIN_SITE_ADDR=http://<domain>`, so Caddy
serves the app as **plain HTTP on :80** and skips Let's Encrypt entirely. TLS
terminates at **Cloudflare's edge** (Cloudflare manages the public certificate for
your hostname), and the encrypted tunnel carries traffic to the box. The connector
reaches Caddy at **`http://proxy:80`** over the compose `edge` network. The browser
talks HTTPS to Cloudflare end-to-end, so the app's `Secure` session cookie works.

```
browser ──HTTPS──> Cloudflare edge ──encrypted tunnel──> cloudflared ──http──> proxy:80 (Caddy) ──> api
```

The connector is opt-in: it lives behind the `tunnel` compose profile and only
runs when `.env` has `TUNNEL_ENABLED=true`. A stock deploy never starts it.

## One-time setup

### 1. Move your domain's DNS to Cloudflare
1. Create a free Cloudflare account and **Add a site** for your domain.
2. Cloudflare gives you two nameservers. At your registrar (Namecheap: *Domain →
   Nameservers → Custom DNS*) replace the existing nameservers with Cloudflare's.
3. Wait for Cloudflare to show the domain as **Active** (usually minutes to a few
   hours). The domain stays *registered* at Namecheap — only DNS hosting moved.

### 2. Create the tunnel and get a token
1. In the Cloudflare dashboard open **Zero Trust → Networks → Tunnels**.
2. **Create a tunnel → Cloudflared**, give it a name (e.g. `jbrain`), and save.
3. On the "Install connector" screen, copy the **token** — the long string that
   starts with `eyJ...`. You don't run the shown install command; the JBrain
   stack runs the connector for you. Just keep the token.

### 3. Add the public hostname
Still in the tunnel config, add a **Public hostname**:
- **Subdomain / domain**: the name you'll use, e.g. `brain.yourdomain.com`.
- **Service type**: `HTTP`
- **URL**: `proxy:80`

Cloudflare automatically creates the DNS record for that hostname. Make sure the
hostname here matches the **Domain** you give the installer.

### 4. Run the installer
```bash
curl -fsSL https://raw.githubusercontent.com/jeffmhopkins/JBrain2/main/deploy/install.sh | sudo bash
```
- **Domain**: `brain.yourdomain.com` (the public hostname from step 3).
- **Access mode**: choose **1) Cloudflare Tunnel** (the default).
- **Cloudflare Tunnel token**: paste the `eyJ...` token from step 2.

The installer writes `JBRAIN_SITE_ADDR`, `TUNNEL_ENABLED=true`, and
`CLOUDFLARE_TUNNEL_TOKEN` to `/opt/jbrain2/.env` and brings the stack up with the
connector. When it finishes, open `https://brain.yourdomain.com` and paste your
owner key.

## Adding the tunnel to an existing install

If you already installed in direct mode, edit `/opt/jbrain2/.env`:
```
JBRAIN_SITE_ADDR=http://brain.yourdomain.com
TUNNEL_ENABLED=true
CLOUDFLARE_TUNNEL_TOKEN=eyJ...
```
then `sudo jbrain restart` (the helper picks up the `tunnel` profile from
`TUNNEL_ENABLED`). Complete the Cloudflare dashboard steps above first.

## Verify and troubleshoot

- `jbrain status` should list **cloudflared** as running.
- `jbrain logs cloudflared` should show registered connections to Cloudflare
  ("Registered tunnel connection"). Auth errors usually mean a bad/rotated token.
- A **502** at the edge means the connector can't reach the origin — confirm the
  public hostname's service is exactly `http://proxy:80` and `jbrain status` shows
  `proxy` healthy.
- If the page loads but you can't sign in, confirm the URL is **https://** (the
  `Secure` cookie is only sent over HTTPS — Cloudflare provides this).

## Notes

- **SSL/TLS mode**: the tunnel is encrypted regardless of the dashboard's
  edge↔origin mode, so the default works. Optionally turn on *Always Use HTTPS*
  so plain-HTTP requests to your hostname are upgraded.
- **No ports exposed**: the connector needs only outbound 443 to Cloudflare. You
  do not (and should not) port-forward 80/443 on your router in tunnel mode.
- The `cloudflared` image is pulled at runtime; nothing extra is built.
- **The tunnel depends on the internet.** A WAN outage (even with the box and LAN
  on battery) takes the tunnel down with it. To keep signing in from devices on
  the same network during an outage, also enable LAN access — see
  `docs/runbooks/LOCAL_ACCESS.md`.

## Per-session web preview (jcode host mode)

`docs/archive/JCODE_PREVIEW_HOST_PLAN.md` serves each jcode session's dev server at its own
**`<slug>-preview.<host>`** under *this same tunnel* — no per-session TryCloudflare
quick-tunnel. The api proxies the preview by slug to the internal control server
(`/__jcode_preview/{slug}` → jcode `/preview/{slug}`, HTTP + the HMR WebSocket), so the
sandbox stays isolated. The Caddy side is rendered automatically from the env (below);
the one thing you do by hand is the Cloudflare wildcard:

1. **One wildcard published hostname.** In **Networks → Connectors → your tunnel →
   Published application routes**, add **Subdomain `*`, Domain `<your-host>`**, Service
   **`http://proxy:80`** (the same origin as the main app), Path empty. That routes
   `*.<your-host>` to the box — one rule covers every session. *Cloudflare wildcards are
   full-label only* (`*.<host>`), so a partial `*-preview.<host>` is **not** valid — the
   full `*.<host>` wildcard catches the previews and Caddy (below) filters out the
   `-preview` ones. If your main app is at the **apex** (`<host>`), the wildcard sits
   cleanly beside it; if it's a subdomain, its exact route still wins.

   > **Two gotchas that cost real time** — both about *this* screen:
   > - **A wildcard route does NOT auto-create its DNS record** (you'll see a yellow
   >   "this domain contains a wildcard, so no DNS record will be created" warning — a
   >   *specific* hostname auto-creates DNS, a wildcard doesn't). So add it by hand:
   >   **DNS → Records → Add record → CNAME, Name `*`, Target `<tunnel-id>.cfargotunnel.com`
   >   (copy it from your apex record), Proxied.** Until that exists, `*.<host>` is
   >   NXDOMAIN and the preview never resolves. (Proxied wildcards are fine on the free
   >   plan — that's not the blocker.)
   > - **Keep `*` in the *Subdomain* field.** Re-saving this route can silently clear the
   >   `*`, turning it into a second `<host>` (apex) route — then every preview falls
   >   through to the connector's `http_status:404` catch-all (a `cf-ray` 404 that never
   >   reaches the box). If previews 404 after an edit, check the Subdomain still reads `*`.
2. **TLS — free, because the slug is one label deep.** `<slug>-preview.<host>` is a
   single label under the zone, so Cloudflare's free **Universal SSL `*.<host>`** already
   covers it — no Advanced Certificate Manager. (A nested `*.preview.<host>` would be a
   two-level wildcard needing paid ACM; the flattened `<slug>-preview` form the control
   server mints avoids it.)
3. **Caddy — automatic.** Setting `JCODE_PREVIEW_BASE_HOST` (step 4) makes the proxy
   entrypoint render a `http://*.<host>` site (`deploy/proxy-preview-conf.sh`) that
   routes ONLY `<slug>-preview.<host>` to the api's `/__jcode_preview/<slug>` and 404s
   every other subdomain; the app sites already 404 `/__jcode_preview*`, so a sandbox dev
   app can only ever be served on its own subdomain. Defense in depth: the api also
   rejects in-process any request whose Host isn't `<slug>-preview.<base>`. Nothing to
   hand-edit; unset the var to tear the site back down.
4. **Enable preview.** Run **`sudo jbrain enable-jcode-preview`** — it sets
   `JCODE_PREVIEW_BASE_HOST` (defaulting to your `JBRAIN_DOMAIN`; pass an apex explicitly
   — `sudo jbrain enable-jcode-preview <apex>` — for a subdomain deploy) and recreates
   the stack so the change takes effect (a `.env` change isn't picked up by `restart`).
   Host-served preview is the only mode (the per-session cloudflared quick-tunnel was
   retired); a base host is all it needs. Turn on **debug access**
   (`docs/runbooks/DEBUG_ACCESS.md`) first so the control server's verbose logs (`/debug/logs/jcode`)
   show `preview proxy → :port` per request. Start a dev server in a session on `$PORT`,
   open its `<slug>-preview.<host>` → the page loads and HMR live-reloads; open a second
   session to confirm concurrency.

   *Host preview assumes tunnel mode (TLS at the Cloudflare edge); the rendered site is
   plain HTTP on `:80` like the main site. On the first `jbrain up` after enabling it,
   check `docker compose logs proxy` for a clean Caddy start.*
