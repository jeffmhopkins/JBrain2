# Durable-Workflow Engine Comparison

**Investigation role:** Researcher E — comparison shop to confirm DBOS is the best
fit or surface a better one.
**Scope:** Round 2. Single-box, in-process, Postgres-only, multi-day
human-in-the-loop profile. Filter hard on "no separate server/broker."
**Date:** 2026-06-14

## Bottom line up front
**Nothing beats DBOS Transact for this profile. CONFIRM DBOS as the lead.** It is
the only candidate that is a true in-process Python library, uses the existing
Postgres with zero extra infra, **and** does durable multi-day human-in-the-loop
waits natively (`DBOS.recv()` with durable timeouts of "days or weeks" surviving
restarts). Every other tool that matches DBOS on workflow features fails the "no
separate server/broker" filter. The only axis where DBOS loses is **data-defined
flows** (it's code-defined) — where **Windmill** is genuinely better, but Windmill
costs a separate Rust server stack.

## 1. Comparison table

| Candidate | Shape | Backing store | Durable + crash-resume | Cron | Durable multi-day HITL wait | Async-Py/FastAPI | Flows | Maturity/License | Verdict |
|---|---|---|---|---|---|---|---|---|---|
| **DBOS Transact (Py)** | **In-process library** | **Existing Postgres only** | Yes (step checkpoints) | Yes (`@DBOS.scheduled`) | **Yes — `recv()`/`sleep()` durable, days/weeks, survives restart** | Native async/FastAPI | Code | MIT, v2.x, active | **KEEP (lead)** |
| **Temporal + Py SDK** | Separate **server cluster** | Postgres/MySQL (prod); SQLite dev-only | Yes (best-in-class) | Yes | Yes (signals + `workflow.sleep`) | Good async SDK | Code | Mature, MIT | **DISQUALIFY** — separate server; SQLite is dev/test only |
| **Restate** | Separate **server** (Rust binary, proxy) | Its own embedded store | Yes | Yes | Yes | Py SDK | Code | Younger, permissive | **DISQUALIFY** — separate server + own datastore |
| **Prefect 3 (self-host)** | Separate **server + workers** | Postgres | Partial (orchestration, not step replay) | Yes | Pause/resume exists, default 1h timeout; weaker model | Good | Code | Mature, Apache-2 | **DISQUALIFY (soft)** — server+worker footprint; HITL bolt-on |
| **Windmill** | Separate **Rust server** | Postgres | Yes | Yes | **Yes — native Suspend/Approval steps** | Runs Python scripts | **Data (OpenFlow JSON)** | Mature, AGPL/EE | **DISQUALIFY on infra** — but only tool with native data-defined flows + approval UI |
| **Hatchet** | Separate **engine/server** (PG-only mode exists) | Postgres (PGMQ) | Yes | Yes | Limited | gRPC worker | Code/DAG | YC W24, young | **DISQUALIFY** — separate engine/server |
| **Procrastinate** | **In-process library** | **Postgres only** | Task retries, not durable multi-step replay | Periodic tasks | **No** | Native async | Code (tasks) | Stable, MIT | **DISQUALIFY** — no durable workflows/HITL |
| **Inngest** | Separate **server** (single binary) | Own store | Yes | Yes | Yes | HTTP SDK | Code | Open-core | **DISQUALIFY** — separate server |
| **Dramatiq/Celery** | Library + **broker** | Broker | No durable workflows | Beat/cron | No | OK | Code | Mature | **DISQUALIFY** — needs a broker |
| **APScheduler + custom** | In-process library | Postgres jobstore | Scheduling only | Yes | No | OK | Code | Mature | **DISQUALIFY** — only a scheduler |
| **Airflow / Dagster** | Separate **scheduler/web/DB** | Metadata DB | DAG-level | Yes | Airflow 3.1 HITL ops (Sept 2025); Dagster WIP | Batch-oriented | Code/DAG | Heavy | **DISQUALIFY** — multi-component, batch-shaped |

## 2. Ranked shortlist of realistic keepers

**1. DBOS Transact (Python) — the pick.**
- *Biggest reason*: the *only* option satisfying the non-negotiable filter
  (in-process library, one Postgres, no server/broker) **while** doing durable
  multi-day human-in-the-loop waits natively. `DBOS.recv(topic, timeout)` blocks
  durably; messages persist to Postgres; wakeup/timeout survives crashes; docs
  support "days or weeks." Exactly the wiki split/merge-approval pattern. Plus
  `@DBOS.scheduled` cron and Postgres-backed queues for per-chunk fan-out — both
  flagships map onto built-ins.
- *Biggest cost*: **flows are code-defined, not data-defined**; "pipelines-as-data"
  isn't met out of the box. Also a relatively young project (v2.x).

**2. Temporal (Python SDK) — the "if you outgrow one box" answer, not now.**
- *Biggest reason*: gold-standard durability + HITL (signals + durable timers).
- *Biggest cost*: **requires a separate server.** SQLite single-binary is officially
  dev/test only; production means a Temporal service beside Postgres — violates the
  one-process filter. Keep on the bench as the migration target if the system ever
  needs horizontal scale.

**3. Windmill — the spoiler on the data-defined axis only.**
- *Biggest reason*: the one tool that natively does **data-defined flows (OpenFlow
  JSON)** *and* native Suspend/Approval steps with a built-in approval page —
  delivering both "pipelines-as-data" and durable HITL that DBOS makes you
  code/build. Modest worker footprint (~0.1 CPU / 128MB).
- *Biggest cost*: **a separate Rust server stack**, not an in-process library — it
  would own/define your pipelines rather than living in your FastAPI worker.
  Reconsider only if "pipelines-as-data + a ready-made approval UI" outweighs
  "in-process, one process, build-your-own Ops UI."

**4. Prefect 3 — possible but unrewarding.**
- *Biggest reason*: mature, Postgres self-host, has `pause/suspend_flow_run`.
- *Biggest cost*: server + worker footprint; HITL is a weaker bolt-on (default 1h
  pause). No advantage over DBOS on one box.

## 3. The decision
**Does anything beat DBOS for the single-box, in-process, multi-day-HITL,
Postgres-only profile? No.** On the hardest requirement — durable multi-day
human-in-the-loop pause/resume on one box with no extra infra — DBOS is uniquely
well-suited and the only KEEP that satisfies the filter at full strength.
Procrastinate is the only other true in-process Postgres library and can't do
durable workflows or HITL waits.

**The single condition under which you'd switch:** if "pipelines-as-data with a
built-in approval UI" outranks "stay in one Python process," then **Windmill** beats
DBOS — but only by accepting a separate Rust server, which contradicts the current
non-negotiable. Until that trade flips, **confirm DBOS Transact (Python)** and note
**Temporal** as the documented escape hatch if the system ever outgrows a single box.

> Decision update (post-research): the owner chose to **drop pipelines-as-data**
> (the agent composes interactively instead), which removes Windmill's only
> advantage and confirms DBOS.

## Sources
- DBOS: [github](https://github.com/dbos-inc/dbos-transact-py) · [dbos.dev](https://dbos.dev/) ·
  [workflow-communication](https://docs.dbos.dev/python/tutorials/workflow-communication) ·
  [durable workflows in Postgres (Supabase)](https://supabase.com/blog/durable-workflows-in-postgres-dbos)
- Temporal SQLite/single-node limits: [#3366](https://github.com/temporalio/temporal/issues/3366) ·
  [self-hosted-guide](https://docs.temporal.io/self-hosted-guide/deployment) ·
  [temporalite](https://temporal.io/blog/temporalite-the-foundation-of-the-new-temporal-cli-experience)
- Restate: [key-concepts](https://docs.restate.dev/foundations/key-concepts) · [sdk-python](https://github.com/restatedev/sdk-python)
- Prefect pause/resume: [pause-resume](https://docs.prefect.io/v3/develop/pause-resume) · [inputs](https://docs.prefect.io/v3/develop/inputs)
- Windmill approval/OpenFlow: [flow_approval](https://www.windmill.dev/docs/flows/flow_approval) · [openflow](https://www.windmill.dev/docs/openflow)
- Hatchet: [architecture](https://docs.hatchet.run/home/architecture) · [#89](https://github.com/hatchet-dev/hatchet/issues/89)
- Procrastinate: [github](https://github.com/procrastinate-org/procrastinate)
- Inngest self-host: [docs](https://www.inngest.com/docs/self-hosting)
- Airflow HITL: [tutorial](https://airflow.apache.org/docs/apache-airflow/stable/tutorial/hitl.html)
