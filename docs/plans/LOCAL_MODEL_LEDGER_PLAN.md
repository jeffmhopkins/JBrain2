# One row per instance, two columns

> **Status:** In progress · **Last verified:** 2026-08-23 · **Waves:** L0✅ L1✅ L1a✅ L2a✅ L2b◻️ L3◻️

> Replaces step 2 of W0 in `LOCAL_MODEL_ACCESS_PLAN.md`, which was attempted and withdrawn —
> see that plan's "STEP 2 WAS ATTEMPTED AND WITHDRAWN" for the three anti-patterns it turned
> out to be. Reconciled with the root `CLAUDE.md`: no new LLM call sites, no storage or RLS
> change, tests land with the code, no GUI surface added.

> **Written from four independent cold research passes, none of which saw the withdrawn
> attempt.** Where this plan asserts a fact about llama-swap it is cited to the pinned commit
> `60226b6`; where it asserts a fact about this repo it is cited to `origin/main`. The existing
> `../reference/MODEL_ACCESS_INVENTORY.md` is NOT a safe source — L0 fixes it first.

## Why the obvious fix is wrong

The gate reads memory and compares it to a prediction. That is the defect, not the stale
`/running` read, and it is why correcting one layer kept reopening the next.

**Two budget layers each read instantaneous memory, at different instants, against different
floors, with one downstream of the other's decision.** `residency._plan` reads
`/proc/meminfo` and keeps `used` under a *fraction of total* (~18.2 GiB on this box).
`gpu_guard.refuse_if_no_device_room` then **re-reads `/proc/meminfo` itself** and applies a
*fixed* 6.0 GiB reserve. `smoketest` applies a third (20.0 GiB). Three answers to one question,
no shared constant. A model in transition is counted by all of them, and any correction applied
to one is absent from the others.

Prior art is unanimous that measurements are the wrong input. Kubernetes admits against
**requests**, never live usage. Borg uses limits for `prod` and only dares use measured usage
for a tier it is willing to kill — which this box does not have, because every model here is
`prod` and the failure mode is a host lock-up, not a rescheduled pod. Linux's
`Committed_AS`/`CommitLimit` refuses an allocation that exceeds *promises* even with RAM free.
Firecracker's balloon docs say never to rely solely on the guest's numbers. Ollama's own source
calls CUDA's free-memory reporting "laggy".

## The design

**One ledger. One row per model INSTANCE. Two size columns.**

```
{instance_id, model_id, phase, host_gb, device_gb, declared_at}
phase ∈ Planned → Starting → Resident → Draining → (row deleted)
```

Both figures are **declared**, never measured, and **immutable for the instance's lifetime** —
the arithmetic that admitted a model is the arithmetic that protects it.

Device accounting is a *column of the same row*, not a second ledger. This is the whole point:
**double-counting across the two layers becomes unrepresentable** rather than something each
layer must remember to correct. (It also means a future vLLM-style sleep, which moves weights
from device to host while the server lives, is bytes moving between columns of one row.)

**Charge at intent, discharge at confirmed death.** The row is inserted *before* the spawn.
`Resident → Draining` on the shutdown decision keeps the **full** charge. The row is deleted
only when the process is reaped and, for the device column, convergence is observed.

**A restart with a changed config is TWO rows** — the old instance `Draining` at its old
declared size, the new one `Planned` at its new size. Never mutate a row's size in place. This
yields `max(old, new)` when tearing down first, and a correct `old + new` if they ever overlap.
It is the direct answer to "the stopping process is running a config we no longer have".

**Admission, per layer, both must pass:**

```
min(measured_free_L, capacity_L − Σ ledger_L) ≥ declared_L(new) + headroom_L
```

The `min` is not belt-and-braces — the two terms fail in opposite directions. The ledger is
blind to consumers it did not create, and §B of the inventory says the pool has **five**
consumers while the gate sees one (ComfyUI evicts LLMs and is never evicted by them; whisper is
a second llama-swap; Kokoro holds a model resident with no accounting in `backend/src` at all).
The measurement is blind to transitions. `min` is the only combination whose error always
points at "refuse".

