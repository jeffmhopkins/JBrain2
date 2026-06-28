# jcode preview — host-served, per-session (retiring the per-session quick-tunnel)

A build plan to move the jcode web preview off **per-session TryCloudflare
quick-tunnels** and onto the box's **own** named Cloudflare Tunnel, giving **each
sandbox session its own stable preview hostname** — concurrent previews, no
quick-tunnel rate limits, no random public DNS. A jcode sub-plan on top of
`docs/proposed/JCODE_PLAN.md` (Wave J4 shipped the quick-tunnel this replaces);
governed by `docs/PROCESS.md` (the binding wave process) and the `CLAUDE.md`
non-negotiables. **Verbose debug logging is a per-wave deliverable, not a
follow-up** — every new path ships its own DEBUG instrumentation, gated by the
debug-access verbose mode landed in Wave P0.

## The reframe: a fronted dev server, not a fresh tunnel each time

The Wave J4 design mints a brand-new `cloudflared` **quick-tunnel** per preview
(`docs/proposed/JCODE_PLAN.md`, `preview.py`). That path is the source of a whole
class of failures observed in practice:

- **Rate limits.** TryCloudflare throttles repeated/concurrent quick-tunnels, so
  after the first, new ones print a URL whose edge connection never registers →
  the hostname fails DNS in the browser ("server IP address could not be found").
