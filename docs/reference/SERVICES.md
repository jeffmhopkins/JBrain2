# JBrain2 — Services & components map

> **Status:** Living · **Last verified:** 2026-07-07

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
| `api` | FastAPI (async) | The REST API, auth, CRUD, search, agent chat. The internet-facing surface — never mounts the Docker socket. | edge, internal, jcode |
| `worker` | same image as `api` | Postgres job-queue consumer: extraction, chunking, embedding, analysis, wiki builds, the scheduled sweeps. | internal |
| `db` | TimescaleDB-HA (Postgres 17 + Timescale + PostGIS + pgvector) | The single stateful service — relational + vector + FTS + time-series + geo + job queue + workflow state. | internal |
| `embed` | HF text-embeddings-inference (CPU) | Local embeddings (`bge-small-en-v1.5`, 384-dim, 1 GB cap). Model = env var; swap ⇒ re-embed job. | internal |
| `supervisor` | minimal socket-mounted service | Holds the Docker socket; a fixed command set (status/restart/start/stop/logs/update/rebuild/provision/export/import/reset) behind an internal token. Drives the Ops screen. | internal |
| `searxng` | SearXNG | Self-hosted metasearch backing `jerv`'s `web_search`/`web_fetch`. Only the KB-blind `jerv` reaches it. | internal |
| `reader` | headless-Chromium reader (r.jina.ai-compatible) | `web_fetch` fallback renderer for bot-walled / JS-only pages. | internal |
| `wall` | stdlib Python | Unauthenticated **neural-wall display** for the host's own monitor / a LAN kiosk — host vitals only (GPU %, RAM, power), no DB, its own LAN port :8800; forwards read-aloud to `tts-stt`. | internal |
| `tts-stt` | whisper.cpp + piper + kokoro | The box's **speech I/O**: warm text-to-speech (:8801, the read-aloud renderer — piper voices plus baked-in Kokoro-82M voices) + whisper.cpp speech-to-text (:8080). Default-on; both TTS engines' voices ride the image build, so no provisioning step — the STT model is the one opt-in (`jbrain enable-whisper`). | internal |

**Opt-in — compose-profile guarded (never start on a stock deploy):**

| Service | Profile | Enabled by | Role |
|---|---|---|---|
| `cloudflared` | `tunnel` | `install.sh` (dial-out tunnel mode) | Cloudflare Tunnel connector — public reachability with no static IP / port-forward, works behind CGNAT. See `../runbooks/CLOUDFLARE_TUNNEL.md`. |
| `local-llm` | `local-llm` | `jbrain enable-local-models` | llama-swap fronting llama.cpp (Vulkan) — several GGUF models on one OpenAI-compatible endpoint, loaded/swapped on demand. |
| `comfyui` | `comfyui` | `scripts/comfyui-setup.sh` | ROCm ComfyUI serving Qwen-Image (gen + edit) for the image tools. |
| `jcode` | `jcode` | `scripts/jcode-setup.sh` | Sandboxed coding sessions: Claude Code's agent engine + `grok` CLI against an on-box coder model. KB-blind, isolated `jcode` network, resource-capped. See `../archive/JCODE_PLAN.md`. |
| `claude-shim` | `jcode` | (with `jcode`) | LiteLLM Anthropic↔OpenAI translator so the Claude Agent SDK can talk to the OpenAI-speaking local gateway. |
| `mqtt` | `mqtt` | JBrain360 setup | Mosquitto + go-auth broker (auth delegated to the API's `/internal/mqtt-*`) — the secure spine for family location. |
| `mqtt-ingest` | `mqtt` | (with `mqtt`) | Server-side subscriber streaming published OwnTracks fixes into the location hypertable. |

**STT model — opt-in, but _not_ profile-guarded:** the `tts-stt` container is
default-on (read-aloud / piper TTS is always available); it is *not* a compose
profile. Only its whisper.cpp speech-to-text GGML model is a heavy opt-in
download — `jbrain enable-whisper` (`scripts/whisper-setup.sh`) fetches the model,
writes `whisper-models/llama-swap.yaml`, sets `WHISPER_URL`, and force-recreates
the always-on service so STT starts alongside piper. Until then the entrypoint
runs piper only, so a stock box still serves read-aloud.

**One-shot (`tools` profile):** `migrate` (`alembic upgrade head`, the only container with DDL rights) · `wipe` (destructive first-install reset, double-guarded).

**Networks:** `edge` (proxy ↔ api ↔ tunnel) · `internal` (the shared backbone) · `jcode` (isolates the arbitrary-code sandbox — only `jcode`, `claude-shim`, `local-llm`, and `api` join it; no route to `db`/`worker`/`supervisor`/blobs).

**Volumes:** `blobs` (content-addressed attachments) · `db_data` · `caddy_data`/`caddy_config` · `embed_models` · `tiles` (basemap cache) · `jcode_work` (per-session scratch checkouts, never backed up). Host binds: `./backups`, `./local-models`, `./comfyui-models`, `./whisper-models`.

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
  works without local LLMs; load-on-demand, unload-after. Read-aloud renders with piper by
  default; **Kokoro-82M** (Apache-2.0, more natural) is baked alongside and offered as extra
  `kokoro-<voice>` picks in Settings — the same warm-model seam, no provisioning step. A box
  without the Kokoro weights simply lists no Kokoro voices.

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
| `/intake/...` | Guest guided-intake stepper (redeems a link secret, submits a conversation). |

Owner-app screens, grouped:

- **Knowledge** — Home stream + omnibox, Search, Note view + Analysis tab, Entity page / Entity list / ego-Graph, Wiki landing + reader + Talk, Review inbox.
- **Authoring / agent** — Full Brain / Research chat (the persona surfaces, with Sessions + Proposals side panels), Lists + list detail, Calendar/Appointments, Image gen/edit, Tasks (scheduled agent runs), Intake links.
- **System** — Ops (health/metrics/restart/logs/update/export/import), Automations + Runs (the workflow surface), Data, Location (Devices/Timeline/Map, pairing, geofences, digest), Settings, LLM Settings, jcode launcher + session (xterm terminal + dev-server preview).

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

Tools are `.tool` files (`backend/src/jbrain/agent/tools/`) with handlers in
`*tools.py`, assembled by `toolregistry.py`. Groups: **knowledge read**
(`search`, `read_note`, `read_entity`, `find_entity`, `read_wiki`) · **staged
graph/wiki writes** (`propose_correction`, `propose_merge`, `relate`,
`file_correction`, `request_rebuild` — never direct edits) · **episodic memory**
(`remember`/`recall`) · **lists** · **appointments** · **location** (firewalled:
`where_is`, `location_history`, `nearby_now`, `save_place`, …) · **weather /
hurricane** · **image** (`generate_image`/`edit_image`/`analyze_image`) ·
**media** (`transcribe`, `analyze_video`) · **Gmail** (`gmail_*`) · **web**
(`web_search`/`web_fetch`) · **sub-agents** (`spawn_subagent`) · **health
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
  that survives the stack restarting beneath it). See `OPERATIONS.md` and the
  `../runbooks/` set.

Owner root of trust is the printed **owner key** (hash-stored, shown once);
recovery is `jbrain reset-owner-key` over SSH. All data isolation is Postgres
**RLS** across `subjects` / `principals` / `domains` — see `ARCHITECTURE.md`.