Headroom is subtracted from capacity **once, at the top**, so no comparison site can forget it.

**Serialize, and recompute rather than adjust.** One admission in flight; requests for an
already-resident model bypass it. After any eviction, discard the arithmetic and recompute from
a fresh census — Ollama's scheduler does exactly this and the `continue` is doing real work.

**Two barriers we do not have to build.** llama-swap already blocks a request behind a stop and
then starts the model (`EnsureReady`), and a 200 from `POST /api/models/unload/{model}` already
guarantees the child is reaped. Host RAM needs no settle barrier — the kernel frees it at reap.
**Only device memory needs one**, because GPU counters lag: snapshot free VRAM before the kill,
poll ~250 ms, release when `free_now − free_before > 0.75 × that instance's declared device
bytes`, hard timeout, and on timeout proceed on the ledger and log it. The barrier is an
optimisation over a ledger that is already correct without it — never a correctness dependency.

**Refuse loudly and retryably.** `Deferred` (fits this box, not right now — here is the
arithmetic, retry) versus `Infeasible` (exceeds total capacity, never retry). Conservative
admission is only tolerable when refusal is cheap and legible. Today's refusal is neither: it
returns HTTP 500 where its docstring promises 409, burns a worker retry where a defer would
not, and prints whole-box memory as though it were one model's need.

## Waves

### L0 — Make the evidence base true ✅

`MODEL_ACCESS_INVENTORY.md` is the declared sole source for this work and it is stale: line
numbers from a different tree, and at least five claims `origin/main` contradicts (two naked
loads it reports as open are closed; a `_default_residency` it cites no longer exists; the
"eight uncoordinated budgets" are six). Re-derive it against `origin/main`, add the sites it
never listed — `local_gateway`'s three internal short circuits, `state_of`/`_note_not_ready`,
`dbless_coordinator`, `host_settings`'s TTM read — and record the **fifteen conflations** where
one value answers two questions. Nothing else in this plan is trustworthy until this lands.

**Done 2026-08-22.** Audited by a cold pass told to falsify rather than confirm. Findings, in
the doc's own header and inline: the `Last verified` date was bumped by a commit whose line
numbers describe its PARENT, so the rule "every row carries a file:line and a verbatim quote"
was broken the day it was written; **123 of 503 citation pairs (24%) pointed at the wrong
line** after four commits, though the quotes themselves were nearly all still right; and eight
rows were factually contradicted, three of which reported gates as missing that had since been
closed — the most dangerous kind, because a plan built on them re-fixes fixed code while the
real holes go unlisted.

The rows are corrected inline rather than deleted (the evidence is still good; the addressing
rotted), and §G adds the sites the document never listed at all — including **G1a**, the
gateway's own already-resident branch, where a load targeting a mid-stop model skips BOTH the
device pre-flight and the watchdog and then makes llama-swap launch the process. That is the
most consequential single finding in the audit and it is downstream of residency, so no
residency-layer fix reaches it.

*Risk:* none — documentation. *Test:* the docs gate.

### L1 — The unambiguous fixes the attempt surfaced ✅

Independent of the ledger, each small, each with a regression test:

1. `unload()`'s docstring asserts a stopping window that v250 does not produce, and **six
   callers reason from it**. Correct it; re-examine whether our client timeout is what actually
   manufactures the window.
2. `GpuBudgetError` reaches the owner's Load button as **HTTP 500** where the docstring promises
   409, and **fails a worker job, burning a retry**, where `ResidencyError` defers. `run_smoketest`
   documents "Never raises" and can.
3. The refusal message renders `projected_gb` — the whole box after the load — as
   "{model} needs ~137 GB". Say what the model needs and what the box has, separately.
4. `jcode._warm_model` loads with **no admission at all**: the body is inside
   `contextlib.suppress(Exception)` and `residency` is used only for `note_evicted`. A third
   naked load path, which the plan's "exactly two" claim missed.

