# Owner tunnel — the Android app reaches the box end to end, Cloudflare carries only ciphertext (proposed)

> **Status:** In progress · **Last verified:** 2026-10-18 · **Waves:** S0✅ A◻️ B◻️ C◻️ D◻️ E◻️ F◻️

**Scheduled 2026-10-17 (owner: "Go").** Revised after a four-lens review (§9). S0 passed on the
owner's phone (§5): the `network_security_config` trust anchor is the trust mechanism. The owner's ask, 2026-10-17:

> *"I basically want to avoid cloudfare man in the middle risks and have a way of sending the
> pwa update from my server that can be verified not man in the middle."*
>
> *"Could we use a android app that gets a key during lan enrollment, and generate our own
> tunnel from that?"*
>
> *"Phone is the primary use case. We still have share links, but that is low risk as they
> don't carry privileges. … we need to have the Android app path side loadable at first
> before retiring the pwa."*
>
> *"a lan only accessed web page that shows a scannable [code] the Android app scans? Then I
> never even have to transfer the stuff."* · *"The screen is right off the server."* ·
> *"No laptop, server only."*

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
and an installed worker cannot veto its replacement.

What is *not* at risk: the box's own code. Ops → Update fetches `main` from GitHub directly
(`deploy/update-inner.sh`), not through Cloudflare. Only the hop from the box to the owner's
browser is exposed.

## 2. The design in one paragraph

The owner app becomes the existing Android host (`android/`, `OwnerActivity`) talking to the
box over **its own end-to-end TLS tunnelled inside the Cloudflare tunnel**. Enrollment happens
**at the box**: the box's own monitor shows a one-time QR code, the app scans it, makes a key
in the phone's secure hardware, and gets a client certificate. Afterwards the app's WebView
loads the owner SPA from a private origin; each connection is a TLS session **from the WebView
straight to Caddy on the box**, trusting only the box's own CA and client-authenticated with
the phone's key, carried as bytes over a WebSocket through Cloudflare. Cloudflare sees
ciphertext: it can drop the connection but cannot read or change the app, its updates or its
data. Because every SPA asset arrives over that channel, **a PWA update the box builds is
verified by construction**. The public PWA keeps working throughout; debug access moves into
the tunnel (Wave E) before public owner access is retired (Wave F).

```
WebView ─(proxy override, owner host only)─> tunnel service (in app) ─wss─> Cloudflare ─> cloudflared ─> proxy:80
   │                                                                                                       │
   │  inner TLS 1.3: box CA only + device client cert                         /api/owner-tunnel (api gateway)
   └────────────────────────────────────── end to end ───────────────────────> proxy:8443 owner site ─> api
```

On the home network the app opens the same WebSocket at `https://jbrain.local` instead, so LAN
and remote are one code path, and owner access keeps working when the internet is down.

## 3. Components

### 3.1 Owner origin and its keys (box)

- **Private origin** `https://owner.jbrain.home.arpa` (`home.arpa` is reserved for home
  networks, RFC 8375). Never in public DNS: through an HTTP proxy Chromium sends `CONNECT
  owner.jbrain.home.arpa:443` without resolving the name. Its own origin means its own
  cookies, storage and service worker, separate from `hopkinsbrain.com`.
- **Owner CA** (box-generated): signs the owner site's TLS leaf. It is the *only* trust anchor
  the app accepts for the owner origin (§3.6). The leaf is long-lived (a year) and re-issued by
  the update one-shot below, which recreates the proxy anyway, so no Caddy reload path is
  needed.
- **Enrollment CA** (box-generated): signs device client certificates. Caddy trusts only it on
  the owner site.
