# Owner tunnel — the Android app reaches the box end to end, Cloudflare carries only ciphertext (proposed)

> **Status:** Proposed · **Last verified:** 2026-10-17 · **Waves:** S0◻️ A◻️ B◻️ C◻️ D◻️ E◻️ F◻️

**Status: proposed. Not on the roadmap, nothing built.** The owner's ask, 2026-10-17:

> *"I basically want to avoid cloudfare man in the middle risks and have a way of sending the
> pwa update from my server that can be verified not man in the middle."*
>
> *"Could we use a android app that gets a key during lan enrollment, and generate our own
> tunnel from that?"*
>
> *"Phone is the primary use case. We still have share links, but that is low risk as they
> don't carry privileges. … we need to have the Android app path side loadable at first
> before retiring the pwa."*

## 1. The problem

The public path is `browser → HTTPS → Cloudflare edge → tunnel → cloudflared → http://proxy:80
→ api` (`../runbooks/CLOUDFLARE_TUNNEL.md`). TLS ends at Cloudflare's edge, so whoever controls
that edge (Cloudflare, anyone in the Cloudflare account, anyone holding
`CLOUDFLARE_TUNNEL_TOKEN`) sees the owner key at login and every session cookie and response.
Worse, they serve the PWA's JavaScript. Edge-injected script runs on the genuine origin, so it
can ride the owner session and would even pass a passkey check. **No key held inside a page
that Cloudflare serves can defend that page from Cloudflare.**

Signing the PWA bundle and verifying it in the service worker does not close it either: the
browser re-fetches the service-worker script itself from the network on every update check,
and an installed worker cannot veto its replacement. Browsers offer no way to pin a page's code
to a signing key.

What is *not* at risk: the box's own code. Ops → Update fetches `main` from GitHub directly
(`deploy/update-inner.sh`), not through Cloudflare. Only the hop from the box to the owner's
browser is exposed.

## 2. The design in one paragraph

The owner app becomes the existing Android host (`android/`, `OwnerActivity`) talking to the
box over **its own end-to-end channel tunnelled inside the Cloudflare tunnel**. At **LAN
enrollment** the phone makes a key in its secure hardware and the box issues it a client
certificate; the phone pins the box's owner-site key. Afterwards the app's WebView loads the
owner SPA from a private origin through an in-app proxy; each connection is a TLS session
**from the WebView straight to Caddy on the box**, pinned on the phone and client-authenticated
on the box, carried as bytes over a WebSocket through Cloudflare. Cloudflare sees ciphertext:
it can drop the connection but cannot read or change the app, its updates or its data. Because
every SPA asset arrives over the pinned channel, **a PWA update the box builds is verified by
construction**. The public PWA keeps working throughout and is retired for owner use only
after the app has proven itself (Wave E).

```
WebView ─(proxy override)─> in-app CONNECT proxy ─wss─> Cloudflare ─> cloudflared ─> proxy:80
   │                                                                                    │
   │  inner TLS 1.3, box key pinned + device client cert          /api/owner-tunnel (api)
   └──────────────────────────────── end to end ───────────────> proxy:8443 owner site ─> api
```

On the home network the app opens the same WebSocket at `https://jbrain.local` instead, so LAN
and remote are one code path, and owner access keeps working when the internet is down.

## 3. Components

### 3.1 Owner origin and its keys (box)

- **Private origin** `https://owner.jbrain.home.arpa` (`home.arpa` is reserved for home
  networks, RFC 8375). It is never in public DNS; the in-app proxy maps it to the tunnel. Its
  own origin means its own cookies, storage and service worker, separate from
  `hopkinsbrain.com`.
- **Owner-site key**: a long-lived key pair generated on the box on first start (no terminal),
  with a box-signed leaf certificate re-issued periodically **from the same key**, so the
  phone's pin (the SPKI hash) never changes. Rotating the key is a deliberate owner action
  that re-enrolls devices.
- **Enrollment CA**: a second box-generated key that signs device client certificates. Caddy
  trusts only it on the owner site.
- Both private keys live in a volume that only the component that needs them can read, the
  same pattern as `/data/lan-root.crt` (`deploy/proxy-publish-ca.sh`, public part only
  copied). The api's file access goes through the storage abstraction (non-negotiable #2).

### 3.2 The owner site (Caddy)

A new site on an **internal-only** listener `:8443`, not published to the host and not
reachable through cloudflared (which only ever dials `proxy:80`):

- `tls` with the owner-site cert; `client_auth` `require_and_verify` against the enrollment
  CA.
- `import app`, so it is the identical SPA and api.
- `header_up X-JBrain-Ingress owner-tunnel` and `header_up X-JBrain-Device
  {tls_client_fingerprint}`. Every other site strips both (`header_up -X-JBrain-…`), and the
  api trusts them only when the TCP peer is the `proxy` container, the same rule
  `api/client_ip.py` already applies to `X-Real-IP`.

### 3.3 The tunnel gateway (api)

`GET /api/owner-tunnel` (WebSocket), routed by the existing `/api/*` handler on every site:

