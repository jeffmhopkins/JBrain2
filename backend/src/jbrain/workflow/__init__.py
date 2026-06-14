"""Phase-5 workflow engine — spike stage.

This package is the isolated landing zone for the workflow-engine work decided in
`docs/research/workflow-engine/` (adopt DBOS Transact; two-surface model over one
shared block registry). It is deliberately self-contained: it imports nothing from
`jbrain.ingest` / `jbrain.analysis` and adds no Alembic migration, so it cannot
collide with the concurrent note-ingestion → entity-graph work. The real ingestion
and wiki pipelines move onto this engine only after that work lands.

What lives here today (the spike):
- `registry` — the unified action/block registry (the convergence target for the
  agent's `.tool` sidecars and Phase-5 `actions`: one library, two callers).
- `safety` — the "IDs not payloads" guard (DBOS adoption condition #1): nothing
  firewalled may be serialized into DBOS's system schema, so step payloads must be
  reference-shaped.
- `spike` — a self-contained DBOS workflow proving the load-bearing primitives
  (conditional gate, fan-out, durable multi-day approval pause, schedule) and the
  determinism/IDs-only discipline. DBOS needs a Postgres system DB, so `spike` is
  exercised by the testcontainers integration test, not imported at app import time.
"""
