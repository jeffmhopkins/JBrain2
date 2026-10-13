# Security review fixes — third-party access hardening

> **Status:** Shipped 2026-10 · migrations 0241–0243 · **Waves:** S1✅ S2✅ S3✅ S4✅ S5✅ · **Superseded-by:** —

A whole-codebase security review (2026-10-12; six independent reviewers, the serious
findings re-verified by hand) looked for programming errors and structural mistakes that
let a **third party** in: a family member's device key, a jcode share-link recipient, an
intake-link stranger, an external-LLM key holder, a panel key, a Minecraft player, code in a
sandbox container, a web page the agent fetches, or an internet attacker. Owner-side
threats and the health/finance/location domain firewall were out of scope.

The database layer held: the app role cannot bypass RLS, every `app.*` table has RLS, the
GUCs are transaction-local and bound, and no SQL injection was found. Every hole is in the
layers around RLS — routes that admit any cookie, side effects that never touch a table,
a proxy that trusts a path, a sandbox that can read its keeper's token, and network
"isolation" that isn't.

This plan was itself reviewed by three cold reviewers (adequacy/bypass, regressions,
completeness) before any build; their findings are folded in below (marked *rev*).

## The constraint every wave is held to: nothing intended breaks

A fix that closes a hole by removing something the owner uses is not a fix. Every task
names the legitimate flows it touches and lands tests proving they still work, beside the
tests proving the attack fails. The per-task reviewer is briefed to hunt regressions, not
only to check the security claim. Anything that would need a host shell step is a
regression too (`CLAUDE.md` #10). The flows, and how each authenticates:

| Flow | Caller | Credential | Origin / transport |
|---|---|---|---|
| PWA (public host, `jbrain.local`, tunnel) | browser | `jbrain_session` owner cookie, Lax, host-only | same-origin relative fetches; WS from `window.location.host` |
| Android owner app | WebView on `<base>/` | owner cookie | same-origin |
| Android member dashboard | WebView on `<base>/dash`; OkHttp | device-key cookie (Strict) via `/api/session/mint`; SSE GET with cookie | same-origin; OkHttp sends no Origin |
| Member dashboard calls | WebView | `/auth/me`, `/member/*`, `/tiles`, live WS | same-origin |
| Pairing | Android OkHttp | pairing code, no cookie | — |
| OwnTracks | phone | HTTP Basic device key | — |
| MQTT | phones | device key via mosquitto-go-auth → `/internal/mqtt-*` | internal network |
| Panels (S3/C6) | firmware | `Authorization: Bearer <device_key>`; `/api/endpoint/ws` tunnel | no cookie |
| jcode share recipient | browser | `jcode_share_link` cookie: redeem, `GET /jcode/sessions/{sid}`, preview GET/POST/DELETE, terminal WS | same-origin |
| Intake link stranger | browser | `intake_link` cookie: `/intake/{redeem,chat,confirm}` | same-origin |
| research / jlaunch / mc share pages | browser | token in path, no cookie, GET only | — |
| jcode preview | PWA iframe + direct tab | slug in host (`<slug>-preview.<host>`), cookies stripped | separate origin, same site |
| jcode agent + helpers | in-container | `GROK_API_KEY` → `http://api:8000/api/jcode/llm/v1` | jcode network |
| jcode session work | in-container | git/npm/pip over https | today direct NAT |
| Wall display | LAN TV → `deploy/wall/serve.py` → `/internal/pet*` | none today | LAN → wall:8800 → internal |
| Gateway-config regen | worker → `/internal/llm/regen-gateway-config` | HMAC(supervisor token) | internal |
| Debug console / external assistant | browser on LAN site; curl | capability bearer | same-origin / no Origin |
| `web_fetch` ladder | api → direct / reader / byparr / Tavily | — | outbound |
| Minecraft "Dave" | players via BDS pack → `deploy/minecraft/bds.py` → api | xuid from BDS log lines | host network |
| Ops → Update | PWA → supervisor → `update-inner.sh` | owner cookie | — |

## Findings → task map

| # | Finding | Sev | Task |
|---|---|---|---|
| F1 | Preview proxy builds the upstream URL from the decoded `{path}`; httpx resolves `..`, so a slug holder reaches the jcode control server's `/sessions*` API carrying the api's bearer | High | S1.1 |
| F2 | Same-site CSRF / CSWSH from the preview subdomain: `origin_allowed` is open with no allow-list (always, in production), no Origin/CSRF check, no frame-ancestors, unsandboxed preview iframe → owner shell, live family locations, `/api/ops/reset`; *rev:* side-effecting GETs, and cookie tossing (the preview can set a parent-domain `jbrain_session` that shadows the owner's) | High | S1.3 |
| F3 | 61 routes gated only by `PrincipalDep` admit device-key, jcode-share, intake-link (and, via F4, capability/external-LLM) cookies; RLS saves the data, not the side effects: LLM load/unload, Gmail creds read/used under a hardcoded owner context, Tavily key spent, TTS, telemetry, a 100 MB blob written before the RLS insert fails; `/tiles` open to share/intake cookies | Med | S1.2 |
| F4 | `login()` lookup is not kind-filtered; cookie lookup is not kind-filtered either, so any already-minted non-owner cookie stays live | Low-Med | S1.2 |
| F5 | jcode shells inherit the ctl's env (`JCODE_TOKEN`) and run as root (so `/proc/1/environ`, the ctl's code); *rev:* the ctl runs git as root in session-controlled checkouts (hook/fsmonitor → root) | High | S2.1 |
| F6 | `/api/jcode/llm/v1/*` is "internal-only" but publicly routed; the leaked gateway token works from the internet after a share is revoked | Med | S2.1 |
| F7 | `/internal/*` is unauthenticated and reachable from every network the api joins (jcode, jlaunch, sandbox, render, browser, radio, panel, internal incl. reader/byparr); `/internal/pet/say` runs as owner, `/internal/mqtt-auth` is a key oracle | Med-High | S2.2 |
| F8 | reader/byparr headless browsers sit on the non-fenced `internal` network and follow redirects/JS after only the first URL is checked → SSRF into comfyui, local-llm, wall, api `/internal`, metadata | Med-High | S2.3 |
| F9 | Direct-fetch guard: check-then-connect DNS rebinding; `_is_public` passes 100.64/10 | Med | S2.3 |
| F10 | Live WS sockets authenticate once: a revoked member keeps the family location feed; *rev:* a revoked jcode share keeps its terminal, its backgrounded processes and its preview slug | Med | S3.1 |
| F11 | `PanelDep` / panel WS accept any device key (a phone acts as a panel); a panel key mints a member session | Low-Med | S3.2 |
| F12 | Debug SQL redaction is by column name; plaintext pairing codes → a device key that outlives the debug token | Med | S3.3 |
| F13 | Every rate limiter keys on `request.client.host` (always Caddy) → one global bucket; `nudge` trusts the first XFF hop | Low | S3.4 |
| F14 | `/member/positions?subject_id=<any>` writes a forged "viewed you" audit row | Low | S3.4 |
| F15 | Secret compares with `!=` (jcode_llm, gmail OAuth state) | Low | S2.1 |
| N1 *rev* | `local-llm` (and `flash-next` aliased as it) is on the jcode network: a session talks to llama-swap directly — loads cold models outside the residency budget (a host freeze), erases slots, bypasses caps | High | S2.1 |
| N2 *rev* | jcode outbound is direct NAT: a share recipient reaches the home LAN, the host's published ports, metadata | Med | S2.4 |
| N3 *rev* | A Minecraft player's `/jb:dave` text containing `Player connected: <me>, xuid: <victim>` matches the unanchored BDS regex → the player takes over another player's Dave session, memory and history | Low-Med | S3.5 |
| N4 *rev* | `href`s built from LLM/web/email-derived URLs (`onlineUrl`, citations) have no scheme allow-list (`javascript:`) | Low | S1.4 |
| N5 *rev* | htmlrender's request block doesn't intercept WebSockets from model-authored HTML | Low | S2.3 |
| F16 | Latent RLS: Timescale chunks lack RLS but `jbrain_app` has DML on them; `viewer_may_see`/`visible_subjects` trust a caller-supplied viewer; `view_audit_write` doesn't check visibility | Latent | S5 |
| F17 | Stolen desk-panel key = owner agent reads; plaintext NVS | Design | Out of scope |
| F18 | Wall on `0.0.0.0:8800` relays pet controls for any LAN client (and, in direct mode, possibly the internet) | Low | Recorded (below) |

## Waves

Each wave is one PR off the session branch, built task-by-task with an independent
reviewer per task and a wave-level red-team review (PROCESS.md). Every task: failing test
first, then the fix, then the package's full gate set (`CLAUDE.md` #11).

### S1 — close the critical web holes ✅

**S1.1 Preview proxy path containment (F1).**
- `api/jcode_preview.py` (HTTP and WS) refuses a path any of whose `/`-segments, after
  percent-decoding, is *equal to* `.` or `..` or contains `\`, `%2f`/`%5c` survivors or a NUL;
  checked on the path only, never the query (*rev:* `main.js`, `node_modules/.vite/deps/…`
  must keep working). It then asserts the normalised httpx URL path still starts with
  `/preview/{slug}/` (or equals `/preview/{slug}`) — the real guard; 404 otherwise.
- Cap the bytes actually read, not just `Content-Length` (*rev:* chunked bodies).
- jcode ctl: preview routes (HTTP *and* the hand-checked preview WS) move to their own
  credential, `preview_token = HMAC(JCODE_TOKEN, "jcode-preview")` derived on both sides (no
  new `.env` key); `authed` and the terminal WS refuse it, the preview routes refuse the main
  token. The ctl's `proxy_http` also refuses dot segments.
- *Keeps working:* every dev-server path (assets, dotfiles like `.vite`, HMR WS, queries,
  encoded spaces); the PWA iframe; the direct preview tab.
- *Tests:* traversal variants (`..`, `%2e%2e`, mixed case, `..;`, backslash) 404 without
  touching the transport; nested/dotted/queried paths proxy; ctl token separation both ways.

**S1.2 Owner gate for owner surfaces (F3, F4).**
- `dependencies=[Depends(owner_only)]` on the `llm_settings`, `gmail_settings`,
  `tavily_settings`, `settings`, `brain`, `notes`, `analysis`, `search`, `wiki` routers and
  on any other router the matrix finds (*rev:* also confirm `ops`, `locations`, `lists`,
  `sessions`, `appointments`, `chat_attachments` — gated at router level today).
- `/tiles` narrows to owner + `device_key` (the member map); `/intake/{chat,confirm}` keep
  their own intake-link check.
- **Route-matrix test** (*rev* design): builds the app with *every* conditional router
  mounted (debug, images_render, jcode, jlaunch, …), walks every HTTP and WS route, and for
  every principal kind in the schema except owner (`device_key`, `jcode_share_link`,
  `intake_link`, `capability_token`, `external_llm`), with sessions created directly through
  the repo, asserts 401/403 (HTTP) or close-before-accept (WS) — unless the **(route, kind)**
  pair is on an explicit allow-list. Allow-list: `/auth/me` and logout (all session kinds);
  `/tiles`, `/member/*`, live WS (`device_key`); `/intake/*` (`intake_link`); jcode
  `GET /jcode/sessions/{sid}`, preview GET/POST/DELETE, terminal WS (`jcode_share_link`, own
  sid only); the token-in-path / bearer / Basic routes are exercised separately. A new
  `PrincipalDep` route then fails CI.
- `upload_attachment` resolves the note under RLS **before** `blobs.put`.
- `login()` filters `kind = 'owner'`; the session-cookie lookup admits only the kinds that
  may hold a session (`owner`, `device_key`, `jcode_share_link`, `intake_link`), which also
  kills any capability/external-LLM cookie already minted through the old login.
- Fix the false "owner gate" docstrings (gmail, tavily) and the "ctx_for is a full owner"
  comments.
- *Keeps working:* the owner's PWA; member dashboard; jcode share page; intake page.
- *Tests:* the matrix; owner 200 on every touched router; member/share/intake flows.

**S1.3 Same-site request forgery (F2).**
- (a) A **pure ASGI middleware** covering both `http` and `websocket` scopes (*rev:*
  `@app.middleware("http")` never sees WS). For any request carrying the session cookie: if
  `Sec-Fetch-Site` is `same-site`, reject unconditionally; if `cross-site`, reject except a
  `GET`/`HEAD` with `Sec-Fetch-Mode: navigate` to an explicit allow-list of callback routes
  (the Gmail OAuth callback) (*rev, rev 2:* closes side-effecting GETs such as
  `/settings/gmail/connect` and `/brain/tts`, including via a popup from the preview; the api
  serves no HTML, so external links into the PWA hit Caddy's static shell, not `/api`). If
  `Sec-Fetch-Site` is absent and `Origin` is present: Origin host[:port] must equal the
  request `Host` (scheme ignored — behind the tunnel the api sees http; default ports
  normalised); `Origin: null` with a cookie is rejected. No cookie, or neither header
  (OkHttp, curl, TestClient), passes. WS rejection closes before accept.
- (b) `origin_allowed` fails closed (Origin host == Host when no allow-list; absent Origin
  still passes; a configured allow-list still wins; missing Host tolerated).
- (c) **Cookie tossing** (*rev*): when `secure_cookies` is on, the session cookie becomes
  `__Host-jbrain_session` (Secure, Path=/, no Domain — a subdomain cannot shadow it). Migration
  is silent: a request authenticated by the legacy `jbrain_session` gets the same token
  re-issued as `__Host-jbrain_session` and the legacy cookie deleted; once the `__Host-`
  cookie is present the legacy name is ignored. No device has to log in again. *(rev 2)* The
  migration runs only for an `owner`/`device_key` principal and only when exactly one legacy
  value is in the raw Cookie header (a tossed parent-domain cookie can't be promoted). *(As
  built)* only an `owner` session is promoted — a member session is mintable by its holder,
  and the dashboard mints a `__Host-` cookie on every launch anyway; the legacy name has no
  sunset, because the Android owner app's OkHttp notification relay never sees the
  migration response; and the guard passes `/__jcode_preview/*` through, since those routes
  read no cookie and a planted one must not 403 every preview. One helper sets *and deletes*
  both names, with
  `Secure; Path=/` on the `__Host-` deletion (browsers ignore it otherwise). When
  `secure_cookies` is off (dev/tests) the legacy name stays. Every cookie reader
  (`deps.py`, `live.py`, `jcode_terminal.py`, `jlaunch_terminal.py`, Android's
  `DashboardActivity` injection) reads through one helper.
- (d) Caddy `(app)` / public / LAN sites send `Content-Security-Policy: frame-ancestors 'self'`
  and `X-Frame-Options: SAMEORIGIN` — never the preview wildcard site.
- (e) The preview iframe gets `sandbox="allow-scripts allow-same-origin allow-forms
  allow-modals allow-popups allow-popups-to-escape-sandbox allow-downloads
  allow-pointer-lock"` (no top-navigation); the comment states `allow-same-origin` is safe
  only because the preview is always a different origin.
- *Keeps working:* every same-origin PWA/WebView call and WS; external links opening the
  PWA; OkHttp mint/pairing/SSE; panels; OwnTracks; debug bearer calls; previews; the YouTube
  embed; existing logins (silent cookie migration). Update `test_live.py:249` and
  `test_debug_tokens_api.py:84` (their assumptions are the bug).
- *Tests:* same-site/cross-site rejected with cookie (HTTP + WS); navigate GET allowed;
  mismatched Origin / `null` rejected; matching Origin, absent headers, cookieless pass;
  legacy cookie migrates; a tossed legacy cookie can't shadow a `__Host-` one.

**S1.4 Safe hrefs (N4).** One `safeHref()` in the frontend (http/https/mailto only, else no
link) used wherever an `href` comes from data (`CalendarScreen` `onlineUrl`, web citations,
FullBrainSurface sources); the backend drops a non-http(s) `online_url` at projection.
*Tests:* unit for `safeHref`; projection rejects `javascript:`.

### S2 — sandboxes and the internal network ✅

*As built (after five reviews):* jcode shells also get `closerange(3, …)` (a later shell
inherited earlier sessions' pty masters — the critical find), umask 077 and `TMPDIR=~/.tmp`,
`RLIMIT_NPROC` 256, SysV IPC and `/run/lock`/`/dev/mqueue` swept on uid reuse, grok's
instructions seeded at `~/.grok/AGENTS.md` (grok never read `/work/AGENTS.md`), and the
container runs `init: true` and `no-new-privileges`. byparr is proxied by `PROXY_SERVER` +
`X-Proxy-Server` (its API has no body field), reader by `X-Proxy-Url` + env, and the
ctl's preview hop never uses the env proxy (`::1` dev servers).

**On-box gate after merge** (the box deploys `main` only, so it cannot precede the merge):
`POST /api/debug/fetch` `tier=reader` and `tier=solver`; in a jcode session clone /
`npm install` / `pip install --user` / `curl` / Node `fetch` / a Vite preview with HMR;
fence probes (`local-llm` direct, metadata, LAN, and the fenced bridge's gateway on host
ports such as `:8800`/`:22`, which squid cannot see). If reader fails through squid, the
reader network move is reverted alone; if the bridge gateway answers host ports, it is
recorded below and fenced in the stack, never by a host step.

**S2.1 jcode: per-session users, no root, no control token (F5, F6, F15, N1).** Owner
decision D1: **per-session unprivileged uids**.
- Each session gets its own uid/gid from a pool; checkout `/work/<sid>` and home
  `/work/.home/<sid>` are pre-created owned by it, mode 0700; `/work` and `/work/.home` are
  root 0711. The forked child does `setgroups([])`, `setgid`, `setuid` before exec; the pty
  slave is `fchown`ed to it. Sessions are in-memory and do not survive a ctl restart (today's
  behaviour, unchanged), so *(rev 2)* on ctl start, before any uid is handed out, every
  `/work/*` and `/work/.home/*` tree that is not a live session is removed — an orphan owned by
  a recycled uid must never become readable by a new session.
- **All git operations run as the session uid** (clone, checkout, reset, clean) — never as
  root inside a session-controlled tree (*rev:* hooks/fsmonitor/filters). No
  `safe.directory` escape hatch.
- Every git call runs with the session's `user`/`group`, `extra_groups=[]`, `HOME=<session
  home>` and a minimal env (*rev 2:* an inherited `HOME=/root` fails as non-root).
- The ctl never writes into a session tree as root after creation; ownership changes use
  `lchown` (*rev:* symlink attacks). `/work/AGENTS.md` (and `CLAUDE.md`) are written by the ctl
  as root 0644 at startup and the profile.d writer is removed (*rev 2:* a non-root shell can't
  write `/work`, and a writable `/work` would let one session rewrite another agent's
  instructions).
- Stop/delete kill the session's processes by uid, not just by cwd.
- *Residual, recorded (rev 2):* sessions still share one network namespace, so a recipient
  of session A can `curl localhost:<B's port>` to read B's dev server while both are live. The
  ctl closes the related port-squatting trick by proxying a preview only when the socket
  listening on the session's port belongs to the session's uid (`/proc/net/tcp` inode → uid);
  blocking loopback itself needs a per-session netns, which `JCODE_SESSION_ISOLATION_PLAN`
  parked.
- The env handed to `spawn_shell` drops `JCODE_TOKEN` and the ctl-only settings by explicit
  name (`JCODE_TOKEN`, `JCODE_DEBUG_ACCESS_ENABLED`, `JCODE_LOG_LEVEL`, …) before the
  per-session overrides; it keeps `JCODE_WORKSPACE_ROOT`, `JCODE_TOOLS_BIN`,
  `JCODE_INTERNET_*`, `JCODE_GROK_*`, `GROK_*` and `HTTP(S)_PROXY`/`NO_PROXY`.
- `local-llm` and `flash-next` leave the `jcode` network (N1) — grok already goes through the
  api's residency-aware proxy; a compose topology test asserts it.
- Caddy 404s `/api/jcode/llm/*` on the app sites (in-container callers use `http://api:8000`).
- `hmac.compare_digest` for every secret compared with `!=` (grep: jcode_llm, gmail state, …).
- *Keeps working:* create/clone/reset/stop/restart/delete; the agent's model, web tools and
  OCR (`AGENTS.md` still lists them); `jcode-grok upgrade`, `npm -g` into home, `pip --user`;
  previews on 5173-5199; the terminal for owner and share recipient. *Lost by design:*
  `apt-get` and writes outside the session's own tree.
- *Tests:* child env lacks `JCODE_TOKEN`; uid/gid drop; 0700 trees; git runs as the uid with
  the session HOME; orphan trees removed at start; preview refused when a foreign uid owns the
  port; Caddyfile block; topology test.

**S2.2 Authenticate `/internal/*` (F7).**
- Per-caller secrets, scoped by route: the wall's token opens `/internal/pet*` only;
  mosquitto's opens `/internal/mqtt-*` only (*rev:* the LAN-facing wall must not hold a key to
  MQTT). Enforced by router-level dependencies, constant-time.
- **Distribution without a lag** (*rev:* `update-inner.sh` keeps executing the *old* script
  after its own `git reset`, so a mint added there only lands on the *second* update). The api
  generates the secrets on first start into a small named volume (`internal_secrets`, mode
  0600 files created with `O_EXCL`; the Dockerfile pre-creates the mount point owned by the
  api's uid so the empty volume inherits it), the wall (root) mounts it read-only and reads
  its token per request, and mosquitto's entrypoint (root, before mosquitto drops privileges)
  waits for its file and renders the read-only-mounted conf plus `auth_opt_http_user_agent`
  into `/tmp`, then runs `mosquitto -c` on that. Same
  update that ships the code ships the secrets; nothing in `.env`.
- The worker's regen call keeps its HMAC bearer (unchanged).
- No rate limit on `/internal/mqtt-auth` (*rev:* one peer → one bucket would lock every phone
  out; 256-bit keys make the oracle moot once the route is gated). Fix the no-op
  `auth_opt_http_response_timeout_ms` → `auth_opt_http_timeout`; pin the go-auth image
  digest and confirm `auth_opt_http_user_agent` in that build; a test asserts no logger
  records the User-Agent of `/internal` calls.
- *Keeps working:* wall pet controls from the LAN TV; MQTT phone auth/ACL; gateway regen; the
  first update.
- *Tests:* each route 401 without/with the wrong caller's token, 200 with its own; secret
  file generation idempotent; mosquitto conf render; wall sends the header.

**S2.3 SSRF fence (F8, F9, N5).**
- `reader` and `byparr` move to a **new** `internal: true` network (`fetchers`) whose only
  exit is the squid `egress` (new networks, not a flag flip on `internal`, so no compose
  network recreation on the box); the api and `egress` join it. Byparr uses per-request
  `proxy: {"url": "http://egress:3128"}` in the FlareSolverr payload (*rev:* the env var is
  unverified); reader's Chromium gets `HTTP(S)_PROXY` — and the network is the guarantee
  either way. Squid's ACLs already deny private/CGNAT/link-local/compose names.
- `_is_public` → `is_global` (reuse `browse_policy._is_public_ip`) plus explicit denies for
  `64:ff9b::/96`, `::a9fe:a9fe`, `fec0::/10`. Direct httpx fetches (WebFetcher, favicon,
  captions, moltbook) connect to the validated IP through an httpcore network backend (SNI and
  Host keep the name; every redirect hop and pooled connection is covered), with a DNS seam
  for tests.
- htmlrender refuses WebSockets too (CSP `connect-src 'none'` on rendered pages).
- **On-box gate before merge** (*rev*): `POST /api/debug/fetch` with `tier=reader` and
  `tier=solver` against a real page must succeed on the box; the owner is asked for a debug
  token (or to run it) at that point. If reader cannot reach the internet through squid, the
  reader move is held back and recorded rather than merged.
- *Residual, recorded:* yt-dlp/ffmpeg stream fetches (`web/stream.py`) are checked once and
  fetch on their own stacks; routing them through squid would hit its 50 MB body cap.
- *Keeps working:* the direct → reader → byparr → Tavily ladder; search; favicons; captions.
- *Tests:* compose topology (reader/byparr only on `fetchers`; `fetchers` internal; egress on
  it); `is_global` cases incl. CGNAT and the three v6 ranges; rebinding via the DNS seam;
  redirect to a private address refused.

**S2.4 jcode egress fence (N2).** Owner decision D3: **fence it**. jcode moves to a new
`internal: true` network (`jcode_fenced`) shared with `api` and `egress`; its proxy env
defaults to `http://egress:3128` in both cases (`HTTP(S)_PROXY` and `http(s)_proxy` — curl
ignores the uppercase `HTTP_PROXY`), `NO_PROXY`/`no_proxy=api,localhost,127.0.0.1`, and
`NODE_USE_ENV_PROXY=1` (Node's fetch ignores proxy env otherwise). Squid has no source ACL, so
the new networks are served, and its compose/private denies stop it bridging networks. Squid
gives git/npm/pip public https and http on 80/443 and refuses the LAN, the host, metadata and
compose names. The old `jcode` network keeps only what still needs it (nothing, after N1 —
then it is dropped from compose). *Lost by design:* git over ssh and non-80/443 outbound from
sessions. *Keeps working:* clone over https, npm/pip installs, grok's model and web tools (via
`api`), previews (served by the ctl via the api, not by outbound). *Tests:* topology; env
defaults; squid ACL test cases for the LAN/host ranges. *On-box gate:* clone + `npm install` +
`curl https://…` + a Node `fetch` in a session.

### S3 — device, session and token hygiene ✅

*As built (after per-task reviews):* socket re-checks don't write and survive a DB blip
(1011 only after three failed checks); a rotated or released preview slug also closes its
open HMR sockets, and the owner's PWA re-reads the preview after a revoke. **S3.3 differs
from the text below:** secret `app.settings` rows are not redacted cell by cell but hidden
from every READ ONLY transaction by a restrictive RLS policy (only the debug routes run read
only), which holds against any alias, expression or SQL-from-text function; the refused list
also covers `ts_stat`/`ts_rewrite`/`pg_ls_dir` and `U&` spellings, and the Gmail OAuth state
is hidden with the credentials. S3.2's phone/panel test fails closed, and a member cookie a
panel key minted before the fix stops authenticating. The rate-limit table is bounded now
that it keys on internet-controlled client IPs. S3.5 also tolerates a `NO LOG FILE! - `
prefix and anchors pack events to the whole line.

**S3.1 Sockets re-validate (F10).** One helper re-authenticates a WS's handshake token on a
~30 s tick (the receive wait gets a timeout so idle sockets are re-checked) and closes 4401
when the session/principal is revoked or expired. Used by the live map (also clears its
`viewer_may_see` cache each tick) and the jcode and jlaunch terminals. Revoking a jcode share
closes the share's sockets at once and **rotates the session's preview slug**. Its processes
are not killed: the owner and recipient type into the same reattached shell, so the share's
processes can't be told from the owner's, and killing the session's would kill the owner's
work (*rev 2*). *Residual, recorded:* anything a revoked recipient planted in the session
(`~/.bashrc`, `.git/hooks`, npm lifecycle scripts) still runs, confined to that session's uid
and the fenced egress, when the owner later uses the session; the existing Reset/Delete
clear it. *Keeps working:* owner and member
live maps, owner terminals, a share's terminal while the share is live. *Tests:* revoke
mid-stream closes; group removal stops that subject; slug rotates on revoke.

**S3.2 Panel vs phone (F11).** `current_panel_principal` and the panel WS accept a device key
only if its subject's `device_role IS NOT NULL`; `/api/session/mint` and OwnTracks accept only
`device_role IS NULL`. Every `/endpoint/flash` label is `panel …`/default, which 0211
back-filled, so no flashed panel is NULL — and the api logs a warning at startup with the count
of NULL-role principals that have `endpoint_status` rows, so a stray one is visible in the
debug logs. *Tests:* phone key refused on panel routes/WS; panel key refused at mint/OwnTracks;
happy paths.

**S3.3 Pairing codes and debug SQL (F12).** Owner decision D2: **hash + harden**, not a
separate DB role. Pairing codes stored as `sha256(code)`; existing in-flight rows are hashed
in place by the migration so an open QR still redeems; `redeem_pairing_code` compares hashes;
never `hash_key` (codes are case-sensitive). The debug SQL route refuses statements naming
the XML/SPI family (`query_to_xml`, `table_to_xml`, `cursor_to_xml`, `schema_to_xml`,
`database_to_xml`), `set_config`, `pg_read_*`, `lo_*`, `dblink` (text match on the
lower-cased statement; `EXPLAIN` checks its inner statement; `SHOW` passes), redacts any
cell of an `app.settings` row whose key is in an explicit secret-key set when the query
selects from `app.settings` (by relation, not alias), and keeps the column-name redaction as a
backstop. The debug console remains owner-level read access by design; its token is the
owner's to give. *Keeps working:* every other debug SQL read, the `SHOW`/`EXPLAIN` diagnostics,
the pairing QR flow. *Tests:* `query_to_xml` refused; aliased settings secret redacted; hashed
redeem; in-flight code survives the migration.

**S3.4 Client IP and audit (F13, F14).** Caddy always overwrites `X-Real-IP` with its peer
(`{remote_host}`); the api trusts `X-Real-IP` only when the TCP peer is the proxy container,
and uses `Cf-Connecting-IP` only when `X-Real-IP` equals the resolved address of the
`cloudflared` service (*rev 2:* a bridge-range test also matches the docker gateway that
published-port and hairpin connections arrive from), else `X-Real-IP` — so a LAN client can't spoof `Cf-Connecting-IP` through the published port in
either mode. The panel tunnel forwards `cf-connecting-ip` too. All limiters and `nudge` use
the helper. `/member/positions` audits only when `viewer_may_see(viewer, target)` and target ≠
viewer. *Tests:* helper matrix (direct, tunnel, LAN-spoof, sandbox peer); audit written for a
visible subject with no fixes, not for an invisible one.

**S3.5 Minecraft log-line forgery (N3).** `deploy/minecraft/bds.py` dispatches pack lines
(`[Scripting]`/`[jbrain]`) first and anchors every BDS regex to BDS's own line prefix
(`^\[[^\]]+ INFO\] Player connected:` etc.), so a player's text can't impersonate a
join/leave, the server-start or save-ready markers. *Tests:* the injected `/jb:dave` question
is parsed as a question, not a connect; real lines still parse.

### S4 — wave-level red team ✅

A cold red team re-analyses the whole codebase after S1–S3 merge: re-attack every finding,
attack the fixes, and separately hunt regressions across the flow table. Findings are fixed in
S4's PR.

*As built.* The red team found four things; three are fixed, one is recorded.

- **S4.1 Chat share publishes private reads without a reliable warning.** A share visitor
  gets every tool step verbatim (`summary`, `args`, `sources[].snippet`), but the share sheet
  warned from the session's *current* `domain_scopes` only, so a chat re-scoped to `general`
  after reading a health note, or one that ran Gmail or memory tools, minted a link with no
  warning. The server now decides: `chat_shares.share_sensitivity` walks every stored turn's
  `tools` for a source/entity `domain` in the firewalled set (`reflexion.SENSITIVE_SCOPES`),
  any `gmail_*` step, and the steps listed in `SENSITIVE_TOOLS` (agent and archivist memory
  reads and writes, `recall`, `remember`, the standing-rules `prefs_*`, the medical-record
  readers, every location tool, and `chart_measurements`, whose view says `general` for a
  finance or mixed chart), plus the appointment and list readers when their result text tags
  a row `[health]`/`[finance]`/`[location]` (their only domain marker, so a grocery list stays
  one tap), plus the session's current firewalled scopes (later turns show on a live link).
  Two tests keep the map honest: every `.tool` declaring a firewalled domain is in it, and
  every reader-shaped tool (`read_*`, `*_read`, memory/prefs/recall/remember) is classified
  as sensitive, domain-tagged, or explicitly harmless. `GET /sessions/{id}/shares/sensitivity`
  returns the reasons, and `POST /sessions/{id}/shares` refuses with 409 (and the reasons)
  unless the body says `confirm_sensitive: true`. The sheet warns from the server's answer
  with its existing second tap, which is what sends the confirm; a 409 the sheet didn't
  expect re-arms it with the reasons, and a reason the sheet has no word for shows as its code. *Keeps working:* a harmless chat mints from a bare
  POST; a confirmed share serves the chat in full, tool steps included, as before.
  *Tests:* detection unit cases (rescoped source, current scope, mail, memory and prefs,
  sourceless readers, tagged appointment/list rows vs a general list, the measurement chart,
  nested and malformed shapes, both guards); the API refuses then accepts a
  re-scoped health chat and a Gmail chat, and mints a harmless one unconfirmed; the sheet
  warns from server data, sends the confirm, and recovers from a 409.
- **S4.2 Opening an intake link signs the owner out.** The intake page redeems on load, and
  the redeem always set the intake cookie over whatever session the browser held. Mirroring
  the jcode share redeem, a browser holding an **owner** session now gets 409 before the
  redeem runs, so no open is spent and no cookie is written; the page says the owner is
  signed in and to try the link in a private window, and leaves the secret in the URL for
  that. A no-cookie success was rejected: the intake routes need the intake cookie, so the
  page would have loaded and then failed on its first turn. *Keeps working:* a stranger's
  redeem, cookie and TTL-capped max-age. *Tests:* owner cookie kept, `opens_used` still 0,
  then the same link redeems for a stranger; the page's owner state.
- **S4.3 Empty control tokens.** Compose passes `JCODE_TOKEN: ${JCODE_TOKEN:-}` and
  `JLAUNCH_TOKEN: ${JLAUNCH_TOKEN:-}`, so an unset key reached the ctl as `""`, which started
  and admitted `Bearer ` despite comments saying it would refuse to start. Both ctls now
  refuse an empty token (`min_length=1`). That is safe because neither can be empty after an
  update: `update-inner.sh` backfills a missing or empty `JCODE_TOKEN` whenever code mode is on
  (since June 2026; `jcode-setup.sh` before it) and a missing or empty `JLAUNCH_TOKEN`
  unconditionally (since August 2026; `install.sh` mints one too), both before the stack's
  `up`. No length floor: a hand-set short token can't be ruled out, and a floor would stop it
  with no terminal to fix it. The supervisor is **not**
  changed (recorded below); its false "refuses to start" comments are corrected.
  *Tests:* for each ctl, an empty token in the environment is refused and a minted 64-hex
  token starts.
- **S4.4 The wall serves the owner's turn text (F18, amended).** Recorded, not fixed; see
  below. The wall's docstring, which said it carried no user data, is corrected.

### S5 — latent RLS hardening ✅

Not reachable by a third party today (each needs arbitrary SQL, and the only arbitrary-SQL
surface runs as owner). Each lands only with an integration test proving no behaviour change;
otherwise recorded as a residual in `ROADMAP.md`. Dropped after review: tightening
`place_geofence_read` (*rev:* ingest-time geofence detection relies on it for owner "all
devices" fences).

*As built.* (a) and (b) landed together in one migration; (c) is recorded.
- **(a) landed.** `view_audit_write` WITH CHECK: a device's row must be about its own subject
  or one `viewer_may_see` admits. Its own subject is spelled out: `viewer_may_see(self, self)`
  is false for a member in no family group, and the live socket audits a member's own track.
  The owner branch is unchanged.
- **(b) landed.** `viewer_may_see` / `visible_subjects` answer a non-owner only about its own
  `app.subject_id`; any other viewer gets false / no rows (not an error, so a wrong caller
  degrades to "can't see"). Owner sessions holding the location domain — the full owner, the
  debug console, a location-narrowed agent, and the worker's `SYSTEM_CTX` that push routing
  runs under — still ask about anyone; a narrowed owner without location gets nothing, since
  `view_scope` itself is location-firewalled. The pin guards against an app bug passing the
  wrong viewer, not against arbitrary SQL (which can set its own GUCs). `SqlViewScopeRepo.may_view` (the
  live socket and the MQTT ACL) now asks under the viewer's `device_context`. Every other
  caller already passed its own subject: the `location_fixes` view-scope policy
  (`current_setting('app.subject_id')`), `member_roster` and `member_timeline` (under the
  member's `device_context`).
- *Tests* (`test_view_scope_pinned_pg.py`): a member asking as another viewer gets false and
  an empty roster; an empty context and a NULL viewer get nothing; a narrowed owner without
  location gets nothing; owner, debug console, location-narrowed owner and `SYSTEM_CTX`
  unchanged; `may_view` still says yes for the family; audit rows land for a family member,
  for self, and for a group-less member's own track, and the forged "viewed Carol" row is
  refused; the live map's `deliver_fix` over the real repos sends and audits self + family and
  drops an outsider; the member dashboard (`/session/mint`, roster, positions, timeline) over
  the real app with its audit rows. The forgery and foreign-viewer tests fail without the
  migration.
- **(c) recorded, not landed.** Probed on TimescaleDB 2.30.2: revoking `jbrain_app`'s
  privileges on an existing chunk does not break hypertable access (insert, read and delete
  through `app.location_fixes` all work, and a direct chunk `SELECT` is denied). But
  Timescale copies the hypertable's ACL onto every new chunk at creation, so the revoke only
  holds until the next chunk, and `ALTER DEFAULT PRIVILEGES` plays no part. Enabling RLS on a
  chunk is likewise not inherited by the next one (and `FORCE` is refused on chunks). The
  only durable mechanism found is a superuser `ddl_command_end` event trigger that revokes
  on each new chunk — it works in a probe, but it keys on Timescale emitting `ALTER TABLE`
  events for its internal chunk DDL (undocumented, version-dependent) and it runs inside
  the INSERT that creates a chunk, so any failure there would stop location ingest and host
  metrics. That is a worse risk than a hole only the owner's own SQL can reach. Revoking
  `USAGE` on `_timescaledb_internal` (where 2.30.2 keeps the chunks; `jbrain_app` has it only
  through `PUBLIC`) was probed too and is a non-starter: every insert, select and delete
  through `app.location_fixes` and `app.host_metrics` then fails with "permission denied for
  schema _timescaledb_internal", because the executor opens the chunks as the querying role.
  Recorded below.

## Owner decisions (taken 2026-10-12)

- **D1 — jcode sessions lose root: yes, per-session uids.** Cost: no `apt-get` in sessions.
- **D2 — debug SQL: hash pairing codes + harden**, not a separate login role.
- **D3 — jcode egress: fenced** through the squid egress. Cost: no git-over-ssh or
  non-80/443 outbound from sessions.
- Not planned: moving previews to a separate registrable domain (S1.3 makes it unnecessary).

## Out of scope / recorded

- **F17 panel flash encryption.** ESP32 flash encryption/secure boot burn eFuses
  irreversibly and interact with the "cable once" OTA rollback design; needs hardware in
  hand. Carried to `ROADMAP.md` as a firmware residual.
- **F18 wall** (amended by S4). A LAN display by design; S2.2 scopes its token to the pet
  routes. Everything on `deploy/wall/serve.py` is unauthenticated for any client that can
  reach `:8800` — the LAN, and in direct mode possibly the internet, since the port is
  published on `${BRAIN_HOST_BIND:-0.0.0.0}` and Docker-published ports bypass ufw:
  - `GET /stats` drains the posted-event queue. With the brain-display text setting on
    (`brain_llm_stream`), the api forwards the owner's real prompts (`llm_input`), answers
    (`llm_output`) and web queries/URLs (`agent/brainevents.py`), so any poller reads them —
    and, since the read drains, takes them from the TV.
  - `POST /event` is unauthenticated: a LAN client can inject popups and text onto the
    display, latch the read-aloud flag and change the answer voice settings.
  - `GET /tts`, `/tts/voices`, `/tts/silence` forward to the tts-stt service, so any client
    can spend its compute.
  - The pet routes relay an owner-context pet line and statue compute (as before).
  Candidate fixes, each an owner choice because each changes what the TV on the LAN can
  do: default `BRAIN_HOST_BIND` to the LAN address or `127.0.0.1` (breaks
  `http://<host>.local:8800` on another device unless set, so it needs a PWA setting, not an
  `.env` edit); or forward turn text only when the bind is loopback (loses the text on a LAN
  TV); or a per-display key the page holds. Not changed in S4: none is safe without the owner
  choosing, and the bind is `.env`-only today, which is itself a terminal gap to design out.
- **Supervisor accepts an empty token** (S4.3). Compose passes `${SUPERVISOR_TOKEN}`, so a
  `.env` without the key starts a supervisor whose bearer is `Bearer `, which the api, holding
  the same empty value, sends — so such a box works today, and refusing it would stop Ops →
  Update and the api watchdog with no terminal to repair it. `install.sh` always mints one,
  and the supervisor is on the internal network only. A safe fix needs the updater to mint a
  missing key first, which runs only on the second update (the self-update lag above).
- **A chat share's sensitivity is checked at mint** (S4.1). A link to a harmless chat stays
  live as the owner keeps chatting, so a later turn that reads a firewalled domain is
  published with no new warning. The sheet already says later turns show; revoking is the
  remedy. Gating the visitor's read on later turns would change what a share is.
- **`update-inner.sh` self-update lag.** The updater runs its pre-reset copy, so new mint steps
  take two updates. S2.2 avoids depending on it; fixing the updater itself is its own change.
- **Stream fetches** (yt-dlp/ffmpeg) keep check-once SSRF guarding (S2.3).
- **MQTT on the first update** (S2.2): profile services are `start`ed, not recreated, so an
  enabled broker keeps its pre-token config until **Ops → Rebuild `mqtt`** (OPERATIONS.md).
- **Revoke with an unreachable jcode ctl** (S3.1): the revoke stands and is logged, but the
  old preview slug stays valid until the next rotate, delete or reap.
- **Minecraft command replies** (S3.5 review): the backend reads `/command` replies from
  whatever lines arrive in the reply window, so a well-timed `/jb:dave` line could make Dave
  report a false result to another asker. No identity, permission or stored state rides on
  it (transport's write is keyed by an unseen trip id); a per-request nonce would close it.
- **A pairing code minted mid-update** (S3.3): one the old api mints after the migration ran
  is stored plaintext and won't redeem on the new api; re-minting from the PWA fixes it.
- **Debug SQL text guard** is a deny-list and refuses a few harmless statements that merely
  mention a blocked name; the secrets themselves are protected by the RLS policy, not by it.
- **Git hooks run by the ctl** (reset/clean, as the session uid) don't get the shells'
  `RLIMIT_NPROC`; only the container `pids_limit` bounds them (S2.1).
- **Browsers without fetch metadata** (Safari before 16.4) send neither `Sec-Fetch-Site` nor
  `Origin` on a top-level GET, so a cross-site navigation from such a browser can still reach
  a side-effecting GET (`/settings/gmail/connect`, `/brain/tts`) with the Lax cookie (S1.3).
- **Cookie bomb.** A preview can set enough large cookies on the parent domain to overflow the
  owner's Cookie header until they are cleared; only a separate registrable domain for
  previews prevents it (D2, not planned).
- **Timescale chunks keep `jbrain_app` privileges** (S5c, F16). A direct
  `_timescaledb_internal._hyper_*` read skips the hypertable's RLS. Reachable only with
  arbitrary SQL, which only the owner's debug console has. A revoke does not survive the next
  chunk (Timescale copies the hypertable ACL), revoking schema `USAGE` breaks every hypertable
  query, and the event-trigger fix that would hold is fragile and sits in the ingest path. Revisit if a non-owner ever gets arbitrary SQL, or if Timescale
  grows a supported hook.
- **A lone planted legacy cookie** on a browser with no session of its own is believed (as
  its own kind, never promoted) until the owner signs in.

## Provenance

Six cold reviewers (non-owner routes, unauthenticated routes, credentials/share links,
devices/MQTT, infra/SSRF, RLS/contexts), three researchers (jcode/internal, CSRF callers,
SSRF/pairing/panels), and three cold plan reviewers (adequacy, regressions, completeness).
F1, F2 (Origin gate default, iframe), F3 (route inventory) and F5 were re-verified by hand.
