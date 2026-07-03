# JBrain2 — Doc Cleanup Plan

> **Status:** In progress · **Last verified:** 2026-07-03 · **Waves:** W1✅ W2✅ W3✅ W4◻️ — W1–W3 executed (docs at a clean, freshness-checked baseline: 0 errors / 0 warnings); **W4 (open):** flip `docs-freshness.sh` to a binding CI gate + add it to `PROCESS.md`'s gate list (owner decision — it changes CI for all future PRs).

A one-time plan to bring `docs/` to a clean baseline and adopt the
`DOC_LIFECYCLE.md` process going forward. Built from a full staleness audit of
every Markdown file under `docs/` on 2026-07-03 (migration head `0114`; Phases
0–5 shipped, Phase 6 wiki in progress). The audit's per-doc verdicts are the
appendix at the bottom — this plan is what to *do* about them.

## The problem in one paragraph

`docs/` drifted. The two "source of truth" docs (`README`, `ROADMAP`) carry a
`(2026-06)` status stamp and "migrations run through **0044**" when the head is
`0114`. **~22 plan docs describe features that have fully shipped** yet still
read as scheduled/in-progress/proposed — the whole image-gen family, guided
intake, email archivist, hygiene sweeps, every Phase-7 location/family/app-map
plan, the location assistant, hurricane tabs, talk board, video, whisper, both
subagent plans, the jcode preview host, and the wiki graph contract. **jcode is
a shipped subsystem still sitting in the `proposed/` icebox.** Every directory
`README` under-lists its own folder. One doc (`DEBUG_ACCESS_SESSION_GUIDE`) has
an actually-wrong instruction, not just stale framing.

Root cause: no status vocabulary and no ritual to flip it at merge time. That is
what `DOC_LIFECYCLE.md` fixes; this plan applies it once to clear the backlog.

## Waves

One PR per wave, per `PROCESS.md`. W1 is the highest-value / lowest-risk truth-up
and can go first on its own.

### W1 — Truth-up the Living reference (in place, no moves)

Fix the docs readers actually trust, and stamp them with freshness headers.

- **`README.md`** — status date `2026-06` → `2026-07`; drop "Migrations run
  through 0044" (R1: don't hardcode the head — point at `migrations/` or state
  it as a dated snapshot); rebuild the **Active plan** / **Proposed** / **Archive**
  sections to match reality after W2–W3; add `DOC_LIFECYCLE.md` and this plan to
  the map; add a freshness header.
- **`ROADMAP.md`** — `Status (2026-06)` → `2026-07`; `0044` → `0114` (two
  occurrences, or restate per R1); relabel **Phase 6** "Planned" → **In progress**
  (the `backend/src/jbrain/wiki/` package + migrations 0045–0053 shipped);
  record that many **Phase-6 follow-ons and Phase-7 slices already shipped**
  (hygiene sweeps, subagents, location, family tracker, app-map, location
  assistant) rather than listing them as "Planned"; add a freshness header.
- **`DEBUG_ACCESS_SESSION_GUIDE.md`** — **correctness fix, not just staleness:**
  step 1 says set `JBRAIN_DEBUG_ACCESS_ENABLED` in `.env` and `jbrain restart`.
  The `.env` var is `DEBUG_ACCESS_ENABLED`, and `DEBUG_ACCESS.md` explicitly
  says `restart` reuses old env — you must `jbrain up`. Correct both.
- **`entity.md`** — flip value-shape validation from "deferred / NOT built" to
  shipped (tier-1, default ON); sweep the top-level `[proposed]` tags on sections
  now marked implemented inline; add freshness header.
- **`DEVELOPMENT.md`** — "Phase-4 tool definitions **will adopt**" → past tense
  (`.tool` sidecars + CI guard shipped); mark the enumerated prompt list `e.g.`;
  add freshness header.
- **`ENTITY_GRAPH_REFOCUS_PLAN.md`** — replace "**plan doc only — no code lands
  from this file**" with a Shipped-reference status (the two-tier model landed in
  `analysis/predicates.py`, `weight.py`, `pipeline.py`, `worker.py`); update its
  own "fix the 0044 note (head 0112)" line to `0114`. Keep in `docs/` as the
  canonical two-tier reference (README cites it), just not as an unbuilt plan.
