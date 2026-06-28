# JBrain2 — Documentation map

JBrain2 is a personal knowledge system: notes in → RAG indexing → an
LLM-maintained wiki with notes as the sole sources of truth. This folder holds
the binding design docs. Project-wide non-negotiables live in the root
`CLAUDE.md`.

## Where the project is (2026-06)

**Phases 0–5 are shipped** — note capture,
ingestion/search, the v3 note→graph analysis pipeline, the personal agent
(tool-calling loop, Tier-A memory, Proposals/review inbox, external connectors,
the Full Brain chat surface), lists and appointments, and the **workflow engine**
(`events`/`triggers`/`pipelines`/`actions`/`runs` + scheduler + unified run-log +
the cutover of ingest/integration/consolidation onto the engine), reflexion in the
live turn, and the recurring self-heal reconcilers. The note-analysis calibration
evals run as a CI quality guard. Migrations run through 0044.

**Phase 5 is complete; next is Phase 6 (Wiki).** The self-improvement Loops 2–4
(skill learning, durable-knowledge promotion, prompt/tool self-edit) and their
eval/promotion harness were **removed** — only Loop 1 (reflexion) shipped. The
not-yet-built hygiene sweeps are deferred to Phase 6. See `ROADMAP.md`; the completed
Phase-5 build record is `archive/PHASE5_COMPLETION_PLAN.md`.

## Living reference (read these)

| Doc | What it covers |
|---|---|
| `ARCHITECTURE.md` | System shape: containers, the one-database design, the knowledge pipeline, security model, operations. |
| `ROADMAP.md` | Phase plan and current status. The source of truth for "what's next." |
| `DEVELOPMENT.md` | Binding standards: the architectural constitution, comments, testing, git, releases, `dev-setup.sh`. |
| `PROCESS.md` | Binding multi-wave execution process for plan work: parallel tasks, per-task + per-wave adversarial review, one PR per wave, the GUI mock gate. |
| `DESIGN.md` | Binding GUI design system: theming, components, navigation, the agent tool-view contract, settled UI decisions. |
| `ANALYSIS.md` | The note→fact→entity pipeline (extract → Integrator → arbiter), supersession, the review inbox. |
| `entity.md` | The entity & soft-schema model: predicates, facets, names, relationships, resolution. |
| `PREDICATE_CANONICALIZATION.md` | Embedding-assisted predicate registry + typed value shapes (core shipped; the self-improvement loop on top of it was removed). |
| `ASSISTANT.md` | The agent design — the Phase-4 core (shipped, incl. Loop 1 reflexion); the further self-improvement loops 2–4 were removed. |
| `OPERATIONS.md` | JBrain360 operator runbook: revoking a member, the encryption-at-rest compensating control, and rotating the device Keystore key + the server's pinned cert (SPKI). |
| `STRIX_HALO_SETUP.md` | End-to-end runbook for self-hosting the optional local models on an AMD Strix Halo (Ryzen AI Max+ 395) box: distro → kernel → Vulkan → install → routing. |
| `CLOUDFLARE_TUNNEL.md` | Reaching a home-network box from outside via Cloudflare Tunnel — the dynamic-IP / CGNAT path: no static IP, no port-forwarding, TLS at Cloudflare's edge. |
| `LOCAL_ACCESS.md` | Signing in on the LAN when the internet/tunnel is down: mDNS `<name>.local` + Caddy local HTTPS (internal CA) so the Secure session cookie works locally. |
| `DEBUG_ACCESS.md` | The owner debug console: a revocable, time-boxed `capability_token` that lets an external assistant iterate on prompts, run read-only SQL, read logs, and switch LLM routing on a running box. Off by default. |
| `DEBUG_ACCESS_SESSION_GUIDE.md` | Assistant-facing runbook for the debug console: how a Claude session requests a token, saves it, confirms reachability, and drives the box via `scripts/debug-connect.sh`. |
| `mocks/` | Interactive HTML UI mockups. `DESIGN.md` cites these as the **binding spec** for reviewed surfaces — a living reference, not throwaway prototypes. |

## Active plan

- `PHASE6_WIKI_PLAN.md` — the **Phase 6 (Wiki)** build plan (in progress): the
  machine-written wiki (cross-domain articles, domain-tagged sections, incremental
  nightly builder, correction-note loop, read-only UI). Owner decisions on scope +
  revision storage are settled; remaining gates are the UI mock round and a cross-stream
  citation/delta-feed contract with the entity-graph rebuild. Most of the phase is gated
  on that rebuild; only the article/index shell + UI are parallel-safe now.
- `IMAGE_GEN_PLAN.md` — the **chat image-generation** build plan (in progress): jerv's
  `generate_image` / `edit_image` tools driving a host-managed localhost ComfyUI
  (Qwen-Image-2512 / Qwen-Image-Edit) on the Strix Halo box, results stored as owner-only
  chat artifacts (never notes, never RAG). Waves G1 (backend foundation + RLS table) and G2
  (tools + by-id serving) shipped; G3 (the chat view, after the GUI mock gate — chosen
  mock C) is in progress.

