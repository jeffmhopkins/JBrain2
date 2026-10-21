# JBrain2 — Architecture

> **Status:** Living · **Last verified:** 2026-10-21

A personal knowledge system: notes go in, a RAG pipeline indexes them, and an
LLM maintains a wiki built **exclusively from notes as primary sources**. Around
that core it also runs a personal agent, structured records (lists, labs,
appointments), guided-intake share links, and Life360-style location tracking.

> For the concrete inventory of **every** container (core + opt-in), the on-box
> GPU model services, the PWA + Android app, and the baked-in functions, see
> `SERVICES.md`. This doc covers the design and the why; that one is the map.

## System shape

One Docker Compose stack on an Ubuntu host. Reachable either directly on a
public domain (Caddy fetches Let's Encrypt; needs inbound 80/443) or, for a
home box on a dynamic IP / behind CGNAT, via an opt-in Cloudflare Tunnel that
dials out (no static IP or port-forwarding) — see `../runbooks/CLOUDFLARE_TUNNEL.md`.

| Container | Technology | Role |
|---|---|---|
| `proxy` | Caddy | Auto-TLS in direct mode, or plain HTTP behind the tunnel; serves the built PWA, routes `/api` |
| `api` | Python / FastAPI | REST API, auth, CRUD, search, chat |
| `worker` | Same image as `api` | Job-queue consumer: extraction, chunking, embedding, analysis, wiki builds |
| `db` | TimescaleDB-HA (Postgres + Timescale + PostGIS; pgvector) | The single stateful service (see below) |
| `embed` | HF text-embeddings-inference (CPU) | Local embeddings (`EMBED_MODEL`, default bge-small-en-v1.5/384-dim; 1GB mem cap); model swap = env change + re-embed job |
| `supervisor` | Minimal socket-mounted service | Host control: stack status/restart, log streaming, update orchestration (see Operations) |

This is the core subset. Other always-on services (`searxng` + `reader` for the
web tools, `wall` for the display, `tts-stt` for speech, among others) run stock
too, and an **opt-in fleet** lives behind compose profiles — the on-box model
services (`local-llm`, `comfyui`), the coding sandbox (`jcode`), the
family-location spine (`mqtt` + `mqtt-ingest`), and the tunnel (`cloudflared`).
`SERVICES.md` is the full inventory; this table deliberately isn't.

Attachments are content-addressed blobs (sha256) on a disk volume behind a
storage abstraction, so S3/MinIO can replace the filesystem without touching
callers.

### One database, seven jobs

Postgres deliberately does everything stateful: relational store, vector store
(pgvector HNSW), full-text search (`tsvector`), time-series (Timescale
hypertables for GPS fixes), spatial queries (PostGIS for geofences), job queue
(`SELECT ... FOR UPDATE SKIP LOCKED`), and workflow state. MVCC means readers
never block writers. No Redis, no broker, no separate vector DB — a personal
system stays operable by one person.

## Backend

- **FastAPI + Pydantic** (async throughout), **SQLAlchemy 2 + Alembic**.
- Job queue on Postgres (a custom `SELECT ... FOR UPDATE SKIP LOCKED` queue,
  `jbrain/queue.py`) with a scheduler for nightly work.
- Document parsing: PyMuPDF for PDFs, Pillow for images.
- Config via pydantic-settings / env vars only.

### LLM adapter

All model access goes through one internal interface with two backends:
Anthropic-native, and OpenAI-compatible (xAI on a stock deploy; opt-in **on-box
models** — llama.cpp behind llama-swap on the Strix Halo iGPU — swap in per task,
see `SERVICES.md`). Every LLM task declares a **task profile** (model tier, max
cost, temperature) so cheap tasks route to cheap models and synthesis routes
to strong ones — per provider, in config. LLM calls never run in tests; the
adapter has a fake implementation.

## Frontend

React 18 + Vite + TypeScript PWA, **mobile-first**: a persistent home stream, a
segmented **omnibox** (capture a note or talk to an agent), and a swipe-up **card
launcher** for every other screen. `vite-plugin-pwa` (Workbox service worker),
plain CSS, Biome + Vitest; `leaflet` for maps, `@xterm/xterm` for the jcode
terminal, `katex` for chat math. Offline note capture via an IndexedDB outbox
that syncs on reconnect (idempotent on `client_id`). The api client is a single
hand-written fetch wrapper (`frontend/src/api/client.ts`) — OpenAPI-generated
types are a future step, not yet in place. The full screen inventory is in
`SERVICES.md`.

PWAs cannot do continuous background location; GPS tracking uses the custom
**JBrain360 Android app**, which posts batched fixes to the authenticated
`/api/owntracks` ingestion endpoint (OwnTracks-shaped) — see `SERVICES.md`.

## Security model: subjects, principals, domains

- **`subjects`** — who/what data is *about* (me, Dad, Mom, tracked devices).
- **`principals`** — who can *act*: the owner, device keys (phones, panels),
  jcode share links, intake links, debug capability tokens and external-LLM keys.
- **`domains`** — information firewalls (`general`, `health`, `finance`,
  `location`, …). Every note, chunk, fact, entity, wiki article, and
  structured record carries a `domain_id`.

Enforcement is **Postgres Row-Level Security**: every session carries a
domain-scope set (session GUC) and RLS filters every query at the database
layer — application bugs cannot leak across domains. The owner's sessions
carry all scopes (everything visible by default); tokens and device keys are
scoped to (subject, domain). Wiki builds run per-domain, so a health fact can
never be cited in a general article. Every new table ships with RLS tests
proving a scoped session cannot see other domains' rows.

The scope set is an *owner* narrowing, never a grant to anyone else: `app.has_domain_scope`
holds for the full owner, or for an owner narrowed (`owner_scoped`) to that domain, and for
no other kind (security round 2, migration 0247). A non-owner kind's `domain_scopes` claim
counts only where a policy names that kind through `app.has_kind_scope(kind, domain)`:
`device_key` + `location` on the tables a phone's fixed queries touch (its own fixes and its
family's, geofence state and fences, shared places, its FCM token, the who-saw-whom audit,
and a SELECT-only arm on `events` for geofence crossings it may see), and `jmolt_observer` +
`jmolt` on the five `jmolt_*_read` policies. So a phone reads and writes no location note,
chunk, entity or fact, and a panel (a `device_key` with no scope) earns none of the location
arms. `tests/integration/test_rls_catalog_pg.py` fails on any table in `app`/`public`
without RLS enabled, forced, and at least one policy (exempt: `alembic_version`,
PostGIS's `spatial_ref_sys`). The workflow dispatcher runs a stamped job with owner
identity, so it entitles only an owner-stamped event (`authorize_domain`); geofence
transitions are emitted owner-stamped, the device named in the payload.

RLS protects rows, not the side effects around them (loading a model, spending an
API key, writing a blob, reading a credential under an owner context), so the HTTP
layer gates by principal kind as well. Only four kinds ever hold a session cookie —
`owner` (login, which accepts the owner key alone), `device_key` (`/session/mint`),
`jcode_share_link` and `intake_link` (their redeem routes) — and the cookie lookup
admits no other. Owner surfaces are owner-only at the router (`owner_only`); a
non-owner cookie reaches only the routes built for it (the member dashboard, the
basemap tiles, the intake stepper, its own jcode session). The route-matrix test
(`backend/tests/unit/test_route_auth_matrix.py`) presents every non-owner kind to
every route and fails on any (route, kind) pair not on its allow-list.

A WebSocket authenticates on its handshake, so the long-lived ones (the live location
feed, the jcode and jlaunch terminals) also re-resolve their handshake session every
~30 s (`api/ws_reauth.py`), idle or not, without stamping `last_seen_at`, and close
4401 once it is revoked or lapses (1011 if three checks in a row cannot be made; a single
failed check, a DB blip, is retried on the next tick).
The live feed re-asks `viewer_may_see` after each re-check, so a member taken out of a
family group stops receiving that subject's fixes. Revoking a jcode share closes its
open terminals at once and rotates the session's preview URL; the session's processes
keep running, since the owner shares that shell.

Phones and room panels are the same `device_key` kind; the device subject's `device_role`
(migration 0211: `NULL` a phone, `jpet`/`display` a flashed panel) is what splits them, and the
auth lookup filters on it. Panel routes (`PanelDep`) and the panel socket take only a role'd
key; OwnTracks and `/session/mint` take only a role-less one, so a family phone cannot act as a
bedroom panel and a panel lifted off a wall cannot open a member dashboard. MQTT takes either
(both are held to the same ACL). The api logs `panel_auth.unroled_panels` at startup if a
role-less key has ever reported panel status.

The client address behind Caddy is `client_ip` (`backend/src/jbrain/api/client_ip.py`): Caddy
overwrites `X-Real-IP` with its peer, the api believes it only when the TCP peer is the `proxy`
container, and reads `Cf-Connecting-IP` only when that peer is the `cloudflared` container
(both resolved by service name, off the request path). Anything else — a sandbox container
reaching the api directly, a LAN client forging Cloudflare's header — is its socket address.
Every per-client rate limit and the panel nudge key on it. A member's `/member/positions`
read is audited ("viewed") only for a subject `viewer_may_see` admits, and never for itself.
The database holds both ends of that: `viewer_may_see` and `visible_subjects` answer a non-owner
only about its own subject, and `view_audit` refuses a member's row about a subject it may not see
(migration 0243).

The rare **public** surface (a report share link, `/api/research-share`) is not
an exception to this — it is another RLS scope. A share visitor runs under an
empty-scope, non-owner context pinned to one share-link id; a row-scoped policy
(`research_reports_share`, migration 0150) grants read to exactly that link's
report or its folder's current members, and nothing else. The token is only the
key that resolves the pin — the database, not the endpoint, decides what the
visitor may read. A chat share link (`/api/chat-share`, migration 0238) is the
same shape: `agent_sessions_share`, `agent_turns_share` and `runs_chat_share`
admit exactly the pinned link's one chat — its session row, turns and runs, read
as they stand at each load — and none of its notes, attachments, generated
images or sub-agent children. Its tool steps travel verbatim, so the mint refuses
(409) a chat whose stored turns read a firewalled domain, mail or memory — or whose
current scope can — until the owner confirms (`chat_shares.share_sensitivity`).
JShare (`/s/<slug>/`, migration 0252, `docs/runbooks/JSHARE.md`) is the same shape with
no resolve step: the slug in the path is the pin, and `jshare_items_public_read` admits the
one item it names. It is the one public surface outside `/api` and the one that serves code
the box did not write, so every response (but a PDF's, which viewers refuse sandboxed)
is CSP-sandboxed to an opaque origin and Caddy strips the cookie on the way in.

## Knowledge pipeline

```
note saved → event → extraction (attachments) → multi-granularity chunking
  → embeddings + tsvector → pending_integration
  → note_converse: the note is turn 0 of an agent conversation about it. The
    agent resolves entities and closes a READING of the note through tools;
    the deterministic core commits it (domain floor/ratchet, per-kind
    supersession, citation anchoring) → facts & entities (cited, firewalled)
```

- **Chunks** are multi-granularity (paragraph-level for precision,
  section-level for context) with offsets back into the source note.
- **Hybrid search**: pgvector dense + Postgres FTS, fused with Reciprocal
  Rank Fusion, always domain-scoped. A reranker container can be added later
  if quality demands.
- **Facts** carry `superseded_by` chains. Conflicts resolve newest-wins
  automatically and the pair lands in the review inbox with both citations.
  Superseded facts stay queryable for citation integrity. The AGENT says what the
  note means; the deterministic core *commits* it, enforcing the domain/subject
  firewalls and validating identity links before any write — `supersession.decide()`
  is never a model-facing verb, and what it did unasked comes back as the tool's
  result for the agent to act on.

## Wiki

> **Build plan: `docs/plans/PHASE6_WIKI_PLAN.md`** (+ `docs/archive/PHASE6_WIKI_GRAPH_CONTRACT.md`). Where this
> section differs, the plan governs: split/merge **thresholds** are vestigial under the
> entity-driven model (the *article* restructure follows the owner-approved **entity**
> merge/split — not a separate review-inbox approval); citations are FKs to **chunks**
> (rendered as their note); and the "elevated extraction weight" below is a **not-yet-built
> Phase-6 prerequisite**, not existing behavior.

Articles, revisions, and citations are Postgres rows; citations are foreign
keys to facts/chunks — enforced data, not markdown convention. The wiki is
**machine-written only**, governed by an editorial config (style guide,
citation-density requirements, split/merge thresholds) stored as data, not
code.

**Incremental builds**: a wiki index (per-article summary + embedding) is
maintained. The nightly job takes only facts created/superseded since the
last run, matches them against the index via hybrid search, and a cheap
triage call per cluster decides update / create / split / merge / ignore.
Only affected articles are rewritten as new revisions. Cost scales with the
day's notes, not corpus size. Split/merge actions require owner approval via
the review inbox.

**Correction loop**: the owner never edits articles. A "discuss this article"
chat, anchored to a revision, produces a **correction note** — a first-class
note citing the disputed revision — which flows through normal ingestion with
elevated extraction weight and queues the article for the next pass.

## Structured records

Not everything is free text, but everything traces to a note:

- **Lists**: `lists` / `list_items`, manipulated by agent tools, pinned in
  the PWA.
- **Lab results**: typed rows (test, value, unit, reference range, date)
  extracted from lab-report attachments, each citing its source note.
  `health` domain.
- **Appointments**: proposed from notes during integration (the Integrator
  agent + arbiter, surfaced via the review inbox), managed by agent tools,
  published as a read-only **ICS feed** the phone's native calendar subscribes
  to.
- **Location fixes**: Timescale hypertable, per-subject, written by OwnTracks
  device keys; PostGIS geofence transitions emit workflow events.

## Workflow engine

Custom, on Postgres: `events` → `triggers` → `pipelines` (stored definitions
of action sequences) → `runs` (full execution logs). Everything that happens
emits an event — note created, schedule fired, geofence crossed — and the
ingest and wiki pipelines are themselves pipeline definitions, proving the
engine on real load.

## Review inbox

One unified queue in the PWA for everything needing human judgment: fact
conflicts, proposed appointments, wiki split/merge approvals, extraction
corrections. Badge count on the bottom nav; each item resolvable in a tap or
two. Rejecting a fact drafts a correction note.

## Operations

### Install

`install.sh` bootstraps a barebones Ubuntu host: installs Docker Engine +
compose plugin and git, places the source tree at `/opt/jbrain2/src` (copied
from the clone it runs from, or cloned fresh when piped), prompts for the
domain, access mode (direct Let's Encrypt or Cloudflare Tunnel), and LLM API
key(s), generates all internal secrets into `.env`, **builds the images from
source**, and brings the stack up. No registry account is required — only
public base images are pulled.

### Owner key

The root credential is a generated **owner key** (256-bit, word-grouped for
transcription), displayed exactly once at install and stored only as a hash —
there is no login/password and no email recovery. Pasting the key on a device
creates a long-lived device session; the key then goes back on paper.
Recovery is `jbrain reset-owner-key` over SSH — shell access to the host is
the root of trust. Passkeys may later be added as per-device conveniences
derived from an owner-key session, never as a replacement root.

### Supervisor

The api container never mounts the Docker socket (socket access is
root-equivalent and the api is the internet-facing surface). The
`supervisor` container holds the socket, lives only on the internal network,
and speaks a fixed command set — `status`, `restart`, `start`, `stop`, `logs`,
`update`, `rebuild`, `provision`, `export`, `import`, `reset` (each long-running
command paired with a `/status` poll) — authenticated by an internal token. No free-form commands,
no shell passthrough. The PWA's **Ops screen** (owner sessions only) shows
per-container health, restart buttons, live log tails (SSE over
`docker logs -f`), and the update panel. Stack restarts bounce all peers
first and the supervisor re-execs itself last; `jbrain` over SSH is the
same code path when the stack is too wedged for the UI.

The supervisor also **watches the api and restarts it when it stops answering**
(`supervisor/src/supervisor/watchdog.py`). This is the one recovery path that does not
run *through* the api: the PWA's restart control posts to `/api/ops/restart` and the
debug console is mounted there too, so the component whose failure most needs recovering
is the one whose recovery path dies with it. `restart: unless-stopped` does not cover it
— that acts when a process EXITS, and the failure seen in practice was a process still
running and holding its socket while serving nothing (the api's own metrics sampler
recorded a 328-second gap while the edge answered 530; Docker saw a healthy container
throughout). Putting the watch in the supervisor adds **no externally reachable surface
and no new credential** — it already holds the socket, is already always-up, and is
already internal-only. Three guards keep it from being worse than the problem:
consecutive failures rather than one (a slow probe during a model load is normal), never
during a one-shot (an update stops the api on purpose, and an unreadable one-shot state
fails closed), and a cooldown so a crash-loop surfaces as a crash-loop instead of being
restarted every minute.

### Updates

Deployments build from source: `jbrain update` (SSH) and the **Ops screen's
one-tap "Update"** both run backup → git pull → image rebuild →
Alembic migrations → restart. The PWA path works by the supervisor spawning
a **detached one-shot updater container** (docker:cli, project dir mounted
at its host path) that survives the stack — supervisor included —
restarting beneath it; the Ops screen polls its status and log tail,
tolerating the api's brief restart window. Updates are **prompted** — never
unattended — and must migrate before they restart. The Ops screen's **Data
card** reuses the same one-shot pattern for whole-system **export/import**:
export bundles a pg_dump + the blob volume + a manifest into one
`.jbrain.tar` the browser downloads; import uploads an archive through the
api's backups mount, takes a safety backup, then a one-shot stops the
writers, pg_restores, replaces the blobs, and restarts the stack. One-shots
(update, export, import) are mutually exclusive. CI still publishes GHCR
images (`edge` on main, `stable` + semver on tags) as build provenance and
as an optional pinned-image escape hatch (`docker compose pull` with image
overrides), but installs do not depend on a registry.

## Agent

A tool-calling personal agent over the LLM adapter: hybrid search, read
note/entity/fact, manage lists and appointments, propose edits (as correction
notes). Tools respect the session's domain scopes via the same RLS plumbing.
Phone chat UI is a primary interface, not an afterthought.
