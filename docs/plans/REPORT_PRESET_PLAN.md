# Report Presets & Batch Runs — uniform reports, run down a list

> **Status:** In progress · **Last verified:** 2026-08-10 (live-run tuned) · **Waves:** P1✅ P2◻️ P3◻️

The deep-research engine plans each report's shape fresh every run, so two reports on
comparable subjects (say, two candidates on the same ballot) come out structurally
different. This plan adds two opt-in capabilities on top of the existing engine:

- **P1 — report presets** ✅: a saved, parameterized template that pins a report's section
  outline and its research angles, so the same subject-family comes out in one uniform
  shape. Design decision (owner, 2026-08-06): a **frozen parameterized plan** stored as a
  **checked-in file**, strictly **opt-in** — no preset falls back to today's self-orchestrating
  planner, byte-unchanged.
- **P2 — batch runs** ◻️: point a preset at a *list* of subjects and get one report each,
  run one-at-a-time in the background, notifying the owner per report and auto-advancing —
  "fire-through" (no blocking gate), on the existing deepest-research background lane.

Design options and prior art that led here: the studio design doc (chat artifact, 2026-08-06)
surveying GPT Researcher / STORM / open-deep-research / the commercial products, the
structured-generation literature (outline-first, form-filling, abstention sentinels), and the
repo seam map.

## P1 — Report presets ✅ (shipped)

**Shape.** A preset is the saved, variable-filled twin of the dict `deep_research._plan`
normally invents: `{sections, sub_questions}`. Supply it and the engine skips planning and
runs the fixed plan; omit it and the run self-orchestrates exactly as before.

**Files.**
- `backend/src/jbrain/agent/research_presets.py` — the loader + strict `{{ variable }}`
  renderer. Pure-YAML `*.preset` files, validated at import (a malformed preset fails
  startup); a render missing a variable, or naming an unknown preset, is a clean
  `PresetError` refusal, never a blank substitution. No `deep_research` import (one-way
  dependency), so `output_kind`/`source_mode` value checks live in the engine.
- `backend/src/jbrain/agent/presets/candidate_profile.preset` — the first preset: a uniform
  candidate profile (`{{candidate}}`, `{{office}}`). Its five gather angles bake in the
  accuracy checklist from the prior fix (search the FEC by name; read an authoritative bio,
  not just the campaign site; verify an absence before asserting it), and its objective
  carries the per-section fill spec + the `Not established — …` sentinel for an empty section.