5. **Evict and load are only atomic on ONE of the four load paths — DO NOT FIX THIS YET.**
   `ensure_room`'s slow path loads INSIDE `_box_locked()`. The owner's Load button, the debug
   prime, `_restore`, `jcode._warm_model`, the warm-keeper fallback and `smoketest` all load
   outside it. A cold design pass (2026-08-22) confirmed the gap and then recommended
   **against** closing it now, for three reasons worth keeping:

   - **The direction is backwards relative to L2.** The ledger makes this window vanish by
     charging a row at intent and holding the lock for the length of an `INSERT` —
     milliseconds. Widening the lock now sets the hold to MINUTES on a request path, against a
     15-connection pool with no `lock_timeout`, and L2's first act would be to shorten it back.
   - **The severity claim would contradict a correction this branch already published.** W0 of
     the access plan says an unadmitted warm "is a documented degradation, not a hole … the
     freeze path is closed either way", because `LocalGatewayClient.load` still runs the device
     guard. The unlocked route load is the same class. Framing a lock change as "closes a
     freeze path" would assert something already written down as untrue — the exact failure
     mode of the three withdrawn attempts.
   - **A correction to my own note here.** I first wrote that the unclaimed window was the
     `jerv_prime_spec` await. It is not: that probe is cached for 30 s and is typically
     microseconds. The window is **the load itself**, 100-200 s, during which memory commits
     incrementally and a competitor's `read_memory_gb()` sees only the part that has landed.
     That is why moving the prime-spec above the admission would narrow nothing.

   **MEASURED 2026-08-22, and inconclusive — by absence.** `box_events` records `model_load`
   as a span with a `source`, so two overlapping spans from different sources IS this race,
   recorded. The query returns **zero**. But the denominator is 5 `model_load` spans in the
   whole 1-day retention window, **all from `api` and none from `worker`** — several of them
   this session's own test loads. The race needs two processes and the second one has not
   loaded a model. So this is "no opportunity", not "no race". Re-run after a stretch of real
   background activity before drawing anything from it.

   If the window ever must close before L2, the smallest correct form is
   `free_room_and_load(served, load=...)` on the coordinator — NOT exposing the lock to the
   route, which reverses two decisions this repo already made about guarding the chokepoint
   rather than the wrapper — plus splitting `_box_locked`'s contention-vs-outage degradation
   (today a pool-exhaustion timeout degrades to UNLOCKED, which fails open exactly when
   loaders are queued), plus a dedicated engine so waiting on the lock cannot consume the
   request pool. All three or none.

   **DONE 2026-08-22 — the one piece that is correct today and NOT undone by L2.** `_restore`
   now takes a **try-lock** (`pg_try_advisory_xact_lock`, `residency.pg_box_try_lock`) and
   SKIPS when it cannot get it, leaving `_displaced` intact so the next `schedule_restore`
   picks the members up. It fires at the end of every displaced turn, aims at exactly the
   memory a concurrent evict just freed, and "someone else is changing residency right now" is
   precisely when restoring to a remembered steady state is meaningless. A background task
   that never blocks also cannot convoy. The rest of item 5 — widening the lock over the other
   three load paths — remains deliberately NOT DONE, for the three reasons above.

6. **The warm-up phase ran outside the runaway watchdog — CLOSED 2026-08-22.** `guarded_load`
   returned, and only then did `_load_and_warm` call `_warm(...)` — which the file itself
   measures at **118 s of a 198 s gpt-oss-120b load**. So ~60% of a cold load allocated KV and
   graph-capture buffers with nothing watching for a runaway. The warm now runs INSIDE
   `guarded_load` (`_load_then_warm`); the pre-flight already admitted weights + KV +
   projector, so the ceiling covered the warm all along and only the watching stopped early.
   The page-cache drop still lands between the two — the warm's allocations should meet the
   memory it returns rather than race it — and the outer `finally` keeps it for the abort
   path, where it must follow `abort()`'s unload.

