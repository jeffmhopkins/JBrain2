# Build-vs-Buy: Workflow Engine Orchestration Layer (Phase 5)

**Investigation role:** Researcher A — orchestration-layer build-vs-buy survey.
**Scope:** the engine that runs `events → triggers → pipelines → actions → runs`,
evaluated under the **original** "zero new runtime dependencies" goal (Round 1).
**Date:** 2026-06-14
**Confidence tags:** `[web]` = found via live search this session (URL cited);
`[repo]` = read from the codebase this session.

> Round-2 note: the owner later relaxed the zero-deps goal, which is what
> dossiers D–E explore. This dossier is the build-side baseline; its conclusion
> ("the hard part is data modeling, not the runtime") still holds and informs the
> DBOS adoption — DBOS retires the runtime mechanics, not the modeling.

## Bottom line up front

**Build on the existing SKIP LOCKED queue.** The custom queue in
`backend/src/jbrain/queue.py` already provides ~70% of what Phase 5 needs (durable
claim/complete/fail, exponential backoff, stale-lock reaping, RLS-scoped sessions,
duplicate guards, run_after delays). What's missing — events/triggers,
pipeline-as-data sequencing, a scheduler, and run logs — is genuinely new
*modeling work* that no library hands you for free in the exact shape Phase 5
specifies. Every buy candidate either violates the Postgres-only/no-broker
constraint or imposes its *own* opinionated state model that fights your per-table
RLS requirement. **DBOS Transact is the only library worth a second look**, and
even it has a structural mismatch worth understanding before adopting.

## 1. Comparison table

| Candidate | Postgres-only? | New runtime deps? | DAG/sequencing? | Scheduler? | Run-log/observability? | Async-Python/FastAPI fit? | Maturity | Single-box operable? |
|---|---|---|---|---|---|---|---|---|
| **Extend custom queue (BUILD)** | already | **None** | must build | must build | must build (trivial — you own the tables) | native (it's your code) | n/a | already runs this way |
| **Procrastinate** | Postgres-only, SKIP LOCKED + LISTEN/NOTIFY | 1 pip lib (no broker) | **no workflow/DAG/chaining** | periodic/cron | job stats + states only (per-task, not pipeline runs) | async-first, ASGI-friendly | Mature, active | yes |
| **DBOS Transact (py)** | Postgres-backed, in-process | 1 pip lib (no server) | durable multi-step workflows + queues | `@DBOS.scheduled()` cron | workflow rows in Postgres, queryable | async steps, FastAPI integration | Young but active (~1.4k★, v2.x, MIT) | yes |
| **Hatchet** | Go engine + RabbitMQ + Postgres | Separate server + broker | yes | yes | yes | client lib only | Mature | **no — disqualified** |
| **Windmill** | Rust server + worker processes | Separate server stack | yes | yes | yes | not a library | Mature | **no — disqualified** |
| **Prefect** | server/API + agent processes | Separate server | yes | yes | yes | Python | Mature | **no — heavy for one box** |
| **Dagster** | daemon + webserver + metadata DB | Multiple processes | yes | yes | yes | Python | Mature | **no — disqualified** |
| **Temporal** | Server cluster + persistence + visibility | Whole control plane | yes | yes | yes | SDK | Mature | **no — heaviest** |
| **Airflow** | scheduler + webserver + metadata DB | Multi-process | yes | yes | yes | Python | Mature | **no — disqualified** |

The bottom five share the **same one-line disqualifier**: each needs a separate
long-running server process (Hatchet additionally a broker), which directly
violates "no Redis, no broker, no separate server process, stays operable by one
person on docker compose."

## 2. The real contest: Procrastinate vs DBOS vs Build

**Procrastinate** is named in `docs/ARCHITECTURE.md`, and it *is* a clean
Postgres-only fit (SKIP LOCKED + LISTEN/NOTIFY, periodic tasks, async-first). But
it is a **task queue, not a workflow engine** — no task chaining, no dependency
graph, no DAG, no pipeline-as-data construct (confirmed against its docs index and
discussions). Adopting it means **swapping your custom queue for a third-party
queue that solves the part you've already solved**, while leaving the actual
Phase-5 deltas (events → triggers → pipelines → runs, DAG sequencing) for you to
build *on top of a schema you don't control*, whose `procrastinate_jobs` tables
you'd still wrap in RLS + isolation tests. Net-negative: a new dependency that
retires none of the hard work.

**DBOS Transact** is the genuinely interesting "buy" that respects the
constraints: an **in-process MIT library** (no server, no broker), Postgres-backed,
giving exactly the three missing pieces — durable multi-step **workflows**
(`@DBOS.workflow`/`@DBOS.step`), **cron scheduling** (`@DBOS.scheduled()`),
**durable queues**, and a **queryable execution history** in Postgres. Async steps;
documented FastAPI integration.

**The catch with DBOS** (why it's not automatic):
1. **It owns its own state model** — a separate "system database" with its own
   schema and decorator-driven programming model. Your rule is "every new table
   needs RLS + an isolation test." DBOS's tables are library-managed, not your
   Alembic migrations, and don't fit the single-owner RLS pattern cleanly.
2. **Programming-model lock-in** — DBOS pipelines are *Python code with
   decorators*, not **data-defined pipeline rows**. Phase 5's exit criterion is
   "ingest and a scheduled job run as *user-defined pipeline definitions*…
   defined as data." DBOS gives durable *code* workflows, so you'd still build
   the data-definition layer yourself; DBOS becomes an execution substrate, not
   the engine — and your queue already *is* a substrate you fully control.

**Build (extend the custom queue)** wins under zero-deps because Phase 5 is
fundamentally a **data-modeling task**, and you already own a battle-tested
runtime. `docs/ROADMAP.md` (lines 103–104): *"The actions are built first (they
work as enqueued jobs today); this phase only gives them their scheduled and
manual triggers."* Mapping existing assets:

- **`actions`** = today's handlers (`ingest_note`, `analyze_note`, `embed_note`,
  `consolidate_predicates`, …) — already exist, idempotent, run as `app.jobs` rows.