- **Outer auth before any byte is pumped**: the app signs a gateway challenge with its device
  key; the api checks it against `owner_devices` (unrevoked). An unknown caller is closed at
  once, so the public internet cannot use the box as a relay or reach `:8443` at all.
- Then a byte pump between the WebSocket and `proxy:8443`, one WebSocket per proxied
  connection (WebView reuses HTTP/2 connections, so they are few). Bounded per device, idle
  timeout, counters and errors visible through the debug API.
- The gateway never sees plaintext: the inner TLS is between the WebView and Caddy.

### 3.4 Device identity and sessions (api)

- New table `owner_devices` (id, label, client-cert fingerprint, public key, enrolled_at,
  last_seen_at, revoked_at), owner-only RLS with its isolation test (non-negotiable #3).
- The enrolled device certificate **is** the owner's credential in the app: no owner key is
  typed there. On the first tunnelled request the api mints an owner session **bound to the
  device**: a `device_sessions` row carries the `owner_devices` id, and a bound session
  authenticates only when `X-JBrain-Device` (trusted per §3.2) matches. A copied cookie is
  worthless anywhere else.
- Settings → Devices lists enrolled phones (label, last seen, ingress) with rename and revoke.
  Revoking kills the device's tunnel, its sessions and its cert in one step (Caddy accepts any
  CA-signed cert; the api refuses a revoked fingerprint on every request).

### 3.5 LAN-only enrollment, by QR code

The owner's refinement (2026-10-17): *"a lan only accessed web page that shows a scannable
[code] the Android app scans? Then I never even have to transfer the stuff."*

1. On a second screen (normally a laptop) the owner signs in at `https://jbrain.local` with
   the owner key and opens Settings → Devices → **Add a phone**. That page, and the api route
   that mints its QR, are **LAN-only** (rules below).
2. The page shows a QR code holding: the box's owner-site SPKI pin, a **single-use enrollment
   code** (256-bit, valid ~5 minutes, stored hashed), the LAN and public gateway addresses,
   and a format version.
3. The app scans it, makes its non-exportable Keystore key, and posts its CSR to
   `POST /api/owner-devices/enroll` together with an HMAC over the CSR keyed by the
   enrollment code. The enroll route is LAN-only too. The box consumes the code (one
   conditional UPDATE, like `consume_jcode_share`), signs the CSR with the enrollment CA and
   returns the client cert.
4. Both the page (live, as it polls) and the app show the new device's fingerprint as a short
   word or digit string; the owner checks they match. A mismatch revokes the device on the
   spot.

The camera is the trust channel: the pin reaches the phone without crossing the network,
and the owner key is never typed into the app. The QR is only as trustworthy as the screen
showing it. On a laptop that clicked through the `jbrain.local` certificate warning, a LAN
attacker in the middle could in principle swap the QR; step 4 catches that, and installing
the box's root certificate on the laptop removes it. A wall panel can show the QR later, since
the box already reaches the panels over a channel they verify (`../plans/ROOM_ENDPOINT_PLAN.md`
§10.4j).

**LAN-only** means a request is accepted only when **all** hold:

1. it came through the LAN site, by an `X-JBrain-Ingress lan` marker set in the LAN site
   block and stripped everywhere else (trusted only from the `proxy` peer);
2. `X-Real-IP` is not the `cloudflared` container and not the docker gateway;
3. `X-Real-IP` is inside the owner's home subnet (a Settings value, auto-suggested).

Never "a private address": the docker networks are RFC 1918 too, and jcode and pysandbox
containers run arbitrary code. `Cf-Connecting-IP`, `X-Forwarded-For` and `Host` are never
consulted.

LAN is a second requirement next to the owner key, never a replacement: guest Wi-Fi, IoT
devices and the panels are on the LAN too.

### 3.6 The Android app

Built on the existing owner host (`OwnerActivity`, `OwnerWebViewClient`, `NavigationPolicy`):

- **Enrollment screen** (LAN): scan the §3.5 QR code, make a non-exportable key in the
  Android Keystore (StrongBox where present), send the CSR, show the fingerprint to compare;
  the cert and the SPKI pin are stored.
- **In-app CONNECT proxy** on loopback, applied only to this WebView (androidx.webkit
  `ProxyController`). `CONNECT owner.jbrain.home.arpa:443` opens the gateway WebSocket
  (LAN first, `hopkinsbrain.com` otherwise) and pumps bytes; anything else is refused, so the
  WebView cannot leak to other hosts through it.
- **Pin and client cert** in `OwnerWebViewClient`: `onReceivedSslError` proceeds only when the
  leaf's SPKI matches the pin (any other cert cancels); `onReceivedClientCertRequest` answers
  with the Keystore key.
- **Reconnect** on network change, and a plain "can't reach your box" screen when both routes
  fail.
- **App updates** are downloaded from the GitHub release (not through Cloudflare), and Android
  installs an update only when it is signed by the same key. Today's APK is debug-signed with
  a key the CI runner generates per run, which gives no such guarantee, so Wave C adds a
  **stable release signing key** held as a GitHub Actions secret. GitHub is then trusted for
  the app exactly as it already is for the box's code.