- **Process churn.** Each preview is a subprocess to spawn, parse, wait-for-ready
  (#629), and reap on every pause/reset/delete path (#630) — and a leaked one
  holds a tunnel slot and worsens the throttling.
- **Public-resolver dependence.** `*.trycloudflare.com` resolves through
  Cloudflare's shared pool, which carrier DNS / VPN / Private Relay routinely
  filter — so a tunnel that's fine on Wi-Fi dies on 5G.

The box **already** has the plumbing to do this properly: one **persistent named
Cloudflare Tunnel** (`docs/CLOUDFLARE_TUNNEL.md`) fronting **Caddy**, and the
**api↔jcode control bridge** the terminal already rides. The preview should ride
those rails — a session's dev server **fronted** at a stable hostname under the
owner's own zone — instead of standing up new public infrastructure per preview.
Per-session reachability comes from a per-session **port + hostname**, served at
**root** (so HMR and absolute asset URLs work untouched).

## Owner decisions (settled)

- **Replace, don't dual-run.** Once the host path is on-box-verified, the
  quick-tunnel adapter is removed (Wave P5) — not kept as a fallback.
- **Per-session, concurrent.** Each session gets its own preview hostname + dev
  port; two sessions can preview at once. (The single-port `preview_default_port`
  model is retired.)
- **Reuse the existing tunnel + Caddy.** No second tunnel, no new public service —
  one wildcard public hostname on the tunnel already running.
- **Verbose logging is innate.** Each wave instruments its own path at DEBUG,
  surfaced by the debug-access verbose mode (Wave P0).

## Open decisions (escalation-worthy, per `PROCESS.md`)

Surface to the owner for sign-off **before Wave P1**; do not guess.

1. **Proxy path & network posture** *(security-touching).* Route the preview
   through the **api↔jcode bridge** (api host-routes → control server → the dev
   port) — keeps the sandbox reachable **only** via the api, as today, and makes
   per-session routing dynamic with **no Caddy reloads** (the api owns the
   host→session→port map). *Recommended.* The alternative — putting **Caddy on the
   `jcode` network** to `reverse_proxy jcode:<port>` directly — is simpler data
   path but joins the public edge proxy to the isolated sandbox network, widening
   surface. Recommend the api-proxy path.
2. **Hostname scheme & TLS.** **Flattened single level** `<slug>-preview.<host>`
   stays under `*.<host>`, which free **Universal SSL already covers** — zero cert
   cost. The prettier `<slug>.preview.<host>` is a **two-level** wildcard that
   needs paid **Advanced Certificate Manager**. *Recommend flattened.*
3. **Auth model for the preview origin.** It's under the box's domain now, but a
   per-session subdomain is a **different origin**, so the PWA's owner cookie does
   **not** carry. Baseline: an **unguessable random slug** (parity with today's
   unguessable URL, `robots`-excluded, "never indexed"); optional gate via the
   existing **jcode share-token** (D2) or Cloudflare Access. *Recommend
   unguessable-slug baseline, share-token optional.*
4. **Port pool & the dev-server contract.** Max concurrent previews = the size of
   the per-session port pool (e.g. 5173–5199). The dev server already learns its
   port via `preview_env`'s **`$PORT`** (now per-session); Vite needs
   `--port $PORT` since it ignores `$PORT` — surface the assigned port + the
   one-liner in the UI (Wave P4).
5. **Wildcard DNS provisioning.** Manual one-time dashboard entry (wildcard public
   hostname + DNS) *recommended* over automating the Cloudflare API for one record.

## Architecture — the pieces, and what they reuse

```
browser → https://<slug>-preview.<host>/…            (per session; HMR WebSocket too)
   → CF edge (owner zone: *.<host> Universal SSL, wildcard DNS → the tunnel)
   → named cloudflared  (already running; docs/CLOUDFLARE_TUNNEL.md)
   → Caddy(proxy:80):  ONE static rule  *-preview.<host>  → api:8000
   → api:  Host → session lookup → reverse-proxy (HTTP + WS upgrade)
        │   internal jcode network — ONLY the api bridges; the sandbox stays isolated
        ▼
   jcode control server:  proxy → 127.0.0.1:<session-port>   (Host rewritten to localhost)
        ▼
   the session's dev server (Vite/Next/Astro/… on its ALLOCATED per-session port)
```

| Need | Reuses | Net-new |
|---|---|---|
| Per-session reachable hostname | the **named** cloudflared tunnel + Caddy fronting (`docs/CLOUDFLARE_TUNNEL.md`); one wildcard public hostname | a Caddy host matcher `*-preview.<host>` → api |
| Reach the sandbox without breaking isolation | the api↔jcode control bridge + the **terminal-WS upgrade proxy** already in `serve_terminal` | a host-routed reverse-proxy on the api + an inner dev-port proxy on the control server |
| Dev server lands on the preview port | `preview_env` already exports **`$PORT`** to the session shell | **per-session port allocation** (a bounded pool) replacing the single default |
| Dev server accepts the request | the **#628** Host→`localhost` rewrite (Vite/webpack `allowedHosts`) | apply it at the proxy that terminates toward the dev server |
| Dies-with-session | the **#630** release-on-stop/reset/delete/reap invariant | release the **port + routing** instead of killing a `cloudflared` |
| Verbose diagnosis | the **debug-access verbose mode** (Wave P0) + `Settings.effective_log_level` | per-wave DEBUG lines on each new path |
| Owner-only / shareable | the existing **jcode share-token** (D2) | optional gate on the preview origin |

**Net-new is small:** a per-session port/hostname allocator on the control server,
one reverse-proxy route on the api (+ an inner dev-port proxy), one Caddy matcher,
one wildcard DNS/ingress entry, and a reworked Preview tab. The `CloudflaredTunnel`
adapter and its lifecycle complexity are **deleted**, net-simplifying the surface.

## Security posture

The sandbox is still the boundary (`docs/proposed/JCODE_PLAN.md` "Security
posture"); this changes **how the preview is exposed**, red-team gated at P2/P3:

- **Isolation preserved (the recommended path).** The api-proxy route keeps the
  sandbox reachable **only** through the api, exactly as the terminal is today —
  no new peer joins the `jcode` network. The dataless-sandbox compose assertion
  sweep must still pass (no socket/DB/blob/notes); P3 must not regress it.
- **The preview origin is authenticated by an unguessable slug** (open decision
  3), `robots`-excluded and never indexed — the "never indexed, dies with the
  session" property of J4 carries over. Owner cookie does **not** cross the
  subdomain origin, so we don't rely on it; the share-token (D2) is the opt-in
  human gate.
- **Loopback dev server.** The dev server binds inside the container; only the
  api→control proxy reaches it. No new published port; the tunnel dials **out**.
- **Dies with the session.** Stop / reset / delete / reap **release the port and
  routing** (inheriting the #630 invariant), so a paused session is unreachable
  and a slug can't outlive its session.

## Verbose logging — the through-line

Per the owner ask, logging is **built into each wave**, not bolted on, and is
gated so the default INFO level stays quiet:

- **Substrate (Wave P0, landed).** `JCODE_DEBUG_ACCESS_ENABLED` →
  `Settings.effective_log_level` forces **DEBUG** whenever debug access is on
  (`docs/DEBUG_ACCESS.md`); a per-request HTTP trace + the preview/session/terminal
  lifecycle at DEBUG. Every wave below hooks into this.
- **P1** — port **allocate / release / reuse** and dev-port **health-probe**
  results at DEBUG.
- **P2** — the proxy **request trace** (`Host → sid → port`), upstream connect,
  **WS upgrade** for HMR, and dev-down `502`s — the diagnostic the old cloudflared
  output used to (not) give.
- **P4** — the api preview-status route at DEBUG.
- **P5** — a coverage review that the new path's DEBUG **replaces** what the
  removed cloudflared logging covered (no observability regression).

## Wave split

Per `PROCESS.md`: each wave runs its tasks in parallel worktrees off a `wave-N`
branch, gets an independent **per-task** review and a **per-wave** review
(security/red-team for the sandbox/exposure-touching waves), and lands as **exactly
one PR**, CI green before merge. The GUI wave goes through the **three-interactive-
mock gate** before implementation.

- **Wave P0 — verbose-logging substrate** *(landed; pending merge).* The
  debug-access verbose mode + jcode logging enrichment (`effective_log_level`, the
  compose flag, per-request + preview/session/terminal DEBUG). Built on
  `claude/preview-tab-iframe-1hkqz9`. Every later wave extends it. *This is the
  "verbose logging innate to the job" foundation.*

- **Wave P1 — per-session port + hostname allocation** *(control server; no GUI;
  **additive — landed**).* A new `HostPreviewManager` that reserves a unique dev
  **port** per session from a bounded pool plus a stable, unguessable **slug** →
  `https://<slug>-preview.<host>`, with `resolve(slug)→sid` for the Wave P2 proxy;
  released on delete/reap (a pause keeps the reservation — the proxy makes a paused
  session unreachable, Wave P2). Pure in-memory, **no subprocess**. Introduced
  **alongside** the tunnel path behind a `preview_mode` setting (default `tunnel`),
  so `main` keeps a working preview until the P5 cutover — `CloudflaredTunnel` is
  **not** removed here. Logs: allocate at INFO (lifecycle parity with the tunnel
  manager), reuse + release at DEBUG. Tests: allocation, idempotence, distinct
  ports, `resolve`, release-and-reuse, partial-release routing, host sanitization,
  pool exhaustion + inverted-pool rejection, fail-closed empty host.

- **Wave P2 — control-server serving path** *(control server; security-touching,
  red-team gated; landed).* Wire the allocator onto the serving path inside jcode:
  the session's shell binds its reserved port via the existing `preview_env`'s
  `$PORT` (now per-session, allocated at first terminal open), and a control-server
  reverse-proxy `/preview/{slug}/{path}` → `127.0.0.1:<session-port>` with the **Host
  rewritten to `localhost`** (the #628 lesson), an **unknown slug 404** and a
  **paused session 404 / dev-down 502**. The host-mode preview lifecycle: status/open
  report the stable URL, a pause **keeps** the reservation (released on delete/reap).
  HTTP via `httpx` (**one new runtime dep**, promoted from dev so the `--no-dev`
  image ships it); body **buffered, not streamed** (a dev page's assets are modest);
  the **HMR WebSocket moves to P3** with the api bridge it has to traverse. DEBUG: the
  `proxy → :port /path` + dev-down trace. Tests: proxy forward + Host rewrite +
  502-on-refused (mock transport), and route-level unknown-slug 404 / paused 404 /
  no-dev-server 502 / hostname-survives-pause-not-delete. *On-box verification of the
  real proxy (a live dev server) is the P5 bring-up.*

- **Wave P3a — api HTTP bridge + edge docs** *(api + docs; security-touching,
  red-team gated; open decision 1; landed).* The backend api proxy
  `/__jcode_preview/{slug}/{path}` → the control server's `/preview/{slug}` (the public
  exposure point; auth = the unguessable slug, no owner cookie). It **adds** the
  api↔jcode bearer for that hop and **strips the owner's Cookie + Authorization** so a
  sandbox-run dev app never sees them; a malformed slug or unconfigured jcode 404s; an
  unreachable control server 502s. The Caddy host-regexp (`<slug>-preview.<host>` →
  `/__jcode_preview/{slug}`, served on the preview subdomain ONLY — the main site 404s
  the prefix so a dev app never runs on the owner origin), the wildcard DNS, and the
  flattened-name/cert note are **documented** in `docs/CLOUDFLARE_TUNNEL.md` rather than
  auto-injected (edge config that can't be verified off-box — shipping it unverified
  could break the live proxy). Tests: forward + bearer-add + credential-strip + query
  preserve, malformed-slug 404, unconfigured 404, control-server-unreachable 502.

- **Wave P3b — HMR live-reload WebSocket** *(jcode + api; landed; live pump
  on-box-verified).* WS proxying both hops, mirroring the terminal proxy: the control
  server's `proxy_ws` bridges `browser ws → ws://127.0.0.1:<port>`, and the api's
  `_proxy_ws` bridges `browser ws → control server` carrying the api↔jcode bearer. Both
  connect upstream FIRST and echo the negotiated **subprotocol** (Vite speaks
  `vite-hmr`); the api WS shares the HTTP route's **slug + origin gate** (Host must be
  `<slug>-preview.<base>`), and the control-server WS is **bearer-authed** on the
  handshake like the terminal. Guards (bad bearer / wrong origin / unknown slug → 4401/
  4404 before any upstream connect) are unit-tested; the live byte pump is
  `# pragma: no cover` (deploy-verified, as the terminal pump is).

- **Wave P4 — the Preview tab UX** *(GUI; **mock gate waived by the owner** — this
  reuses the existing Preview tab + ⋯ menu (#627), not a new surface, so it's
  reuse-and-plumb, not a redesign; landed).* The control server's `preview_status` /
  `preview_open` now report **`mode`** (`host`/`tunnel`) and, in host mode, the reserved
  **`port`** (the api passes both through verbatim). The Preview tab keys off `mode`:
  host mode shows the stable per-session URL as the **iframe** (the proxy's
  "start your dev server" 502 fills it until the server is up), a **port hint**
  (`run your dev server on :<port>`) in the empty state, and drops the tunnel-only
  **Stop preview** menu item (host mode has no tunnel to close). Tunnel mode is
  untouched. Component tests in mock mode for both.

- **Wave P5a — edge wiring** *(Caddy + compose + DNS docs; landed).* The proxy
  entrypoint renders a `http://*.<host>` Caddy site from `JBRAIN_JCODE_PREVIEW_BASE_HOST`
  (`deploy/proxy-preview-conf.sh`, mirroring the LAN-site renderer — env-gated, inert
  when unset) that routes only `<slug>-preview.<host>` to the api's `/__jcode_preview/<slug>`
  and 404s every other subdomain; the app sites 404 the prefix. The compose passes the
  shared base-host env to the proxy. `docs/CLOUDFLARE_TUNNEL.md` corrected: the wildcard
  is the **full-label `*.<host>`** (Cloudflare has no partial-label wildcard) + the
  apex/SSL notes. Tests on the deploy config + the render script (set/unset). Caddy
  runtime is verified on the box (no caddy binary in CI). Enablement is a single
  **`jbrain enable-jcode-preview [apex]`** (`deploy/jcode-preview-setup.sh`) that writes
  `JCODE_PREVIEW_BASE_HOST` — defaulting to `JBRAIN_DOMAIN`, fail-closed when jcode/tunnel
  are off or the host is malformed — and recreates the stack. A non-empty base host *is*
  the switch (the Cloudflare wildcard is still the one manual step). No more hand-editing
  `/opt/jbrain2/.env`.

- **Wave P5b — cutover & teardown** *(landed, on-box-verified first).* Host preview is
  now the **only** mode: the `preview_mode` config, the `CloudflaredTunnel`/`PreviewManager`
  adapters (and `FakeTunnel`), the `cloudflared` binary in the jcode `Dockerfile`, and the
  `JCODE_PREVIEW_MODE` compose env are all removed. `host_preview` is the sole, always-
  constructed allocator (fail-closed `.enabled` with no base host); the control surface,
  reaper, and lifespan dropped every tunnel branch. The host preview was verified
  end-to-end on the box (DNS wildcard + tunnel ingress + HTTP) before the cutover. The
  dead-config tail followed: the frontend's unreachable tunnel-mode UI, the setup
  script's now-ignored `JCODE_PREVIEW_MODE=host` write, and the `jcode-preview-backfill.sh`
  self-heal (whose only trigger was `MODE=host`) are gone — the operator's persisted
  `JCODE_PREVIEW_BASE_HOST` is the single enable signal, so nothing needs backfilling.

**Scope = P0–P5:** per-session port/hostname allocation, the api/control reverse-
proxy, the edge wiring, the reworked Preview tab, and the cloudflared cutover.

## What this plan deliberately does **not** do

- **No second tunnel / new public service** — one wildcard hostname on the tunnel
  already running.
- **No multi-level wildcard cert** unless the owner opts into ACM (the flattened
  name avoids it).
- **No per-session container or network namespace** — still one jcode container;
  the cross-session filesystem read remains the documented residual
  (`docs/proposed/JCODE_PLAN.md`), unchanged by this work.
- **No change to the headless agent or the model bridge** — preview transport only.
- **No public indexing** — unguessable slug + `robots` exclusion keep the J4
  "never indexed" property.
- **No cloudflared fallback after P5** — the quick-tunnel path is removed, not
  kept in parallel.

## On-box bring-up / provisioning (the last mile)

Owner-gated, after P1–P4 land and before P5 removes the old path:

1. **One-time:** in Cloudflare Zero Trust → Tunnels, add a wildcard **public
   hostname** `*.<host>` → `http://proxy:80` and the matching wildcard **DNS**
   (full-label wildcard; documented by P3 in `docs/CLOUDFLARE_TUNNEL.md`).
2. Run **`sudo jbrain enable-jcode-preview`** — it writes `JCODE_PREVIEW_BASE_HOST`
   (default `JBRAIN_DOMAIN`; pass an apex for a subdomain deploy) and recreates the
   stack. No hand-editing `.env`; the key persists across updates.
3. **Smoke:** start a dev server in a session on `$PORT`, confirm its
   `<slug>-preview.<host>` serves it (HTTP **and** HMR), then open a **second**
   session concurrently and confirm both previews are live and independent.
4. Once verified, land **P5** to remove `cloudflared`.
