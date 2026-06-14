# Workflow engine — research & synthesis

The research behind the **Phase-5 workflow engine** decision (`events → triggers
→ pipelines → actions → runs`, per `docs/ROADMAP.md` Phase 5 and
`docs/ARCHITECTURE.md` §"Workflow engine"). Brief: choose the technology stack to
generalize the hardcoded ingest/analysis pipeline into a data-/code-defined
workflow engine that can also run the nightly wiki build, and that the
self-improving agent's loops (`docs/ASSISTANT.md`) become pipeline defs on.

Produced by a parallel swarm over 2026-06-14: five researchers (A–E), two rounds.
Round 1 (A–C) assumed the standing **zero-new-runtime-deps** goal. The owner then
relaxed that ("accept a dependency if it makes pipelines easier; accept richer
trigger/workflow capability"), which opened Round 2 (D–E) on durable-execution
libraries. No source, prompts, or schema were changed — these are design
dossiers. This README is the decision surface.

## The dossiers

| | Dossier | Owns | Headline finding |
|---|---|---|---|
| A | [build-vs-buy](A-build-vs-buy.md) | Orchestration-layer survey under the original zero-deps goal | Under zero-deps, **build on the existing SKIP-LOCKED queue** — Phase 5 is a data-modeling task, not a runtime task; every server/broker tool is disqualified; DBOS is the only library worth a second look |
| B | [scheduling & event dispatch](B-scheduling-and-dispatch.md) | The low-level scheduler + event→trigger mechanics on Postgres | **Custom tick loop + `croniter`** beats pg_cron/APScheduler; durable `events` table + `SKIP LOCKED` poll is the system of record, **LISTEN/NOTIFY only as an optional wake-up** |
| C | [pipeline-as-data & run-log](C-pipeline-and-runlog.md) | Pipeline-definition format, run/run-step schema, run-log UI | It's a **linear-sequence-with-fan-out** problem, not a DAG; JSONB def + action registry; the **DBOS two-table run-log shape**; poll-don't-SSE mobile UI; skip OTel |
| D | [DBOS deep-dive](D-dbos-deep-dive.md) | Fit of DBOS Transact against the two flagship workflows | **Adopt with caveats** — ingestion (OCR gate + fan-out) and nightly wiki (multi-day human-approval pause) map onto native primitives; the system-DB/RLS and code-vs-data tensions are managed, not blockers; **4 conditions decide it** |
| E | [durable-workflow comparison](E-durable-workflow-comparison.md) | DBOS vs Temporal/Restate/Prefect/Windmill/Hatchet/… | **Nothing beats DBOS** for the single-box, in-process, Postgres-only, multi-day human-in-the-loop profile; Windmill is the only spoiler (data-defined flows + approval UI) but costs a separate server |

## The decision

**Adopt DBOS Transact** (in-process Python durable-execution library on the
existing Postgres) as the Phase-5 engine, **subject to four conditions** (D):

1. **`dbos`-schema RLS exception, in writing** + a guard test asserting
   workflows/steps pass **IDs/handles, never raw health/finance/location
   payloads** (firewalled data never serialized into the DBOS-owned schema).
   Adapts non-negotiable #3 rather than breaking it.
2. **Determinism discipline as a `DEVELOPMENT.md` standard** — every LLM-adapter
   call, SQLAlchemy query, and storage I/O inside a workflow is an idempotent
   `@DBOS.step()`. (The #1 reported DBOS footgun.)
3. **Clean migration boundary** — Alembic owns `public`; `dbos migrate` owns
   `dbos`; both run as deploy steps; `scripts/dev-setup.sh` updated for the dep
   (non-negotiable #8); the version is pinned with a tested upgrade process.
4. **Version-aware deploy** — recovery is gated on a workflow-source hash, so a
   deploy while a multi-day approval is paused needs `DBOS.patch()`/blue-green
   draining or that workflow won't auto-resume.

If conditions 1–2 are unacceptable, the **fallback is Round-1's custom build**:
extend the SKIP-LOCKED queue with a `scheduled_triggers` table + `croniter` (A,
B). DBOS supersedes — but does not invalidate — that plan.

## The convergence (the conversation that produced this)

| Question | Decision | Why |
|---|---|---|
| Engine: build or buy? | **Buy — DBOS Transact** | Only pip-library-on-one-Postgres option; makes the hard parts native |
| What DBOS gives | Durable workflows, crash-resume, cron, per-chunk fan-out, **multi-day human-approval pause** (`recv`) | Exactly ingestion + nightly wiki |
| Pipelines: DSL or code? | **Code-defined** (drop the declarative DSL/interpreter) | Aligns with "no framework runtime, native tool-calling"; a DSL is bloat |
| Low-code/composition builder? | **No — don't build it** | The agent composes dynamically at runtime, more reliably than a DSL |
| Who composes flows? | **Two surfaces**: agent (interactive) + pipelines (triggered/scheduled) | Ingestion/wiki must run unattended & deterministic — not agent-driven |
| Building blocks | Shared library of **atomic actions/`.tool`s** (Python or LLM), each a DBOS step | One block library feeds both surfaces; ~80% of reuse, near-free |
| Reuse of composed sequences | **Skills** (agent-side, distilled from verified runs) + **composite actions / child workflows** (pipeline-side) | Reuse without a DSL — the agent "authors by doing"; pipelines nest |
| "Custom Python tools" | Vetted blocks shipped via **PR**, then composed — **not** runtime arbitrary code | "No code execution" non-negotiable |
| Scheduler / event dispatch | Falls to DBOS (`@DBOS.scheduled`, queues); the custom-`croniter` plan is the **fallback** | DBOS supersedes Round-1's custom plan |
| Net scope change | **Simpler** — dropped the pipeline DSL **and** the authoring builder | Less to build than the original Phase-5 sketch |

## The two-surface model

The agent and the engine are **two execution surfaces over one block library**,
and only one is agentic:

| | Agentic surface | Pipeline surface |
|---|---|---|
| Invoked by | the owner, in chat | a trigger / schedule / event |
| Composer | the agent (ReAct loop, runtime) | a fixed definition (deterministic) |
| Example | "summarize last week's new entities" | nightly wiki build, note ingestion |
| Reuse unit | **skill** | **pipeline / composite action** |
| Runs on | DBOS steps | DBOS workflow |
| Already designed? | yes (loop + `.tool` registry) | Phase 5 (this work) |

Both call the same atomic `@DBOS.step` actions. Reuse comes in two tiers: atomic
actions via the registry, and composed sequences via **skills** (agent-side,
distilled from verified runs — `docs/ASSISTANT.md` Loop 2) and **composite
actions / DBOS child workflows** (pipeline-side, including a skill graduating into
a standing scheduled/triggered pipeline).

## Still open (for the implementation plan)

1. **The promotion path** — when an agent skill graduates into a standing
   scheduled/triggered pipeline, and what gates it (review inbox +
   skill-promotion machinery). The one genuinely new design decision left; the
   seam between the two surfaces.
2. **The four DBOS conditions** above, each made concrete (the guard test, the
   `DEVELOPMENT.md` determinism standard, the migrate/deploy procedure).
3. **Pipeline parameterization vs topology** (D §7) — how much stays data
   (config rows read in an early step) vs code (the workflow body).
4. **Run-log retention** on a small box (C) and the IDs-not-content property of
   `runs`/`run_steps` so the engine's audit log keeps the queue's one-policy
   security class.

## The through-line

> The hard parts of Phase 5 are not throughput or sequencing — they are
> **durable resume**, **multi-day human-in-the-loop approval**, and **scheduled
> exactly-once-after-downtime**. Those are precisely what a durable-execution
> library makes native and what a hand-rolled job loop makes painful. DBOS earns
> a dependency by retiring that hard middle; the agent's native tool-calling loop
> earns the right to *replace* a composition DSL; and the shared action registry
> is what lets both surfaces reuse one block library. Engine owns *what runs on a
> trigger*; the agent owns *what runs in a conversation*; neither owns *what is
> true* — that stays with notes → facts → wiki.