7. **G1a — a STOPPING model took the already-resident free pass. CLOSED 2026-08-22.** The most
   consequential finding of the L0 audit, and the one place a load could still reach llama-swap
   with no pre-flight, no watchdog and no ledger: `/running` lists a model llama-swap is
   stopping, `_load_and_warm`'s already-resident branch reads that list, and the health GET it
   then issues makes llama-swap **launch a fresh process**. The fix WAITS
   (`_settle_a_stopping_model`) rather than re-deciding on the state name — routing a stopping
   model to the guarded path would ask for room while the dying model's footprint is still
   charged to the device pool, which is the double-count three earlier attempts made. A stop
   that has not landed in 20 s is a retryable `LocalGatewayError`, never a fallthrough.

*Risk:* low for 1-4, and each is separable. 5's remaining half — widening the load lock over
the other three paths — is NOT low and is not separable from the ledger's admission story, so
it stays open by decision, not by omission. 6 and 7 turned out to be separable after all: both
are about WHERE an existing guard is applied, and neither changes what the guard computes.
*Test:* one regression test each, all mutation-checked (revert the fix, watch the test fail
with the right message). The balloon-during-the-warm and stop-settle cases had no coverage
at all before this wave.

### L1a — What a cold adversarial review of L1 found ✅

L1 shipped, and an independent reviewer barred from reading the branch was asked to falsify it
rather than confirm it. It returned three HIGH findings, all of them regressions L1 itself
introduced. Recorded here because the pattern is the point: each was a fix whose COMMENT was
true and whose CODE was not, which is the same failure mode as the three withdrawn attempts.

1. **The try-lock inverted the convoy instead of removing it.** `_restore` took the box lock
   and then held it across its loads — 100-200 s each. `ensure_room` takes the BLOCKING form of
   the same advisory lock, and there is no `lock_timeout` anywhere in this repo, so a chat turn
   in the other process would wait out a background restore, with every waiter pinning one of
   the fifteen pooled connections. The docstring's "a background task that never blocks also
   cannot convoy" was false as written: the task never waited, it made everyone else wait.
   **Fixed** by splitting the restore into `_restore_plan` (under the lock — the census, which
   is what the lock protects) and `_restore_core` (outside it — the loads). The operator
   switches are read before the lock, too: three settings round trips while holding a
   connection inside an open transaction is the classic pool-deadlock shape.

2. **The stop-settle could still hand a stopping model the resident free pass.** Its loop
   exited on `state_of`, which reads a process-global cache that every `running()` caller
   overwrites — and this client is shared with `warm_keeper`'s reconcile loop. A concurrent poll
   could end the wait, after which the settle returned its OWN older snapshot, which still
   listed the model. **Fixed** by `running_states()`: one observation decides both the exit and
   the answer. It also returns None for "the read failed", which an empty dict does not — a
   dropped poll was being read as "the stop landed and the box is clear".

3. **The abort's cache drop landed before the unload on the new warm path.** A flag meant to
   avoid a redundant second drop suppressed the only EFFECTIVE one: a breach during the warm
   drops while llama-server still holds the weight file, and `abort()` unloads afterwards. An
   aborted load strands the whole weight file in `Cached` (MEASURED at +4.29 GiB for a 4.3 GB
   model), `read_memory_gb` counts page cache as used, and residency suppresses `GpuBudgetError`
   on the restore — so the ratchet turns silently. **Fixed** by dropping unconditionally on the
   exception path; a redundant second sweep over evicted files is the cheap side of that trade.

Five more, all fixed in the same pass: a skipped restore had no retry and would wait for the
next end-of-turn, possibly hours (`_rearm_restore`); an outer cancellation orphaned the load —
now the whole load+warm — running unwatched with the load lock already released
(`guarded_load` now cancels and unloads it); the watchdog had **no host-pages term**, so on this
box's `amdgpu.gttsize=126976` configuration its floor essentially could not fire and the "warm
inside the watchdog" change was nearly inert (the host floor is now the one that can fire, the
same asymmetry `refuse_if_no_device_room` was given after 2026-08-19); a failed restore task's
exception was never retrieved; and `_narrate_reload_casualties` counted a model still being
KILLED as a survivor, so it reported an empty casualty list in exactly the case it exists for.

