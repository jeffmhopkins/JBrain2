# One row per instance, two columns

> **Status:** Proposed · **Last verified:** 2026-08-22 · **Waves:** L0◻️ L1◻️ L2◻️ L3◻️

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

### L1 — The unambiguous fixes the attempt surfaced ◻️

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

   The one piece that is correct today and NOT undone by L2: `_restore` should take a
   **try-lock** (`pg_try_advisory_xact_lock`) and skip when it cannot get it. It fires at the
   end of every displaced turn, aims at exactly the memory a concurrent evict just freed, and
   "someone else is changing residency right now" is precisely when restoring to a remembered
   steady state is meaningless. A background task that never blocks also cannot convoy.

6. **The warm-up phase runs outside the runaway watchdog.** `guarded_load` returns, and only
   then does `_load_and_warm` call `_warm(...)` — which the file itself measures at **118 s of
   a 198 s gpt-oss-120b load**. So ~60% of a cold load allocates KV and graph-capture buffers
   with nothing watching for a runaway. Verified 2026-08-22 (`local_gateway.py`, the
   `guarded_load` call and the `_warm` call that follows it).

*Risk:* low for 1-4, and each is separable. 5 and 6 are NOT low and are not separable from the
ledger's admission story — they are listed here because they were verified while looking at
something else, not because they belong in the same change. *Test:* one regression test each
for 1-4; the 409/500 and defer-vs-burn cases have no coverage today.

### L2 — The ledger ◻️

Introduce the row, the phases, the two columns, and the `min(measured, capacity − Σ ledger)`
admission test. Make the ledger the only thing any admission path consults, and make
`read_memory_gb`/`probe.sample()` callable from exactly one place, enforced by an AST guard in
the style of `test_llm_load_guard_chokepoint.py` — the precedent that already exists here, and
whose own docstring gives the reason: "a reviewer noticing is what already failed three times."

Persist the ledger and reconcile at startup: rows with no live process are phantoms, live
processes with no row are foreign. Roll a reservation back explicitly on every failure path,
with a TTL as backstop — and **do not expire a reservation whose transition is still running**
(a 90 s model load must not be swept at 30 s).

*Risk:* high — this is the wave that changes behaviour. It cannot be split: the moment
admission stops reading memory, every layer that still does is inconsistent with it.

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