- **Generated before Caddy needs them.** A one-shot `python -m jbrain.cli owner-keys ensure`
  runs from `update-inner.sh` before `up -d`, the way the update already runs its other
  one-shots, so Ops → Update delivers and renews it with no terminal (#10). The owner site is
  rendered only when its files exist (like `deploy/proxy-lan-conf.sh`), so a missing key can
  never take the public site down with it.
- **Two volumes, never one.** The enrollment CA *key* lives in an api-only volume (the api signs
  CSRs). The owner-site key and both CA *certificates* live in an api-writable volume the proxy
  mounts read-only. The internet-facing Caddy never sees a CA private key. Precedent:
  `internal_secrets` (api writes through the storage abstraction, consumers mount read-only).
- **Backups and restore:** both CAs are in the box backup. A box restored without them has new
  CAs: every phone re-enrolls at the screen and the app build carrying the old owner CA is
  replaced (§3.6).
- New backend dependency `cryptography` (CSR signing, ECDSA P-256), added to `pyproject.toml`
  and `scripts/dev-setup.sh` (#8).

### 3.2 The owner site (Caddy)

A new site on `:8443` with `tls` from the owner-site files and `client_auth`
`require_and_verify` against the enrollment CA. The `:8443` listener sets `strict_sni_host on`
(so a cert-less handshake under another name cannot ask for the owner host) and HTTP/1.1 and 2
only; the proxy entrypoint drops the site if its files do not load, and an update that makes
new owner keys restarts the proxy so Caddy reads them.

- **mTLS is the gate, not the network.** `:8443` is not published to the host, but cloudflared,
  the api and the proxy share the `edge` network, and a stolen tunnel token's remote config
  could point at `https://proxy:8443`. Without a client cert from the enrollment CA nothing gets
  past the handshake.
- **Ingress marker set on every site, never stripped.** `header_up` only lives inside
  `reverse_proxy`, which is inside the shared `(app)` snippet, so the snippet takes the ingress
  as an argument (`import app public` / `lan` / `owner-tunnel`) and always *sets*
  `X-JBrain-Ingress {args[0]}`. Setting overwrites anything a client sent, so no site can
  forget to strip. The generated LAN site and the jcode preview site's own `reverse_proxy`
  (`deploy/proxy-preview-conf.sh`) get the same treatment.
- **Device identity** = `X-JBrain-Device {http.request.tls.client.public_key_sha256}`, the
  hash of the client's public key, which survives cert re-issue (the cert fingerprint does
  not). For an EC key Caddy hashes the raw uncompressed point, not the DER SPKI;
  `keys.public_key_sha256` matches it, which the real-Caddy test pins. Set to empty on every
  other site.
- The api trusts both headers only when the TCP peer is the `proxy` container (the rule
  `api/client_ip.py` applies to `X-Real-IP`).
- **Real client address:** every tunnelled request reaches Caddy from the api's gateway, so
  `X-Real-IP` would be the api for all of them and every per-client limit one bucket. The
  gateway writes a PROXY-protocol v2 header with the outer `client_ip()`, and the `:8443`
  server accepts it from the api only (`listener_wrappers { proxy_protocol { allow <api> } tls
  }`).
- CI renders the full Caddyfile from each entrypoint script and runs `caddy validate`, and a
  test against real Caddy (a testcontainer beside Postgres) proves a client-sent
  `X-JBrain-Ingress`/`X-JBrain-Device` never reaches the api on any site.

### 3.3 The tunnel gateway (api)

`GET /api/owner-tunnel` (WebSocket), routed by the existing `/api/*` handler on every site.
Caddy, not the api, terminates the inner TLS: the SPA is served by Caddy's `file_server`, and
ending TLS in the api would mean in-process TLS, re-dispatch and static serving on one loop.

- **Outer auth, anti-relay only.** The server sends a fresh nonce (short expiry, single use);
  the app signs it with its device key; the api checks the key against `owner_devices`. An
  unknown caller is closed before any byte is pumped, so the internet cannot use the box as a
  relay. This is not a confidentiality boundary: after a genuine device authenticates,
  Cloudflare could inject bytes into the stream, which only reach `:8443`'s TLS handshake.
- **Byte pump** to `proxy:8443`, one WebSocket per proxied connection (HTTP/2 keeps them few),
  awaiting drain in both directions, keepalive pings well under Cloudflare's ~100 s WebSocket
  idle cutoff, bounded per device.
- **Revocation kicks live tunnels at once** through an in-process registry like
  `panel_ws.disconnect`, plus the `ws_reauth` tick.
- **Load:** the api runs one uvicorn worker, and each owner request now crosses its loop twice
  (gateway, then api). S0 and B measure throughput on uploads and exports; if it hurts, the pump
  moves to its own small container with the same auth.
- Counters and errors are visible through the debug API.

### 3.4 Device identity and sessions (api)

- New table `owner_devices` (id, label, SPKI hash, public key, enrolled_at, last_seen_at,
  revoked_at). RLS copies `device_sessions`' policies (`is_owner() OR auth_ctx()='login'`),
  because the gateway challenge and the per-request device check run before a principal exists;
  the isolation test covers the `login` context (#3).
- **The device certificate is the owner's credential in the app**; no owner key is typed there.
  On the first tunnelled request the api mints an owner session bound to the device: a nullable
  `device_id` on `device_sessions`, enforced **in the repo lookup** so all eight
  `service.authenticate` call sites (terminals, live, intake, ws_reauth, jcode share…) get it,
  not just `deps.current_principal`. A bound session authenticates only when `X-JBrain-Device`
  matches; a copied cookie is worthless elsewhere.
- **User-bound too.** A bound session lives 30 days, sliding. The Keystore key requires user
  authentication (biometric or screen lock) on app unlock, so a stolen unlocked phone does not
  hold a year-long owner session.
- **Every owner-site `/api/*` request checks the device**, authenticated or not. Caddy accepts
  any CA-signed cert and serves the static SPA to it, which is harmless; the api refuses a
  revoked device everywhere.
- Settings → Devices lists enrolled phones (label, last seen, ingress) with rename and revoke.

### 3.5 Enrollment by QR code on the box's own screen

There is no laptop or browser path (owner decision): the box's own monitor, already driven by
the wall kiosk (`deploy/wall/`), is the only place a QR is ever shown.

1. The owner taps **Add a phone** (Settings → Devices in the PWA or app). That only *arms*
   enrollment for ~2 minutes and reveals nothing, so it can be pressed from anywhere. Nothing
   at the box arms it (owner decision, 2026-10-18): the box's loopback screen is also reachable
   from the host-network containers (cast, minecraft), so arming stays behind the owner's
   session, and the box's keys only close the window or reject a phone.
2. The wall kiosk shows a full-screen QR holding: the owner CA's SPKI hash, a **single-use
   enrollment code** (256-bit, held only in the api's memory, never on disk or in the
   database, and gone when the window closes or the api restarts), the LAN and public gateway
   addresses, the LAN site's root (`/data/lan-root.crt`) hash so the app can validate
   `jbrain.local` without a warning, and a format version. The wall fetches it from the api's
   internal-only routes with its own `wall-enroll` token, and serves the QR view **only on a
   port bound to the box's loopback** (`127.0.0.1`). Never on the wall's existing `:8800`,
   which is published to the whole LAN over plain HTTP.
3. The app scans it, makes its Keystore key, and posts its CSR to `POST
   /api/owner-devices/enroll` over the LAN site, which it validates against the QR's LAN-root
   hash. The request carries `HMAC(code, "app→box" ‖ CSR)`. The box consumes the code (a
   synchronous check-and-mark in the api's one process, so two requests cannot both spend
   it), signs the CSR, and answers with the cert and `HMAC(code, "box→app" ‖ app's MAC ‖
   device id ‖ issued cert ‖ CA certs)`; the app pins nothing until that verifies. Direction
   labels stop a reflected MAC. A failure after the code is spent closes the window, so the
   screen asks for a new one. The route is rate limited per client address.
4. The monitor and the app both show the new device's fingerprint as a short word string; the
   owner checks they match. A mismatch revokes the device on the spot. The app derives its
   words from its own key, never from the reply. The words stay up for a full window after the
   phone enrolls, and an owner session cannot re-arm over them, so a stolen one cannot wipe
   them before they are compared; a key at the box closes them.

**A new phone works only once the owner confirms it** (owed to A5, before arming has any UI):
the device row starts pending, the device guard and the session mint refuse it, and Settings →
Devices shows its words beside a Confirm button. The box's screen is reachable by everything
in the host network namespace (cast, minecraft, any host process), which can read a QR while a
window is open and win the race to it, so the screen alone is not proof of presence; an owner
confirmation from their own session is. Until A5 the box refuses to close over a phone just
added, so its words cannot be hidden.

The `wall-enroll` token lives alone in the `enroll_secrets` volume, which only the api and the
wall mount: the `internal_secrets` tokens are also readable by the cast sidecar and mosquitto,
and the screen hands out the code.

Physical presence at the box is the proof. The enrollment secret is never derived from stored
data (an early draft keyed it on `hash_key(owner_key)`, which debug `/sql` can read), and the
owner key is never typed into the app.

**The enroll route is also LAN-only**, as defence in depth: the `lan` ingress marker,
`X-Real-IP` not the `cloudflared` container, and inside the owner's home subnet (a Settings
value, LAN-only to change; until it is set there is no address rule beyond the first two).
Never "a private address": the docker networks are RFC 1918 too, and jcode and pysandbox
containers run arbitrary code. `Cf-Connecting-IP`, `X-Forwarded-For` and `Host` are never
consulted. Limits, recorded: in direct mode (no tunnel) the LAN site is reachable from the
internet by SNI, so the subnet rule is the whole network check there. The docker gateway is
not refused yet: if Caddy sees LAN clients as the gateway (docker's userland proxy), refusing
it would refuse the owner's own phone. What the live box's Caddy sees is owed to the first
real enrollment (wave D), which the debug API can read back; until then the gateway passes. If
it is the gateway, the network rule stays as it is (fixing it needs host networking, a shell
step) and enrollment rests on the QR alone, which is still the real proof.

### 3.6 The Android app

A separate **owner flavor** of the existing module (`applicationId` `com.jbrain.owner`), so it
installs beside the current APK: the member dashboard, pairing and location engine on a phone
are not wiped by the signing switch below. Built on `OwnerActivity`, `OwnerWebViewClient`,
`NavigationPolicy`.

- **Trust: the box's CA, positively.** The owner flavor's `network_security_config` gives
  `owner.jbrain.home.arpa` a `domain-config` whose only trust anchor is the owner CA certificate
  and a `pin-set` on its SPKI. The cert chain is then *valid*, not an error the app clicks
  through: service workers register, the HTTP cache works, and no system or MDM CA can stand in
  for the box. The owner CA's public certificate is baked into the owner build from a repository
  file the box's CA is copied into once after Wave A (public data; an agent can do it through
  the debug API). The QR's CA hash is checked against the baked one. S0 confirmed WebView honours
  both the anchor and the `pin-set`. A pin failure surfaces in `onReceivedSslError`, so that
  callback always cancels for the owner host: proceeding there would bypass the pin. (The
  `onReceivedSslError` SPKI-pin fallback is dropped: S0 showed Chromium refuses the service
  worker on that origin, as expected.)
- **Client cert:** `onReceivedClientCertRequest` answers with the Keystore key (EC P-256,
  StrongBox where present); `clearClientCertPreferences()` on revoke.
- **Tunnel service.** A foreground-capable `Service`, not the activity, runs the loopback
  CONNECT proxy and the gateway client. Two consumers:
  - the WebView, through androidx.webkit `ProxyController` with a **reverse-bypass** rule so
    only `owner.jbrain.home.arpa` is proxied (`setProxyOverride` is process-wide; without it the
    member dashboard WebView would be routed too). New dependency `androidx.webkit`, gated on
    `WebViewFeature.isFeatureSupported`, set before the first `loadUrl`;
  - native HTTP (OkHttp) for the **notification relay**, downloads and anything else outside
    the WebView, with the same CA trust and Keystore key. `NotificationRelayService` moves onto
    it; today it reads the WebView cookie and opens the notify SSE against the public host.
- **Loopback is not private.** Any app on the phone can reach `127.0.0.1`. The proxy listens on
  a random port and demands a per-launch credential, answered in `onReceivedHttpAuthRequest`
  (it is called for a 407), and refuses every host but the owner origin.
- **Reconnect** on network change (a stale h2 socket shows as a main-frame error), and a plain
  "can't reach your box" screen when both routes fail.
- **Parity work** the WebView needs that the PWA got from the browser: an `ACTION_SEND` intent
  filter for the share target, a `WebChromeClient` with `onShowFileChooser` for uploads and
  camera, `DownloadManager` replaced by the tunnel service, native notifications from the relay.
- **Links that leave the app** must not carry the private origin. Share links, chat shares,
  jcode shares, map shares, the pairing payload (`u: window.location.origin`) and external-LLM
  URLs are built from `window.location.origin` today; they switch to a **public base URL**
  setting.
- **Signing and updates.** Today every CI run generates a new debug key, so no update is
  checked against a stable signer. The owner flavor gets a **stable release key** held as a
  GitHub Actions secret, backed up offline by the owner, APK Signature Scheme v3 so the key can
  later rotate, `versionCode` from `github.run_number`, and its own release asset. The app
  checks the GitHub release for updates (not through Cloudflare) and installs through
  `PackageInstaller` with the owner confirming; Android refuses an update signed by any other
  key. GitHub is trusted for the app exactly as it already is for the box's code.

## 4. What it protects, and what it doesn't

| Threat | Today | After Wave F |
|---|---|---|
| Cloudflare or a Cloudflare-account attacker reads or alters owner traffic | Yes | No: ciphertext only |
| Edge-injected JS in the owner app / a tampered PWA update | Yes | No: assets arrive over the box-CA channel |
| Stolen `CLOUDFLARE_TUNNEL_TOKEN` or repointed tunnel ingress (to `proxy:8443`, `proxy:443` by SNI, or `api:8000`) | Owner phishing and traffic | Ciphertext; mTLS and the LAN rules hold. Pointing at `api:8000` skips Caddy's `/api/jcode/llm` 404 and body caps, as it would today |
| Leaked owner key used from outside | Full access | Nothing from outside: login is LAN-browser only and enrollment needs the box's screen |
| Stolen owner session cookie | Full access for up to a year | Useless off the enrolled device |
| Debug grant seen in transit | Owner-grade bearer until expiry | After Wave E, the agent's own key never leaves it; nothing replayable crosses Cloudflare |
| Stolen unlocked phone | n/a | Keystore user-auth gate and 30-day sessions; revoke from a second device or at the box |
| Cloudflare blocks or drops the connection | Outage | Still an outage (LAN path keeps working) |
| GitHub compromise (box code, APK signing secret) | Trusted | Still trusted |

Share links, intake links, the member dashboard, OwnTracks and panels keep using the public
host as today; share links carry no owner privilege. jcode previews open in the external
browser on the public host as today; they carry no owner session (the preview proxy reads no
cookie).

## 5. Waves

- **S0 — spike, go/no-go. ✅** The spike is a separate test APK (`android/spike/`,
  `com.jbrain.tunnelspike`, published beside the main APK as `jbrain-tunnel-spike.apk`) that
  carries its own stand-in box: a loopback TLS server that demands a client cert, behind an
  in-app CONNECT proxy, with a test CA as the owner host's only anchor. It runs the checks below
  in two stages (the design, then the fallback) and produces a report to paste back. The
  Cloudflare and on-box checks are not in the APK; they fold into B. On a real phone: WebView honours the owner flavor's
  `network_security_config` trust anchor and `pin-set` for the owner origin (else the §3.6
  fallback, and confirm Chromium refuses the service worker there); reverse-bypass
  `ProxyController` carries HTTPS, WebSockets, SSE and service-worker fetches, including a cold
  start where the first wss/SSE races the main frame; `onReceivedClientCertRequest` with a
  StrongBox key, and its handshake latency; HTTP/2 over CONNECT and reconnect on network
  change; a WebSocket byte pump through the Cloudflare tunnel stays up for real use and real
  upload sizes; the phone validates `jbrain.local` by the LAN-root hash. On the box, through the
  debug API: whether Caddy sees real LAN client addresses (§3.5 says what happens if not).

  **Result, 2026-10-17 (Pixel 10 Pro, Android SDK 37, WebView 155): go.** On the design stage
  every check passed: secure context, service worker registered and controlling the page with
  fetches through it, fetch, SSE and a WebSocket echo (also when racing the main frame on cold
  start), the server saw the Keystore key's SPKI, the wrong-pin host was blocked, and internet
  traffic bypassed the proxy (reverse bypass). The key is **StrongBox**; a signature averages
  37 ms and the client-cert callback proceeds in 67 ms. The proxy's 407 challenge was answered
  once through `onReceivedHttpAuthRequest`. The fallback stage confirmed the service worker
  fails on a cert-error origin, so it is dropped. Not covered by the APK, and moved into B: the
  Cloudflare WebSocket pump, HTTP/2 over CONNECT, reconnect on network change, `jbrain.local`
  validation by the LAN-root hash, and whether Caddy sees real LAN addresses.
- **A — box: keys, owner site, devices, enrollment. ◻️** §3.1 one-shot key generation and the
  two volumes; §3.2 snippet argument on every site, the `:8443` site rendered only when its
  files exist, PROXY protocol, `caddy validate` in CI and the real-Caddy header test;
  `owner_devices` + RLS isolation test; the §3.5 arm route, the wall's loopback QR view, the
  enroll route and the home-subnet setting; device- and user-bound sessions in the repo lookup;
  Settings → Devices; the public-base-URL setting and the link builders that use it. Security
  paths at 100% coverage. Built in five PRs: **A1 keys ✅** (`jbrain/owner_tunnel/keys.py`,
  the `owner_keys` and `owner_site` volumes, the `owner-keys ensure` one-shot in update and
  install; the owner CA carries a name constraint to the owner host), **A2 owner site ✅** (`deploy/proxy-owner-conf.sh`;
  `X-JBrain-Ingress`/`X-JBrain-Device` on every site, read by `client_ip.ingress` and
  `device_key`; `test_owner_site.py` runs the real Caddyfile in a pinned Caddy, in CI too;
  PROXY protocol waits for the gateway in B), **A3 devices and bound sessions ✅**
  (`app.owner_devices`, `device_sessions.device_id`; `OwnerDeviceGuard` refuses any
  owner-site `/api` request or socket not from an enrolled, unrevoked phone, and holds that
  check for the request's life, so a revoke ends the phone's open streams and sockets at once;
  `POST /api/owner-devices/session` mints an owner session bound to that phone (one live per
  phone), which authenticates only with its key and idles out after 30 days, while an unbound
  session never works over the owner site; list, rename and revoke, with revoke ending the
  phone's sessions), **A4 enrollment, box side ✅**
  (`jbrain/owner_tunnel/enrollment.py`: the arm routes, the screen routes
  `/internal/owner-enroll*` behind their own `wall-enroll` token that render the QR as SVG,
  `POST /api/owner-devices/enroll` with both MACs, the LAN and subnet rules and a rate limit,
  and a reject that revokes a mismatched phone), **A4b the wall's QR screen ✅**
  (`deploy/wall/enroll.html` on `:8802`, published to the box's loopback only; the box's
  browser opens `http://localhost:8802/`, which frames the wall and covers it with the QR
  while a window is open; Esc closes, R rejects; arming is Settings only),
  A5 Settings → Devices and the
  public-base-URL links. Still owed from §3.1: the two key volumes in the box backup and
  restore. Owed to A5: the cookie-minting logins (`/api/auth/session` and the redeem routes)
  still answer over the owner site with an unbound cookie, which the tunnel then refuses; the
  app must mint through `/api/owner-devices/session`, or those routes refuse the owner site.
- **B — box: the tunnel gateway. ◻️** §3.3 with nonce auth, drain and keepalive, the kick
  registry, limits, debug-API counters, a throughput check. Tests with a fake device key
  against real Caddy.
- **C — Android: owner flavor, tunnel service, trust, release signing. ◻️** Everything in §3.6.
  JVM tests for the reverse-bypass rule, the proxy's host gate and credential, the QR payload,
  both MACs, and the relay's OkHttp trust. Sideloadable from its GitHub release asset, beside
  the existing APK.
- **D — side by side. ◻️** The owner uses the app daily while the public PWA stays as it is. Ops
  shows tunnel health per device. A second device is enrolled. Checklist to sign off:
  notifications, share target, uploads and camera, downloads, the jcode and jlaunch terminals,
  link sharing, Gmail connect (its OAuth callback lands on the public host, so the app needs a
  hand-off), back gesture, and the update path from a GitHub release. Also, from the first real
  enrollment: the client address the enroll route saw (the debug API reads it), which settles
  whether the docker gateway can be refused (§3.5).
- **E — debug access through the tunnel. ◻️** Remote agents cannot run the app. The agent
  generates its own key and sends a CSR; the owner approves it in the app, which shows the
  fingerprint, and the box issues a short-lived client cert (Caddy enforces `notAfter`, and so
  does the api). Debug certs live in their own table and kind, map to the existing
  `capability_token` scope through `current_debug_principal`, keep today's suspend and revoke,
  and are refused by the enroll, login and retirement-toggle routes. The agent's client opens
  the §3.3 gateway and the inner TLS, trusting the owner CA. First step: confirm the cloud
  environment's egress proxy carries a long-lived WebSocket upgrade to the public host. When it
  works, the bearer form of the debug token is turned off. Lands before F so nothing
  owner-grade is left on the public host at retirement.
- **F — retire public owner access. ◻️** On `hopkinsbrain.com`, owner-key login and unbound owner
  sessions are refused, decided by the ingress marker, never by `Host`; the owner screens say
  "open the JBrain app". Owner login stays on `jbrain.local` from a browser at home. The
  toggle and debug-grant minting accept only the owner principal on a bound device session or a
  `lan`-ingress request, never a debug principal and never `owner-tunnel` ingress alone. Ops →
  Update keeps the last known-good proxy config so a bad Caddy render rolls back. Precondition:
  two enrolled devices. On-box sign-off.

## 6. Owner decisions

- **Break-glass when away from home: none (owner decision, 2026-10-17: *"No remote recovery
  needed"*).** If every enrolled device is lost or the app breaks while away, owner access waits
  until the owner is home: re-enroll at the box's screen, or sign in at `jbrain.local`. No
  recovery code reopens public owner login, so Cloudflare never re-enters the trust path.
- **Laptop after retirement.** Default: browser at `jbrain.local` on the home network only.
- **iPhone.** Out of scope; the design is Android-specific (proxy override, Keystore).

## 7. Non-negotiables touched

RLS + isolation test for `owner_devices` and the debug-cert table (#3); key files via the
storage abstraction (#2); tests in the same PRs with security paths at 100% (#5);
`cryptography`, `androidx.webkit` and the Caddy testcontainer in `scripts/dev-setup.sh` (#8);
every operator step PWA-, app- or box-screen-operable, including key generation and renewal, the
home subnet and the retirement toggle (#10).

## 8. Open questions

- ~~Does WebView honour `pin-set` as well as the trust anchor for the domain?~~ Yes (S0).
- ~~StrongBox handshake latency on the owner's phone.~~ StrongBox present; 67 ms per client-cert
  handshake (S0). TEE stays the fallback for phones without it.
- Is the single uvicorn worker enough for the doubled hop, or does the pump get its own
  container?

## 9. Review record

Four reviews (security, Android feasibility, box fit, rollout and recovery) ran on the first
draft. What changed because of them:

- **Enrollment MAC reflection** and a **secret derived from stored data** (readable by debug
  `/sql`): replaced by the QR's one-time code with direction-labelled MACs (§3.5).
- **Pinning through `onReceivedSslError`** left the origin in a cert-error state, which blocks
  service workers and only checks when a cert is *untrusted*: replaced by a positive
  `network_security_config` trust anchor, with the old path as the fallback (§3.6).
- **`ProxyController` is process-wide**: reverse-bypass for the owner host only (§3.6).
- **The notification relay and downloads bypassed the tunnel**: a shared tunnel service (§3.6).
- **Loopback proxy reachable by other apps**: random port plus a per-launch credential (§3.6).
- **Caddy mechanics** (`header_up` placement, the fingerprint changing on re-issue, key
  generation order, the CA key reachable by Caddy, client IP collapsing to the api):
  §3.1–§3.3.
- **RLS policy and session binding at every `authenticate` call site**: §3.4.
- **Links built from `window.location.origin`**, the release-signing uninstall, and the Wave D
  parity list: §3.6 and Wave D.
- **No remote way back after retirement**, the toggle reachable by a debug principal, and debug
  access still public at retirement: Waves E and F swapped; the owner chose no remote
  break-glass (§6), and a second enrolled device is a precondition of F.
- **Debug private key shipped inside the payload**: the agent makes its own key and CSR (Wave E).