- **Nits (optional, same PR):** `DEBUG_ACCESS.md` `.env`-var name clarity;
  `OPERATIONS.md` `scripts/deploy/install.sh` path glyph; `PROCESS.md` example
  repoint from the archived `WORKFLOW_ENGINE_PLAN` to `PHASE6_WIKI_PLAN`;
  `ARCHITECTURE.md` soften "when a GPU arrives" (local models now opt-in shipped).
- Add freshness headers to the verified-current Living docs when touched:
  `ARCHITECTURE`, `DESIGN`, `ANALYSIS`, `ASSISTANT`, `MODEL_PROMPTING`,
  `OPERATIONS`, `STRIX_HALO_SETUP`, `CLOUDFLARE_TUNNEL`, `LOCAL_ACCESS`,
  `WIKI_TYPE_GUIDES`.

### W2 — Archive the shipped plans

`git mv docs/<plan> docs/archive/`, add a `Shipped <month> · <evidence>` banner,
and **carry any residual/deferred item into `ROADMAP.md`** so nothing is lost.
Then rebuild `archive/README.md`'s index.

Shipped plans to archive (evidence in the appendix):

| Plan | Ship evidence | Residual to carry to ROADMAP |
|---|---|---|
| `IMAGE_GEN_PLAN` | migr 0078, `image_gen/`, `agent/imagegentools.py` | — |
| `IMAGE_GEN_LIVE_PLAN` | `ToolProgressEvent`, interrupt endpoint | — |
| `IMAGE_GEN_SERVICE_PLAN` | `image_gen/gateway.py`,`render.py`, lightning graphs | — |
| `IMAGE_LAUNCHER_PLAN` | `screens/ImageScreen.tsx`, `render.py` service | — |
| `EMAIL_ARCHIVIST_PLAN` | 9 gmail sidecars, migr 0094/0095 | note triage layer (0096/0101) shipped too |
| `GUIDED_INTAKE_PLAN` | migr 0107–0113, PR #700 | — |
| `HYGIENE_SWEEPS_PLAN` | `analysis/hygiene.py` etc., migr 0066 | — |
| `PHASE7_LOCATION_PLAN` | migr 0059–0064/0073, `api/owntracks.py` | — |
| `PHASE7_LOCATION_DETAIL_PLAN` | `SamplingPolicy.kt`, array ingest | — |
| `PHASE7_FAMILY_TRACKER_PLAN` | `mqtt/`, migr 0067/0075 | M7c ops runbook; Android FCM hardening |
| `PHASE7_APP_MAP_PLAN` | `MemberDashboard.tsx`, `api/member.py` | — |
| `LOCATION_ASSISTANT_PLAN` | 10 location `.tool`s, `agent/locationtools.py` | L5 segmenter deferred to analytics tier |
| `HURRICANE_TABS_PLAN` | `agent/hurricanetools.py`, `hurricane_card` view | — |
| `TALK_BOARD_PLAN` | migr 0053, `wiki/talkstore.py`, `TalkScreen.tsx` | — |
| `VIDEO_ANALYSIS_PLAN` | migr 0084, `ingest/video.py` (self-marked done) | — |
| `WHISPER_TRANSCRIPTION_PLAN` | migr 0079, `transcribe.py` (self-marked done) | on-box GPU smoke test |
| `SUBAGENT_SPAWNING_PLAN` | migr 0105, `agent/spawn.py` (fix "Nothing is built yet") | — |
| `SUBAGENT_FEEDING_WAVES_PLAN` | F1–F3 LANDED, `spawn.py` (fix "proposed" header) | run-log persistence + live SSE |
| `JCODE_PREVIEW_HOST_PLAN` | `api/jcode_preview.py`, P0–P5b landed | — |
| `PHASE6_WIKI_GRAPH_CONTRACT` | contract fulfilled (0046, image_sha, wiki_built) | — |
| `CALIBRATION_LOOP` | `evals/box/`, integrate/disambiguate runners built | — |

