# JBrain2 — docs archive

> **Status:** Living · **Last verified:** 2026-07-03

Historical documents: completed build plans, fulfilled contracts, rejected
designs, and the design research that informed them. Kept for the audit trail
and to preserve the reasoning behind shipped decisions. **They do not describe
the current system** — for that, start at `../README.md` and the living
reference docs beside it. Terminal `Status` banners at the top of each doc name
its ship evidence.

## Core pipeline & engine
| Item | What it is |
|---|---|
| `ASSISTANT_PLAN.md` | Phase-4 personal-agent implementation plan (P4.1–P4.9). |
| `INTEGRATOR_PLAN.md` | Note→graph Integrator (v3) implementation plan. |
| `CUTOVER_V1_REMOVAL.md` | Record of removing the v1 `analyze_note` path. |
| `WORKFLOW_ENGINE_PLAN.md` | Phase-5 workflow-engine + cutover plan (superseded by `PHASE5_COMPLETION_PLAN.md`). |
| `PHASE5_COMPLETION_PLAN.md` | Phase-5 residual-completion plan; Phase 5 closed. |
| `CALIBRATION_LOOP.md` | Analysis-layer calibration harness + CI guard (`evals/box/`). |
| `DOC_CLEANUP_PLAN.md` | The 2026-07 one-time doc cleanup that adopted `../DOC_LIFECYCLE.md` — includes the full per-doc staleness audit as its appendix. |

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
| `WHISPER_TRANSCRIPTION_PLAN.md` | On-box whisper.cpp transcription. |

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
| `JPET_PLAN.md` | The family wall pet: server-authoritative `pet_state`, a 3D WebGL Wall + phone Control screen synced over SSE, a `pet.turn` talk brain (text + voice), memory, and autonomous wander (migrations 0123–0124). Residual (idle `pet.thought`, environment feed, day/night, kiosk/pairing) carried to `../ROADMAP.md`. |

## jcode
| Item | What it is |
|---|---|
| `JCODE_PLAN.md` | jcode on-box code-mode sidecar. |
| `JCODE_2TAB_PLAN.md` | 2-tab Terminal·Preview session layout. |
| `JCODE_SESSION_TOOLS_PLAN.md` | Per-session PATH-shadowing tool shim. |
| `JCODE_PREVIEW_HOST_PLAN.md` | Host-served per-session dev preview. |
| `JCODE_CONTAINER_PER_SESSION_PLAN.md` | **Rejected** — per-session container (red-teamed non-viable). |

## Research & exploration (subdirectories)
| Item | What it is |
|---|---|
| `research/` | Design-research dossiers (self-improving agent, brain-tooluse-ux, session-panel-ux, subject-object-grammar, fix-options) + the shipped `legacy-links` dossier and plan. |
| `ui-exploration/` | Early PWA-icon and entity-graph / search-icon explorations. |

> Note: cross-references inside these archived files may use the docs' original
> pre-archive paths (e.g. `docs/research/...` rather than `docs/archive/research/...`).
> Left as written to preserve the historical record.
