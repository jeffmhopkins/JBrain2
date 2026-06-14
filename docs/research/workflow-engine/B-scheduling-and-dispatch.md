# Scheduling & Event-Dispatch Mechanisms

**Investigation role:** Researcher B — the low-level scheduler + event→trigger
dispatch primitives on Postgres.
**Scope:** how scheduled triggers fire and how events match triggers and enqueue
runs, given one Postgres + one worker (Round 1, zero-deps goal).
**Date:** 2026-06-14

> Round-2 note: if DBOS is adopted (dossiers D–E), `@DBOS.scheduled` and DBOS
> queues subsume most of this. These mechanics are the **fallback** plan and the
> reasoning that validates DBOS's choices (durable table over NOTIFY; exactly-once
> after downtime).

## Bottom line up front

For both A and B the **custom Postgres-table + existing-worker-poll** approach wins
under the hard constraints. It adds **one tiny pure-Python dep (`croniter`)** and
nothing else. pg_cron is available but offers no advantage on a single box;
APScheduler and Redis/brokers are net-negative. LISTEN/NOTIFY is at best a latency
optimization layered on a durable table — never the system of record.

## A. Scheduling (cron / periodic triggers)

| Option | New runtime deps | Persistence / missed-run handling | Calls app logic? | Single-box operability | Verdict |
|---|---|---|---|---|---|
| **pg_cron** | None new (ships in timescaledb-ha) but needs `shared_preload_libraries` edit + superuser `CREATE EXTENSION` | Durable; **no built-in catch-up** — missed ticks during downtime skipped | **SQL only** (can `INSERT` an event row, can't run Python) | Requires postgresql.conf edit + container restart; state outside Alembic/structlog | Viable fallback, not recommended |
| **APScheduler** (AsyncIOScheduler + SQLAlchemyJobStore) | `apscheduler` (own locking/threading model) | Durable; `misfire_grace_time` + `coalesce` | Yes | A second scheduling subsystem inside the worker; redundant with the queue | Not recommended |
| **Custom tick loop** — `scheduled_triggers(next_run_at, cron_expr, tz)` swept by the existing poll, claim due rows `FOR UPDATE SKIP LOCKED`, INSERT events, recompute `next_run_at` via `croniter` | **`croniter` only** (pure Python) | Durable; **you control catch-up explicitly** | Yes — it's your worker | Reuses queue, RLS session, structlog, Alembic. One subsystem | **Recommended** |

### Recommendation: custom tick loop + `croniter`

Collapses scheduling into the primitive you already own. The worker already polls
~2s with `SKIP LOCKED`; `scheduled_triggers` is just another claimable source. Each
sweep: `SELECT ... WHERE next_run_at <= now() FOR UPDATE SKIP LOCKED`, INSERT the
event(s) (so scheduled and ad-hoc triggers flow through the *same* dispatch path),
then `next_run_at = croniter(expr, now).get_next()`. ~2s granularity is irrelevant
for nightly/hygiene jobs.

- **pg_cron** can only emit SQL — the most it buys is `INSERT INTO events ...` on a
  schedule, which the tick loop does anyway in Python (keeping structlog, RLS,
  Alembic). It is installed but **not preloaded** in the timescaledb-ha image
  (only `timescaledb`, and `pg_textsearch` on PG17+, are in
  `shared_preload_libraries`), and needs superuser `CREATE EXTENSION`. Not "free."
- **APScheduler** is a second scheduler with its own jobstore-locking bolted into
  the single async worker, duplicating the queue. Its misfire/coalesce policy is
  ~5 lines against your own table.

### Gotchas
- **Missed runs after downtime.** Make it a policy field. For "nightly build" use
  **coalesce**: fire once, advance `next_run_at` past `now()` (don't replay 8
  missed nights) — loop `get_next()` until `> now()`. The explicit control pg_cron
  *lacks* (silently skips) and APScheduler buries behind misfire grace.
- **Timezone / DST.** Store/compute in the trigger's civil timezone, persist
  `next_run_at` as UTC `timestamptz`. Feed croniter a tz-aware `datetime` via
  `zoneinfo`, convert result to UTC. croniter has documented DST edge-case history;
  for "02:00 nightly" the spring-forward/fall-back ambiguity is low-stakes if
  fire-and-advance dedupes on the computed slot. `cronsim` is the stricter
  alternative if strict Debian-cron DST semantics ever become a hard requirement —
  almost certainly overkill.
- **Double-fire (>1 worker).** `FOR UPDATE SKIP LOCKED` already serializes who
  advances a trigger row; alternatively wrap the tick in
  `pg_try_advisory_lock(<const>)`. With one worker neither is strictly needed; keep
  the row-level claim — free and future-proof.

## B. Event → trigger dispatch

| Concern | Option | Trade-off at single-worker scale | Verdict |
|---|---|---|---|
| **Delivery** | Durable `events` table + poll (`SKIP LOCKED`) | At-least-once, replayable, survives restart, one code path with scheduled triggers | **Recommended (system of record)** |
| | LISTEN/NOTIFY | At-most-once; **dropped while disconnected, no replay**; 8000-byte cap; needs asyncpg `add_listener` | Optional **latency optimizer only** |
| **Fan-out (1 event → N triggers)** | Resolve matching triggers at dispatch, insert one pipeline-run row per match | Natural with a table; transactional with event-consume | Recommended |
| **Debounce / coalesce bursts** | Idempotency key + debounce window (`coalesce_key` + `not_before`) | Many note edits collapse to one wiki build | Recommended |
| **Double-fire / leadership** | `SKIP LOCKED` on events; `pg_try_advisory_lock` for singleton sweeps | Already your model | Recommended |

### Recommendation: durable `events` table, worker polls; NOTIFY optional wake-up

You already proved this with `app.jobs`. Reuse it verbatim for events.

- **Why the table, not NOTIFY.** NOTIFY is **at-most-once**: only currently-listening
  sessions get it, nothing stored, no replay — a brief disconnect or restart loses
  it, plus an 8000-byte payload cap. For a knowledge system where a lost
  note-created or geofence event means silent data drift, that's unacceptable as
  the source of truth. Production pattern: **keep work in a table, claim with `FOR
  UPDATE SKIP LOCKED`, use NOTIFY only to *wake* an idle worker**.
- **Is NOTIFY worth it here? Not initially.** The worker already polls ~2s; ~2s p99
  latency is fine. NOTIFY only shaves that to sub-second, at the cost of an asyncpg
  `add_listener` connection you must reconnect-and-rescan-on-reconnect. If
  sub-second ever matters, add it as a pure optimization: on NOTIFY poll
  immediately; on reconnect always full-sweep. The table stays authoritative.
- **Fan-out.** At dispatch, claim an unprocessed `events` row, query `triggers` for
  all matches (event type + structured predicates — PostGIS `ST_Contains`/geofence,
  note-tag filters), insert **one pipeline-run row per matched trigger** in the
  same transaction that marks the event consumed (transactional outbox).
- **Debounce / coalesce.** Give coalescing pipelines a **`coalesce_key`** (e.g.
  `"wiki-build"`) + debounce window. On insert, upsert on `(coalesce_key,
  status='pending')`: if a pending run exists, bump `not_before = now() + window`
  instead of inserting a second. Worker only claims runs where `not_before <=
  now()`. N edits in a burst → one build once the dust settles.
- **Manual / emergency trigger.** Free: an Ops action INSERTs an `event` (or a
  pipeline-run) through the same path. Scheduled, event-driven, and manual triggers
  converge on one enqueue mechanism.

### Gotchas
- **NOTIFY-loss** — avoided: table is source of truth; NOTIFY is a non-authoritative
  wake-up; every reconnect full-rescans.
- **Double-fire on consumption** — `SELECT ... FOR UPDATE SKIP LOCKED` on the event
  row; mark-consumed and run-insert share one transaction (crash rolls back,
  re-claimed; at-least-once, idempotent via `coalesce_key`).
- **Duplicate runs from retries** — `coalesce_key`/idempotency-key dedup makes
  re-dispatch safe.
- **Multi-worker future** — `SKIP LOCKED` prevents double-grab; gate genuinely
  singleton sweeps with `pg_try_advisory_lock(<const>)`. No Redis/ZooKeeper.

## Dependency verdict
Add exactly one pure-Python dep: **`croniter`** (cron-expr → next datetime). It's
the only piece the existing primitives can't express; the alternative
(hand-rolling a cron parser, or APScheduler/pg_cron) is strictly worse. Everything
else — claiming, retries, backoff, stale-lock reaping, fan-out, debounce, leader
election — reuses what exists. **Net new for the whole engine: one small library,
zero new processes, zero brokers.**

## Sources
- [timescaledb-docker-ha Dockerfile](https://github.com/timescale/timescaledb-docker-ha/blob/master/Dockerfile) ·
  [pg_cron shared_preload_libraries (citusdata/pg_cron #167)](https://github.com/citusdata/pg_cron/issues/167)
- [PostgreSQL NOTIFY docs — at-most-once, 8000-byte limit](https://www.postgresql.org/docs/current/sql-notify.html) ·
  [Stacksync — LISTEN/NOTIFY limits](https://www.stacksync.com/blog/beyond-listen-notify-postgres-request-reply-real-time-sync)
- [Postgres LISTEN/NOTIFY for job queues](https://nerdleveltech.com/postgres-listen-notify-job-queue) ·
  [Sequin — capturing changes in Postgres](https://blog.sequinstream.com/all-the-ways-to-capture-changes-in-postgres/)
- [APScheduler user guide](https://apscheduler.readthedocs.io/en/3.x/userguide.html) ·
  [pallets-eco/croniter](https://github.com/pallets-eco/croniter) ·
  [Healthchecks.io — Debian cron & DST](https://blog.healthchecks.io/2021/10/how-debian-cron-handles-dst-transitions/)
- [Kerkour — leader election with advisory locks](https://kerkour.com/postgresql-leader-election-advisory-lock) ·
  [Jeremy Miller — advisory locks for leader election](https://jeremydmiller.com/2020/05/05/using-postgresql-advisory-locks-for-leader-election/)