The member dashboard and location engine in the same APK are untouched.

## 4. What it protects, and what it doesn't

| Threat | Today | After Wave E |
|---|---|---|
| Cloudflare or a Cloudflare-account attacker reads or alters owner traffic | Yes | No: ciphertext only |
| Edge-injected JS in the owner app / a tampered PWA update | Yes | No: assets arrive over the pinned channel |
| Stolen `CLOUDFLARE_TUNNEL_TOKEN` (rogue connector gets a share of traffic) | Owner phishing and traffic | Ciphertext, and the outer challenge fails |
| Leaked owner key used from outside | Full access | Nothing: login and enrollment need the app or the LAN |
| Stolen owner session cookie | Full access for up to a year | Useless off the enrolled device |
| Debug token seen in transit | Owner-grade access until expiry | After Wave F, a debug grant is a client cert used only inside the tunnel; nothing replayable crosses Cloudflare |
| Cloudflare blocks or drops the connection | Outage | Still an outage (LAN path keeps working) |
| GitHub compromise (box code, APK signing secret) | Trusted | Still trusted |
| Lost or compromised phone | n/a | Revoke in Settings → Devices from the LAN browser or another device |

Share links, intake links, the member dashboard, OwnTracks and panels keep using the public
host as today; share links carry no owner privilege.

## 5. Waves

- **S0 — spike, go/no-go. ◻️** On a real phone, prove the four Android behaviours the design
  rests on: `ProxyController` routes the WebView's HTTPS, WebSockets, SSE and service-worker
  fetches through a loopback proxy; `onReceivedSslError` pinning holds for subresources and
  WebSockets (and re-fires for a different cert); `onReceivedClientCertRequest` works with a
  Keystore key; a WebSocket byte pump through the Cloudflare tunnel stays up for real use.
  Also confirm on the box, via the debug API, that Caddy sees real LAN client addresses for
  `jbrain.local` (not the docker gateway). A failure here reshapes §3.6 before anything else
  is built.
- **A — box: owner origin, devices, enrollment. ◻️** §3.1 key generation, the §3.2 Caddy
  site and the ingress markers on every site, `owner_devices` + RLS isolation test, the §3.5
  QR page, enroll endpoint and home-subnet setting, device-bound sessions, Settings → Devices.
  Security paths at 100% coverage.
- **B — box: the tunnel gateway. ◻️** §3.3 with challenge auth, limits, debug-API counters.
  Tests with a fake device key against real Caddy in compose.
- **C — Android: enrollment, proxy, pinning, release signing. ◻️** §3.6, the stable signing
  key and a separately named release asset, JVM tests for the proxy's host gate, the pin
  check, the QR payload and the enrollment MAC. Sideloadable from the GitHub release page.
- **D — side by side. ◻️** The owner uses the app daily while the public PWA stays as it is.
  Ops shows tunnel health per device. Ends with the owner's sign-off that the app covers
  everything the owner uses the PWA for, back gesture, notifications and share target included.
- **E — retire public owner access. ◻️** On `hopkinsbrain.com`, owner-key login and unbound
  owner sessions are refused; the owner screens show "open the JBrain app". Owner login stays
  available on `jbrain.local` from a browser as the break-glass and enrollment path. The
  switch is a Settings toggle, operable from the app, so it can be turned back off without a
  terminal. Share links, devices and panels are unaffected. On-box sign-off.

- **F — debug access through the tunnel. ◻️** Remote agents (cloud sessions) cannot run the
  app, so a debug grant becomes a short-lived **client certificate and key** issued by the
  enrollment CA, plus the owner-site SPKI pin, packed into the debug payload in place of
  today's bearer (`../runbooks/DEBUG_ACCESS.md`). The agent runs a small client that opens the
  §3.3 gateway WebSocket and the pinned inner TLS, so debug traffic is ciphertext to
  Cloudflare as well. Grants are minted only from the app or the LAN, keep today's TTL,
  suspend and revoke controls (revoke refuses the cert's fingerprint), and map to the
  existing `capability_token` principal, so the debug routes' scope does not change. First
  step: confirm the cloud environment's egress proxy carries a long-lived WebSocket to the
  public host. Once it works, the bearer form of the debug token is turned off.

## 6. Decisions for the owner before Wave E

- **Laptop after retirement.** Default: owner access from a laptop only on the LAN (browser at
  `jbrain.local`). A desktop client using the same gateway is possible later.
- **Debug API on the public host until Wave F.** Today's debug token is an owner-grade
  bearer that travels through Cloudflare. Default: keep it working, with short TTLs, until
  Wave F moves debug access into the tunnel; then the bearer form is turned off.
- **iPhone.** Out of scope; the design is Android-specific (proxy override, Keystore).

## 7. Non-negotiables touched

RLS + isolation test for `owner_devices` (#3); key files via the storage abstraction (#2);
tests in the same PRs with security paths at 100% (#5); `scripts/dev-setup.sh` updated for any
new tool (#8); every operator step PWA- or app-operable, including key generation, the home
subnet and the retirement toggle (#10).
