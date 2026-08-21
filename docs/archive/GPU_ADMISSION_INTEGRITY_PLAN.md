# GPU Admission Integrity — every load the box makes goes past the guard

> **Status:** Superseded · **Last verified:** 2026-08-21 · **Waves:** W0❌ W1❌ W2❌ W3❌ W4❌

> **SUPERSEDED by `LOCAL_ONLY_BOX_PLAN.md`.** Four cold adversarial reviews found W0
> unbuildable (no per-model memory attribution exists on this box), W1 unsound (its keystone
> category has no data source, and applying it at `local_gateway.py:762` reintroduces the
> mid-load-baseline bug #1186 fixed), W4's premise unreachable, and W3 in contradiction with
> W4. The one thing worth shipping out of it — `unload()` racing llama-swap's 10 s graceful
> stop on a 3 s client timeout — was found BY the review and has shipped separately.
>
> Kept for its evidence and for the record of how it failed: three of its claims were labels,
> summaries or partial source reads mistaken for evidence. The successor plan carries that
> lesson as a standing rule rather than an anecdote.

> Reconciled with the root `CLAUDE.md` non-negotiables — no LLM-adapter or storage
> surface changes (rules 1–2); no wave adds a table, so rule 3's isolation test does
> not apply; every wave names its test (rule 5); no wave adds a dependency (rule 8);
> W2's runbook obligation is named rather than left implicit (rule 9); rule 10 is
> load-bearing and W4 is deliberately shaped to avoid a terminal step.

> **v2, after two adversarial reviews that between them killed a wave, inverted a
> rationale, reversed the wave order and falsified this plan's closing claim.** v1's
> errors are kept visible below rather than deleted, because three of them were the
> same mistake — reading a label, a summary or a comment as evidence without opening
> the primary source — and that is the failure this plan most needs not to repeat.

## The problem, in one sentence

This box's admission guard is repeatedly handed a picture that does not match reality,
and on this hardware a wrong picture costs a power cycle rather than an error.

| | what was wrong | state |
|---|---|---|
| a | the runaway ceiling anchored to a GTT baseline taken mid-load | fixed, `399b15e` on main (#1186) |
| b | `unannounced_load` reported the client's own guarded loads | fixed **on branch**, `656c92a`, not yet merged |
| c | `running()` collapses five llama-swap states into one boolean — `starting` and `stopping` both read as resident | W1 |
| d | `residency._plan` mixes live `used` with **catalog** footprints | W0 |
| e | admission is advisory — the completion POSTs whatever it decides | W3 |

(a) and (b) were symptoms of (c). (d) was found by review and falsifies v1's claim that
"residency's accounting is already correct". (e) is the only path that can still commit GPU
memory with nothing watching it.

## Evidence

**The false abort (a).** `ceiling = baseline.gtt_used + max(24.1 × 1.75, 24.1 + 2.0) = 78.6`
(`gpu_guard.py:330`, `RUNAWAY_MULTIPLE = 1.75` at `:95`), so `baseline.gtt_used = 36.4 GB` —
against a gpt-oss that measures **≈68–69 GB mid-load**. It had reloaded 30 s earlier and was
half-arrived. GTT reached 79.9 and the guard blamed the model being staged:

    gpu_guard.aborting_load — device memory ran away while loading qwen3.8-27b-abliterated:
    GTT 79.9 GB, past the 78.6 GB ceiling for a model predicted at 24.1 GB

*The two-decimal "69.24" v1 used here was wrong to state as "resident".*
`MEMORY_ADMISSION_PLAN.md:333` records that **67.6 is resting GTT and 69.26 is
peak-across-warm, and the two are not comparable** — v1 walked into the trap that plan flags.
The argument survives on either figure (36.4 is roughly half of both); the precision did not.

**The false signal (b).** Same client, three seconds apart, one load — reproduced in the code
comment at `local_gateway.py:251-253`:

    21:41:14.804  local_gateway.load_cache_swept   qwen3.8-27b-q4
    21:41:17.873  local_gateway.unannounced_load   qwen3.8-27b-q4

**The operator override.** `daily_inbox_triage` is `interval_seconds = 3600` despite its name
(`0101_hourly_inbox_triage.py`), calls `triage.classify` against gpt-oss, and its
`reasoning_model_loaded` precondition is evaluated **once at claim time** (`worker.py:193`)
while `_classify` issues one completion per email up to **200** (`gmail/triage.py:73,249-261`).
An explicit unload was undone three times in ninety seconds.

*v1 also claimed a false abort "strands the weights in the page cache". It does not any
more:* `_drop_weights_cache` runs in a `finally` on all three branches
(`local_gateway.py:733-737, 763-767, 780-792`), shipped and on main. Whether it is *effective*
while llama-server still holds the fd is `MEMORY_ADMISSION_PLAN` W0's open research question,
and the 21.4 GB observed on the box on 2026-08-21 suggests it may not be. That belongs to that
plan, not this one.

## The waves

Reordered by review: v1 claimed W0 (triage) had "no dependency on the others" and that was
**false** — its re-check runs through `workflow/preconditions.py:68`, which is a `running()`
call, i.e. exactly the reading W1 corrects. Triage now comes after the split.

### W0 — Fix `_plan`'s arithmetic before anything changes what feeds it ◻️

`residency._plan` mixes two incompatible measures: `used` (`:349`) is a live `read_memory_gb()`
reading, so a starting model contributes only what it has allocated *so far*, while `_footprint`
(`:386,391`) is the **catalog full** size. So `freed += -neg_fp` credits the planner 69 GB for
evicting a model that has allocated 20 — `projected` under-estimates and the load is admitted.
Same defect on the restore path (`:619-640`).

**First, because W1 makes this calculation run more often.** Fixing the inputs before fixing
the reader is the wrong order.

*Files:* `backend/src/jbrain/llm/residency.py`. *Risk:* medium — it is the eviction budget.
*Test:* a plan over a partially-arrived model credits its measured contribution, not its
catalog size.

### W1 — Split `running()` into `running()` and `ready()` ◻️

**The keystone, and it is gated on one measurement.** llama-swap's `handleRunning`
(`internal/server/api.go:333-352` at our pin) emits `State` for every non-stopped entry and
`StateStarting` exists (`internal/process/process.go:15`); `_parse_running`
(`local_gateway.py:1161-1183` — *v1 cited 1119-1141, which is the `/metrics` parser*) reads
only `model`/`id`/`name` and never `state`.

**Gate — SATISFIED 2026-08-21 from primary source**, after the two reviews disagreed on whether
it could be checked at all (one called it unverifiable in-repo, the other reported it verified).
Neither was decisive, so the source was fetched at the pinned commit
(`LLAMA_SWAP_VERSION=60226b63776efac11e15828abe0bb302ec259699`, `deploy/Dockerfile.local-llm:58`):

    // internal/server/api.go:313-321
    type runningModel struct {
        Model string `json:"model"`
        State string `json:"state"`
        ...
    }
    // internal/server/api.go:336-346 — populated for EVERY entry
    for id, state := range states { list = append(list, runningModel{Model: id, State: string(state), ...

So `state` is on the wire and `_parse_running` discards it. The premise holds.

**Three states reach the wire, not five.** `process.go:14-20` defines five constants, but
`internal/router/base.go:366-375` filters TWO of them:

    if st == process.StateStopped || st == process.StateShutdown { continue }

so only **`starting`, `ready`, `stopping`** can ever appear. *A v2 revision of this plan claimed
five, having fetched `api.go` and `process.go` and inferred the rest — the fourth instance in one
day of reading part of a source and guessing the remainder. It would have encoded a `shutdown`
entry, which production cannot emit, into this wave's own acceptance test: precisely the
fake-diverges-from-production failure the wave's second hazard warns about.*

`stopping` still matters — a model mid-unload reports as resident — but see W-1: the reason that
window is wide at all was a client bug, not a design gap, and it is now fixed.

There are **23 call sites across 10 files**. v1 listed five filenames, one of which
(`api/debug.py`) has **zero**. Each site needs one of **four** answers:

**v2's four-row table is withdrawn.** Cold review found three of its four rows wrong or
unbuildable, and the mapping needs re-deriving from scratch:

- *"bill the remaining footprint"* — **not computable.** `read_memory_gb` is whole-host;
  `_footprint` is catalog; `gpu_guard.measure_footprint` needs a *completed* load, measures
  device rather than RAM, and its own docstring says *"Logged rather than stored for now"*.
  There is no per-model attribution on this box, and `host_metrics.read_page_cache_gb`'s
  docstring explains why one should not be invented: *"a number split by guesswork would
  recreate the problem this is fixing."*
- applying that row at `local_gateway.py:762` **reintroduces defect (a)** — it anchors
  `guarded_load`'s ceiling to a mid-load baseline, and shrinking `projected` *tightens* the
  ceiling while the baseline is already inflated. Both terms move the wrong way, into a guard
  `MEMORY_ADMISSION_PLAN.md:208-222` records as already false-aborting a 4B model on a 0.9%
  overshoot — which this plan declines to fix.
- *"leaving — do not evict again"* is **backwards at its own example.** `image_gen/render.py:205`
  unloads everything to free the pool for ComfyUI; a `starting` model is the largest incoming
  allocation and skipping it is the worst outcome. Same at `cli.py:64` and `api/jcode.py:162`.
- assigning `_require_resident` → `ready()` **blinds the load bar**: `/slots` during a load is
  where the progress fraction comes from, and this branch exists to provide that indicator.
- at least three answers are missing, including "a `starting` model must still be unloaded so it
  reloads at a changed `-c`" (`llm_settings.py:831,899`) — a correctness question, not a memory
  one.

And `api/jcode.py:122-127` already documents this wave's central insight and works around it
with `_warming_models`. Any re-derivation starts there.

The third category is the wave's real work. `local_gateway.py:762` is the branch that **skips
`refuse_if_no_device_room` and `guarded_load` entirely** — a plan about admission integrity
must own it. `residency.py:351` returns `target_gb=0.0, already_resident=True` for a
half-arrived 69 GB model, declaring the load free.

Also unlisted in v1 and required: `workflow/preconditions.py:68`, `smoketest.py:162` (an
admission bypass on the **deploy** path), `api/jcode.py:154,161` (one read serving both
questions five lines apart), `image_gen/render.py:205`, `cli.py:64`,
`local_gateway.py:333,717` (reload-casualty narration — must agree or the operator is told a
surviving model was killed), `api/llm_settings.py:161` (a **fourth** question: UI truth).

Two hazards v1 missed:
- **The detector's invariant.** `running()` is the sole caller of `_drop_cache_for_unannounced`
  (`local_gateway.py:221`). If `ready()` also polls `/running` and feeds it, `_seen_resident`
  thrashes — a starting model added by one, removed by the other, re-detected as an arrival —
  logging `unannounced_load` and dropping the page cache of a model still reading its weights.
  That is defect (b) resurrected with real damage.
- **Protocol churn.** `ready()` on the `LocalGateway` Protocol means **18+ fakes** grow a
  method, and a fake whose `ready()` returns the same set as `running()` makes every test pass
  while production diverges. The fakes must diverge by default.

*Files:* the ten above. *Risk:* **high** — widest blast radius in the plan, and a wrong
assignment fails toward host freeze. *Test:* a fake driven through all five states, asserting
accounting counts `starting`, reachability refuses it, the arriving case bills the balance, and
`stopping` is neither reached nor evicted twice — plus a fake-fidelity test that fails if
`ready()` is a synonym for `running()`, since 18+ fakes could otherwise make the suite green
while production diverges.

### W2 — Stop triage overriding an explicit unload ◻️

Re-check `reasoning_model_loaded` between emails rather than once per claim.

**But not by deferring mid-batch, which is what v1 proposed.** `InboxTriage.run`
(`gmail/triage.py:178-206`) classifies the **whole** batch at `:193` and only files at `:198`,
one `batchModify` per bucket — and the docstring at `:160-162` says persistence *is* the Gmail
labels. So there is no resumption state: deferring mid-`_classify` discards every verdict
computed, up to 199 wasted local completions, and the next fire redoes them. `progress_note` is
a display string for the Ops Runs screen (`worker.py:182`), not a checkpoint.

So W2 is **incremental filing or a verdict checkpoint first**, then the defer. That is the
wave's actual scope, and it is not "low risk".

**The rename is out of scope.** v1 said "also fix the name". `daily_inbox_triage` is the
pipeline name seeded by `0096` and referenced by `app.triggers.pipeline` (`0096:66-67`), and
migration `0101:24` **explicitly declined** the rename: *"the seeded identifier (0096); kept
stable, not renamed."* Renaming needs a lockstep update of `app.pipelines.name`,
`app.triggers.pipeline` and five occurrences in
`backend/tests/integration/test_runs_reader_rls.py:165,203-214`, against a prior decision on
record. Not a throwaway line in another wave.

**One instance of a class.** `triage.classify` is the *only* registered precondition in the
codebase (`grep precondition=` → one hit). `wiki/lint.py`, `analysis/pipeline.py`,
`ingest/ocr.py`, `agent/daily_briefing.py` and `jpet/brain.py` all run per-item LLM loops with
no gate at all and can override an owner unload identically. W2 fixes the instance; the class
needs its own decision.

*Files:* `backend/src/jbrain/gmail/triage.py`, `backend/src/jbrain/worker.py`,
`backend/src/jbrain/workflow/preconditions.py`. *Risk:* medium — it changes when Gmail labels
are written. *Test:* a sweep whose model is unloaded mid-batch files what it has classified,
defers the rest, and resumes without reclassifying. *Docs:* no runbook mentions inbox triage
at all today (`grep -rl "inbox.triage" docs/` → empty); W2 owns adding it, including that the
scheduler never replays missed fires (`scheduler.py:427-443`) so the minute-of-hour drifts.

### W3 — Make a declined admission fail a background sweep ◻️

`router.py` awaits `_admit_local` and POSTs **regardless** — at `:619` (`complete`), `:696`
(`converse`), `:747` (`converse_stream`). *v1 cited `:359-361`, which is the definition, not a
call site.*

**Five silent declines, not four.** `residency.py` `:451` (not enabled), `:469-471` (no box
lock — non-production), `:474` (resident at poll time), `:486-490` (`_plan` raised), `:491-492`
(`plan is None`). And review found the commonest: **`plan.over`** — projected past the
free-RAM ceiling after evicting everything evictable — is *not* refused; only `plan.over_box`
raises (`:427-436`).

The decision: **a background sweep fails, an interactive turn degrades.** The hard part is that
the intent is not expressible where v1 said it was. `tasks/runner.py:120` already has
`supervised: bool` and **never propagates it to the router**; one `AgentLoop.run_stream` serves
both a supervised continuation with a PWA client watching and a headless scheduled task, under
the same `agent.turn` name. Every agent tool (`imagegentools.py`, `visiontools.py`,
`croptools.py`, …) calls `router.complete` from a handler reached identically from chat and
from a scheduled sub-agent. So W3 threads intent from `tasks/runner.py` through `agent/loop.py`,
not through the three files v1 named — and needs a **third** category for
`warm_keeper.py:189`, which is background but must not fail loudly or the keeper spins.

Also in scope, and missed by v1 in a file it listed: `api/jcode_llm.py:176-180` raises
`ResidencyError` **inside the streaming generator**, after `StreamingResponse` has committed a
200. The client gets a truncated stream, not an error.

*Files:* `router.py`, `residency.py`, `tasks/runner.py`, `agent/loop.py`, `api/jcode_llm.py`.
*Risk:* medium-high — wrong scoping makes chat turns fail. *Test:* a background caller whose
admission declines raises and loads nothing; an interactive caller with the same decline still
answers; the jcode proxy's decline reaches the client as an error, not a truncation.

### W4 — Make jcode's gateway fallback fail closed ◻️

**v1 proposed building a residency-aware streaming proxy for jcode. It already exists** —
`backend/src/jbrain/api/jcode_llm.py`, which calls `ensure_room` before every forward, holds a
per-box swap lock, and streams. v1 cited `docker-compose.yml:527`
(`JCODE_MODEL_BASE_URL`, consumed nowhere in this repo, feeding `jcode_ctl` — whose own
`config.py:37-39` carries a comment written for exactly this misreading). The CLI reads
line **538**: `GROK_MODELS_BASE_URL: ${JCODE_GROK_MODELS_URL:-http://api:8000/api/jcode/llm/v1}`.

The residual defect is real, small, and inverted in shape from what v1 described:
`jcode/grok-config.sh:24` falls back to `http://local-llm:8080/v1` — the raw gateway — when the
var is unset, and `update-inner.sh` writes `JCODE_MODEL_URL` (`:439`) but never
`JCODE_GROK_MODELS_URL`. So a box whose compose predates `:538` gets unguarded inference with
no signal.

Fail closed instead: no proxy URL, no inference. And `update-inner.sh` should write the var —
using the in-place `.env` key-rewrite idiom **already in that file at `:552-559`**, which v1
overlooked while calling this "a terminal-shaped change on a box with no terminal".

*Files:* `jcode/grok-config.sh`, `deploy/update-inner.sh`. *Risk:* low. *Test:* the rendered
config refuses to start against the bare gateway URL.

## Relationship to `MEMORY_ADMISSION_PLAN`

That plan owns **how big the budget is and how it is measured**; this one owns **whether a load
reaches the budget at all**. Stated as functions rather than a slogan, because review found the
seam contends in two places:

- `local_gateway._load_and_warm:762-792` — its W2/D4a extends the guard across `_warm()`; this
  plan's W1 decides whether the guard runs at all. **Same twenty lines.** Whichever lands first
  owns them; the second reconciles.
- `refuse_if_no_device_room` — its W1a changes the admission ceiling. That is refusal logic, not
  only sizing. This plan does not touch it.

**`ttm.pages_limit` belongs entirely to that plan, and v1 got it backwards.** v1 drafted a wave
to "re-arm the last backstop", then deleted it citing that plan as having overturned the idea.
Both were wrong: `MEMORY_ADMISSION_PLAN.md:105-140` is a **`✅ RESOLVED`** block whose conclusion
is *"A BOOT-TIME `ttm.pages_limit` below MemTotal is a genuine hard cap"* — v1 quoted the
`ttm_tt_populate` soft-loop half, which that plan kept *specifically to explain why it is not
the mechanism*. So `gpu_guard.py:19-22` is **correct** and v1 proposed "correcting" it. The
host-side number is that plan's W1d.

## What this plan does NOT do

- It does not widen `RUNAWAY_MULTIPLE`. The ceiling exists to catch an order-of-magnitude
  balloon; loosening it enough to absorb a second model's allocation blinds it to that.
- It does not make `unannounced_load` react by unloading. Cross-process sightings remain
  unavoidable even after `656c92a`, and until W1 lands `/running` includes starting models — an
  auto-unload would race llama-server startup and kill models the box deliberately just loaded.
- It does not rename `daily_inbox_triage` (see W2).
- It does not touch `ttm.pages_limit` (see above).
