# Proposed (not scheduled)

> **Status:** Living · **Last verified:** 2026-09-01

Forward-looking design specs **dropped in for the record but not on the
roadmap** — the icebox: ideas worth keeping shaped, kept out of the active-plan
list in `../README.md` so they're never mistaken for in-flight work. Per
`../DOC_LIFECYCLE.md`, this folder holds `Proposed` docs only — nothing built,
nothing rejected. A built design moves to `../archive/`; a killed design moves
to `../archive/` with a `Rejected` banner.

When a doc here is picked up, it must be reconciled with the root `CLAUDE.md`
non-negotiables (LLM adapter, storage abstraction, RLS + isolation tests, etc.),
given a roadmap slot in `../ROADMAP.md`, and promoted out of this folder.

## Contents

- `JERV_CONTEXT_BUDGET_PLAN.md` — rebalance what a jerv turn spends its context on
  (44 tools ≈ 28.7k tokens of schema per turn, nothing carried across sessions). W1 fixes the
  identity paragraph that made the on-box model discount its own prompt; W2 adds a
  cross-session **scratchpad** on a generalized `app.agent_scratchpad` table (the archivist's
  memory migrates into it) split by **write authority** — the agent writes only dated
  `Threads`/`Watch`, injected inside the `briefs.py` untrusted-data sentinel, while
  `Preferences`/`Corrections` are owner-written from the PWA and carry sanctioned-instruction
  framing; W3 puts config-derived lists into the tool schemas at `schemas_for` time. The
  description trim lives in `TOOL_CATALOG_PLAN` W0b, which W2 must land after. **Revised after
  a four-lens adversarial review** (§8); records five deliberate rejections with corrected
  evidence (Python sandbox, skills-in-memory, per-persona sidecars, `anyOf`, catalog W2/W3).
- `PHOTO_ARCHIVE_PLAN.md` — photo archive pipeline: a staged, idempotent map over
  a decade of phone dumps (hash-keyed dedup, deterministic dating, a vision worker
  bridging pixels to the text-only 120B, CLIP search, InsightFace faces, residual
  RAG-backed date/identity inference, browser viewer).
- `MUSIC_GEN_PLAN.md` — music generation on the existing opt-in `comfyui` service
  (ACE-Step 1.5 XL Turbo, AMD/gfx1151-validated): a new audio workflow + audio-aware
  driver output path, an owner-only `generated_audio` artifact table, a `generate_music`
  tool, and a MusicScreen — mirroring the shipped image stack. Backend (Waves M0–M3) +
  frontend (M4), with M0 a blocking on-box host-validation spike. Interactive mock:
  `../mocks/music-gen-live/live-music-tool-card.html`.
- `TEACHER_MODE_AGENTS_PLAN.md` — split the `teacher` persona into two agents:
  an owner **instructor** (authors/approves lessons + curricula, assigns to a
  child, reviews results) and a sandboxed non-owner **student** behind an
  anonymous scoped link (a live, KB-less tutor). Clones the shipped intake-link
  substrate; net-new lesson/curriculum domain model + server-owned lesson-runtime
  state machine + two-sided child-safety layer + two UIs. Waves W1–W8 with a hard
  safety gate before child exposure. Backed by the approved component work in
  `../research/teacher-mode/` (`COMPONENT_CATALOG.md`) and the four mocks in
  `../mocks/teacher-mode/`.
- `CONTEXT_COMPACTION_PLAN.md` — **(cold-reviewed &amp; parked — see its §0)** one cross-session context-management capability so a turn
  keeps going when its context fills instead of dying: an on-demand `compact` tool + automatic
  compaction, both inside the shared `AgentLoop` so jerv, sub-agents, jmolt, Tasks, and
  continuations all inherit it. Two-tier eviction — **offload** artifact-backed tool results to
  their `read_artifact` reference line (lossless), **summarize only the remainder** into one
  DATA-fenced synthetic turn after the stable prefix, keeping the last K rounds verbatim and the
  citation side-channel untouched. Accurate triggering off the existing post-hoc token meter (no
  pre-call tokenizer exists) at a conservative window fraction, with `LlmContextOverflowError` as
  a compact-and-retry backstop. Reuses the tool-artifact substrate + the `briefs.py` fence;
  aligns with `../plans/TOOL_CATALOG_PLAN.md` + `JERV_CONTEXT_BUDGET_PLAN.md`; tuned for the weak
  local summarizer (offload &gt; summarize, extractive copying, no summary-of-a-summary) and
  firewalled per non-negotiable #8 (summarizer on the triggering session's scope, most-restrictive
  domain, fenced content stays fenced). Composed from four research passes (loop seam, existing
  machinery, accurate accounting, strategy + safety). Motivated by jmolt's autonomous hour and
  jerv's long fan turns.