Two comments were corrected rather than their code: the reason given for not waiting on
`starting` ("another loader already admitted it") is false, because llama-swap loads on request
and most loads never touch this client; and `STOP_SETTLE_TIMEOUT_S` was justified by llama-swap's
10 s graceful budget, which bounds when SIGKILL is SENT, not when the kernel finishes tearing
down 85 GB — it is now 60 s, sized off the config-regen case that actually produces the window.

*Risk:* low individually; the batch is the point. *Test:* one regression test each, every one
mutation-checked by reverting the fix and confirming the failure message.

### L2a — The ledger, in shadow ✅

Introduce the row, the phases, the two columns, and the `min(measured, capacity − Σ ledger)`
admission test. Make the ledger the only thing any admission path consults, and make
`read_memory_gb`/`probe.sample()` callable from exactly one place, enforced by an AST guard in
the style of `test_llm_load_guard_chokepoint.py` — the precedent that already exists here, and
whose own docstring gives the reason: "a reviewer noticing is what already failed three times."

Persist the ledger and reconcile at startup: rows with no live process are phantoms, live
processes with no row are foreign. Roll a reservation back explicitly on every failure path,
with a TTL as backstop — and **do not expire a reservation whose transition is still running**
(a 90 s model load must not be swept at 30 s).

**Landed so far — the ledger exists and is proven against real Postgres; nothing consults it
yet.** Split at the one seam that is safe to split at: the ledger is additive until an admission
path reads it, so the arithmetic, the storage and the schema can each be verified on their own
before any behaviour changes.

- `llm/admission.py` — the arithmetic, with no I/O in it. `min(measured − reserve, capacity −
  reserve − Σledger) ≥ declared`, per layer, both must pass; `INFEASIBLE` vs `DEFERRED` split
  because a caller that retries the first retries forever; the phase-TTL rule and the
  phantom/foreign reconciliation split. Sixteen tests, no database, no GPU.
- `llm/ledger.py` — the rows, and `charge()`: **read the ledger, decide, and INSERT inside one
  transaction holding the box lock**. That is the whole efficiency argument made real — the lock
  that is held for 100-200 s today is held here for a SELECT and an INSERT.
