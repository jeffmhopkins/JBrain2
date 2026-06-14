# DBOS spike + de-confliction contract

**Role:** Researcher F / implementer — the proof-of-concept that validates the
DBOS decision against a live runtime, run **collision-free** alongside the
concurrent note-ingestion → entity-graph session.
**Date:** 2026-06-14
**Status:** spike landed and green (10 unit + 3 integration tests).

## De-confliction contract (why this can't collide)

The note-analysis session owns a large, active surface (`analysis/**`, `ingest/**`,
the entity-graph schema, and the Alembic chain — they are adding `0029`/`0030`…).
The spike was therefore built to touch **none** of it. The rules:

| Off-limits (theirs) | This spike (mine) |
|---|---|
| `backend/src/jbrain/ingest/**`, `analysis/**` | new `backend/src/jbrain/workflow/**` only |
| any new Alembic migration (`0031+` collides) | **no app migration** — DBOS owns its `dbos` schema via `dbos migrate` |
| `main.py`, `worker.py`, `queue.py` (shared entrypoints) | not wired into the worker; driven by a test |
| `pyproject.toml` / `uv.lock` (shared) | **`dbos` dep deferred** — installed transiently to develop/run; committed when we wire it in, ideally after their migration churn settles |
| entity/graph docs | `docs/research/workflow-engine/**` |

Result: the diff is **new files only**. Nothing merges into a file the other
session edits, and no Alembic revision is added to the shared chain.

## What the spike proves (and where)

`backend/src/jbrain/workflow/`:
- **`registry.py`** — the unified block registry (the convergence target for the
  agent's `.tool` sidecars and Phase-5 `actions`: one library, two callers). Pure
  Python; fields mirror `ToolSpec` so convergence is a merge, not a rewrite.
- **`safety.py`** — `assert_reference_shaped`, the executable form of DBOS adoption
  **condition #1** ("IDs not payloads"): step payloads must be reference-shaped so
  nothing firewalled is serialized into DBOS's system schema.
- **`spike.py`** — a self-contained DBOS workflow exercising the four load-bearing
  primitives on synthetic IDs.

`backend/tests/`:
- **`unit/test_workflow_registry.py`** (10 tests) — registry validation + the
  reference-shape guard. Runs anywhere (no Postgres).
- **`integration/test_workflow_spike_pg.py`** (3 tests) — DBOS against a real
  testcontainers Postgres.

| Claim from the research | Proven by | Result |
|---|---|---|
| Durable multi-day human approval (`set_event`/`recv` → `send` by workflow ID) | `test_durable_approval_pause_and_resume` | pass — run parks pending, resumes on owner decision |
| Conditional gate + fan-out via a bounded `Queue` | `test_digest_runs_and_fans_out` | pass |
| Schedule | `@DBOS.scheduled("0 2 * * 1")` in `spike.py` | defined (cron registered) |
| Condition #1 — nothing firewalled in `dbos` schema | `test_no_firewalled_payload_in_system_schema` (scans persisted in/out) | pass |
| Determinism discipline (condition #2) — every effect a `@DBOS.step` | `spike.py` structure | demonstrated |

Note: the bare sandbox had no obvious Docker daemon, but testcontainers' bridge-less
fallback (per `tests/conftest.py`) brought up Postgres, so the integration tests
**actually ran end-to-end here**, not merely in CI.

## The four DBOS adoption conditions — spike status

1. **`dbos`-schema RLS exception + IDs-not-payloads guard** — guard implemented
   (`safety.py`) and enforced against the live system schema. ✔ seeded
2. **Determinism discipline as a `DEVELOPMENT.md` standard** — demonstrated in
   `spike.py`; the written standard is still to be added. ◻ pending
3. **Alembic / `dbos migrate` boundary** — spike uses `dbos_system_schema="dbos"` on
   the same Postgres; the deploy-step wiring + `dev-setup.sh` update land with the
   dependency commit. ◻ pending
4. **Version-aware deploy for paused workflows** — not exercised (no deploy in a
   spike); to be addressed in the plan. ◻ pending

## Next steps (post-spike)

- Commit the `dbos` dependency (`pyproject` + `uv.lock` + `dev-setup.sh`, per
  non-negotiable #8) — sequenced to avoid lock conflict with the other session.
- Write the `DEVELOPMENT.md` determinism + IDs-not-payloads standard (conditions
  #1/#2).
- Decide the **promotion path** (agent skill → standing scheduled/triggered
  pipeline) — the one open design seam from the README.
- Only after the ingestion→entity work merges: migrate the real ingest pipeline
  (the OCR gate + chunk/embed fan-out) onto the engine, side-by-side with a
  row-diff before cutover.