- `DEEP_RESEARCH_MODULE_SPLIT_PLAN.md` — break the ~2,300-line `deep_research.py`
  orchestrator monolith into topic modules (`research_sources.py`, `research_directives.py`,
  `research_report_view.py`, `research_backstops.py`), leaving `DeepResearchService` in
  `deep_research.py`. A pure mechanical move with NO behaviour change; the one gotcha is
  re-exporting the moved private helpers from `deep_research` so the tests' imports still
  resolve. Its own PR, done in a git-push-capable session. Follows from the scratchpad work
  (PR #1049).
- `DITOO_PLAN.md` — feasibility + build sketch for driving the owner's **Divoom Ditoo**
  (16x16 pixel Bluetooth speaker) from the box. Verdict: the display half is integrable and
  fits the SDR-shaped hardware pattern; the audio half is not (the box has no audio stack at
  all, and A2DP needs host packages the no-terminal update path cannot install). Three hard
  parts: Bluetooth sockets only work in the **initial network namespace**, so the sidecar
  needs `network_mode: host` and forfeits the `internal: true` egress lock that `radio`/
  `render` rely on; the sidecar therefore **ships its own BlueZ** rather than asking the
  owner to apt-install one (CLAUDE.md #10); and pairing is driven over BlueZ's D-Bus API
  from the PWA. Gated on a **free blocking probe (D0)** — the shipped supervisor `/usb`
  inventory already answers "does this box have a Bluetooth radio at all?", exactly as SDR
  S0a answered it for the dongle. Records why the obvious escape hatch is closed (§1.1):
  the USB-C port is **power only**, Divoom ships no desktop app, and every reverse-engineered
  implementation is RFCOMM-only — so there is no wired route that would collapse this into
  the SDR pattern. **§5 corrects an earlier draft that scoped audio out**: `bluez-alsa` needs
  no host sound server, so the one sidecar carries display (SPP), voice out (A2DP) and voice
  in (HFP/mSBC at 16 kHz — whisper's native rate), and the §4 cost is paid once for the whole
  device. §0.2 surveys the alternatives against the owner's real spec — pixel art + wireless +
  speaker + mic in one small object — and finds nothing shipping that hits all four with an
  open API. Payoff waves are **JPet on the panel** (one more `PetBroadcaster` subscriber) and
  **D6/D7**, a voice endpoint in the room off the shipped Kokoro + whisper services.
  **§4.5 is the recommended shape:** an **ESP32 bridge** beside the speaker holds the
  Bluetooth link, so the box talks Wi-Fi/MQTT (on the broker already in the compose) and
  §4.1-§4.4 all evaporate — no BlueZ, no host-netns sidecar, no host modules, no pairing UI,
  no range limit. The display half is off-the-shelf (`esp32-divoom`, browser-flashed, Ditoo
  listed, classic ESP32 only — S3/C3/C6 are BLE-only); the audio half is net-new firmware
  whose Wi-Fi/BT coexistence must be measured. Best mod: put an I2S mic on the bridge rather
  than use the Ditoo's, for wideband capture and a clean AEC reference.
- `ROOM_ENDPOINT_PLAN.md` — **supersedes `DITOO_PLAN.md`**, and the hardware is ordered
  (2 × Waveshare ESP32-S3-Touch-AMOLED-1.8): a small AMOLED satellite that is the box's face
  and ears in a room. **No Bluetooth anywhere**, so the whole §4 of the Ditoo plan evaporates;
  the endpoint is a Wi-Fi client like any other. 368x448 AMOLED gives true blacks (pixel art
  reads as *emitting*, not photographed) and **368/16 = 23 px per cell exactly**, leaving a
  368x80 caption strip. 8 MB PSRAM leaves room for framebuffers, audio and an on-device
  wake-word model, and **ESP-SR brings wake word + acoustic echo cancellation** — so
  full-duplex barge-in, which the Ditoo could never do, is reachable. Server half is largely
  shipped (Kokoro, whisper.cpp, `PetBroadcaster`, `notify/bus.py`, `ACTION_SPECS`) and the
  endpoints need **no new auth model** — they are `Subject(kind='device')` +
  `Principal(kind='device_key')`, the substrate MQTT already reuses. Net-new: firmware, one
  protocol, renderers, an audio round-trip. Two units by design: one bench, one deployed, and
  **OTA is a W1 feature** because rule 10 forbids a USB cable being the update path.
_(The jcode plans, `GUIDED_INTAKE_PLAN.md`, and `SUBAGENT_SPAWNING_PLAN.md` were
promoted out of the icebox and have since shipped; `JPET_PLAN.md` and `JPET_V2_PLAN.md`
shipped and now live in `../archive/`. `EXTERNAL_VIDEO_INGESTION_PLAN.md`,
`DEEP_RESEARCH_TOOL_PLAN.md`, and
`VIDEO_IMAGE_TOOLS_PLAN.md` were promoted to `../plans/` and are in progress.
`ENTITY_GRAPH_INGEST_V2_PLAN.md` was ratified and promoted to `../plans/` (In progress — V1 landed).
`DEEP_PRODUCE_PLAN.md` was reviewed, hardened, and promoted to `../plans/` (In progress — W1✅ W2✅ W3◻️).
`TOOL_CATALOG_PLAN.md` was promoted to `../plans/` (In progress — W1 umbrellas shipped).
`JMOLT_PLAN.md` was researched (three passes: platform/repo/culture digest, persona
workshop `../research/jmolt/PERSONA_CANDIDATES.md`, threat model
`../research/jmolt/THREAT_MODEL.md`), had its nine owner decisions ratified, and was
promoted to `../plans/` (Scheduled — W1◻️ W2◻️ W3◻️ W4◻️).
`CROSS_TURN_TOOL_RESULTS_PLAN.md` was promoted to `../plans/` (In progress — W0+W1 landed).
`DEEPEST_RESEARCH_TOOL_PLAN.md` was promoted, shipped (R1–R8), and now lives in `../archive/`.
`DYNAMIC_PORTAL_FETCH_PLAN.md` was promoted, shipped (P1–P3, 2026-08), and now lives in `../archive/`.
`GROKIPEDIA_TOOL_PLAN.md` was promoted, shipped (W1–W3, PR #993), and now lives in `../archive/`.
`JMOLT_SITTINGS_PLAN.md` was split out of `CONTEXT_COMPACTION_PLAN.md`, promoted to `../plans/`, and
is in progress (W1 landed).
`SDR_RADIO_PLAN.md` was scoped with the owner (seven decisions, a closed GUI gate for the omnibox
tuner), promoted to `../plans/`, and is in progress (S0a — the debug USB probe — landed).)_
