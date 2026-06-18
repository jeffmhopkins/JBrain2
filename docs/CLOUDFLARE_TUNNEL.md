# Home-network access via Cloudflare Tunnel

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