- `backend/src/jbrain/agent/presets/daily_news.preset` — a spoken, text-to-speech-ready daily
  news briefing for the owner's morning commute. One-call for the caller (its single `{{today}}`
  variable is auto-supplied by the engine — see REPORT_EXPIRY_PLAN.md), and dated so each day is a
  distinct library row rather than clobbering yesterday; `output_kind: brief` so it stays ~10
  minutes read aloud (a preset forces `complexity=deep`, so `report` would balloon). It opts into a
  `retention_days: 7` TTL, so the nightly expiry sweep keeps only a rolling week. Its objective
  carries the spoken-format discipline (numbers/dates written as said, no tables/links/markers in
  the body, spoken attribution + transitions) and the neutral multi-source synthesis rule; its five
  angles pull the last day's new developments with heavy weight on the space industry and AI
  (always surfacing SpaceX/Tesla/Musk) and a narrow, impact-first local scope (Port St. John &
  Titusville: severe weather, an imminent launch, or serious local events). Known limitation: the
  shared synth prompt still appends inline `[^n]` markers + a `## Sources` list; the objective
  corrals them to sentence/paragraph ends and a trailing section, but a dedicated spoken/`audio`
  output_kind that suppresses them is the clean follow-up if the TTS layer voices them.
  Gathering (2026-08-09 tune, from a live run that came back headline-thin — 57 of 60 sources
  never opened, four sections empty): the five angles now NAME authoritative, fetchable sources
  per category (AP/Reuters/NPR-text for national+world; CNBC/Reuters/AP/Fed/BLS for economy; the
  AI labs + TechCrunch/Verge/Ars for AI; Spaceflight Now/NASASpaceflight for space; NWS-MLB +
  Space Coast Daily/Talk of Titusville/Orlando-TV for local — Florida Today is flagged PAYWALLED),
  and the objective requires each angle to OPEN ≥3 real articles and pull specifics (not skim
  headlines) before calling a category empty. A systemic follow-up — flagging paywalled/blocked
  domains in the fetch/search layer so they're auto-excluded for ~24h — shipped
  (DOMAIN_HEALTH_PLAN.md). A second live run (2026-08-09) drove two more fixes: (1) the preset now
  forbids sourcing a specific dated event to a monthly/"trending" roundup (open the wire/primary
  instead) and adds a status/tense guard (a schedule or press release is a PLAN — "scheduled for",
  never "launched"), and names fetch-friendly economy/world fallbacks (text.npr.org, BBC) so a
  bot-walled Reuters/CNBC/AP doesn't leave Business/World empty; (2) `jerv.prompt` (v44) now REQUIRES
  fetching a saved report via `research_report(action=read)` and reproducing it verbatim when the
  owner asks to see the full text — the earlier "don't re-paste" rule had let jerv confabulate a
  fresh briefing (invented entertainment items + a launch that never happened) instead of reading
  the stored one. A follow-up run then exposed the real gathering bug: a live run opened only 4 of
  60 sources (the launch pages) and reported everything else from search SNIPPETS, which the
  synthesizer then dropped as uncited — so the sections read "could not be confirmed". A tool-level
  nudge — `web_search` (v2) now states in its description AND every result header that a
  title/snippet is an UNVERIFIED LEAD that must be `web_fetch`ed before it is reported or cited —
  helped but did NOT change the local model's behaviour: a verify run still opened only 4 of 60
  sources and its own `## Sources` list tagged every citation "(search result)", i.e. the whole
  briefing was still snippet-derived. So the fix moved from nudging to STRUCTURE:

  **Two-phase (scout → read) gather (the structural cure).** A preset that sets `min_reads` opts its
  gather into two phases instead of the single all-in-one `research` fan:
  - **scout** — a fan of the `research_scout` persona (the LEAD-FOLLOWER: `web_search` +
    `web_fetch` + clock), one child per angle. It searches AND FOLLOWS LEADS — opening a
    hub/section/search page to reach the *specific* article URLs behind it, and briefly confirming
    a candidate is a real, on-topic, fetchable article — then surfaces those URLs. Its PROSE is
    DISCARDED, so it can never leak a snippet claim; but with fetch it hands the reader real article
    URLs instead of a hub the reader would only see headlines on. (The split is by ROLE, not a hard
    tool line — the earlier search-only scout was handed the reader's "open and read the articles"
    brief, which it had no tool for, so it flailed; `_scout_brief` now reframes each angle as a
    scouting task.) EXPLICIT RECOMMENDATION (v4): the scout ends its reply with a machine-read
    `RECOMMENDED SOURCES:` URL list, and `_angle_candidates` reads ONLY those (via `_recommended_urls`,
    falling back to all touched URLs if the block is absent) — so a hub/aggregator/blog the scout
    merely OPENED to navigate is kept OUT of the read set. This is the fix for a live briefing that
    read three blogs the scout only used to *find* the news (`spacereport.blogspot`, a WordPress "AI
    news bulletin board", a local-news home page) instead of the AP/Reuters/operator primaries it had
    actually located. The scout is also BOUNDED (v5) against over-searching — a live run had each
    scout run 34-36 `web_search` calls (a ~26-minute scout phase). Three gpt-oss-120b prompting
    researchers (armed with `docs/reference/MODEL_PROMPTING.md`) traced it to that model's
    contradiction-sensitivity: the scout received the angle briefs, written for the all-in-one
    research persona ("open and READ at least three of these ~12 sources, cover HEAVILY, don't
    conclude a category empty, pivot to another outlet if one blocks") — a named-source CHECKLIST the
    model swept one search at a time, in direct conflict with the scout's soft "stop early" plea.
    The fix is prompt-only (no budget/effort change), in three layers: (1) scout prompt v5 replaces
    the ignored "stop early" plea with ONE countable ceiling stated once — AT MOST 6 `web_search`
    calls and 5 URLs, whichever comes first, `web_fetch` unlimited — plus "the named sources are a
    MENU to sample, not a checklist" and an explicit priority ("when the brief conflicts with the
    search budget, the budget wins"); (2) `_scout_brief` is now a SHORT wrapper (marks the task as
    scouting + the angle as topic-plus-menu) rather than re-stacking rules (which gpt-oss reads as
    conflict); (3) the daily_news angle briefs are SLIMMED to a ~3-source menu with the "at least
    three / SEPARATELY open / don't conclude empty / pivot" checklist language removed (they feed
    only the scout in fetch-first mode, so this is safe). Also (from v3): search by topic+outlet,
    never stuff an exact date into the query (it pulls calendar/almanac pages like "50 fun facts
    about August").
  - **read** — a fan of the `research_fetch` persona (the READER: `web_fetch` + clock, NO
    `web_search`), ONE reader per angle. The missing search tool is the point: it can't wander off
    searching, so its whole job is the deep read the writer's findings rest on.

  The engine (`_gather_scout_then_read` / `_angle_candidates` in `deep_research.py`) ranks each
  angle's candidate URLs by embedding relevance and spawns ONE reader per angle — NAMED after the
  angle (the scout's label), so a reader row reads "Space industry…" not a generic "read 3" —
  reading that angle's top `ceil(min_reads / angles)` URLs (deduped across angles). The read
  budget is bounded by the tree's 12-agent ceiling, reserving slots for the analyst + critique (5
  scouts + 5 angle-readers + analyst + critique = 12). Only the reader (fetched) findings reach the
  RESEARCH ledger, so the SYNTHESIZER has no unopened snippet to cite; a totally-blocked read
  refuses rather than falling back to scout prose (strict fetched-only). A fetch-first run also SKIPS
  the reflect→refill gap loop (a refill re-searches, which would smuggle snippets back). It is OPT-IN
  (via `min_reads`; `daily_news` sets 12) because it suits fetch-light NEWS-style gathering ("find
  articles, read them") but NOT investigative research, which needs the plain `research` persona's
  interleaved `web_search` + `portal_search` + verify-an-absence loop (candidate_profile).

  **Synth must not drop what the readers delivered (`dr-synth-v14`).** A live daily-news run
  (2026-08-10) laid the real depth loss bare in the logs: the five readers returned ~13k chars of
  specific, on-topic findings — the GPT-5.6 Sol update (Aug 6), the Minnesota Senate primary, the
  New York Harbor boat deaths, two completed launches — yet the writer emitted a 3,093-char brief
  that KEPT four items and declared three whole sections empty, even printing "the sources did not
  include a new AI model release" while a reader had handed it exactly that. The drop was at
  SYNTHESIS, not gather (and it was inconsistent: it kept a same-vintage Pentagon story while cutting
  the others). The shared synth prompt's length rule let brevity be earned by CUTTING items; v14 adds
  one countable coverage rule — a multi-topic brief covers every distinct story a finding delivered,
  each held to the target's length (two-three spoken sentences), and "no X appeared in the sources"
  is banned the moment a finding delivered an X (the delivered-content twin of the existing absence
  rule; gpt-oss "obeys countable output-shape rules"). A recency target orders and picks the lead,
  it does not licence silently dropping a delivered on-topic item. (Two adjacent contributors seen in
  the same logs are left for a follow-up per the owner: the per-angle read cap `ceil(min_reads/angles)`
  truncated accessible recommendations — an AP Fed-rates story, Claude Sonnet 5 — before the read
  stage, and Reuters CAPTCHA-blocked every World/Economy fetch, hollowing those sections at the
  fetch layer rather than the writer.)

  **Citation hygiene, no recycling, and freshness (`dr-synth-v15`, reader `agent-research-fetch-v2`).**
  A frontier review of a later daily-news run (2026-08-10) surfaced three OUTPUT defects the pipeline
  didn't guard, all confirmed in the report row: the writer emitted DUPLICATE footnote definitions
  (`[^22]`/`[^38]` each defined twice, one with a literal "(duplicate …)" line), left an ORPHAN
  source (`[^37]` Doge in the `## Sources` list, cited nowhere in the body), and PADDED the "Around
  the World" section by restating a domestic wind-lease story rather than declaring it had no
  international finding. v15 adds two rules: one number per source with the `## Sources` list a
  one-to-one mirror of the in-body markers (reconcile before finishing — drop uncited entries, no
  duplicate/orphan numbers), and each delivered story belongs to ONE section (a section with no
  distinct finding is declared, never filled by recycling a neighbour). Freshness — the review's
  "recycled/stale" complaint (a week-old market close, a past-day weather alert written as today's) —
  is fixed at the READER (`agent-research-fetch-v2`): each finding now carries the page's own
  publication date and flags anything dated well before the run day, and a matching v15 synth rule
  forbids presenting a stale page's figures as current (attach the date or drop the item). Finally,
  the `daily_news` "Space industry & launch outlook" angle now asks for launch STATUS — primary vs.
  backup/reset-after-scrub (with reason), the backup opportunity, and launch-weather odds — the
  Space-Coast utility the review found missing. The two fetch-layer contributors above (per-angle
  read cap, Reuters CAPTCHA) remain open.

  **Citation apparatus moved from prompt to engine (`dr-synth-v16`).** The v15 prose rules did not
  hold: the very next run DROPPED the `## Sources` block entirely (gpt-oss won't reliably do the
  list bookkeeping, exactly as MODEL_PROMPTING predicts). A 7-researcher design sweep converged on
  keeping the writer in-process (rejecting a spawned-writer — it breaks the `ROOT_RESERVE`
  synthesis-always-completes budget invariant — and structured/typed output — it kills the live
  stream and is fragile at a 12k-token brief) and enforcing the citation apparatus in code:
  `_finalize_sources` rebuilds the trailing `## Sources` block as a NO-RENUMBER projection of the
  in-body `[^n]` markers (both ASCII and fullwidth `【n】`, via `reflexion.cited_indices`) onto the
  curated source registry, run once after the revise settles and before persist/return/view — so
  duplicate defs, orphan entries, a missing block, and out-of-range markers are impossible by
  construction, and the positional `[^n]`→`sources[n-1]` contract (persisted chips, view
  `web_sources`) is untouched. The one-shot `_backstop_critique` became a scored gate set
  (`_draft_gates` → `VerificationResult`): the existing zero-citation + missing-heading gates plus
  a new dangling-citation gate (a marker past the registry — the one citation defect code can
  detect but not fix), with a keep-the-better-attempt guard (`reflexion.strictly_improves`) on the
  single corrective re-synth and on the revise (regression-only, generalizing the old
  "revise dropped all citations" check). The prompt (v16) stops asking the writer to author or
  reconcile the list. Freshness-as-today and over-length were deliberately NOT made code gates (no
  finalize-time ground truth for staleness; length is whole-report-neutral by design) — they stay
  with the reader date-stamp and the fuzzy `_critique`.

  **Owner-local, time-of-day-aware dates.** `_run_preset` reads the owner's stored timezone
  (`SettingsStore.owner_timezone`, fallback UTC) and auto-supplies two variables: `{{today}}` (the
  owner-local calendar day — an evening run in US Eastern is already the next UTC day, so a UTC date
  would drift a day ahead and split the dedup row) and `{{now}}` (local date + time of day).
  `daily_news` frames its window as "the last 24 hours up to `{{now}}`", so a morning run leads with
  yesterday's news and an evening run with today's, and the greeting fits the time of day.

  **Grounding critique gate (the universal net).** For the paths NOT converted to scout→read
  (default questions, candidate research, the deepest lane), robustness comes from a strengthened
  critique: a web-capable critic now runs a GROUNDING SWEEP over every concrete claim (named
  person/place/org, event, date, number) — opening its cited page to confirm the page actually
  reports THAT claim, checking STATUS/TENSE (a scheduled launch is not a completed one), and flagging
  any fabrication or unsupported claim EXPLICITLY as one to CUT or hedge so the revise pass removes
  it. This fires on every deep-research run, not just presets.
  (3) The `deep_research_report` card gained a **read-aloud play button** next to
  copy/download (`registry.tsx` `DeepResearchReport`, threaded through `ToolView`/`ViewProps` from
  the surface's `useReadAloud`), so the owner plays the report's TTS straight from the card — which
  removes the reason to have jerv paste the text into a turn at all (the root cause of the confab).
  The card feeds the read-aloud through `speakable.reportToSpeech` first, which drops the section
  HEADING lines (a spoken brief shouldn't voice "## Good Morning", which read aloud doubles the
  "Good morning…" greeting) and the trailing `## Sources` block (TTS would otherwise read the URL
  list aloud) — so the written card keeps its structure while the spoken read is clean prose.

**Engine seam** (`deep_research.py`). `_run` gained `plan_override` + `enforce_headings`.
The single branch is at the PLAN phase: with a `plan_override` the planner is skipped and the
fixed dict is used; otherwise `_plan` runs as before. `research()` gained an opt-in `preset`
+ `variables` path (`_run_preset`) that renders the preset, validates `output_kind`/`sources`,
caps the gather angles at `DR_MAX_BREADTH` (sections are uncapped — they aren't fans), and
drives `_run` with `enforce_headings=True`. The preset's objective rides the existing
deep_produce `objective` block, so no synth-prompt change was needed.

**Heading backstop.** `_backstop_critique` folds the existing zero-citation backstop together
with a new missing-heading check (preset runs only): if the writer dropped or renamed a
required section, one hardened re-synth (`STRUCTURE DEFECT`) forces it back — the same
one-retry, symptom-driven pattern as the citation backstop. Empty section → keep the heading
+ the honest sentinel, never a drop.

**Tool surface.** `deep_research.tool` gained `preset` + `variables` (and relaxed `required`
so a preset-only call — question derived from the preset — validates).

**Tests.** `test_research_presets.py` (loader/render/validation) and preset cases in
`test_deep_research.py` (planner skipped, fixed angles + outline used, missing-variable and
unknown-preset refusals, the heading backstop fires once on a preset run and never on a
default run, and the `_missing_headings` / `_backstop_critique` unit logic).

## P2 — Batch runs ◻️ (next)

Point a preset at a list; get one report each, serially, in the background, notifying per
report. Not the workflow engine — the batch rides the existing **deepest-research lane**
(`deepest_lane.py`), reusing `research_run_state` (a `batch-` run-id discriminates it) as the
durable list-cursor, `DeepResearchService.research(preset=…, variables=item, require_persist=True)`
per item, and the progress/notify channel per completion. Idempotency falls out of the report
library's `(question_hash, tool)` upsert. Fire-through by default; a verify-gate is a later
option. See the design artifact for the option analysis. (Note: the deepest-lane framing here
predates the owner's 2026-08-06 decision to avoid the deepest tool; the batch host is being
re-chosen — Jerv Tasks runner is the leading candidate — before P2 is built.)

## P3 — Compare-and-contrast preset (from the library) ◻️ (in progress)

A follow-up preset that produces ONE contrast-and-compare report across every candidate in a
race, synthesized **only from the per-candidate accountability profiles already in the research
library** — no web, no video corpus. Design decisions (owner, 2026-08-06):

- **Reports-only, no web (v1).** The compare draws exclusively from the owner's already-vetted,
  already-cited profiles. This is what makes "no fresh citations needed" coherent (every fact
  traces to a cited profile) AND makes the anti-hallucination check tractable (a closed, small
  source set that fits in context). A `reports_first` variant (reports as substance, web only to
  verify a specific time-sensitive fact, web-cited) is a later upgrade, not v1.
- **Dimension-parallel angles.** The 5 gather angles each compare ONE dimension (record, career,
  money+endorsements, positions/consistency, controversies) across the WHOLE field, with the
  candidate list passed as a `{{candidates}}` variable — so the fixed 5-angle preset handles a
  race of two candidates or nine, with no schema change.
- **Grounding gate (anti-hallucination).** Three layers: the writer may only restate what the
  profiles support and attributes every claim to a profile ("per X's profile"); the critique
  pass is run by a reports-reading reviewer that verifies the draft against the profiles and
  flags any unsupported claim; and (follow-on) a deterministic tripwire that flags any number/
  name/date in the draft absent from every source profile.

**Built (this branch):**
- `presets/compare_candidates.preset` — the outline, the five per-dimension angles, and the
  grounding/attribution objective.
- The `reports` source mode in `deep_research.py` (`_SOURCE_MODES` + the six source-mode
  helpers: `_personas_for` → the report-library personas, the "run the profiles first"
  empty-gather message, no-web/no-`[^n]`-SOURCES flags, the supplement clause).
- Two sandboxed personas in `agents.py` with digest-pinned prompts and report-library-only
  tool allowlists (`search`/`read`/`list_research_report` + clock, no web): `research_reports`
  (gather/refill) and `review_reports` (the analyst + critique grounding reviewer, whose prompt
  makes it a faithfulness gate — flag any draft claim the reports don't support).
- Discoverability: `deep_research.tool` (v6) now lists both presets and the compare workflow,
  so jerv reaches for them.
- Tests: reports-mode persona routing, the compare preset run (planner skipped, five
  dimension angles, no web/corpus persona ever spawned), the empty-gather refusal, and all the
  persona/sidecar pin updates.

**Deferred to a follow-on:** the deterministic new-entity tripwire (a number/name in the draft
absent from every source report) — the LLM `review_reports` grounding gate is the v1 check, and
a string-level number check is brittle against honest reformatting ("$10.8M" vs "$10,802,229"),
so it needs care. Also deferred: the `reports_first` web-verify variant.
