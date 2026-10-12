# Security review fixes — third-party access hardening

> **Status:** Scheduled · **Last verified:** 2026-10-12 · **Waves:** S1◻️ S2◻️ S3◻️ S4◻️

A whole-codebase security review (2026-10-12; six independent reviewers, the serious
findings re-verified by hand) looked for programming errors and structural mistakes that
let a **third party** in: a family member's device key, a jcode share-link recipient, an
intake-link stranger, a panel key, code running in a sandbox container, a web page the
agent fetches, or an internet attacker. Owner-side threats and the health/finance/location
domain firewall were out of scope.

The database layer held: the app role cannot bypass RLS, every `app.*` table has RLS, the
GUCs are transaction-local and bound, and no SQL injection was found. Every hole is in the
layers around RLS — routes that admit any cookie, side effects that never touch a table,
a proxy that trusts a path, a sandbox that can read its keeper's token, and network
"isolation" that isn't.

## The constraint every wave is held to: nothing intended breaks

A fix that closes a hole by removing something the owner uses is not a fix. Every task
names the legitimate flows it touches and lands tests that prove they still work, beside
the tests that prove the attack fails. The per-task reviewer is briefed to hunt
regressions, not only to check the security claim. The flows, and how each authenticates:

| Flow | Caller | Credential | Origin / transport |
|---|---|---|---|
| PWA (public host, `jbrain.local`, tunnel) | browser | `jbrain_session` owner cookie, Lax, host-only | same-origin relative fetches; WS from `window.location.host` |
| Android owner app | WebView on `<base>/` | owner cookie | same-origin |
| Android member dashboard | WebView on `<base>/dash`; OkHttp | device-key cookie (Strict) via `/api/session/mint`; SSE GET with cookie | same-origin; OkHttp sends no Origin |
| Pairing | Android OkHttp | pairing code, no cookie | — |
| OwnTracks | phone | HTTP Basic device key | — |
| MQTT | phones | device key via mosquitto-go-auth → `/internal/mqtt-*` | internal network |
| Panels (S3/C6) | firmware | `Authorization: Bearer <device_key>`, `/api/endpoint/ws` tunnel | no cookie |
| jcode share recipient | browser | `jcode_share_link` cookie; `GET /api/jcode/sessions/{sid}`, terminal WS, preview | same-origin |
| Intake link stranger | browser | `intake_link` cookie; `/api/intake/{redeem,chat,confirm}` | same-origin |
| research / jlaunch / mc share pages | browser | token in path, no cookie, GET only | — |
| jcode preview | browser iframe in the PWA + direct tab | slug in host (`<slug>-preview.<host>`), no cookie forwarded | separate origin, same site |
| jcode agent + helpers | in-container | `GROK_API_KEY` bearer → `http://api:8000/api/jcode/llm/v1` | jcode network |
| Wall display | LAN TV browser → `deploy/wall/serve.py` → `/internal/pet*` | none today | LAN → wall:8800 → internal |
| Gateway-config regen | worker → `/internal/llm/regen-gateway-config` | HMAC(supervisor token) bearer | internal |
| Debug console / external assistant | browser on LAN site; curl | capability bearer | same-origin / no Origin |
| Ops → Update | PWA | owner cookie → supervisor | — |

## Findings → task map