- `EMAIL_ARCHIVIST_PLAN.md` — the **email archivist** build plan: a 4th Full Brain
  persona (`archivist`), shaped like `jerv`, that triages a 20+ year Gmail history
  via web-gated `gmail_*` tools over a thin httpx client. Reads no knowledge base,
  adds no table/migration/RLS surface (stateless on the box), writes only reversible
  label/archive ops (`gmail.modify` scope, no delete). Email-into-RAG is a deferred
  second step. Open owner decision: local vs cloud LLM routing for email content.
- `PHASE7_LOCATION_DETAIL_PLAN.md` — the **high-detail, low-battery trails** build
  plan: motion-adaptive sampling on the framework fused provider (no Play Services),
  an accuracy filter, and batched array upload, for Life360-grade member trails.
  Wave 0 (plan) is this doc; open decisions await owner sign-off before Wave 1.
- `JCODE_PREVIEW_HOST_PLAN.md` — the **host-served, per-session jcode preview** build
  plan: retire the per-session TryCloudflare quick-tunnel (rate-limited, public-DNS
  dependent) in favour of each sandbox session getting its own stable hostname under the
  box's **existing** named Cloudflare Tunnel + Caddy, fronted through the api↔jcode
  bridge so the sandbox stays isolated. Concurrent previews via per-session ports;
  verbose debug logging is a per-wave deliverable. Wave P0 (verbose-logging substrate) is
  landed; the five open decisions await owner sign-off before Wave P1.
- `SUBAGENT_SPAWNING_PLAN.md` — the **sub-agent spawning** build plan (scheduled,
  design-complete): `jerv` fans out web-sandboxed research/review/summarize
  sub-agents (parent-authored brief as data, child ⊆ parent, depth ≤ 2, direct
  caps-bounded fan, shared tree budget, live in-chat panel + nested session tree).
  Decomposed into waves **S1–S4** (spawn core + structural enforcement → loop
  ChatEvent channel + tree budget → live chat surface → session-tree surface).
  Both GUI layouts owner-approved; survived a three-lens adversarial review
  (record: `archive/SUBAGENT_SPAWNING_REVIEW.md`). Tree budget locked at 1.5× the
  per-turn jerv limit; remaining derived cap numbers tuned at S2. Open: the S3
  non-happy-state mock re-review.
- `JCODE_SESSION_ISOLATION_PLAN.md` — the **per-session network namespace** design
  (**PARKED** after the Wave P1 spike): give each jcode session its own `lo` so every
  session can bind the same port. The on-box spike confirmed namespace _creation_ works
  with a tailored seccomp profile, but giving an unprivileged netns _outbound_ needs broad
  privilege (`CAP_SYS_ADMIN`) or hand-rolled rootless-container networking — not worth it
  for the payoff, so the owner parked it and the Wave P0 substrate was reverted. Doc kept
  for a future revisit. Concurrent-Vite is covered by `--port $PORT`.

## Proposed (not scheduled)

`proposed/` is the icebox: forward-looking design specs kept for the record but
**not on the roadmap** — nothing built, no phase committed. See `proposed/README.md`.

- `proposed/PHOTO_ARCHIVE_PLAN.md` — the **photo archive pipeline** design spec:
  a staged, idempotent map over a decade of phone dumps — hash-keyed dedup,
  deterministic EXIF/filename dating, a vision worker (VLM) that turns pixels into
  caption/OCR/class text for the text-only 120B, CLIP search, InsightFace faces,
  and residual `infer_date`/`infer_identity` reasoning over JBrain2's RAG, surfaced
  in a browser viewer. Must reconcile with the `CLAUDE.md` non-negotiables (LLM
  adapter, storage abstraction, RLS + isolation tests) when picked up.

## Archive (history, not active)

`archive/` holds completed build plans and the design research that fed them.
Kept for the audit trail; not the place to learn the current system.

- `archive/PHASE5_COMPLETION_PLAN.md` — the Phase-5 residual-completion build plan
  (completed): reflexion in the live turn, the fed eval harness + nightly schedule,
  the last reconciler, the nits, doc hygiene, and the Phase-6 deferrals (incl. the
  Loop-4 self-edit decision).
- `archive/WORKFLOW_ENGINE_PLAN.md` — the Phase-5 workflow-engine + cutover build
  plan (completed); superseded by `archive/PHASE5_COMPLETION_PLAN.md`.
- `archive/ASSISTANT_PLAN.md` — the Phase-4 agent build plan (completed).
- `archive/INTEGRATOR_PLAN.md` — the v3 note→graph pipeline build plan (completed).
- `archive/CUTOVER_V1_REMOVAL.md` — the v1 `analyze_note` removal record (completed).
- `archive/research/` — design-research dossiers (self-improving agent, tool-use
  UX, session-panel UX, subject/object grammar, extraction fix-options).
- `archive/ui-exploration/` — early icon and entity-graph view explorations.

Still-open items from the archived plans are carried forward in `ROADMAP.md`
(Phase 5) so nothing is lost by archiving.
