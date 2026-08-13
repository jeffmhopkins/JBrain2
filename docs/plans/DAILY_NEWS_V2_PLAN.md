# JBrain2 — Daily News v2 (Briefing Engine) Plan

> **Status:** In progress · **Last verified:** 2026-08-13 · **Waves:** V1✅ V2◻️ V3◻️

A lean, deterministic-gather → single-writer engine for the daily-news briefing, offered
side-by-side with the existing fan pipeline so the two can be A/B'd without risk.

Peer to `NEWS_FEED_PLAN.md` (the curated feeds this reuses) and the deep-research pipeline
it forks from. Grounded in three independent clean-sheet design memos (simplicity, quality,
latency lenses) that — working blind to the current system — all reached the same
conclusion: **now that fetch/extract is reliable (the reader→byparr→Tavily recovery chain
un-walls a URL off-box), gathering no longer needs a fan of search-scout + fetch-reader
sub-agents.** Gathering is a *deterministic* step (feeds + news search + force-fetch) that
spends zero model tokens; the only unavoidable model work is *writing*.

## The problem with the pipeline for this shape

The fan pipeline (plan → scout-fan → reader-fan → analyst → synthesize → critique → revise)
was built to make a fetch-light local model rest on real article text despite bot-walls. It
works, but for the daily-news shape it is now over-engineered and slow: a live run spent a
~16-minute scout phase alone (scouts following leads into byparr dead-ends), then a reader
phase, and still produced a thin briefing. With Tavily un-walling any URL, the anti-bot-wall
machinery is solving a problem that no longer exists.

## The v2 design (what all three memos converged on)

**Deterministic gather → force-fetch → ONE tool-less writer → deterministic check → ≤1 repair.**

1. **Gather (deterministic, 0 model tokens, concurrent).** For each of six content beats
   (national, economy, world, space, ai_tech, local), pull the curated `news_feed` category
   (space/local carry full body) and `news_search` the beat, dedupe across sources.
2. **Force-fetch (deterministic, concurrent, bounded).** Open the picks via the reliable
   fetch/extract chain → clean full article text. A full-text feed item is used as-is (no
   re-fetch). Fetch failures are skipped best-effort; over-gather so a failure doesn't
   starve a section.
3. **Write (ONE model call).** A single tool-less writer — the *same* synthesizer prompt the
   pipeline uses — is handed the real article text grouped by section, plus the numbered
   SOURCES, and writes the full 8-section spoken briefing. It holds no search/fetch tool, so
   it **physically cannot write from a snippet** — the anti-hallucination guarantee is
   structural, not prompted.
4. **Validate + repair (deterministic + ≤1 model call).** A code check confirms every content
   section heading is present; a single targeted repair call re-adds any the writer dropped.
   The trailing `## Sources` block is rebuilt deterministically (`_finalize_sources`, reused).

Cost: ~1–2 model calls over pre-fetched text (vs. the pipeline's ~13 sub-agents), so the run
is bounded by one writer pass, not a fan.

## How it's offered side-by-side (no risk to daily_news)

- A new preset field **`engine`** (`pipeline` default, or `briefing`), on `Preset`/`RenderedPreset`.
  Every existing preset is `pipeline` and byte-unchanged.
- `deep_research._run_preset` branches on `rp.engine`: `briefing` → `_run_briefing` (the new
  builder), else the pipeline. Persist + frame + tool-view are shared, so a v2 report renders
  in the library and the PWA identically to a pipeline report.
- The new **`daily_news_v2`** preset carries the same 8 sections + spoken objective as
  `daily_news`, sets `engine: briefing`, and omits `news_feeds`/`min_reads` (pipeline-only
  knobs — the builder gathers itself). The shipped `daily_news` is untouched, so both run and
  can be triggered independently for A/B.

## V1 surface (shipped this plan)

- `agent/daily_briefing.py` (new) — `DailyBriefingBuilder` (deterministic per-beat gather +
  concurrent force-fetch + one writer via the LLM adapter + completeness check + ≤1 repair),
  the fixed `_BRIEFING_BEATS`, `_Article`/`BriefingResult`.
- `agent/research_presets.py` — the `engine` field.
- `agent/deep_research.py` — `_PRESET_ENGINES`, the `_run_preset` branch, `_run_briefing`
  (frames + persists like `_run`); `DeepResearchService` gains `searxng`/`fetcher` handles.
- `agent/readtools.py` + `main.py` — thread the same `SearxngClient`/`WebFetcher` in.
- `agent/presets/daily_news_v2.preset` (new).
- Tests: `test_daily_briefing.py` (gather/fetch/write/repair/degrade/routing), preset
  engine-field tests in `test_research_presets.py`.

## Deferred

- **V2 — measured tuning + selection.** After A/B on the box: add the one **triage/selection
  call** the synthesis flagged (deterministic outlet-count salience mis-ranks the day's
  biggest story — the "missed the eclipse / empty World" failures were selection failures),
  tune per-beat quotas / body caps, and decide ledes-vs-full-body.
- **V3 — streaming + promotion.** Stream the writer call so the PWA shows the briefing being
  written (the builder is currently non-streaming); if v2 wins the A/B, point the scheduled
  daily task at it and retire the pipeline path for this preset.

## Hardened after adversarial review

V1 was adversarially reviewed; the confirmed defects are fixed: a totally-empty gather now
**refuses** rather than shipping a hollow "nothing happened anywhere" briefing (empty-gather
parity with the pipeline), and a partial blank flags **coverage_limited**; the
section-completeness check is **heading-line based and `&`≡"and" normalized** (a whole-body
substring test both missed dropped headings whose topic appears in prose and needlessly fired
the repair when the writer spelled `&` as "and"); the fetch semaphore is now **global** (was
6× per-beat); `_canon` strips **tracking params** so a feed's `?utm_medium=rss` copy folds
with the bare search URL; gather is **exception-isolated** (an off-contract raise skips one
lead, never crashes the run); dangling `[^n]` markers are neutralized before the Sources block
is rebuilt; and the progress phase moves to "Writing" at the right moment.

## Tradeoffs / open risks

- **No live judgment in selection yet (V1).** Selection is deterministic (feed + search
  order); the biggest-story-per-section guarantee is V2. Mitigated short-term by over-gathering
  and heavier quotas on the space/AI beats.
- **Writer calls aren't tree-budget/deadline governed (V2).** The builder makes 1–2 direct
  adapter calls (usage is still recorded at the router for cost); a per-run wall-clock/token
  ceiling is a V2 item, low-risk for a 1–2 call engine.
- **Non-streaming writer (V1).** The PWA shows a "Writing" phase but not live text until V3.
- **Leans entirely on reliable fetch/extract.** If Tavily/the fetch chain degrades, the
  summary beats thin out (the builder skips failed fetches; a total failure now refuses).