| # | Finding | Sev | Task |
|---|---|---|---|
| F1 | Preview proxy builds the upstream URL from the decoded `{path}`; httpx resolves `..`, so a slug holder reaches the jcode control server's `/sessions*` API carrying the api's bearer | High | S1.1 |
| F2 | Same-site CSRF / CSWSH from the preview subdomain: `origin_allowed` is open when no allow-list is set (always, in production), no Origin/CSRF check on unsafe methods, no frame-ancestors, unsandboxed preview iframe → owner shell, live family locations, `/api/ops/reset` | High | S1.3 |
| F3 | ~70 routes gated only by `PrincipalDep` admit device-key, jcode-share and intake-link cookies; RLS saves the data, not the side effects: LLM load/unload, Gmail creds read/used under a hardcoded owner context, Tavily key spent, TTS, host telemetry, 100 MB blob written before the RLS insert fails | Med | S1.2 |
| F4 | `login()` lookup is not kind-filtered (a device key mints a Lax cookie on `/api/auth/session`) | Low | S1.2 |
| F5 | jcode session shells inherit the ctl's env (`JCODE_TOKEN`, every session's control bearer) and run as root (so `/proc/1/environ` too); share recipients get both | High | S2.1 |
| F6 | `/api/jcode/llm/v1/*` is "internal-only" but publicly routed under `/api`; the leaked gateway token works from the internet after a share is revoked; `!=` compare | Med | S2.1 |
| F7 | `/internal/*` is unauthenticated and reachable from every network the api joins (jcode, jlaunch, sandbox, render, browser, radio, panel, internal incl. reader/byparr); `/internal/pet/say` runs as owner, `/internal/mqtt-auth` is an unthrottled key oracle | Med-High | S2.2 |
| F8 | reader/byparr headless browsers are on the non-fenced `internal` network and follow redirects/JS after only the first URL is checked → SSRF into comfyui, local-llm, wall, api `/internal`, metadata | Med-High | S2.3 |
| F9 | Direct-fetch guard: check-then-connect DNS rebinding; `_is_public` passes 100.64/10 | Med | S2.3 |
| F10 | Live location WS checks the cookie once; a revoked/removed member keeps the family feed | Med | S3.1 |
| F11 | `PanelDep` / panel WS accept any device key (a phone acts as a panel: voice to a child's panel, converse compute, telemetry spoof); a panel key mints a member session | Low-Med | S3.2 |
| F12 | Debug SQL redaction is by column name (alias bypass): settings secrets, plaintext pairing codes (→ device key that outlives the debug token) | Med | S3.3 |
| F13 | Every rate limiter keys on `request.client.host`, which is always Caddy → one global bucket (anyone locks out pairing); `nudge._client_ip` trusts the first XFF hop | Low | S3.4 |
| F14 | `/member/positions?subject_id=<any>` writes a forged "viewed you" audit row for any subject | Low | S3.4 |
| F15 | jcode_llm and other secret compares use `!=` | Low | S2.1 |
| F16 | Latent RLS: TimescaleDB chunk tables lack RLS but `jbrain_app` holds DML on them; `viewer_may_see`/`visible_subjects` SECURITY DEFINER take a caller-supplied viewer; `place_geofence_read` admits every device to unshared fences; `view_audit_write` doesn't check visibility | Latent | S4 |
| F17 | Stolen desk-panel key = owner agent reads; keys in plaintext NVS, no flash encryption | Design | Out of scope (below) |
| F18 | Wall binds `0.0.0.0:8800` unauthenticated | Low | Accepted (below) |

## Waves

Each wave is one PR off the session branch, built task-by-task with an independent
reviewer per task and a wave-level red-team review (PROCESS.md). Every task: failing test
first, then the fix, then `ruff`/`pyright`/`pytest` (or `biome`/`tsc`) for its package.

### S1 — close the critical web holes ◻️

**S1.1 Preview proxy path containment (F1).** In `api/jcode_preview.py` (HTTP and WS),
refuse any `path` whose segments contain `.` or `..` (after percent-decoding, including
`%2e`, `%2f`, `%5c` and backslash), and build the upstream URL so it can only ever be under
`/preview/{slug}/` (assert the normalized httpx URL's path keeps that prefix; 404 otherwise).
Defence in depth on the control side: the ctl's preview routes get their own credential —
`preview_token = HMAC(JCODE_TOKEN, "jcode-preview")`, derived on both sides so no new `.env`
key — on a router separate from `authed`, and `authed` (sessions, terminal) stops accepting
it. The ctl's `proxy_http` also refuses dot segments.
*Keeps working:* every preview path a dev server serves (assets, HMR WS, query strings,
encoded spaces), the PWA preview iframe, direct preview tab. *Tests:* traversal variants
404 and never reach the transport; normal nested paths/queries proxy; ctl rejects the main
token on preview routes and the preview token on `/sessions`.

**S1.2 Owner gate for owner surfaces (F3, F4).** Add `dependencies=[Depends(owner_only)]`
to the `llm_settings`, `gmail_settings`, `tavily_settings`, `brave_settings`,
`moltbook_settings`, `settings`, `brain`, `notes`, `analysis`, `search` and `wiki` routers
(and any other router the matrix test below finds). Non-owner surfaces stay as they are,
explicitly allow-listed: `/auth/me`, `/auth/session` DELETE (logout), `/tiles/*` (member
map), `/intake/{chat,confirm}` (own intake check), `/member/*` (MemberDep), jcode
operational routes (`JcodeAccessDep`), PanelDep/DeviceDep/DebugDep routes, the token
routes. Add a **route-matrix test** that builds the real app, walks every route, and for
each non-owner cookie kind (device_key, jcode_share_link, intake_link) asserts 401/403
unless the route is on the explicit allow-list — so a future `PrincipalDep` route fails CI.
`upload_attachment` resolves the note under RLS **before** `blobs.put`. `login()` filters
`kind = 'owner'`. Fix the false "owner gate" docstrings in gmail/tavily.
*Keeps working:* PWA everything (owner), member dashboard (`/member`, `/tiles`, live WS),
jcode share page (`GET /jcode/sessions/{sid}`, terminal, preview), intake page.
*Tests:* matrix; owner still 200 on each touched router; member/share/intake flows end to end.

**S1.3 Same-site request forgery (F2).** (a) An HTTP middleware rejects POST/PUT/PATCH/DELETE
that carry the session cookie when `Sec-Fetch-Site` is `same-site` or `cross-site`, or when
`Sec-Fetch-Site` is absent and an `Origin` is present whose host[:port] ≠ the request
`Host` (scheme ignored — behind the tunnel the api sees http; default ports normalized;
`Origin: null` rejected when a cookie is present). Requests without the cookie, or without
both headers (OkHttp, curl, TestClient), pass. (b) `origin_allowed` fails closed: with no
allow-list it requires Origin host == Host; absent Origin still passes (native clients);
a configured allow-list still wins; tolerate a missing Host. (c) Caddy `(app)` / public /
LAN sites send `Content-Security-Policy: frame-ancestors 'self'` and
`X-Frame-Options: SAMEORIGIN` — never on the preview wildcard site. (d) The preview iframe
gets `sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups
allow-popups-to-escape-sandbox allow-downloads"` (no top-navigation); `allow-same-origin`
is safe only because the preview is always a different origin — the comment says so.
*Keeps working:* every same-origin PWA/WebView call, OkHttp mint/pairing/SSE, panels,
OwnTracks, debug bearer calls, previews, YouTube embed. Update `test_live.py:249` and
`test_debug_tokens_api.py:84` (their assumptions are the bug). *Tests:* same-site and
cross-site rejected with cookie; mismatched Origin rejected; matching Origin, absent
headers, and cookieless cross-origin pass; WS fail-closed cases.

### S2 — sandboxes and the internal network ◻️

**S2.1 jcode secrets (F5, F6, F15).** The ctl strips its own settings (`JCODE_TOKEN`,
`JCODE_*` ctl config) from the env it hands `spawn_shell`; `GROK_API_KEY` stays (the agent
and helpers need it). Session shells run as an unprivileged `jcode` uid (owner decision
D1): the ctl chowns `/work/<sid>` and `/work/.home/<sid>`, runs git as that uid or with
`safe.directory`, writes `AGENTS.md` itself, and drops to the uid in the forked child. Caddy
404s `/api/jcode/llm/*` on the app sites (in-container callers use `http://api:8000`,
unchanged). `hmac.compare_digest` everywhere a secret is compared with `!=` (grep).
*Keeps working:* sessions create/clone/reset, the agent's model + web tools, `jcode-grok
upgrade`, npm -g into home, previews on 5173-5199, the share terminal. *Lost by design under
D1:* `apt-get` and writes outside the session's home/checkout. *Tests:* child env lacks
`JCODE_TOKEN`; uid drop; Caddyfile has the block; existing jcode suites.

**S2.2 Authenticate `/internal/*` (F7).** A new `INTERNAL_API_TOKEN`, minted automatically by
`update-inner.sh` / `install.sh` when missing (the existing mint-on-update pattern — no
terminal step), required by a middleware on every `/internal/*` route (constant-time).
Callers: the wall sends it as a bearer (passed via compose env); mosquitto-go-auth sends it
in its configurable `auth_opt_http_user_agent` (rendered into the conf at container start
by a small entrypoint, since the conf is a static mount); the worker's regen call keeps its
own HMAC bearer and additionally sends the token. `/internal/mqtt-auth` gets a rate limit.
Fix the no-op `auth_opt_http_response_timeout_ms` → `auth_opt_http_timeout`. Pin the
mosquitto-go-auth image digest.
*Keeps working:* wall pet controls from the LAN TV, MQTT phone auth/ACL, gateway regen, and
a box updated before the token exists (fails closed for the callers until the update mints
it — the update is the same action that ships the code). *Tests:* each route 401s without
the token, 200s with it; wall/mosquitto render tests.

**S2.3 SSRF fence (F8, F9).** Put `reader` and `byparr` on a new `internal: true` network
whose only exit is the squid `egress` (as the `browser` service is): byparr via
`PROXY_SERVER=http://egress:3128`; reader's Chromium via `HTTP(S)_PROXY` env (Chromium on a
headless Linux honours them), with the network fence as the guarantee if it doesn't. The api
joins that network. `_is_public` uses `is_global` (reuse `browse_policy._is_public_ip`);
direct fetches pin the validated IP through an httpcore network backend so the address
checked is the address connected (redirect hops and keep-alive included), with a test seam
for DNS. Apply to favicon, captions and moltbook clients.
*Keeps working:* `web_fetch`'s direct → reader → byparr → Tavily ladder, SNI/Host on the
original name, search. *Risk:* reader egress through squid is unverified on-box — S2's PR
calls it out and the post-deploy check is `POST /api/debug/fetch` with `tier=reader` and
`tier=solver`; if reader cannot use the proxy the ladder still falls through to byparr and
Tavily (degraded, not broken). *Tests:* compose topology test (reader/byparr only on the
fenced network + egress), `is_global` cases incl. CGNAT, rebinding test via the DNS seam.

### S3 — device and token hygiene ◻️

**S3.1 Live feed re-validation (F10).** The live WS re-authenticates its handshake token and
clears its `viewer_may_see` cache on a ~30 s tick (the wait gets a timeout so an idle socket
is re-checked too), closing 4401 on a revoked session/principal and dropping subjects that
are no longer visible. *Keeps working:* owner and member live maps. *Tests:* revoke mid-stream
closes; removal from group stops that subject's fixes.

**S3.2 Panel vs phone (F11).** `current_panel_principal` and the panel WS accept a device key
only if its subject's `device_role IS NOT NULL`; `/api/session/mint` accepts only
`device_role IS NULL`. (Panels are always provisioned with a role by `/endpoint/flash`;
0211 back-filled legacy panels.) *Keeps working:* every flashed panel, phones' dashboard,
OwnTracks, MQTT (unchanged). *Tests:* phone key 401 on panel routes/WS; panel key refused at
mint; panel and phone happy paths.

**S3.3 Debug SQL secrets (F12).** Pairing codes stored as `sha256(code)` (migration deletes
unredeemed plaintext rows — 15-min TTL; Python hashes before insert/redeem; never use
`hash_key`, codes are case-sensitive). The debug SQL route first runs `EXPLAIN (VERBOSE,
FORMAT JSON)` and refuses plans touching a deny-list of relations (`pairing_code`,
`principals`, `device_sessions`, `fcm_token`, the share-link tables, `intake_links`) or
calling `set_config`/`current_setting`/`pg_read_*`/`lo_*`/`dblink`; `app.settings` rows are
redacted by an explicit secret-key set, not by column alias. *Keeps working:* every other
debug SQL read, pairing QR flow. *Tests:* alias bypass now redacted; deny-listed tables refused;
pairing redeem with hashed code.

**S3.4 Client IP and audit (F13, F14).** One `client_ip(request)` helper: when the peer is the
proxy container, use `Cf-Connecting-IP` (tunnel) else the last `X-Forwarded-For` hop; any
other peer uses the socket address. All limiters and `nudge` use it; Caddy strips an inbound
`Cf-Connecting-IP` in direct mode. `/member/positions` audits only when rows came back and
the subject isn't the viewer. *Tests:* helper matrix; audit not written for invisible subject.

### S4 — latent RLS hardening ◻️

Not reachable by a third party today (each needs arbitrary SQL, and the only arbitrary-SQL
surface runs as owner), so each item lands only if it can be done without behaviour risk,
proven by the integration suite; otherwise it is recorded as a residual in `ROADMAP.md`.
(a) `view_audit_write` WITH CHECK adds `app.viewer_may_see(viewer, target)`.
(b) `place_geofence_read` requires an actual share row for non-owners.
(c) `viewer_may_see` / `visible_subjects`: non-owner callers may only pass their own
`app.subject_id` (raise otherwise), keeping the signature RLS policies use.
(d) TimescaleDB chunks: investigate whether revoking `jbrain_app`'s direct privileges on
`_timescaledb_internal` chunks keeps hypertable reads/writes working (Timescale propagates
grants to new chunks); land only with an integration test that inserts and reads fixes and
host metrics as `jbrain_app`.
Each new/changed policy gets an RLS isolation test.

## Owner decisions

- **D1 — jcode sessions lose root.** Recommended: yes. Root in the session container is what
  turns "a share recipient has a shell" into "a share recipient owns every jcode session"
  (`/proc/1/environ`, the ctl's code). Cost: no `apt-get` in sessions. Alternative: keep
  root, strip the env only — closes `echo $JCODE_TOKEN` but not `/proc/1/environ`.
- **D2 — preview on a separate registrable domain.** Not required once S1.3 lands; would be
  belt-and-braces but needs a DNS/Cloudflare change the owner makes. Not planned.

## Out of scope, recorded

- **F17 panel flash encryption.** ESP32 flash encryption/secure boot burn eFuses
  irreversibly and interact with the "cable once" OTA rollback design; it needs hardware in
  hand. Carried to `ROADMAP.md` as a firmware residual.
- **F18 wall on `0.0.0.0:8800`.** The wall is a LAN display by design; S2.2 stops it being a
  credential-free path into `/internal`, and its own LAN surface is accepted.

## Provenance

Six cold reviewers (non-owner routes, unauthenticated routes, credentials/share links,
devices/MQTT, infra/SSRF, RLS/contexts) and three researchers (jcode/internal, CSRF callers,
SSRF/pairing/panels). F1, F2 (Origin gate default, iframe), F3 (route inventory) and F5 were
re-verified by hand.
