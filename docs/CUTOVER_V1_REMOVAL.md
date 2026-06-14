# V1 (`analyze_note`) removal — deferred work

## Status

The v3 `integrate_note` pipeline is now the **default** (`NOTE_PIPELINE_DEFAULT =
"integrate"`), so a fresh install runs v3 with no settings step. The cutover
**toggle** and the **legacy `analyze_note` path** are still present and are the
work captured here.

This was split off deliberately: the deletion forces rewriting the extraction
test suite, and that can only be done safely in an environment with **Docker**
(the affected tests are Postgres testcontainer integration tests). This note is
the to-do list for that session.

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
