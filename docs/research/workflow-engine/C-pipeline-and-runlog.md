# Pipeline-as-Data, Run-Log Schema & Run-Log UI

**Investigation role:** Researcher C — pipeline-definition format, run/run-step
schema for diagnosability, and the run-log UI.
**Scope:** how to represent pipelines as data, model execution state, and render a
mobile-first run viewer (Round 1, zero-deps goal).
**Date:** 2026-06-14

> Round-2 note: the **decision dropped the data-defined pipeline DSL** in favour of
> code-defined DBOS workflows + agentic composition (see README, dossiers D–E).
> What survives and remains directly useful here: the **runs/run_steps schema for
> diagnosability + idempotency** (§2, which mirrors DBOS's own tables), the
> **run-log UI** (§3), the **skip-OTel** verdict, and the over-engineering warnings.
> §1 (pipeline-as-data) is retained as the analysis that justified *not* building a
> DSL.

Bottom line: this is a **linear-sequence-with-fan-out** problem at single-owner
scale, not a DAG-orchestration problem. The hard part is the run-log schema, not
the execution model. Don't pull in Airflow/Prefect/Temporal generality; the
closest design template is **DBOS** (durable execution on plain Postgres tables),
which validates almost exactly the schema shape needed.

## 1. Pipeline-definition-as-data (retained as analysis)

### Format: JSONB column, not YAML files, not a normalized step table
Store each definition as a single `JSONB` document in a `pipelines` row (+ `name`,
`version`, `created_at`):
- **JSON over YAML**: Windmill's OpenFlow, the closest comparable, is a "JSON
  serializable value… a linear sequence of steps (modules)." JSON round-trips
  losslessly through Pydantic/asyncpg with zero parsing deps. Author as YAML in the
  repo if humans prefer, but store canonical JSON.
- **JSONB document over normalized `pipeline_steps` rows**: the definition is read,
  validated, and versioned *whole*. Splitting into rows buys nothing at this scale.
  (Contrast: the *run* log below *is* normalized into rows, because it's queried
  per-step.)

### Sequencing: ordered list + explicit fan-out, NOT a dependency DAG
Two control-flow primitives suffice (both in Windmill's `FlowModule` set):
1. **Ordered list** (output of step N feeds N+1).
2. **Fan-out / for-each** (one step emits a collection; next runs per-item) —
   chunk→embed exactly.

Add a per-step optional `run_if` predicate for conditional skip. That covers ingest
and the nightly wiki build's update/create/split/merge branching without a real
DAG. **Do not** model arbitrary `depends_on` edges, join nodes, or dynamic DAG
expansion — that is the Airflow generality to refuse. If a diamond dependency ever
arises, topologically pre-sort into a linear list at validation time rather than
running a runtime DAG scheduler.

### Action registry: name → (callable + Pydantic params model)
Actions stay registered Python callables; only the *sequence* is data:
- module-level `ACTIONS: dict[str, RegisteredAction]` via an `@action("chunk")`
  decorator;
- each holds `callable`, a Pydantic `params_model`, optional output model;
- prefer an explicit `params_model` (introspectable for UI/JSON-schema) over
  `@validate_call`.

### Validation before a run starts
On create/update *and* at enqueue: every `action` exists; every step's `params`
parses; fan-out steps reference a preceding producer; `run_if` fields exist. Pure
Pydantic + registry lookup — turns "unknown action / bad params" from a 3am failure
into a synchronous 422.

### Versioning + migrating the existing ingest pipeline
- **Version field on the row** (immutable rows; edits insert a new version); stamp
  each run with `pipeline_id`+`version` (mirrors DBOS `application_version`).
- **Migration**: write current hardcoded ingest (parse → chunk → embed → extract
  facts/entities) as the first seeded definition, each handler a registered action.
  Behavior-preserving (same callables, externalized ordering). Seed via an Alembic
  data migration; run old/new side-by-side and diff before cutover.

### Illustrative ingest pipeline-def sketch
```json
{
  "name": "ingest_note",
  "version": 3,
  "on_event": "note.created",
  "steps": [
    { "id": "extract",  "action": "extract_attachments", "params": {} },
    { "id": "chunk",    "action": "chunk_note",
      "params": { "granularities": ["paragraph", "section"] } },
    { "id": "embed",    "action": "embed_chunk",
      "for_each": "chunk.chunk_ids",
      "params": { "model": "default" } },
    { "id": "facts",    "action": "extract_facts",
      "params": { "with_citations": true } },
    { "id": "entities", "action": "extract_entities",
      "run_if": "note.domain != 'location'",
      "params": {} }
  ]
}
```
Note it carries **IDs, not content** (`chunk_ids` flow between steps) — consistent
with the security model.

## 2. runs / run_steps schema for diagnosability + idempotency

Use the **DBOS two-table split** as the template — proven minimal shape for
"diagnosable from Postgres alone." (This survives the Round-2 decision because DBOS
itself produces tables of this shape.)

### `runs` (one per execution) — mirrors DBOS `workflow_status`
| field | purpose |
|---|---|
| `id` (uuid) | run id; **the correlation/trace id** |
| `pipeline_id`, `pipeline_version` | exactly which def ran |
| `event_id`, `trigger_id` | links event→trigger→run |
| `status` | `queued / running / succeeded / failed / cancelled` |
| `input_ref` | row IDs / event payload ref (NOT note content) |
| `error`, `error_traceback` | serialized exception + traceback |
| `created_at`, `started_at`, `finished_at` | timing |
| `idempotency_key` | unique; dedupes "same event fired twice" |

### `run_steps` (one per step attempt) — mirrors DBOS `operation_outputs`
| field | purpose |
|---|---|
| `run_id` (fk) | parent |
| `step_index` (int) | DBOS's `function_id` (execution order); with `run_id`, unique step identity for replay |
| `step_id`, `action_name` | the def's step id + registered action |
| `status` | `queued / running / succeeded / failed / skipped / retrying` |
| `attempt` | retry counter |
| `input_ref`, `output_ref` | row IDs / small JSON; never note content |
| `error`, `error_traceback` | per-step failure |
| `queue_job_id` | links step → its enqueued job |
| `started_at`, `finished_at` | per-step timing |

### Idempotency & retry — checkpoint, don't restart
DBOS's checkpoint-and-skip: **before executing a step, check `run_steps` for a
`succeeded` row for `(run_id, step_index)`; if present, reuse `output_ref` and
skip.** On retry/resume, re-enter at the first non-succeeded step — no full
restart, no double-apply. Make each action's DB write idempotent by carrying
`run_id`/`step`. For DB-only steps, commit the write **and** the checkpoint in one
transaction → exactly-once.

### What to log
Per-step structlog with `run_id`, `step_index`, `action_name`, `status`,
`attempt`, duration; full `error_traceback` in the row (not just the log). The row
is the durable queryable record; structlog is streaming detail.

### Big payloads & security
Store only row IDs / refs in `input_ref`/`output_ref`, **never note content** — a
diagnosing operator follows the ID to the RLS-scoped source row. Keeps run logs
small and keeps health/finance/location content out of an Ops table; worth an
explicit RLS test on `runs`/`run_steps`.

### Retention
Keep all `failed` runs + last N `succeeded` per pipeline (e.g. 50); nightly
`DELETE ... WHERE finished_at < now() - interval` partitioned by status.

### OTel verdict: skip it
`runs` + `run_steps` rows + structlog suffice and are better here. OTel is a new
dep + collector/backend ("extra infra"), and its distributed-trace model is for
services you don't have. `run_id` *is* the trace id; SQL *is* the trace query.
Revisit only if you ever go multi-node.

## 3. Minimal run-log UI (mobile-first PWA)

IA patterns worth copying:
- **GitHub Actions**: run list → run summary → expandable jobs/steps, each step its
  own status icon + collapsible log; **failed steps auto-expand**. Steal the
  auto-expand.
- **Temporal/Prefect/Windmill**: run → step timeline, per-step logs, retries. These
  are desktop-dense; don't replicate the graph view on mobile.

**Recommended one-person Ops screen (two views):**
1. **Run list** — reverse-chron cards: pipeline name, status pill, relative time,
   duration. Filter by `failed`. Tap → detail.
2. **Run detail** — vertical **step timeline** (one row per `run_steps`, ordered by
   `step_index`): status icon, action name, duration. Tap to expand →
   `error_traceback` + input/output **IDs** (links to source rows). Failed step
   expanded by default. Controls: **Retry / re-run-now** (re-enqueue from first
   non-succeeded step) and **Cancel** (running only).

**Live updates: poll, don't SSE.** TanStack Query `refetchInterval` (~2s) on the
active run, returning `false` once status is terminal. Single-user, mobile,
intermittent connectivity makes a held-open SSE connection a liability. Add SSE only
for a passive always-live dashboard later.

## Over-engineering risks to flag
1. A real DAG scheduler — need ordered-list + fan-out + conditional-skip only.
2. Normalizing pipeline defs into step rows — keep the def as one JSONB doc; only
   the *run log* is row-normalized.
3. A bespoke retry/backoff DSL — per-action `max_attempts` + fixed backoff is enough.
4. OTel / a tracing backend — `run_id` + SQL is your tracing.
5. WebSockets / SSE infra for a one-person Ops screen — self-stopping polling.
6. A visual flow-builder UI — (Round-2 confirms: don't build it).

## Sources
- [DBOS system tables](https://docs.dbos.dev/explanations/system-tables) ·
  [Why Postgres for durable execution](https://www.dbos.dev/blog/why-postgres-durable-execution) ·
  [Durable workflows in Postgres (Supabase/DBOS)](https://supabase.com/blog/durable-workflows-in-postgres-dbos)
- [Windmill Architecture](https://www.windmill.dev/docs/flows/architecture) ·
  [OpenFlow](https://www.windmill.dev/docs/openflow) ·
  [FlowModule type](https://app.windmill.dev/tsdocs/types/FlowModule.html)
- [Pydantic validation decorator](https://docs.pydantic.dev/latest/concepts/validation_decorator/)
- [FOR UPDATE SKIP LOCKED (Netdata)](https://www.netdata.cloud/academy/update-skip-locked/) ·
  [PgQueuer](https://github.com/janbjorge/PgQueuer)
- [GitHub Actions run logs](https://docs.github.com/actions/managing-workflow-runs/using-workflow-run-logs)
- [TanStack Query polling](https://tanstack.com/query/latest/docs/framework/react/guides/polling) ·
  [SSE vs polling](https://dev.to/itaybenami/sse-websockets-or-polling-build-a-real-time-stock-app-with-react-and-hono-1h1g)
- [Orchestration vs job scheduling](https://branchboston.com/workflow-orchestration-vs-traditional-job-scheduling-in-data-pipelines/) ·
  [Temporal alternatives (ZenML)](https://www.zenml.io/blog/temporal-alternatives)
