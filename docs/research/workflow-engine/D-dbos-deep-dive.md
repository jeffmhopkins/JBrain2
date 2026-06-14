# DBOS Transact — Deep Fit Assessment

**Investigation role:** Researcher D — deep fit-assessment of DBOS Transact against
the two flagship workflows.
**Scope:** Round 2, after the owner relaxed the zero-deps goal. Judge DBOS for
note ingestion and nightly wiki maintenance; resolve the two tensions that
previously set it aside (system-DB vs RLS; code- vs data-defined pipelines).
**Date:** 2026-06-14

**Bottom line: ADOPT WITH CAVEATS.** DBOS is a strong fit for both flagship
workflows. It is a pip-only, in-process, Postgres-backed library — no separate
server or broker — satisfying the single-box / one-Postgres / no-Redis constraint.
The two historical objections (self-managed system DB vs RLS; code- vs data-defined
pipelines) are **real but manageable** trade-offs, not dealbreakers. The decision
hinges on four conditions (end).

## 0. "No separate server/broker" — CONFIRMED
DBOS runs inside your existing application process and uses your existing Postgres
to store/recover workflow state. Queues, scheduling, and message-passing are all
Postgres-backed (rows + `LISTEN/NOTIFY`), not a broker. You call `DBOS.launch()`
inside the existing process; nothing else runs. It adds one hard dependency —
Postgres only (SQLite for dev/test) — which aligns with the stack.
([dbos-transact](https://www.dbos.dev/dbos-transact) ·
[database-connection](https://docs.dbos.dev/python/tutorials/database-connection))

## 1. Durable workflows & steps
`@DBOS.workflow()` decorates an orchestrator; `@DBOS.step()` decorates each unit of
nondeterministic work (LLM call, DB read, storage I/O, OCR). Step outputs
checkpoint to Postgres; on crash/restart DBOS *recovers from the last completed
step* — completed steps are never re-executed, incomplete steps retried
at-least-once (so **steps must be idempotent**).

**Async: first-class** (`async def` workflows; `DBOS.sleep_async`,
`start_workflow_async`, `enqueue_async`, `list_workflows_async`). Matches Python
3.11 async / FastAPI / asyncpg.

**Critical constraint — determinism.** The workflow *body* must be deterministic;
every nondeterministic thing — **every LLM-adapter call, SQLAlchemy query, storage
read** — must live inside a `@DBOS.step()`. Standard `if/for` is fine when branch
inputs come from prior steps. This is the #1 footgun (async concurrent steps
especially).

```python
@DBOS.workflow()
async def ingest_note(note_id):
    artifact = await extract_attachments(note_id)        # step
    if await needs_ocr(artifact):                        # step → bool
        await run_ocr_and_wait(artifact)                 # child workflow / queue + await
    chunks = await chunk_multi_granularity(note_id)      # step: paragraph + section
    handles = [DBOS.enqueue_workflow("embed_q", embed_chunk, c) for c in chunks]
    for h in handles: await h.get_result()
    await extract_facts_entities(note_id)                # LLM step w/ citations
```
([workflow](https://docs.dbos.dev/python/tutorials/workflow-tutorial) ·
[step](https://docs.dbos.dev/python/tutorials/step-tutorial) ·
[async is secretly deterministic](https://www.dbos.dev/blog/async-python-is-secretly-deterministic))

## 2. Conditional gating + fan-out — both flagship "hard parts" expressible
- **Conditional OCR branch**: `needs_ocr()` is a step returning a bool; the workflow
  `if`-branches.
- **OCR-wait gate**: child workflow + await (`handle =
  DBOS.start_workflow(run_ocr, …); await handle.get_result()`) durably blocks the
  parent; recovery resumes both. Or queue + handle for concurrency control (e.g.
  `worker_concurrency=1` on a heavy OCR queue).
- **Fan-out** (note → many chunks → embeddings): DBOS queues — enqueue one workflow
  per chunk, collect handles, await all. `worker_concurrency`/`concurrency` +
  `limiter={"limit":50,"period":30}` rate-limits the embedding API.
  `DBOS.wait_first()` reacts as results complete. Strictly better than hand-rolled
  fan-out.
([queues](https://docs.dbos.dev/python/tutorials/queue-tutorial) ·
[queue reference](https://docs.dbos.dev/python/reference/queues))

## 3. Scheduling — fit for nightly wiki build
Cron via `@DBOS.scheduled('0 2 * * *')` (croniter syntax, UTC, optional seconds);
the decorator form is now deprecated in favour of the runtime
`DBOS.create_schedule()` API (runtime pause/resume/delete — useful for "emergency
manually-triggerable" while still callable via `DBOS.start_workflow`).
**Exactly-once-per-interval across restarts**: `mode=SchedulerMode.ExactlyOncePerInterval`
fires a run missed during downtime on restart. **Catch-up** via
`DBOS.backfillSchedule` / `automaticBackfill: true`; already-executed intervals are
skipped. ("Cost scales with the day's notes" is a workflow-design property —
operate on delta facts — not a DBOS feature.)
([scheduled workflows](https://docs.dbos.dev/python/tutorials/scheduled-workflows) ·
[dynamic scheduling](https://www.dbos.dev/blog/workflows-as-code-dynamic-scheduling))

## 4. Human-in-the-loop / durable multi-day pause — THE make-or-break, and DBOS nails it
DBOS ships a dedicated **Human-in-the-Loop "agent inbox"** example matching the
split/merge-approval pattern:
- The workflow calls `DBOS.recv(topic, timeout_seconds)` which durably waits a
  configurable length (**hours or days**), recovering from restarts; on restart it
  replays to the same `recv()` and reads the persisted message. `DBOS.sleep()` is
  likewise durable.
- **Correlating an approval from a REST endpoint** is by **workflow ID**: the
  endpoint calls `DBOS.send(workflow_id, decision, topic)`, waking that workflow.
  Messages persist with exactly-once delivery.
- **The review inbox is a query**: `DBOS.list_workflows(status="PENDING", …)` +
  `DBOS.set_event("status", "pending_approval")`. The workflow state *is* the inbox
  (keep your own RLS-scoped `review_inbox` row for UI if desired).

```python
@DBOS.scheduled('0 2 * * *')
@DBOS.workflow()
async def nightly_wiki(scheduled, actual):
    clusters = await match_delta_facts_to_wiki()      # step: hybrid search on delta only
    for cl in clusters:
        action = await triage_cluster(cl)             # cheap LLM step → update/create/split/merge/ignore
        if action in ("split", "merge"):
            DBOS.set_event(f"pending::{cl.id}", cl.summary)
            decision = DBOS.recv(topic=f"approve::{cl.id}", timeout_seconds=7*24*3600)
            if decision != "approved": continue
        await rewrite_article_with_citations(cl)      # step → versioned revision
```
Approval endpoint: `DBOS.send(wf_id, "approved", topic=f"approve::{cluster_id}")`.
([agent-inbox](https://docs.dbos.dev/python/examples/agent-inbox) ·
[workflow-communication](https://docs.dbos.dev/python/tutorials/workflow-communication))

## 5. System database & RLS reconciliation — real tension #1, manageable
**Same Postgres, separate schema: YES.** DBOS stores workflow/step state in its own
**system database**; the Postgres schema is configurable via `dbos_system_schema`
(default `dbos`) and can be the *same Postgres instance* (`system_database_url`).
One Postgres, DBOS in a `dbos` schema alongside `public`.

**The honest RLS conflict.** Non-negotiable #3 is "RLS on *every* table + isolation
test." DBOS auto-manages its `dbos`-schema tables outside Alembic and without RLS —
strictly, a violation of the letter. What the owner must explicitly accept:
- The `dbos` schema is a **quarantined, DBOS-owned namespace** of operational
  metadata, not firewalled domain data. The sharp edge: **serialized step
  inputs/outputs land there**, so passing health/finance payloads as step args
  would put them in `dbos` unprotected. **Mitigation: pass IDs/handles, not raw
  firewalled content** — steps re-fetch via RLS-scoped sessions. A genuine new
  constraint to document.
- Single-owner + the worker already runs owner-context crossing all firewalls, so
  the *between-tenant* RLS absence is moot (one tenant). The residual risk —
  defense-in-depth against your own code leaking domain data into operational
  metadata — is addressed by the "IDs not payloads" rule.
- Pragmatic path: treat `dbos` as an **explicitly-documented exception** (outside
  Alembic/RLS) **plus a guard test** asserting no firewalled values are serialized
  into step args — adapting the spirit of the RLS-isolation rule.

**Atomic step-write + checkpoint.** DBOS *can* commit a DB write and its checkpoint
in one transaction — but only via **datasources** (`SQLAlchemyDatasource` /
`AsyncSQLAlchemyDatasource`, `@ds.transaction()`), which manage their own
connection pool (complicating routing the write through your RLS-scoped session). A
plain `@DBOS.step()` doing a SQLAlchemy write is **not** atomic with its checkpoint
(write commits, then checkpoint — crash between → step replays; hence idempotency).
For most JBrain2 steps, **idempotent steps on your existing RLS session** is simpler
and keeps RLS intact; reserve datasources for the rare true write+checkpoint
atomicity need.
([database-connection](https://docs.dbos.dev/python/tutorials/database-connection) ·
[configuration](https://docs.dbos.dev/python/reference/configuration) ·
[datasources](https://docs.dbos.dev/python/reference/datasources))

## 6. Migrations / Alembic coexistence
DBOS auto-creates/migrates its system tables; control it explicitly — CLI `dbos
migrate` creates the system DB + internal tables (for the "app runs with minimum
privileges" case); `dbos reset` drops that metadata. Operational pattern: **Alembic
owns `public`; `dbos migrate` owns `dbos`; never cross.** Run `dbos migrate` next to
`alembic upgrade head`. Sharp edge: **the DBOS system-DB schema changes across
library versions**; upgrades re-run `dbos migrate` — combined with fast cadence,
budget upgrade testing.
([cli](https://docs.dbos.dev/python/reference/cli) ·
[configuration](https://docs.dbos.dev/python/reference/configuration))

## 7. Code-defined vs data-defined pipelines — real tension #2
Phase-5 wanted pipelines as **data (rows)**; DBOS workflows are **Python +
decorators** — the opposite.
- **What you lose**: editing a pipeline (reorder, add a stage, change branching)
  needs a code change + deploy; you can't author a brand-new pipeline *shape* purely
  as data.
- **What you keep**: a thin data/registry layer dispatching *into* DBOS workflows.
  An `app.jobs`-style table becomes a trigger/parameter layer ("run pipeline X for
  note Y with config Z"); a small dispatcher maps `pipeline_name → @DBOS.workflow`.
  Inside a workflow you data-drive *parameters* (chunkers, models, thresholds,
  skip-flags) by reading a config row in an early step and branching deterministically.
  So **parameterization stays data; topology becomes code.**
- **Net**: for two stable-shaped, conditional-logic-rich, human-gated pipelines,
  code-defined durable workflows are an acceptable — arguably better — substitute,
  because the hard parts (OCR gate, fan-out, multi-day approval, crash-resume) are
  exactly what's painful as generic data-driven handlers and exactly what DBOS makes
  trivial. The cost is "redefine structure without a deploy," small for a
  single-owner system. Revisit if you later want many user-authored pipelines.

> Decision update (post-research): the owner chose to drop the data-defined DSL
> entirely and let the **agent** compose interactively (README two-surface model),
> which makes this tension moot for the agentic surface and accepts code-defined
> topology for the deterministic pipeline surface.

## 8. Testing — clean fit
`DBOS.destroy()` → reconstruct `DBOS(config=…)` pointing at the **testcontainers
Postgres** → `DBOS.reset_system_database()` → `DBOS.launch()`. Steps/workflows are
just Python functions, so the **existing LLM-adapter fake and storage fake work
unchanged** via `mock.patch`. System DB runs in a `dbos` schema on the same
testcontainers Postgres — no extra infra. Compatible with the 80%-coverage gate and
"real Postgres via testcontainers." (No documented local time-travel/replay debugger
for Python — that's a Cloud/console feature.)
([testing](https://docs.dbos.dev/python/tutorials/testing))

## 9. FastAPI integration & operability
`DBOS(fastapi=app, config=config)` then `DBOS.launch()` — runs **in the same process
as uvicorn**; coexists with a custom async worker; migrate handlers into workflows
incrementally. `DBOS.listen_queues([...])` lets one process pick which queues it
drains. **Observability**: an optional admin server (`run_admin_server=True`, port
3001) + Conductor/console UI (commercial/Cloud — ignorable); crucially **run state
is plain rows in `dbos`**, queryable with SQL, plus `dbos workflow
list/get/steps/cancel/resume/fork` CLI and `DBOS.list_workflows()`. So you build your
own RLS-scoped Ops view by querying `dbos` directly.
([widget-store](https://docs.dbos.dev/python/examples/widget-store) ·
[cli](https://docs.dbos.dev/python/reference/cli))

## 10. Maturity / risk / lock-in / exit cost
- **Version/cadence**: latest stable **2.23.0 (Jun 1, 2026)**, "Production/Stable";
  very fast (~9 minor releases in 3 months). Pin + budget tested upgrades (system-DB
  schema evolves).
- **License: MIT.** State lives in *your* open Postgres `dbos` schema → **low data
  lock-in**.
- **Maintainer**: DBOS, Inc. (VC-backed, $8.5M seed Mar 2024); founders **Stonebraker
  + Zaharia** — strong pedigree, early-stage viability risk. DBOS Cloud is the
  commercial layer; staying on the self-hosted MIT library avoids that lock-in.
- **Repo**: ~1,400★, ~4 open issues, commits within the last day, Python 3.10–3.13.
- **Sharp edges**: (1) determinism footgun (every LLM/DB/storage call a step; async
  concurrency error-prone); (2) steps re-run on retry → idempotent; (3) system-DB
  migrations across versions + fast cadence = upgrade discipline; (4) **workflow
  versioning**: DBOS hashes workflow source into an `application_version`; only
  workflows matching the current version auto-recover, so a naive deploy can strand
  in-flight (multi-day-paused) workflows — mitigate with `DBOS.patch()` /
  `deprecate_patch()`, blue-green draining
  (`get_latest_application_version()`/`set_latest_application_version()`), or
  `fork_workflow`.
- **Exit cost**: moderate — state in open Postgres (low *data* lock-in), but logic is
  authored to DBOS's determinism + versioning model.

## Recommendation: ADOPT WITH CAVEATS — the 4 conditions
1. **RLS exception accepted in writing** + a hard rule & guard test:
   workflows/steps pass IDs/handles, never raw health/finance/location payloads.
2. **Determinism discipline by convention + review** — every LLM/SQLAlchemy/storage
   call inside a workflow is a `@DBOS.step()`, idempotent. New `docs/DEVELOPMENT.md`
   standard.
3. **Clean migration boundary** — Alembic owns `public`; `dbos migrate` owns `dbos`;
   both deploy steps; `dev-setup.sh` updated (non-negotiable #8); version pinned with
   a tested upgrade process.
4. **Deploy strategy for in-flight workflows** — `DBOS.patch()`/blue-green draining
   before relying on long pauses in production.

If conditions 1–2 are unacceptable, **stay custom** (dossiers A/B). If acceptable
(reasonable for a single-owner system), DBOS is the right engine and a clear upgrade
over the hand-rolled job loop for these two pipelines.

**Key docs:** [workflows](https://docs.dbos.dev/python/tutorials/workflow-tutorial) ·
[queues](https://docs.dbos.dev/python/tutorials/queue-tutorial) ·
[scheduling](https://docs.dbos.dev/python/tutorials/scheduled-workflows) ·
[communication](https://docs.dbos.dev/python/tutorials/workflow-communication) ·
[human-in-the-loop](https://docs.dbos.dev/python/examples/agent-inbox) ·
[db/system-db](https://docs.dbos.dev/python/tutorials/database-connection) ·
[config](https://docs.dbos.dev/python/reference/configuration) ·
[datasources](https://docs.dbos.dev/python/reference/datasources) ·
[cli](https://docs.dbos.dev/python/reference/cli) ·
[testing](https://docs.dbos.dev/python/tutorials/testing)
