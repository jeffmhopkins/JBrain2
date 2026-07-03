# JBrain2 — Documentation map

> **Status:** Living · **Last verified:** 2026-07-03

JBrain2 is a personal knowledge system: notes in → RAG indexing → an
LLM-maintained wiki with notes as the sole sources of truth. This folder holds
the binding design docs. Project-wide non-negotiables live in the root
`CLAUDE.md`.

## Where the project is (2026-07)

**Phases 0–5 are shipped** — note capture,
ingestion/search, the v3 note→graph analysis pipeline, the personal agent
(tool-calling loop, Tier-A memory, Proposals/review inbox, external connectors,
the Full Brain chat surface), lists and appointments, and the **workflow engine**
(`events`/`triggers`/`pipelines`/`actions`/`runs` + scheduler + unified run-log +
the cutover of ingest/integration/consolidation onto the engine), reflexion in the
live turn, and the recurring self-heal reconcilers. The note-analysis calibration
evals run as a CI quality guard. The migration head advances continuously — see
`backend/migrations/versions/` for the current head.

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
| `DOC_LIFECYCLE.md` | Binding process for how a doc is born, changes, and is archived alongside its feature: the two doc kinds, the state vocabulary, the freshness header, and the anti-rot rules. Enforced by the `docs` CI gate (`scripts/docs-freshness.sh`). The one-time cleanup that adopted it is `archive/DOC_CLEANUP_PLAN.md`. |
| `DESIGN.md` | Binding GUI design system: theming, components, navigation, the agent tool-view contract, settled UI decisions. |
| `ANALYSIS.md` | The note→fact→entity pipeline (extract → Integrator → arbiter), supersession, the review inbox. |
| `entity.md` | The entity & soft-schema model: predicates, facets, names, relationships, resolution. |
| `PREDICATE_CANONICALIZATION.md` | Embedding-assisted predicate registry + typed value shapes (largely superseded by `ENTITY_GRAPH_REFOCUS_PLAN.md`: new_predicate cards no longer file; the embed index serves only the held-fact suggestion picker). |
| `ENTITY_GRAPH_REFOCUS_PLAN.md` | Entity graph refocus (spine, not encyclopedia): the two-tier predicate model, salience-first prompts, n-hop neighborhood traversal. |
| `ASSISTANT.md` | The agent design — the Phase-4 core (shipped, incl. Loop 1 reflexion); the further self-improvement loops 2–4 were removed. |
| `MODEL_PROMPTING.md` | Prompting reference for the two local models (gpt-oss-120b `high`/`low`, Qwen3-VL-30B `vision`): per-tier behaviours, do/don't for `.prompt` authoring, and the sampling-config gap. |
| `OPERATIONS.md` | JBrain360 operator runbook: revoking a member, the encryption-at-rest compensating control, and rotating the device Keystore key + the server's pinned cert (SPKI). |
| `STRIX_HALO_SETUP.md` | End-to-end runbook for self-hosting the optional local models on an AMD Strix Halo (Ryzen AI Max+ 395) box: distro → kernel → Vulkan → install → routing. |
| `CLOUDFLARE_TUNNEL.md` | Reaching a home-network box from outside via Cloudflare Tunnel — the dynamic-IP / CGNAT path: no static IP, no port-forwarding, TLS at Cloudflare's edge. |
| `LOCAL_ACCESS.md` | Signing in on the LAN when the internet/tunnel is down: mDNS `<name>.local` + Caddy local HTTPS (internal CA) so the Secure session cookie works locally. |
| `DEBUG_ACCESS.md` | The owner debug console: a revocable, time-boxed `capability_token` that lets an external assistant iterate on prompts, run read-only SQL, read logs, and switch LLM routing on a running box. Off by default. |
| `DEBUG_ACCESS_SESSION_GUIDE.md` | Assistant-facing runbook for the debug console: how a Claude session requests a token, saves it, confirms reachability, and drives the box via `scripts/debug-connect.sh`. |
| `mocks/` | Interactive HTML UI mockups. `DESIGN.md` cites these as the **binding spec** for reviewed surfaces — a living reference, not throwaway prototypes. |

## Active plans

- `PHASE6_WIKI_PLAN.md` — the **Phase 6 (Wiki)** build plan (in progress): the
  machine-written wiki (cross-domain articles, incremental nightly builder,
  correction-note loop, read-only UI). Waves A–C shipped — the builder,
  `wiki_citations`/`wiki_links` graph coupling, and Talk. **Wave D (open):**
  re-enable the nightly build schedules (disabled at migration 0088),
  grounding-gate tuning, and purge→rebuild. Archives once Wave D closes.
- `JCODE_SESSION_ISOLATION_PLAN.md` — **parked** per-session network-namespace
  design. The on-box spike confirmed namespace _creation_ works, but unprivileged
  outbound needs broad privilege; the owner parked it and the P0 substrate was
  reverted. Kept for a future revisit; concurrent-Vite is covered by `--port $PORT`.
- `LOCATION_ASSISTANT_TOOLS.md` — reference catalog of candidate location tools
  (✅/🟡/⛔ triage). The ✅ spine shipped (`archive/LOCATION_ASSISTANT_PLAN.md`);
  the 🟡/⛔ items are parked ideas kept as reference.

## Proposed (icebox)

`proposed/` holds forward-looking design specs kept for the record but **not on
the roadmap** — nothing built, no phase committed. See `proposed/README.md`.

- `proposed/PHOTO_ARCHIVE_PLAN.md` — the **photo archive pipeline** design spec:
  a staged, idempotent map over a decade of phone dumps (hash-keyed dedup,
  deterministic dating, a vision worker, CLIP search, InsightFace faces, residual
  RAG-backed date/identity inference, browser viewer).
- `proposed/MUSIC_GEN_PLAN.md` — **music generation** on the opt-in `comfyui`
  service (ACE-Step): an audio workflow, an owner-only `generated_audio` table, a
  `generate_music` tool, and a MusicScreen — mirroring the shipped image stack.

## Archive (history, not active)

`archive/` holds completed build plans, a fulfilled contract, a rejected design,
and the design research that fed them. Kept for the audit trail; not the place to
learn the current system. See **`archive/README.md`** for the full index.

Recently archived (all shipped, this cleanup): `PHASE6_WIKI_GRAPH_CONTRACT`, the
four image-gen plans, `EMAIL_ARCHIVIST`, `GUIDED_INTAKE`, `HYGIENE_SWEEPS`, the
four `PHASE7_*` location/family/app-map plans, `LOCATION_ASSISTANT`,
`HURRICANE_TABS`, `TALK_BOARD`, `VIDEO_ANALYSIS`, `WHISPER_TRANSCRIPTION`, both
`SUBAGENT_*` plans, the four `JCODE_*` plans (one `Rejected`), and
`CALIBRATION_LOOP`. Residuals were carried into `ROADMAP.md` so nothing is lost.
