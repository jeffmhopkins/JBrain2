# GPU Admission Integrity — every load the box makes goes past the guard

> **Status:** Draft · **Last verified:** 2026-08-21 · **Waves:** W0◻️ W1◻️ W2◻️ W3◻️ W4❌

> Reconciled with the root `CLAUDE.md` non-negotiables — no LLM-adapter, storage or
> RLS surface changes (rules 1–3); tests land with the code (rule 5); rule 10 is
> load-bearing, and W4 is named host-bound with no pretence otherwise.

## The problem, in one sentence

This box's admission guard is repeatedly handed a picture that does not match reality,
and on this hardware a wrong picture costs a power cycle rather than an error.

Four separate defects found on 2026-08-21, all the same shape:

| | what was wrong | already fixed |
|---|---|---|
| a | the runaway ceiling anchored to a GTT baseline taken mid-load | #1186 |
| b | `unannounced_load` reported the client's own guarded loads | `656c92a` |
| c | `running()` says "resident" from process spawn, before a byte is read | **no** |
| d | admission is advisory — the completion POSTs whatever it decides | **no** |

(a) and (b) were symptoms. (c) is upstream of both. (d) is the only path that can still
commit GPU memory with nothing watching it.

## Evidence

All measured on the live box, 2026-08-21, and reproduced in the log excerpts below.

**The false abort (a).** `ceiling = baseline.gtt_used + max(24.1 × 1.75, 26.1) = 78.6`, so
`baseline.gtt_used = 36.4 GB` — while gpt-oss measures **69.24** resident. It had reloaded
30 s earlier and was half-arrived. GTT reached 79.9 (gpt-oss finishing + the staged model
starting) and the guard blamed the staged model:

    gpu_guard.aborting_load — device memory ran away while loading qwen3.8-27b-abliterated:
    GTT 79.9 GB, past the 78.6 GB ceiling for a model predicted at 24.1 GB

A false abort is not cheap: it unloads a healthy model **and** strands the weights it read in
the page cache, which `read_memory_gb` counts as used, so the next load sees less headroom and
is likelier to abort in turn. Observed: cache 4.2 → 21.4 GB in one step, and 36 GB on the
operator's memory bar minutes later.

**The false signal (b).** Same client, three seconds apart, one load:

    21:41:14.804  local_gateway.load_cache_swept   qwen3.8-27b-q4
    21:41:17.873  local_gateway.unannounced_load   qwen3.8-27b-q4

**The operator override.** `daily_inbox_triage` is `interval_seconds = 3600` despite its name,
calls `triage.classify` against gpt-oss, and its `reasoning_model_loaded` precondition is
checked **once at claim time** (`worker.py:240-267`) while `InboxTriage._classify` issues one
completion per email up to **200** (`gmail/triage.py:239-261`). An explicit unload was undone
three times in ninety seconds; llama-swap logged the collision itself
(`[WARN] group: starting gpt-oss-120b failed: aborted`).

## The waves

Ordered so each one makes the next safe to attempt. W1 is a prerequisite for trusting any
measurement W2–W4 depend on.

### W0 — Stop triage overriding an explicit unload ◻️

The operator-facing bug, and the only wave with no dependency on the others.

Re-check `reasoning_model_loaded` between emails, not once per claim. When the model has gone,
**defer the remainder** rather than reloading — the batch is already resumable
(`progress_note` records "processed N of M"), so the tail costs nothing to postpone. An owner
unload means "I want this memory back" and must win over a background sweep.

Also fix the name: an hourly job called `daily_inbox_triage` misled this investigation for an
hour. The scheduler never replays missed fires (`workflow/scheduler.py:426-443`), so its
minute-of-hour drifts from the deploy — worth stating in the runbook.

*Files:* `backend/src/jbrain/gmail/triage.py`, `backend/src/jbrain/worker.py`, a migration for
the trigger name. *Risk:* low. *Test:* a sweep whose model is unloaded mid-batch defers
instead of reloading, and resumes on the next fire.

### W1 — Split `running()` into `running()` and `ready()` ◻️

**The keystone.** llama-swap returns every entry in `RunningModels()` including
`StateStarting` (`internal/server/api.go:333-352`); `_parse_running`
(`local_gateway.py:1119-1141`) drops `state` on the floor. So "is it resident?" answers yes for
a model that has not read a byte.

Two callers ask two different questions and today get the same wrong answer:

- **accounting** — `residency._plan`'s eviction candidates, the guard's baseline — wants
  starting models **included**. They are consuming. Excluding them under-counts, which is the
  direction that freezes this box.
- **reachability** — `_require_resident`, the load-join check, `warm_keeper`'s `cold` test —
  wants **Ready only**. Reaching a starting model's endpoint is the on-demand-load path we
  guard against.

So: `_parse_running` starts carrying `state`; `running()` keeps today's meaning (anything
spawned, for accounting); `ready()` is added (Ready only, for reachability); **every call site
is audited and assigned deliberately**, one at a time, with the reason in the diff.