### W3 — Fix the holding areas

- **jcode out of the icebox:** `git mv docs/proposed/{JCODE_PLAN,
  JCODE_2TAB_PLAN,JCODE_SESSION_TOOLS_PLAN}.md docs/archive/` — all three are
  built (jcode-specific migrations 0098/0100/0102/0103; code under
  `api/jcode*.py` + `models/jcode.py`, with `JcodeSessionScreen.tsx`).
- **Rejected ≠ icebox:** `JCODE_CONTAINER_PER_SESSION_PLAN` is red-teamed
  "NOT VIABLE" — flip to `Rejected`, `git mv` to `archive/`.
- **Research whose plan shipped:** `git mv docs/research/legacy-links-*.md
  docs/archive/research/` (feature shipped: `analysis/supersession.py`,
  `FactTenure`). This empties `docs/research/` of completed work.
- **`PHASE6_WIKI_PLAN.md` status rewrite (stays active):** fix "Plan doc only —
  no code lands"; mark Waves A–C shipped (migr 0045–0053, `wiki/builder.py`);
  header status `In progress` with the genuine residual listed — grounding-gate
  tuning, purge→rebuild, and the build schedules currently *disabled* per migr
  0088. Archive it once those close.
- **Rebuild indexes:** `proposed/README.md` (drop the false "nothing here is
  built"; list `PHOTO_ARCHIVE` + `MUSIC_GEN`, the only real icebox items);
  `archive/README.md` (add the two subagent review records + everything archived
  in W2/W3); `README.md` Active/Proposed/Archive sections.
- **Keep as-is (verified current):** `LOCATION_ASSISTANT_TOOLS` (reference
  catalog — add a one-line "✅ spine shipped" header note pointing at the
  archived plan), `JCODE_SESSION_ISOLATION_PLAN` (accurately `Parked`),
  `PREDICATE_CANONICALIZATION` (supersession banner already correct),
  `proposed/PHOTO_ARCHIVE_PLAN`, `proposed/MUSIC_GEN_PLAN` (genuinely `Proposed`).

### W4 — Wire the enforcement

- `scripts/docs-freshness.sh` already exists (landed with this plan, advisory).
  Flip it from advisory to a binding CI gate **only after W1–W3**, once every
  doc carries a freshness header — otherwise CI goes red repo-wide.
- Add the check to the per-wave gate list in `PROCESS.md` §Verification
  (alongside lint/types/testcontainers/coverage/`.prompt`+`.tool` pins).
- Add the definition-of-done line to `DEVELOPMENT.md` and the PR template:
  *"Docs reconciled: plan status flipped or archived, Living docs corrected,
  `Last verified` bumped."* (single canonical location, per `DOC_LIFECYCLE.md`.)
- Update `scripts/dev-setup.sh` if the CI wiring adds a setup step (per
  `CLAUDE.md` non-negotiable #8).

## Appendix — full per-doc audit (2026-07-03)

Verdict legend: **CURRENT** (no change) · **MINOR-DRIFT** (small fix in place) ·
**STALE** (content wrong, fix in place) · **SHIP→ARCHIVE** (shipped, move) ·
**SUPERSEDED** / **PARKED** / **REJECTED**.

### Living reference
| Doc | Verdict | Action |
|---|---|---|
| `README.md` | STALE | W1: date, drop 0044, rebuild sections |
| `ROADMAP.md` | STALE | W1: date, 0044→0114, Phase 6 In progress, note shipped slices |
| `ARCHITECTURE.md` | CURRENT | header only; soften "when GPU arrives" (opt.) |
| `DEVELOPMENT.md` | MINOR-DRIFT | W1: "will adopt"→past; prompt list `e.g.` |
| `PROCESS.md` | CURRENT | repoint archived example (opt.) |
| `DESIGN.md` | CURRENT | header only |
| `ANALYSIS.md` | CURRENT | header only |
| `entity.md` | MINOR-DRIFT | W1: value-shape shipped; sweep `[proposed]` tags |
| `ASSISTANT.md` | CURRENT | header only |
| `MODEL_PROMPTING.md` | CURRENT | header; opt. cloud-default note |
| `PREDICATE_CANONICALIZATION.md` | SUPERSEDED | keep (banner already correct) |
| `ENTITY_GRAPH_REFOCUS_PLAN.md` | STALE | W1: "no code lands"→shipped; 0112→0114 |
| `DOC_LIFECYCLE.md` | NEW (Living) | this cleanup — the going-forward process doc |
| `DOC_CLEANUP_PLAN.md` | NEW (Plan) | this doc — the one-time cleanup, archives at W4 |

### Ops / runbook
| Doc | Verdict | Action |
|---|---|---|
| `OPERATIONS.md` | CURRENT | opt. path-glyph nit |
| `STRIX_HALO_SETUP.md` | CURRENT | header only |
| `CLOUDFLARE_TUNNEL.md` | CURRENT | header only |
| `LOCAL_ACCESS.md` | CURRENT | header only |
| `DEBUG_ACCESS.md` | CURRENT | opt. `.env`-var name clarity |
| `DEBUG_ACCESS_SESSION_GUIDE.md` | MINOR-DRIFT | **W1: wrong instruction — env var + `up` not `restart`** |
| `CALIBRATION_LOOP.md` | SUPERSEDED | W2: harness shipped → archive |
| `WIKI_TYPE_GUIDES.md` | CURRENT | header only (Phase-6 config) |

### Plans — shipped, archive (W2)
`IMAGE_GEN_PLAN`, `IMAGE_GEN_LIVE_PLAN`, `IMAGE_GEN_SERVICE_PLAN`,
`IMAGE_LAUNCHER_PLAN`, `EMAIL_ARCHIVIST_PLAN`, `GUIDED_INTAKE_PLAN`,
`HYGIENE_SWEEPS_PLAN`, `PHASE7_LOCATION_PLAN`, `PHASE7_LOCATION_DETAIL_PLAN`,
`PHASE7_FAMILY_TRACKER_PLAN`, `PHASE7_APP_MAP_PLAN`, `LOCATION_ASSISTANT_PLAN`,
`HURRICANE_TABS_PLAN`, `TALK_BOARD_PLAN`, `VIDEO_ANALYSIS_PLAN`,
`WHISPER_TRANSCRIPTION_PLAN`, `SUBAGENT_SPAWNING_PLAN`,
`SUBAGENT_FEEDING_WAVES_PLAN`, `JCODE_PREVIEW_HOST_PLAN`,
`PHASE6_WIKI_GRAPH_CONTRACT`. (Evidence + residuals in W2 table.)

### Plans — other dispositions
| Doc | Verdict | Action |
|---|---|---|
| `PHASE6_WIKI_PLAN.md` | STALE (mostly shipped) | W3: status rewrite, **stays active** (residual) |
| `LOCATION_ASSISTANT_TOOLS.md` | CURRENT | keep (reference catalog); header note |
| `JCODE_SESSION_ISOLATION_PLAN.md` | PARKED | keep (accurate) |

### Holding areas
| Item | Verdict | Action |
|---|---|---|
| `proposed/JCODE_PLAN.md` | BUILT | W3 → archive |
| `proposed/JCODE_2TAB_PLAN.md` | BUILT | W3 → archive |
| `proposed/JCODE_SESSION_TOOLS_PLAN.md` | BUILT | W3 → archive |
| `proposed/JCODE_CONTAINER_PER_SESSION_PLAN.md` | REJECTED | W3 → archive (mark rejected) |
| `proposed/PHOTO_ARCHIVE_PLAN.md` | PROPOSED | keep |
| `proposed/MUSIC_GEN_PLAN.md` | PROPOSED | keep |
| `proposed/README.md` | STALE index | W3: rebuild (under-lists 3 files, false "nothing built") |
| `research/legacy-links-plan.md` | SHIPPED | W3 → `archive/research/` |
| `research/legacy-links-handling.md` | SHIPPED | W3 → `archive/research/` |
| `archive/README.md` | STALE index | W3: add 2 review records + W2/W3 arrivals |
| `mocks/*` (17 dirs) | CURRENT | none (binding spec, no orphans found) |
