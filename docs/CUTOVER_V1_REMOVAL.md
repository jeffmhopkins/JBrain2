# V1 (`analyze_note`) removal — DONE

## Status

**Complete.** `integrate_note` is the only note→graph path. The cutover toggle
(`NotePipeline`/`note_pipeline`/`analysis_job_kind`), its DB key, the two v1-only
queue backfills, and `AnalysisPipeline.analyze_note` are removed; every enqueue
site emits `integrate_note` directly and the worker runs the integration backfill
unconditionally at boot. Full suite green (1264 passed, 13 skipped, 2 xfailed),
96% backend coverage. The remainder of this file is kept as the record of what
moved and the tracked residual below.

## Residual (tracked follow-ups)

Migrating the suite surfaced behaviour that is genuinely different under
integrate (resolution + per-fact domain are the agent's job, not the
deterministic resolver / per-fact tag). These tests are **skipped** with a
`CUTOVER` reason rather than deleted, so a focused session can rework them:

- `tests/integration/test_extraction_pg.py` — 6 red-team derived-shadow /
  cross-subject lifecycle tests (`test_cross_subject_inverse_*`,
  `test_*_shadow*`, `test_conflict_resolution_cascades*`,
  `test_entity_view_set_valued*`, `test_ambiguous_mention_*`). They assert v1
  resolver + `_apply` behaviour and need an explicit integrate intent
  (`cross_subject`/`ambiguous` flags) + assertion revision. The core
  cross-subject firewall is still covered by `test_apply_intent_pg`.
- `tests/integration/test_entity_resolution_pg.py` — 7 `run_note` tests driving
  the deterministic resolver/disambiguation through the pipeline; that path is
  bypassed under integrate (the agent resolves every ref). The resolver layers
  stay covered by the direct `resolve_entity` units in the same file, and
  declared-name/collision by harness scenarios.

Two real integrate-path gaps the cutover exposed (no longer masked by v1):

- The `extraction_truncated` review is never filed: `plan_to_extraction`
  reconstructs the `Extraction` with `dropped_facts=0`, so the per-note fact cap
  still fires but the user-facing card does not. (Drove the removal of the
  `adv_over_extraction_no_cap` harness scenario.)
- A low-confidence OCR fact only stays held if the agent marks it `inferred`:
  `effective_weight` grants a surface-attested fact its full ceiling regardless
  of self-confidence (weight.py), so a blurry-OCR reading tagged
  `inferred=False` would commit and supersede a confident prior. The harness
  `health_low_confidence_ocr_guard` scenario now scripts the `inferred` intent.

## How the test suites were migrated (for reference)

- Scenario harness: each step scripts BOTH model calls — `note.extract` and a
  compiled `integrate.note` intent (explicit when authored, else a name-match
  default derived from the PARSED extraction), with existing-entity refs
  resolved to live ids at step time. 9 scenarios removed as not-applicable
  (per-fact-domain promotion/demotion; deterministic-resolver hops).
- `test_extraction_pg`: a shared `_IntegrateDriver` presents `analyze_note`'s
  one-call surface but drives `integrate_note` with the name-match default
  intent (reused by `test_reanalysis_pg`).

## Why it wasn't done in-session

- No Docker → the ~30 affected integration tests **skip locally**; they can only
  be validated by CI, blind.
- The migration is **not mechanical**: `analyze_note` does `extract → _apply`
  (direct write); `integrate_note` does `extract → Integrator (LLM judgment) →
  plan_intent → apply_intent`. Each migrated test must additionally fake the
  Integrator's intent, and assertions change because `apply_intent` produces
  different graph states than the direct write (dispositions, review cards,
  supersession closure).

## Production code to remove

- `settings_store.py`: `NotePipeline`, `NOTE_PIPELINES`, `NOTE_PIPELINE_DEFAULT`,
  `NOTE_PIPELINE_KEY`, `ANALYZE_JOB`, `INTEGRATE_JOB`, `note_pipeline()`,
  `analysis_job_kind()`.
- `queue.py`: the `analysis_job_kind` protocol method + `PgJobQueue` impl;
  `backfill_unanalyzed_notes`; `backfill_unlinked_relationship_facts`. Simplify
  `has_active_analysis` and `backfill_pending_integration`'s cross-kind guard to
  `integrate_note` only.
- `ingest/pipeline.py`, `ingest/ocr.py`, `api/notes.py`: already enqueue via
  `analysis_job_kind`; change to enqueue `"integrate_note"` directly.
- `worker.py`: drop the `note_pipeline == "integrate"` boot branch (run
  `backfill_pending_integration` unconditionally); remove the `"analyze_note"`
  handler entry; drop the v1 relink backfill call.
- `install_wipe.py`: remove `_enable_v3` (+ its call, the `NOTE_PIPELINE_KEY`
  import, and the now-unused `json` import) — the default makes it redundant.
- `analysis/pipeline.py`: delete `AnalysisPipeline.analyze_note` and any v1-only
  helper not shared with `integrate_note` (note: `_extract_note`, `_apply`, and
  the extraction helpers are shared — keep what `integrate_note`/`apply_intent`
  use).

## Tests to migrate or delete

The deep migration (need the Integrator faked + assertions revisited):

- `tests/integration/test_extraction_pg.py` — ~29 tests / 38 `analyze_note`
  calls. The bulk of the work.
- `tests/harness/runner.py` + `tests/harness/scenarios/*.json` — the scenario
  harness drives through `analyze_note`; repoint to `integrate_note`.
- `tests/integration/test_reanalysis_pg.py` — re-run-analysis behavior.
- one `analyze_note` call each in `tests/integration/test_ocr_pg.py` and
  `tests/integration/test_entity_resolution_pg.py`.
- `tests/integration/test_analysis_gating_pg.py` — currently pinned to v1 via the
  `_pin_v1_pipeline` autouse fixture (added when the default flipped). Migrate the
  gate assertions to `integrate_note` and remove the pin. The draining tests need
  an Integrator fake.

Mechanical edits/deletes:

- `tests/integration/test_settings_pg.py` — delete
  `test_analysis_job_kind_follows_the_cutover_toggle`.
- `tests/integration/test_cutover_pg.py` — relies on the cross-kind guard;
  revisit when the guard is simplified.
- `tests/integration/test_queue_pg.py` — delete the
  `backfill_unanalyzed_notes` / `backfill_unlinked_relationship_facts` tests.
- `tests/unit/test_install_wipe.py` — remove the `_enable_v3` monkeypatch +
  `test_enable_v3_writes_the_integrate_toggle_key`.
- `tests/unit/test_notes_api.py` — drop the `analysis_job_kind`/`pipeline_kind`
  fake; the analyze endpoint enqueues `integrate_note` directly now.
- `tests/unit/test_worker.py` — drop the `note_pipeline` / v1-backfill fakes and
  switch the dispatch-test job kind to `integrate_note`.

## Watch-outs

- Coverage gate (80% backend) — don't let the migration silently drop coverage of
  extraction/temporal/naming/dedup behavior; it must be re-asserted *through*
  `integrate_note`.
- A queued `analyze_note` job after the handler is removed hard-fails. Fine on a
  fresh DB (none exist); for an upgraded DB, drain the queue first.
- Confirm `integrate_note` writes the `note_analysis` row the "analyzed" status
  reads (it should — verify under test).
