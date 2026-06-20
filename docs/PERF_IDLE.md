# Idle CPU: where it goes and how to trim it

The stack burns measurable CPU with **zero user requests** because it is built on
poll loops and periodic background workers that run on timers, not on traffic. So
"stack up" is never truly idle, and `up` vs `down` shows a real baseline. This doc
maps that baseline to its sources, lists the levers (which this PR starts pulling),
and gives a benchmark so each lever's gain is **measured, not guessed**.

"Idle" here means **no client connected** — close the PWA/app. An open app adds its
own polling (see "Client-driven" below), which is a separate axis.

## Where idle CPU goes

| Source | Cadence | Evidence | Notes |
|---|---|---|---|
| **Worker job-claim poll** | every 2s (now backs off to 8s) | `worker.py` `POLL_SECONDS`, `run_loop` | A `SELECT … FOR UPDATE SKIP LOCKED` per tick even when idle — the largest app-side contributor. |
| **Scheduler tick** | every 30s | `workflow/scheduler.py` `TICK_SECONDS`, `worker.py:run_loop` | One indexed due-schedules query; rides the worker loop. |
| **Dispatcher tick** | every 30s | `worker.py:run_loop`, `workflow/dispatcher.py` | One undispatched-events query; SHADOW by default. |
| **`db` healthcheck** | every 10s → 30s | `docker-compose.yml` db `healthcheck` | A `pg_isready` process spawn each time. |
| **`api` healthcheck** | every 30s → 60s | `docker-compose.yml` api `healthcheck` | A python process spawn + a light `/api/healthz` hit. |
| **TimescaleDB / Postgres bgworkers** | continuous | `timescale/timescaledb-ha:pg17` | Autovacuum launcher, checkpointer, WAL/bg writer, stats, Timescale job scheduler + continuous-aggregate policies, telemetry. The largest *fixed* baseline; mostly inherent to running a DB. |
| **Resident service containers** | idle | `embed`, `searxng`, `local-llm` (if enabled) | Hold worker pools / a model resident; idle CPU is low but non-zero. |

**Client-driven (only when the app is open, not "zero requests"):** OpsScreen polls
every 3s (`OpsScreen.tsx`), LLM settings every 4s (`LLMSettingsScreen.tsx`), notes,
and a service-worker update check. The Ops polls call the supervisor, which calls the
Docker SDK including `container.stats()` (`supervisor/.../gateway.py`) — the most
expensive per-poll path. A backgrounded PWA keeps polling unless told not to.

## Levers in this PR

| Lever | Change | Expected gain | Risk / tradeoff |
|---|---|---|---|
| Worker idle backoff | idle sleep grows 2s→4s→8s, resets to 2s on work (`worker.py`) | Idle claim queries ~30/min → ~7–8/min (≈75% fewer) with **no** throughput cost — a burst still drains at full speed | First job after a long idle waits up to 8s to start (tunable via `MAX_IDLE_SECONDS`). |
| `db` healthcheck | 10s → 30s interval, `start_interval: 2s` keeps boot fast | ~⅔ fewer `pg_isready` spawns | Minor; `start_interval` needs Docker 25+ (older engines ignore it). |
| `api` healthcheck | 30s → 60s interval | one fewer python-process spawn/min | Minor; slower liveness detection (nothing gates boot on it). |

Magnitudes above are reasoned, not measured — **run the benchmark on the target box
to confirm where the gain actually lands** before weighting any lever.

## Measuring (per-lever attribution)

`deploy/idle-cpu-bench.sh` samples per-container CPU with the stack at rest:

```sh
# close the app first for a true zero-request baseline
deploy/idle-cpu-bench.sh            # 12 samples, 5s apart
```

Method: take a **baseline**, apply **one** lever, restart the affected service, and
re-measure. `docker stats` reports CPU as a percentage of one core (100% = one busy
core; totals can exceed 100% on multi-core hosts). Attribute the delta to the lever
you changed.

## Bigger strategies (not in this PR — for independent review)

1. **`LISTEN`/`NOTIFY` instead of polling (largest structural win).** Have the worker
   `LISTEN` on a channel and have `queue.enqueue` `NOTIFY` it; the worker sleeps on
   the notification with a fallback timeout (≈30s) that still drives the scheduler
   tick. This removes idle claim queries almost entirely — the resting worker wakes
   only on real work or the 30s tick. Medium effort (asyncpg LISTEN plumbing + tests);
   biggest payoff. The backoff in this PR is the cheap down-payment on the same goal.
2. **TimescaleDB telemetry off.** `ALTER SYSTEM SET timescaledb.telemetry_level = off;
   SELECT pg_reload_conf();` stops the periodic telemetry job (and its outbound call).
   Deliberately **not** baked into compose here: the `-ha` image manages its own config
   (Patroni/Spilo), so a blind compose/init change is fragile and telemetry is a minor
   contributor — verify on the actual image before committing it.
3. **Frontend visibility backoff.** Pause/slow the 3–4s OpsScreen/LLM polls when
   `document.hidden` (Page Visibility API), so a backgrounded PWA stops driving the
   api + supervisor + `docker stats` path. Self-contained frontend change; only helps
   the "app open" axis, not the zero-client baseline.
4. **Pause optional always-on services when unused.** `embed`/`searxng` are resident
   even when no RAG/web-search is happening. On-demand start/stop would reclaim their
   idle slice at the cost of first-use latency — weigh against the simplicity of
   leaving them up.
