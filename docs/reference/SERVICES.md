# JBrain2 — Services & components map

> **Status:** Living · **Last verified:** 2026-09-01

The concrete inventory of everything the box runs and everything baked into it:
the Docker containers, the two apps (the PWA and the JBrain360 Android client),
the on-box GPU model services, and the functions that ride on top (the agent,
the knowledge pipeline, the workflow engine, the wiki). Where `ARCHITECTURE.md`
explains the *design* and the *why*, this is the *what's actually here*.

Everything is one Docker Compose stack (`deploy/docker-compose.yml`, project name
`jbrain`) on one Ubuntu host. Most services are internal-only; only `proxy`
(80/443) and the `wall` display (LAN :8800) publish a port.

## The stack at a glance

**Core — always on (no profile):**

| Service | Tech | Role | Net |
|---|---|---|---|
| `proxy` | Caddy | TLS termination (Let's Encrypt direct, or plain HTTP behind the tunnel), serves the built PWA, routes `/api`, LAN HTTPS for `jbrain.local`, jcode-preview wildcard. Ports 80/443. | edge |
| `api` | FastAPI (async) | The REST API, auth, CRUD, search, agent chat. The internet-facing surface — never mounts the Docker socket. | edge, internal, jcode, jlaunch, render |
| `worker` | same image as `api` | Postgres job-queue consumer: extraction, chunking, embedding, analysis, wiki builds, the scheduled sweeps. | internal |
| `db` | TimescaleDB-HA (Postgres 17 + Timescale + PostGIS + pgvector) | The single stateful service — relational + vector + FTS + time-series + geo + job queue + workflow state. | internal |
| `embed` | HF text-embeddings-inference (CPU) | Local embeddings (`bge-small-en-v1.5`, 384-dim, 1 GB cap). Model = env var; swap ⇒ re-embed job. | internal |
| `supervisor` | minimal socket-mounted service | Holds the Docker socket; a fixed command set (status/restart/start/stop/logs/update/rebuild/provision/export/import/reset, plus `/metrics` and `/usb` host reads) behind an internal token. Drives the Ops screen. | internal |
| `searxng` | SearXNG | Self-hosted metasearch backing `jerv`'s `web_search` (general, + infoboxes/instant answers), `news_search` (news category, dated leads), and `science_search` (science category, paper leads). Reached only by the KB-blind `jerv` and the jcode search bridge (the sandbox has no route here — the api brokers). | internal |
| `reader` | headless-Chromium reader (r.jina.ai-compatible) | `web_fetch` fallback renderer for bot-walled / JS-only pages. | internal |
| `byparr` | Byparr (stealth headless browser, FlareSolverr-compatible) | `web_fetch`'s challenge-solver leg (direct → reader → byparr → Tavily): solves bot-wall challenges and returns the solved HTML; a genuine miss falls through to Tavily. See `../plans/CHALLENGE_SOLVER_PLAN.md`. | internal |
| `rapidocr` | RapidOCR (PP-OCR / ONNX, CPU) | Deterministic OCR: cross-validates the VLM `vision.ocr` extraction (stores a `tool="rapidocr"` row) and backs the direct `ocr` tools (jerv + the jcode sandbox, which reaches it via the api bridge). Default-on; the engine lazy-loads on first call and idle-unloads. See `../plans/RAPIDOCR_PLAN.md`. | internal |
| `htmlrender` | headless-Chromium HTML→PNG renderer | Renders model-authored HTML (the canvas `html` op — flowcharts, tables, report cards) to pixels so the PWA never receives live model DOM; Chromium idle-unloads. Egress-free by network topology. See `../plans/AGENT_CANVAS_PLAN.md`. | render |
| `jlaunch` | `jlaunch` control server | Self-serve launcher for long one-shot scientific computations (first spec: the Erdős–Straus census to 10¹²). Clones a code-defined repo read-only, runs it as a supervised job with a live terminal + start/stop/kill, collects the artifact, and mints a public `/results/{token}` share page. Three Erdős–Straus specs ship: the Python census (clones the repo) and **native Rust reruns to 10¹² and 10¹³** (`es-census`, built from `research/es-census` and baked into the image — no clone) whose windowed SPF factorization is complete to √a (fixing the Python path's non-minimal R above ~1.3×10¹¹) and which stream output in bounded memory so they scale past 10¹². Default-on; isolated `jlaunch` network, CPU/mem uncapped by design (meant to use the box for hours) — the Python run's worker pool is sized to RAM, not core count, so it doesn't OOM. See `../archive/JLAUNCH_PLAN.md`. | jlaunch |
| `wall` | stdlib Python | Unauthenticated **neural-wall display** for the host's own monitor / a LAN kiosk — host vitals only (GPU %, RAM, power), no DB, its own LAN port :8800; forwards read-aloud to `tts-stt`. | internal |
| `tts-stt` | whisper.cpp + kokoro | The box's **speech I/O**: warm text-to-speech (:8801, the read-aloud renderer — baked-in Kokoro-82M voices) + whisper.cpp speech-to-text (:8080). Default-on; the Kokoro voices ride the image build, so no provisioning step — the STT model is the one opt-in (`jbrain enable-whisper`). Kokoro is the sole on-box engine; the browser's native voice is the only fallback. | internal |

**Opt-in — compose-profile guarded (never start on a stock deploy):**

| Service | Profile | Enabled by | Role |
|---|---|---|---|
| `cloudflared` | `tunnel` | `install.sh` (dial-out tunnel mode) | Cloudflare Tunnel connector — public reachability with no static IP / port-forward, works behind CGNAT. See `../runbooks/CLOUDFLARE_TUNNEL.md`. |
| `local-llm` | `local-llm` | `jbrain enable-local-models` | llama-swap fronting llama.cpp (Vulkan) — several GGUF models on one OpenAI-compatible endpoint, loaded/swapped on demand. |
| `comfyui` | `comfyui` | `scripts/comfyui-setup.sh` | ROCm ComfyUI serving Qwen-Image (gen + edit) for the Images launcher (`api/images_render.py`). |
| `jcode` | `jcode` | `scripts/jcode-setup.sh` | Sandboxed coding sessions: xAI's Grok Build (`grok`) CLI against on-box models. `grok`'s `/model` switches live between every installed tool-capable model (plan on the reasoner, execute on the coder) via the api's residency-aware jcode proxy (`api.jcode_llm`), which evicts-to-budget and serializes swaps so one model loads at a time — no unified-memory thrash. KB-blind, isolated `jcode` network, resource-capped. See `../archive/JCODE_PLAN.md`. |
| `mqtt` | `mqtt` | JBrain360 setup | Mosquitto + go-auth broker (auth delegated to the API's `/internal/mqtt-*`) — the secure spine for family location. |
| `mqtt-ingest` | `mqtt` | (with `mqtt`) | Server-side subscriber streaming published OwnTracks fixes into the location hypertable. |
| `sdr` | `sdr` | plugging a dongle in + `Ops → Update` | Software-defined radio: `librtlsdr`/`rtl_fm`/`rtl_power` over one USB tuner, `/dev/bus/usb` passed through and the radio selected by SERIAL (devnum moves on every re-plug). Owns the device because the box has exactly one tuner, so captures serialize behind a lock and a second caller is told it is busy rather than queued. **Egress-free** on its own `radio` network (`internal: true`) — only `api` joins it. The update path blacklists and unbinds the kernel DVB driver that otherwise claims the dongle. See `../plans/SDR_RADIO_PLAN.md`. |

**STT model — opt-in, but _not_ profile-guarded:** the `tts-stt` container is
default-on (read-aloud / Kokoro TTS is always available); it is *not* a compose
profile. Only its whisper.cpp speech-to-text GGML model is a heavy opt-in
download — `jbrain enable-whisper` (`scripts/whisper-setup.sh`) fetches the model,
writes `whisper-models/llama-swap.yaml`, sets `WHISPER_URL`, and force-recreates
the always-on service so STT starts alongside Kokoro TTS. Until then the entrypoint
runs TTS only, so a stock box still serves read-aloud.

**One-shot (`tools` profile):** `migrate` (`alembic upgrade head`, the only container with DDL rights) · `wipe` (destructive first-install reset, double-guarded).

**Networks:** `edge` (proxy ↔ api ↔ tunnel) · `internal` (the shared backbone) · `jcode` (isolates the arbitrary-code sandbox — only `jcode`, `local-llm`, and `api` join it; no route to `db`/`worker`/`supervisor`/blobs) · `jlaunch` (isolates the compute job — only `jlaunch` and `api` join it; the artifact crosses into the blob store via the api, so jlaunch needs no route to `db`/blobs) · `render` (egress-free — `internal: true`, no gateway — so the model-authored HTML that `htmlrender` executes cannot reach off-box; only `htmlrender` and `api` join it).

**Volumes:** `blobs` (content-addressed attachments) · `db_data` · `caddy_data`/`caddy_config` · `embed_models` · `tiles` (basemap cache) · `jcode_work` (per-session scratch checkouts, never backed up) · `jlaunch_work` (job checkout + scratch, kept until the owner deletes the run) / `jlaunch_artifacts` (both never backed up). Host binds: `./backups`, `./local-models`, `./comfyui-models`, `./whisper-models`, `./searxng` (SearXNG config), `./db-init` (Postgres init scripts), `./mosquitto/mosquitto.conf`, and the read-only source mounts `./src/deploy/wall` + `./src/deploy/tts-stt`.

## The on-box GPU / local-model side

Three optional services share the host's single AMD **Strix Halo** iGPU
(`gfx1151`) — each joins the host's `video`/`render` GIDs to open
`/dev/dri/renderD128`, runs `seccomp=unconfined`, and is off unless the operator
opted in. Full runbook: `../runbooks/STRIX_HALO_SETUP.md`; prompting behaviour:
`MODEL_PROMPTING.md`.

- **`local-llm`** — Vulkan (RADV) llama.cpp under **llama-swap**, which loads a
  GGUF on first request. Every model is a **non-swapping group member**, so the
  gateway never auto-evicts — the **app** (`jbrain.llm.residency`) is the sole
  evictor, freeing the fewest models to hold a free-RAM floor before each load and
  restoring what a displacement removed (the old all-or-nothing ~91 GB pin froze the
  box). Serves the text tiers only — transcription is the `tts-stt` service
  below. The api hot-reloads its config after a context-window edit.
- **`comfyui`** — ROCm (needs both `/dev/kfd` and `/dev/dri`, plus
  `HSA_OVERRIDE_GFX_VERSION`) serving Qwen-Image / Qwen-Image-Edit, with a
  Lightning fast path. Emits live `b_preview` frames so the chat shows a
  progressive image. See `../archive/IMAGE_GEN_*_PLAN.md`.
- **`tts-stt`** — whisper.cpp behind its own llama-swap (plus warm TTS) so transcription
  works without local LLMs; load-on-demand, unload-after. Read-aloud renders with **Kokoro-82M**
  (Apache-2.0, natural) — the sole on-box engine, baked into the image and offered as
  `kokoro-<voice>` picks in Settings via the warm-model seam, no provisioning step. A box
  without the Kokoro weights simply lists no Kokoro voices and read-aloud falls back to the
  browser's native voice (the only fallback).

Stock deploys route LLM calls to the cloud (Anthropic / xAI) through the LLM
adapter; the local services are an opt-in swap, chosen per task in **LLM
Settings**.

## The apps

### PWA — the owner app (`frontend/`)

React 18 + TypeScript on **Vite**, an installable **offline-first PWA** (Workbox
service worker, `autoUpdate`; hourly foreground update check). Auth is an
httpOnly session cookie; any 401 drops to login. Mobile-first: a persistent home
stream + segmented **omnibox** (capture a domain-tagged note *or* talk to an
agent), a swipe-up **card launcher**, and slide-in reading layers
(note → entity → wiki). Offline capture uses an **IndexedDB outbox**;
`POST /api/notes` is idempotent on `client_id`, so an interrupted sync just
re-sends. The api client is a single hand-written fetch wrapper
(`frontend/src/api/client.ts`); streaming (agent/intake chat) is SSE, live logs
and location are `EventSource`/WebSocket.

It is a **multi-entry build** — three separate bundles plus two guest surfaces:

| Bundle / surface | What it is |
|---|---|
| Owner app (`index.html`) | The full PWA below. |
| **JBrain360 dashboard** (`dash.html`) | Standalone location-only surface loaded in the Android app's WebView: live family map, person switcher, trail/heat history. |
| **Debug console** (`debug-console.html`) | Token-authed, throwaway debugging page (no service worker). See `../runbooks/DEBUG_ACCESS.md`. |
| `/jcode/s/{sid}` | Scoped guest view of a single shared code session. |
| `/results/{token}` | Public results page for a finished jlaunch run — headline block + machine specs + artifact download, no login. |
| `/intake/...` | Guest guided-intake stepper (redeems a link secret, submits a conversation). |

Owner-app screens, grouped:

- **Knowledge** — Home stream + omnibox, Search, Note view + Analysis tab, Entity page / Entity list / ego-Graph, Wiki landing + reader + Talk, Review inbox.
- **Authoring / agent** — Full Brain / Research chat (the persona surfaces, with Sessions + Proposals side panels), Lists + list detail, Calendar/Appointments, Image gen/edit, Tasks (scheduled agent runs), Intake links.
- **System** — Ops (health/metrics/restart/logs/update/export/import), Automations + Runs (the workflow surface), Data, Location (Devices/Timeline/Map, pairing, geofences, digest), Settings, LLM Settings, jcode launcher + session (xterm terminal + dev-server preview), Math launcher + job (Overview/Terminal/Result tabs — live xterm, start/stop/kill, sharelink).

### JBrain360 — the Android location client (`android/`)

A native **Kotlin** app (label "JBrain360", `minSdk 26`), sideloaded as a
debug-signed APK (CI's rolling `android-latest`) — **not** the note app; it only
reports location. **No Google Play Services** (uses the platform FUSED provider)
and **no Firebase/FCM**. One universal APK learns its server from the pairing
payload.

- **Sampling** (`LocationService`, a foreground service): motion-adaptive
  cadence via `SamplingPolicy` (moving ≈ every 5 s / 8 m; stationary relaxed,
  with hysteresis + a 15-min parked heartbeat); a 50 m accuracy gate.
- **Upload**: kept fixes go to an on-disk NDJSON queue, drained oldest-first in
  batches — a network lapse backfills in order with real capture times.
- **Transport**: plain **HTTPS POST to `/api/owntracks`** (an OwnTracks-shaped
  JSON array), auth = the device key as HTTP Basic password. **No MQTT / no
  `:8883` in the app** — the `mqtt` broker profile is the *server-side* spine;
  this client is discrete HTTPS requests. Pairing redeems a code at
  `/api/pairing/redeem`; the WebView session is minted at `/api/session/mint`.
  The key lives in Keystore-backed `EncryptedSharedPreferences`.

### JBrain — the Android owner app (`android/`, `OwnerActivity`)

A second launcher activity in the same APK (label "JBrain") that hosts the **owner
SPA** (the server root) in a WebView — distinct from the location-only dashboard
above. Its reason to exist is **deterministic back**: the system back button is a
native callback (predictive-back on 13+), so it climbs the page's own layer stack via
a `window.__jbrainBack()` bridge and **backgrounds** the app (`moveTaskToBack`) when
nothing is open, never exiting — which the PWA's History-API trap can't guarantee on
Android's gesture back. The owner signs in through the web page (owner key), so there
is **no native key/cookie mint** here; setup only captures the server URL, and
off-origin links open in the system browser.

- **Notifications relay** (`NotificationRelayService`, a `dataSync` foreground
  service): holds one authenticated **SSE** connection to `/api/notifications/stream`
  (auth = the WebView session cookie) and posts each event as a local Android
  notification. Self-hosted end to end — **no Firebase/FCM**, content flows straight
  from the owner's own server to the owner's own device; it reconnects with backoff.

## Functions baked into the box

### The agent (Full Brain) — personas & tools

Personas (`backend/src/jbrain/agent/agents.py`, each a `.prompt` sidecar); an
`AgentProfile` = system prompt + tool allowlist + `reads_knowledge_base`:

| Persona | Role | Scope |
|---|---|---|
| **curator** | Default Full Brain agent — the **only** KB-reading persona | Every in-scope knowledge tool, RLS-narrowed to the session's domains. |
| **teacher** | Socratic tutor | No tools, no retrieval. |
| **jerv** | Sandboxed web chatbot (the approved web-egress exception) | Web + weather/hurricane + image/media + `spawn_subagent` + host metrics. **No KB.** |
| **archivist** | Gmail triage/organizer | `gmail_*` + an owner-only cross-session memory. **No KB**; present only when Gmail is configured. |
| **intake** | Guided-intake interviewer, run by a **non-owner** | **No tools, no KB** — capture is the server's job. |
| research / review / summarize | The closed sub-agents `jerv` can spawn | Web-only or no tools; always leaves. |
| research_scout / research_fetch / research_deep | The deep-research gather tiers (scout searches only, fetch opens only, deep may decompose one sub-fan) | Web-only; always leaves. |
| research_library / review_library · research_reports / review_reports | The corpus twins of research/review — video-library and stored-report reads instead of the web | Corpus reads only; always leaves. |

Tools are `.tool` files (`backend/src/jbrain/agent/tools/`) with handlers in
`*tools.py`, assembled by `toolregistry.py`. Groups: **knowledge read**
(`search`, `read_note`, `read_entity`, `find_entity`, `read_wiki`) · **staged
graph/wiki writes** (`propose_correction`, `propose_merge`, `relate`,
`file_correction`, `request_rebuild` — never direct edits) · **episodic memory**
(`remember`/`recall`) · **lists** · **appointments** · **location** (firewalled:
`where_is`, `location_history`, `nearby_now`, `save_place`, …) · **weather**
(`weather` forecast + `weather_history` archive — full past-weather aggregates with
on-box heat-index compute) **/ hurricane** ·
**image** (`analyze_image` — a vision read; generation/editing lives in the
Images launcher, `api/images_render.py`, not in an agent tool) ·
**media** (`transcribe`, `analyze_video`, `analyze_stream` — the last reads a video
URL, live or on-demand, via yt-dlp + ffmpeg; a second SSRF-guarded outbound leg) ·
**Gmail** (`gmail_*`) · **web**
(`web_search`/`web_fetch`) · **sub-agents** (`spawn_subagent`) · **planning**
(`read_plan`/`write_plan` — an owner-approved, per-conversation plan jerv executes
across turns; owner-initiated, owner-only approval, auto-continued between steps; see
`../archive/JERV_PLANNING_TOOL_PLAN.md`) · **deep research** (`deep_research` /
`deepest_research` / `decompose_research` / `deep_produce` — the multi-agent
fans) · **research reports** (`research_report` + show/remove — the stored
report library) · **news / reference** (`news_search`, `science_search`,
`news_feed`, `grokipedia`, `public_records`, `portal_search`) ·
**canvas / draw** (`canvas` + `show_canvas`, the `render_chart` /
`render_bars` / `render_html` pixel renders) · **external video**
(`external_video` + show/remove, `check_channel`) · **ocr** (deterministic
RapidOCR text extraction) · **health
lookups** · **host telemetry** (`query_server_metrics`) · `current_time`.

### Knowledge pipeline (`backend/src/jbrain/analysis/`)

`note saved → extraction (+ attachments) → chunking → embeddings + tsvector →
pending_integration → integrate_note`. `integrate_note` runs
**extract → Integrator** (graph-aware LLM judgment against existing
entities/facts) **→ arbiter** (deterministic: commit vs. hold, enforcing the
domain/subject firewalls) **→ apply** (layered entity resolution: exact alias →
relationship hop → embedding → one batched `entity.disambiguate`; fact upsert;
two-tier predicate canonicalization). **Supersession** retires prior functional
facts (newest-wins); held / ambiguous / low-confidence / truncated items land in
the **review inbox**. **Hybrid search** (pgvector dense + FTS, RRF-fused,
always domain-scoped) backs the `search` tool. See `ANALYSIS.md`, `entity.md`.

### Workflow engine (`backend/src/jbrain/workflow/`)

The Phase-5 `event → trigger → pipeline → action → run` spine on Postgres.
`events.py` emits, `dispatcher.py` fans to enabled triggers (fail-closed domain
auth, registry-only actions), `scheduler.py` is the time-driven twin, `runlog.py`
is the run log, `automations.py` projects it into the Ops "Workflow" screen with
enable/disable. Seeded actions: `ingest_note`, `embed_note`, `integrate_note`,
`ocr_attachment`, `consolidate_predicates`, `sync_predicates`. In-code scheduled
**sweeps** (schedules seeded, mostly disabled, Ops-fireable): the reconciler
backfills, `purge_deleted_artifacts`, `geofence_sweep`, the hygiene trio
(`entity_hygiene` / `reembed_stale` / `tag_consolidate`), `triage_inbox`, and the
wiki actions (`wiki_refresh` / `wiki_rebuild` / `wiki_reindex` / `wiki_prune`).

### Wiki (`backend/src/jbrain/wiki/`) — Phase 6, in progress

Machine-written only. `WikiBuilder` scans the dirty-bit, sources each entity's
citable facts, writes type-guided single-domain sections as append-only
revisions with clause-level citations + wiki links, per-section embeddings, and a
lead blurb. Prose comes from an injected `Rewriter` (stub in tests, `LlmRewriter`
live behind a grounding gate + build budget). **Talk** is an owner-only editorial
board per article; the Editor agent can enact corrections *only* through the
sanctioned write tools — corrections flow through notes, never direct edits. Plan:
`../plans/PHASE6_WIKI_PLAN.md`.

### Structured records

Everything traces to a note: **lists** (`lists`/`list_items`, agent-managed) ·
**appointments** (proposed during integration, published as a read-only **ICS
feed**) · **lab results** (typed rows from lab attachments, `health` domain) ·
**location fixes** (Timescale hypertable per subject; PostGIS geofence
transitions emit workflow events).

## Operator surface

- **`deploy/install.sh`** — barebones Ubuntu → running stack: installs Docker,
  places the source at `/opt/jbrain2/src`, prompts for domain / access mode
  (direct Let's Encrypt vs Cloudflare Tunnel) / LLM keys, generates secrets,
  **builds from source**, installs the nightly backup cron.
- **`jbrain`** (host CLI, `deploy/jbrain`, shares code with the supervisor):
  `status` · `restart [svc]` · `logs [svc]` · `up` / `down` · `update` (backup →
  git reset → rebuild → migrate → restart) · `reset-owner-key` · `backup` /
  `restore` · `enable-lan` · `enable-local-models [ids]` · `enable-whisper` ·
  `enable-jcode-preview [host]` · `strix-halo-host-setup`. Opt-in features off
  the main CLI: image-gen (`scripts/comfyui-setup.sh`), jcode
  (`scripts/jcode-setup.sh`), tunnel (chosen at install), and the debug console
  (`scripts/debug-connect.sh`).
- **Supervisor + Ops screen** — per-container health, restart, live log tails,
  and the update / export / import flows (a detached one-shot updater container
  that survives the stack restarting beneath it). See `../runbooks/OPERATIONS.md` and the
  `../runbooks/` set.

Owner root of trust is the printed **owner key** (hash-stored, shown once);
recovery is `jbrain reset-owner-key` over SSH. All data isolation is Postgres
**RLS** across `subjects` / `principals` / `domains` — see `ARCHITECTURE.md`.
