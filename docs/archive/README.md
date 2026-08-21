# JBrain2 — docs archive

> **Status:** Living · **Last verified:** 2026-08-11

Historical documents: completed build plans, fulfilled contracts, rejected
designs, and the design research that informed them. Kept for the audit trail
and to preserve the reasoning behind shipped decisions. **They do not describe
the current system** — for that, start at `../README.md` and the living
reference docs beside it. Terminal `Status` banners at the top of each doc name
its ship evidence.

## Core pipeline & engine
| Item | What it is |
|---|---|
| `GPU_ADMISSION_INTEGRITY_PLAN.md` | **Superseded** by `../proposed/LOCAL_ONLY_BOX_PLAN.md`. Four cold adversarial reviews found its keystone wave unsound (its central category has no data source on this box, and applying it at `local_gateway.py:762` reintroduces the mid-load-baseline bug #1186 fixed), one wave unbuildable, one wave's premise unreachable, and two waves in contradiction. The one fix worth shipping — `unload()` racing llama-swap's 10 s graceful stop on a 3 s client timeout — was found BY the review and shipped separately. Kept for its evidence and for the record of how it failed: three of its claims were a log label, a README summary and an env-var name mistaken for evidence. |
| `ASSISTANT_PLAN.md` | Phase-4 personal-agent implementation plan (P4.1–P4.9). |
| `INTEGRATOR_PLAN.md` | Note→graph Integrator (v3) implementation plan. |
| `CUTOVER_V1_REMOVAL.md` | Record of removing the v1 `analyze_note` path. |
| `WORKFLOW_ENGINE_PLAN.md` | Phase-5 workflow-engine + cutover plan (superseded by `PHASE5_COMPLETION_PLAN.md`). |
| `PHASE5_COMPLETION_PLAN.md` | Phase-5 residual-completion plan; Phase 5 closed. |
| `CALIBRATION_LOOP.md` | Analysis-layer calibration harness + CI guard (`evals/box/`). |
| `DOC_CLEANUP_PLAN.md` | The 2026-07 one-time doc cleanup that adopted `../DOC_LIFECYCLE.md` — includes the full per-doc staleness audit as its appendix. |
| `LLM_PROMPT_CACHE_PLAN.md` | Local-LLM prompt cache: a cache-stable prompt layout (volatile `now_block`/presence moved to the message tail so the static jerv/curator prefix stays byte-stable) + gateway `--cache-reuse 256`, cutting on-box first-token latency. No migration. W1–W2 shipped. |

## Wiki (Phase 6)
| Item | What it is |
|---|---|
| `PHASE6_WIKI_GRAPH_CONTRACT.md` | Wiki↔entity-graph interface contract — fulfilled. |
| `TALK_BOARD_PLAN.md` | Article-anchored wiki Talk board (owner/editor/builder voices). |
| `WIKI_LINT_PLAN.md` | Corpus-wide wiki health sweep (`wiki_lint`, fifth ActionSpec): deterministic checks + LLM contradiction/stale-claim cards. Ships disabled. |

## Agent capabilities
| Item | What it is |
|---|---|
| `SUBAGENT_SPAWNING_PLAN.md` / `SUBAGENT_SPAWNING_REVIEW.md` | `jerv` sub-agent fan (plan + three-lens red-team record). |
| `SUBAGENT_FEEDING_WAVES_PLAN.md` / `SUBAGENT_FEEDING_WAVES_REVIEW.md` | Producer→consumer feeding waves (plan + review). |
| `EMAIL_ARCHIVIST_PLAN.md` | Sandboxed `archivist` Gmail persona. |
| `HURRICANE_TABS_PLAN.md` | Tabbed hurricane card (track/cone/alerts/surge). |
| `VIDEO_ANALYSIS_PLAN.md` | On-box video understanding. |
| `STREAM_ANALYSIS_PLAN.md` | `analyze_stream`: jerv reads a video URL (live/VOD) via yt-dlp + ffmpeg. |
| `DEFERRED_TOOL_CALLS_PLAN.md` | Turn-ending background jobs + a reusable `task_status` card; `analyze_stream` full-video is the first adopter. |
| `WHISPER_TRANSCRIPTION_PLAN.md` | On-box whisper.cpp transcription. |
| `READ_ALOUD_LEGIBILITY.md` | Read-aloud legibility + fluidity: shared `speakable` normalizer + stream-safe chunker + prefetch pump (W1/W2), and the `wall`/`tts-stt` split with warm piper (W0). |
| `READ_ALOUD_AUDIOBOOK_PLAN.md` | Audiobook-grade Kokoro read-aloud: two-layer `toProse`/`toUtterance` split, misaki G2P + `KOKORO_LEXICON`, env-tunable pacing (speed + trailing silence), narrator voice blends, and an automatic markup-vs-prose classifier (no mode UI). W0–W4 shipped. |
| `KOKORO_TTS_CONSOLIDATION_PLAN.md` | Standardised read-aloud on **Kokoro only** (removed Piper across box/backend/frontend/wall/docs; `piper_server.py`→`tts_server.py`; browser-native the sole fallback). Fixed the reported symptoms at root — the "U.S." pause (dotted-initialism collapse + streaming guard), flat headings (colon lead-in), and "Titusville" (an engine-agnostic owner **respelling lexicon** + `/tts/health` phonemizer-path visibility) — plus coverage gaps (inequalities/snake_case/times/phones/versions/ISO-dates). `speakable.js` is the single source of truth (the streaming chunker keeps it client-side; the literal box-side merge was deferred, see ROADMAP). Adds the Pronunciations settings panel (mock A). W1–W4, PR #1068. |
| `INLINE_APPROVALS_PLAN.md` | Proposal approval moved into the conversation (inline variant-D card): per-leaf approve / decline-with-reason / correct-in-place + one Enact that returns a server-authored outcome to the assistant (the enact→agent feedback loop). Migration 0130. |
| `CHAT_CHARTS_PLAN.md` | Interactive (zoom/pan) chart + lab-plot tool-views in Full Brain chat: the `InteractiveChart` engine, the `chart`/`lab_chart` tabbed views (GUI-gate variant C), `lab_chart` from `read_labs` trend, and the `chart_measurements` (grounded) + `render_chart` producers. |
| `RESEARCH_LIBRARY_PLAN.md` | The owner-facing card-launcher **Research Library** (GUI variant B) over jerv's `external`-corpus artifacts — deep-research reports + video analyses: search / view / delete via an owner-gated `/api/research-library` API that reuses the existing corpus callables (no migration/grant), a detail layer (`<Markdown>` / `<VideoAnalysis>`), and per-item actions. PR #907; R1–R3 with both review gates. |
| `RESEARCH_SHARE_LINKS_PLAN.md` | Public, revocable, no-login **report share links** (migration 0150, GUI variant B): mint a token targeting one report or one folder; anyone reads it at `/share/<token>`; revoke to kill it. The read is enforced in RLS via a row-scoped `research_reports_share` policy over an empty-scope share context; the public `/api/research-share` router is rate-limited + `no-referrer`/`noindex`. |
| `GROKIPEDIA_TOOL_PLAN.md` | jerv's Grokipedia tool set (`grokipedia_search`/`outline`/`section`/`citations`/`related`): search xAI's encyclopedia, traverse an article by its table of contents, read a single section, and pull citations to follow to primary sources. Open-internet only (no xAI key) — API-first (`/api/typeahead` + `/api/page-preview`) with SSR `/page/<slug>` fallback, one surface-agnostic `{outline, section, citations}` model, per-slug cache. W1–W3 in PR #993. Research dossier: `research/grokipedia-tool/RESEARCH.md`. |
| `DEEP_RESEARCH_STAGED_PIPELINE_PLAN.md` | Staged single-source pipeline for `deep_research`/`deep_produce` (the interview-task fix). W1: child sub-agent tool-call + reasoning persistence (`AgentResult.tool_steps`/`reasoning` → own session). W2: an optional ordered `stages` plan the gather runs sequentially, feeding each stage forward via the feeding-waves envelope, per-stage persona (extract=library, fact-check=web under `library_first`; `library` stays corpus-only per stage); additive — <2 stages = the byte-stable flat gather. W3: windowed `read_external_video(from_ms)`, an enumeration-mode `research_library.prompt` clause, and jerv/tool routing so a specific analysed video uses `sources=library_first` (never `web`). PR #966. |
| `DEEPEST_RESEARCH_TOOL_PLAN.md` | `deepest_research` — the autonomous, resumable **background** run: two-tier recursion (orchestrator → task agent → sub agent, `max_depth=2`), loop-until-covered or an owner-set ceiling, checkpoint/resume, periodic progress streamed back to chat, report landed in the research library. Build waves R1–R8 shipped (migrations 0146–0148: `research_deep` persona, `research_run_state`, tool-aware `(question_hash, tool)` dedup). R0 (a value-probe/kill-gate) was deliberately overridden by the owner and never fired — carried to ROADMAP as a skipped coverage-gap probe. |
| `DEEP_RESEARCH_VIDEO_SOURCES_PLAN.md` | A `sources` knob on `deep_research` (`web` / `library` / `library_first`) letting a run draw from the owner's external-video corpus instead of, or ahead of, the open web. Structural — `sources` only picks the gather/refill/review child persona (`research_library`/`review_library`). Migration 0142 (`source_mode`) + 0144 (library sub-agent personas); DV1–DV3 shipped, report-view provenance chip included. |
| `JERV_PLANNING_TOOL_PLAN.md` | jerv's owner-approved, per-conversation **plan** tool: `read_plan`/`write_plan`/`write_plan_result` over an owner-only `agent_session_plans` row (jerv can never self-approve — the anti-injection guard), re-injected each turn, executed across turns by a bounded owner-interruptible auto-continuation loop that streams live, with per-plan isolation, per-step results scratchpad + timing, and reopen reconcile. Migrations 0155, 0157–0159; waves P1–P9.5 shipped. |
| `DYNAMIC_PORTAL_FETCH_PLAN.md` | Resolver adapters (codename "dinosaurs") that let the research fan actually *query* dynamic JS/POST government search portals (FL Sunbiz corp search, FL DFS licensee search) via each portal's real endpoint through the SSRF-guarded `WebFetcher` (`submit_form`, no headless browser), surfaced as `WebSource`s — plus a precision `_is_search_form_page` detector so an un-adaptered portal degrades honestly instead of laundering an empty form into "no record exists". `web/portals` framework; P1–P3 shipped. |
| `DOMAIN_HEALTH_PLAN.md` | A global 24h paywall/bot-wall skip list (`app.blocked_domains`, migration 0163, RLS like `canonical_predicates`) so `web_fetch`/`web_search` stop wasting calls on a domain that just proved unreadable — precision `_is_paywall_page` + the existing `_is_challenge_page`/403/429 record the host for 24h; later fetches short-circuit and later searches drop the host. Never records a 404/transient/search-form; lazy expiry, fail-open. |
| `REPORT_EXPIRY_PLAN.md` | Opt-in TTL for research-library reports + the per-run dedup-key fix that makes it useful: an `expires_at` column (migration 0161) stamped from a preset's `retention_days`, a nightly `expire_research_reports` sweep (migration 0162) that hard-deletes past-TTL rows, an auto-supplied `{{today}}` render variable (so `daily_news` dates each day distinctly and self-expires after 7 days), and a "Keep this report" action that clears the TTL. W1–W4 shipped. |

## Image generation
| Item | What it is |
|---|---|
| `IMAGE_GEN_PLAN.md` | `generate_image`/`edit_image` chat tools + owner-only artifacts. |
| `IMAGE_GEN_LIVE_PLAN.md` | Progressive live previews + mid-render Stop. |
| `IMAGE_GEN_SERVICE_PLAN.md` | ComfyUI/Qwen as a managed service + Lightning path. |
| `IMAGE_LAUNCHER_PLAN.md` | Standalone non-agent image screen + shared render service. |

## Location & family (Phase 7)
| Item | What it is |
|---|---|
| `PHASE7_LOCATION_PLAN.md` | OwnTracks ingest, hypertable, geofence brain. |
| `PHASE7_LOCATION_DETAIL_PLAN.md` | Motion-adaptive dense trails (no GMS). |
| `PHASE7_FAMILY_TRACKER_PLAN.md` | Family-scale tracker (MQTT, pairing, FCM). |
| `PHASE7_APP_MAP_PLAN.md` | Full-screen live member map. |
| `LOCATION_ASSISTANT_PLAN.md` | Owner-only location assistant tool spine. |
| `GUIDED_INTAKE_PLAN.md` | Owner-minted intake share links → attributed notes. |
| `HYGIENE_SWEEPS_PLAN.md` | Core-data maintenance engine actions. |
| `JPET_PLAN.md` | The family wall pet v1: server-authoritative `pet_state`, a 3D WebGL Wall + phone Control screen synced over SSE, a `pet.turn` talk brain (text + voice), memory, and autonomous wander (migrations 0123–0124). Its Tamagotchi decay *interaction model* is superseded by `JPET_V2_PLAN.md`. |
| `JPET_V2_PLAN.md` | JPet v2 (migration 0125): the pivot from Tamagotchi decay to a positive, command-and-response play companion for 3–4-year-olds. Happy meters that never decay; a bounded, enum-constrained **action-script** the pet plays out (`dance`, `chase the ball`, `pick up the ball and put it in the corner`); room objects + object-targeted actions + carry; big kid play-buttons + push-to-talk on the phone; per-action WebAudio sound cues + day/night on the `:8800` wall; capped ambient life. Backed by three deep-research dossiers. |
| `JPET_V3_PLAN.md` | JPet v3 (migrations 0126–0127): the wall becomes the pet's continuous real-time brain. W1 — the autonomy engine (constrained-randomness behaviour + damped-spring fluid motion + always-on idle micro-motion), **drive meters ripped out**, **solid-wireframe** render, **2× room**. W2 — the living world: **ball physics** + **mouse click-to-play**, the **block-builder** (small solid bricks → varied statues, knocked down by the ball + rebuilt), detailed furniture + **TV** + **window** + **circadian** day/night + a **vacuum**. W3 — a **hybrid talk→action router** (keyword-first, LLM never 500s), **colour-on-command** (rainbow cycling), and two activities: a **jump rope** and a **playable synth** (clickable pentatonic keys, WebAudio), plus the phone Control colour palette + activity buttons. Backed by a 105-agent autonomy-design dossier. |

## jcode
| Item | What it is |
|---|---|
| `JCODE_PLAN.md` | jcode on-box code-mode sidecar. |
| `JCODE_2TAB_PLAN.md` | 2-tab Terminal·Preview session layout. |
| `JCODE_SESSION_TOOLS_PLAN.md` | Per-session PATH-shadowing tool shim. |
| `JCODE_PREVIEW_HOST_PLAN.md` | Host-served per-session dev preview. |
| `JCODE_CONTAINER_PER_SESSION_PLAN.md` | **Rejected** — per-session container (red-teamed non-viable). |

## jlaunch
| Item | What it is |
|---|---|
| `JLAUNCH_PLAN.md` | Default-on job launcher (Math tile): supervised long computations + public results share. |

## Research & exploration (subdirectories)
| Item | What it is |
|---|---|
| `research/` | Design-research dossiers (self-improving agent, brain-tooluse-ux, session-panel-ux, subject-object-grammar, fix-options) + the shipped `legacy-links` dossier and plan. |
| `ui-exploration/` | Early PWA-icon and entity-graph / search-icon explorations. |

> Note: cross-references inside these archived files may use the docs' original
> pre-archive paths (e.g. `docs/research/...` rather than `docs/archive/research/...`).
> Left as written to preserve the historical record.