*Files:* `backend/src/jbrain/llm/local_gateway.py`, `residency.py`, `warm_keeper.py`,
`api/llm_settings.py`, `api/debug.py`. *Risk:* **medium-high** — this is the widest blast
radius in the plan and a wrong assignment fails in the freeze direction. *Test:* a fake
gateway that reports a `StateStarting` model, asserting accounting counts it and reachability
refuses it.

### W2 — Make a declined admission fail the completion ◻️

`router.py:359-361` awaits `_admit_local` and POSTs **regardless**. Four ways it declines while
the request still goes out (`residency.py:469-496`), each committing GPU memory with no
`refuse_if_no_device_room`, no `guarded_load`, no box lock, and no eviction having run — and
`swap: false` means the model is *added* beside whatever is resident.

The decision this wave encodes: **a background sweep fails, an interactive turn degrades.**
Today everything degrades, which is defensible for a chat turn and indefensible for the path
whose failure mode is a power cycle. Needs a caller-intent flag through `complete`/`converse`/
`converse_stream`.

*Files:* `backend/src/jbrain/llm/router.py`, `residency.py`, `api/jcode_llm.py`. *Risk:*
medium — turns a degraded-but-working path into an error for background work; wrong scoping
makes chat turns fail. *Depends on W1*, because "resident at poll time" (`residency.py:472`) is
one of the four declines and is exactly the reading W1 corrects.

### W3 — Bring jcode's inference inside the guard ◻️

`deploy/docker-compose.yml:527` points the sandbox's CLI straight at llama-swap. Unguarded
inference on `POST /v1/chat/completions`, outside the app entirely.

**llama-swap cannot fix this.** Verified against its source at our pinned commit: `upstream.
ignorePaths` (`internal/config/upstream.go`, enforced `internal/server/api.go:513-533`) returns
409 for a non-ready model but covers `/upstream/...` **only**; nothing in the full top-level key
set refuses a load on the dispatch routes. `apiKeys` would block unknown callers but breaks
jcode until jcode carries the key.

So: a streaming residency-aware inference proxy in `api/jcode.py`, and `JCODE_MODEL_URL`
repointed at it. Note `update-inner.sh:439` only writes the var when **absent**, so a box whose
`.env` already pins the direct URL needs a migration — and that is a terminal-shaped change on
a box with no terminal (rule 10), which W3 must design out rather than paper over.

*Risk:* **high** — streaming fidelity, an added hop, `.env` drift. *Deliberately last*: it is
the widest change and the least certain to be worth its cost.

### W4 — ~~Re-arm the host backstop~~ ❌ DELETED, and the reason matters

Drafted as "`ttm.pages_limit` is the last backstop and it is switched off", citing
`gpu_guard.py:20-23`. **`MEMORY_ADMISSION_PLAN` v2 already overturned exactly this**, after
three adversarial reviews: `ttm.pages_limit` is a self-eviction *trigger*, not a cap;
`ttm_pages_allocated` can never exceed physical RAM, so at or above `MemTotal`
`ttm_global_swapout()` never runs and there is **no allocation-time back-pressure at all**. It
was never the lost boundary, so there is nothing here to "re-arm".

Kept visible rather than deleted quietly, because the comments in `gpu_guard.py` still assert
the superseded version and a researcher read them in good faith this afternoon. Correcting
those comments belongs to `MEMORY_ADMISSION_PLAN`, whose W1 owns the host-side number.

## Relationship to `MEMORY_ADMISSION_PLAN`

That plan (Draft, v2) owns **how big the budget is and how it is measured** — the reserve, the
projection, `no_alloc` measurement, the host-side number. This plan owns **whether a load goes
past the budget at all**. They meet at one seam and must not both edit it: `gpu_guard`'s
ceiling arithmetic is theirs, `gpu_guard`'s *reachability* — which loads even enter it — is
this plan's.

Two of its findings are relevant here and are NOT re-litigated:
- its W0 covers the page-cache drop on the abort path. Re-checked 2026-08-21: the drop now
  runs in a `finally` (`local_gateway.py`), and a live abort left cache reclaimable — so that
  half of its W0 appears shipped. Its plan text should be reconciled; not this plan's call.
- its D0 ("eight uncoordinated budgets, no single source of truth") is the frame this plan's
  W1 serves: a `running()` that means two things is one of those eight.

## What this plan does NOT do

- It does not widen `RUNAWAY_MULTIPLE`. The ceiling exists to catch an order-of-magnitude
  balloon; loosening it enough to absorb a second model's allocation would blind it to exactly
  what it is for.
- It does not make `unannounced_load` react by unloading. The signal cannot support it: even
  after `656c92a`, cross-process sightings remain unavoidable, and `/running` includes starting
  models until W1. An auto-unload would race llama-server startup and kill models the box
  deliberately just loaded — on the exact surface an owner watches during a freeze.
- It does not touch `residency`'s accounting, which is already correct: `_plan` re-reads
  `running()` and live memory every call, so an unannounced model is counted and is a valid
  eviction candidate. What is lost is admission, not accounting.