- Migration 0170 + `models.telemetry.ModelReservation`, owner-only RLS on the `box_events` /
  `deploy_history` pattern (`app.is_owner()`, no domain predicate — this is the box's
  bookkeeping about its own hardware, and both writers are the owner's machinery).
- `local_catalog.declared_gb` — **the one place a declaration is computed**, returning both
  columns. Its host figure is asserted equal to `footprint_gb` for EVERY catalog entry, which is
  what keeps the ledger from becoming a ninth budget; the gap to the device column is exactly
  the host-only buffers (context checkpoints, `--cache-ram`), and the device column carries the
  vision projector at its RESIDENT size rather than the pre-flight's warmup cap.
- `tests/unit/test_memory_reader_inventory.py` — the AST guard this wave asks for, landed at the
  stage the code is at: it does not claim one reader, it pins the SET of readers, in both
  directions (a new one fails; a stale allowlist entry fails too). A seventh budget now needs a
  deliberate edit rather than a reviewer noticing.
- `tests/integration/test_model_reservations_rls.py` — run against real Postgres, not just
  written: the RLS isolation, a charge visible across two `ReservationLedger` instances standing
  in for the api and the worker, and `DRAINING` keeping its full charge until discharge.

**Wired, and deciding nothing.** `LocalGatewayClient` charges before a load, advances to
STARTING then RESIDENT, releases on ANY failure including cancellation, marks DRAINING before an
unload and discharges only on the confirmed 200 — the one moment llama-swap lets this codebase
say the memory is back. Both processes construct one (`source="api"` / `"worker"`).

**Shadow is the point of the split.** The ledger records what it WOULD have admitted and lets
every load through. A ledger that has never charged a live load has no numbers to be judged on,
and this repo has the precedent and the reason in `_note_not_ready`'s own words: "the fix removes
the thing being measured, so shipping both together would only ever report zero." The
disagreements go to `ledger.shadow_would_refuse`; L2b is `shadow=False`.

#### L2a's own cold review — nine findings, all fixed

A second independent reviewer, again barred from the branch, was pointed at the foundation. The
severe ones composed into one story: **`reconcile()` could delete a live reservation, and
nothing downstream could tell.**

- **`reconcile()` swept rows that had not reached the box yet**, and the in-progress fix scoped
  it by `source`, which is wrong for a deeper reason than the bug it was fixing: **llama-swap
  owns the model processes**, so neither the api nor the worker restarting kills a model, and
  "my process restarted, therefore my rows are dead" is false in both directions. PHASE is what
  is conclusive: PLANNED and STARTING look exactly like a load in progress — 198 s of it, with
  the model absent from `/running` — so only RESIDENT and DRAINING rows are sweepable.
- **`advance()` and `discharge()` could not fail.** Zero-row updates were silent, which is what
  made the above invisible: the load carries on, the model goes resident, and the ledger charges
  nothing for it. `advance` now logs at error, and advancing to RESIDENT **re-charges at the
  original declaration** — the box is holding it either way.
- **`charge()`'s docstring promised what it does not control.** It claimed the wait is a SELECT
  and an INSERT; `ensure_room` still holds the same advisory key across a whole evict-and-load
  (L1 item 5, open by decision), so a charge can queue behind one. Corrected, plus a
  `lock_timeout` so fifteen queued charges cannot become the api's whole connection pool.
- **Two invariants moved from comment to constraint.** `CHECK (device_gb >= 0 AND host_gb >=
  device_gb)` makes "double-counting is unrepresentable" a fact rather than an assertion; the
  policy moved from `is_owner()` to **`is_full_owner()`**, so a domain-narrowed agent session
  cannot read or write the box's memory accounting. Both proved against real Postgres.
- An unknown `phase` string made the whole ledger **unreadable** (now falls back to RESIDENT —
  full charge, no TTL, the only safe direction for a row this build cannot reason about);
  `admit()` reported DEFERRED for a request INFEASIBLE on the other layer, so a caller would
  retry it forever; `reconcile_split` gave a different answer for a generator; the PLANNED TTL
  was 2 minutes against a gap containing a bounded 60 s stop-settle; an unmeasured pool degraded
  the gate silently; and the reader-inventory guard **was false the day it landed** — it watched
  two names and missed `read_page_cache_gb`, which five sites in the load path call.

**One finding was NOT a defect and is recorded because it changes what this wave claims.** On
this hardware the device layer's *ledger* arm can never bind: `declared_gb` makes device a subset
of host, host capacity is 121 GB against a ~18 GB fraction reserve while device is ~124 GB
against a 6 GB one, so `device_usable > host_usable` for every reachable state. The device layer
binds only through its MEASUREMENT. That is conservative — the host arm is the real physical
constraint and it is checked — but "both layers protect the GTT hang mode" would be an
overstatement, and the two-layer shape earns its keep as future-proofing (a vLLM-style sleep
moves bytes between the columns) rather than as a second live gate today.

### L2b — Let it decide ◻️

Flip `shadow=False`, and act on the two outcomes the arithmetic already separates: `DEFERRED`
must defer a worker job rather than burn a retry, and `INFEASIBLE` must reach the owner as a 409
that says the model will never fit. Do it after a stretch of real box traffic has been read back
from `ledger.shadow_would_refuse` — if the ledger would have refused loads that in fact
succeeded, the declaration is wrong and no amount of correct plumbing fixes that.

*Risk:* high — this is the part that changes behaviour, and it cannot be split further: the
moment admission stops reading memory, every layer that still does is inconsistent with it.

### L3 — Retire the duplicate budgets ◻️

Three host-RAM reserves collapse to one. The device pre-flight consumes the admitting row
rather than re-deriving. `smoketest`'s gate reads the same ledger.

*Risk:* medium. *Test:* one budget, asserted from a single constant.

## What this plan does NOT do

- It does not adopt Borg-style admission against measured usage. That is sound only with a
  sacrificial tier; here every model is `prod` and an OOM is a power cycle.
- It does not touch W1's intent question (owner turn vs agent vs scheduled). The ledger is what
  W1's policy will decide *on*; it does not depend on W1.
- It does not add a host-side settle barrier. The kernel already is one.