- **`runs`** = a thin layer over `app.jobs` (already has `attempts`, `last_error`,
  `status`, `locked_at`, `finished_at`); a `runs` table grouping a pipeline's jobs
  + per-step status gives "failures diagnosable from run logs alone." Observability
  primitives (`worker.job_done`/`job_failed`/`job_unhandled` structlog events)
  already emit.
- **`pipelines`/`triggers`/`events`** = the genuinely new tables, modeled as data,
  each with RLS + isolation test (required regardless of build-vs-buy).
- **Scheduler** = a new periodic sweep in the existing `run_loop` (it already has a
  heartbeat clock and a backfill phase — no new process).
- **On-demand/emergency trigger** = a FastAPI endpoint inserting an `event` (or
  enqueuing a pipeline run), reusing `enqueue()` + the `has_active()` duplicate
  guard.

## 3. Risks / open questions the implementation plan must resolve

1. **DAG sequencing in a transaction-safe model.** Today the ingest multi-step flow
   is encoded as handlers *enqueuing the next job* (the OCR fallback in
   `worker._after_exhaustion` / `enqueue_analysis_fallback`). Phase 5 needs
   **declarative pipeline definitions** with fan-out/fan-in and conditional gates
   (the OCR-before-analysis gate is the canonical hard case), and a `run` row that
   tracks partial progress so a crashed worker resumes mid-pipeline. This is the
   core build effort and the main thing a library could help with — re-evaluate
   DBOS for *this* step if hand-rolled sequencing proves fragile.
2. **Scheduler correctness on a single-threaded loop.** A "due periodic triggers"
   sweep must guarantee exactly-one-fire per interval across restarts (cf. the
   `backfilled` flag's restart-idempotency pattern); a `schedules` table with
   `next_run_at` claimed via SKIP LOCKED, with an explicit catch-up policy.
3. **RLS for the new tables (+ any library's tables).** `events`, `triggers`,
   `pipelines`, `runs` each need an owner-only policy + isolation test
   (non-negotiable #3). If DBOS is adopted, its system-DB tables fall *outside*
   Alembic/RLS — the plan must state whether that's acceptable for a single-owner
   box. This alone may settle build-vs-buy.
4. **Run-log granularity vs. retention.** "Diagnosable from logs alone" needs
   per-step inputs/errors, but the queue stores **row IDs only, never note
   content** (so one owner-only policy covers `app.jobs`). The `runs` log must
   preserve that, and define retention/pruning so it doesn't grow unbounded on a
   4GB box.

## Sources
- `[repo]` `backend/src/jbrain/queue.py`, `backend/src/jbrain/worker.py`;
  `docs/ROADMAP.md` (Phase 5, lines 90–107).
- `[web]` [Procrastinate GitHub](https://github.com/procrastinate-org/procrastinate) ·
  [Procrastinate docs](https://procrastinate.readthedocs.io/) (periodic tasks; SKIP
  LOCKED + LISTEN/NOTIFY; no workflow/DAG in docs)
- `[web]` [DBOS Transact-py GitHub](https://github.com/dbos-inc/dbos-transact-py) ·
  [DBOS Transact](https://www.dbos.dev/dbos-transact) ·
  [DBOS Configuration](https://docs.dbos.dev/python/reference/configuration) ·
  [Why Postgres for durable execution](https://www.dbos.dev/blog/why-postgres-durable-execution)
- `[web]` [Temporal self-hosting](https://kestra.io/resources/infrastructure/temporal-alternatives) ·
  [Hatchet GitHub](https://github.com/hatchet-dev/hatchet) ·
  [Windmill (Rust server)](https://www.windmill.dev/blog/launch-week-1/fastest-workflow-engine) ·
  [Orchestration tools comparison](https://www.bytebase.com/blog/top-open-source-workflow-orchestration-tools/)
