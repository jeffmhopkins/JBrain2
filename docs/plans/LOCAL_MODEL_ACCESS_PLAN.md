# One way in, one way out

> **Status:** In progress · **Last verified:** 2026-08-22 · **Waves:** W0🟡 W1◻️ W2🟡 W3◻️ W4◻️

> Reconciled with the root `CLAUDE.md` non-negotiables — every LLM call still goes through the
> adapter (rule 1) and W4 shrinks that surface; no storage or RLS changes (rules 2–3), no new
> table; tests land with the code (rule 5); W2 repurposes an **existing** PWA control, so no wave
> adds a GUI surface and the `PROCESS.md` GUI gate does not fire.

> **This plan is written from `../reference/MODEL_ACCESS_INVENTORY.md`, not from memory.** Four
> predecessors were withdrawn after cold review, each for the same reason: a claim asserted from a
> docstring, a log label, an env-var name, or a grep count without the file being opened. The
> inventory exists so that no claim here has to be taken on trust — every number below is a row
> count in that document, and every row there carries a verbatim quote. **If a statement in this
> plan is not traceable to an inventory row, it is a defect.**

## What the inventory found

Not "there is no door". A door exists, and `build_router` already refuses to hand out a router
without one (`router.py:818-838`). The problem is narrower and worse:

**1. The gate can be constructed half-wired, and two of them are.** `ResidencyCoordinator` takes
its switches as optional keyword arguments. `router._default_residency` (`:867-896`) omits
`hold_loader`, `box_lock`, `slots_loader` and `fraction_loader`; the worker's coordinator
(`worker.py:539`) omits `auto_restore_loader` entirely — `grep -c auto_restore worker.py` → `0`.
A coordinator with no `hold_loader` returns an empty set from `_held_names()`, and **empty means
"admit everything"**. So an operator control can be enforced on one instance and invisible on
another, with nothing in the type system objecting.

**2. Admission does not know who is asking.** `ensure_room(served_model)` takes a model name and
nothing else. Every demand site looks identical to it — a chat turn you typed, a tool reaching for
a second model mid-turn, an hourly sweep. A control that treats those the same cannot be the
control you asked for. §C of the inventory classifies all **38** demand sites into four kinds; that
table is the missing argument.

**3. Refusals are outside the LLM error hierarchy, so some sites swallow them.**
`ResidencyError`, `GpuBudgetError` and `LocalGatewayError` subclass `Exception` directly
(`residency.py:109`, `gpu_guard.py:227`, `local_gateway.py:87`), not `LlmError`. The 12
`except LlmError` sites therefore let a refusal propagate — correct — but every bare
`except Exception` swallows it. Inbox triage is **one call per email, up to 200 per fire, with no
try/except in the loop**; wiki lint batches 20 and swallows bare `Exception` inside
`_verify_batch`. Enforcement that is silently discarded at the call site is not enforcement.

**4. Ten mechanisms load a model without any `.load()` in our code**, enumerated as I1–I10 in §A.5
and confirmed against llama-swap at the pinned commit `60226b6`. Most matter little; three do, and
W0 states plainly which of the ten the gate can and cannot cover. **This is where the last plan
lied, so it is stated as a limit rather than a guarantee.**

**5. The main pool never reclaims anything on its own.** `llama_swap_config.py` emits no
`ttl`/`globalTTL`, and llama-swap's `GlobalTTL` defaults to `0`, which gates the TTL goroutine off.
Only whisper has one (`ttl: 300`). Every model that becomes resident on the main pool stays
resident until our code unloads it. That is good for co-residency and it means the gate is the
only thing standing between a demand and the memory.

**6. Agent turns do not use the task table.** `_resolve` (`router.py:374-389`) orders env pin →
**strength tier** → task default, and `AgentLoop` passes `SYSTEM_STRENGTH` on every call
(`loop.py:632, 647, 1060, 1469`). So an un-pinned agent turn resolves through `TIER_DEFAULTS`
and never reaches its `agent.turn` entry. All 20 task defaults and all 3 tier defaults are the
identical string `"xai:grok-4.3"`, so W4 is uniform — but it must target the **tiers**, which
carry the dominant route.

## What the live box added

Read through the debug console on 2026-08-22 and recorded in §E of the inventory. Three things
change the plan's sizing, and one is the reason W0 comes first.

**Routing is already fully local.** All 19 selectable tasks resolve to on-box models; the provider
list carries 12 local models and no cloud entries. Finding 6's "all 23 name the same cloud string"
describes `TASK_DEFAULTS` on a *fresh* box, not this one. **W4 is therefore housekeeping, not a
migration** — there is no re-routing to do first, and no quality risk to measure, because the box
has been running local for some time.

**The toggle is already off, and models load anyway.** `auto_restore: false` live. That is W2's
thesis confirmed from the box rather than from source.

**And a reproduction of the owner's complaint, with timestamps.** `gpt-oss-120b` was unloaded
deliberately at 21:42; at 22:41 the hourly inbox sweep used it and billed 7,519 tokens; it has been
resident ever since; **no `model_load` event exists between the unload and now.** Three mechanisms
failed together — the toggle (does not gate admission), the codebase's only registered
`precondition=` (did not defer), and event narration (absent, so the load did not go through
`gateway.load`).

A stale residency read explains all three, and it is what W0 fixes — but it is a **hypothesis**,
not a finding. The reproduction is cheap and named in §E. **W0 must not ship on the hypothesis
alone**: its step 1 (log the short-circuit with the state llama-swap reported) is what turns this
into evidence, which is why that log line ships before the behaviour change.

This also means W2 alone would not have prevented what happened. A gate that asks the right
question still fails on a wrong answer.

## The waves

### W0 — A gate that can be trusted 🟡

> **Landed so far.** The half-wired-gate half is complete and the measurement is in place;
> the collapse and the `ready()` split are not.
>
> | item | state |
> |---|---|
> | switches required, not optional | ✅ `c76288f` closed the two instances, `81ac6b9` closed the class — `ResidencyWiring` has no defaults, so an omitted switch is a pyright error |
> | admission behind the shared warm helpers | ✅ `2f9904f` — moved *inside* `gateway_load`/`gateway_prime`, which is why the planned call-site AST walk was replaced (below) |
> | collapse to one `admit`/`load`/`unload`/`resident` | ◻️ **not started** — the 7/14/17/28 counts below are unchanged |
> | 1. log the short-circuit with llama-swap's state | ✅ `1c5fcb1` |
> | 2. split `running()` from `ready()` | ◻️ **waits on the measurement above reaching the box** — see the note at the end of this wave |
> | the AST guard | ✅ `68975dc` + `81ac6b9`, but narrower than planned — see below |
>
> **Why the AST guard is smaller than this wave specified.** The plan assumed admission would
> stay at the call sites, so a walk over them was the only way to check it. Moving it inside the
> two helpers made `residency` a required keyword, and pyright now rejects an omission — a
> call-site walk would only re-check what the type system already enforces. What types cannot
> see is the *value*: `None` is legal, and a `getattr` default that silently yields it is how the
> debug console loaded twice while evicting nothing. So the guard pins the two approved spellings
> of the coordinator, plus `ResidencyWiring.inert()` staying out of `src/`.
>
> **And a correction to this wave's own framing.** An unadmitted warm is a documented
> *degradation*, not a hole: `LocalGatewayClient.load` still runs the device-memory guard, so it
> loses eviction — a model that would have fit after freeing room gets a 409 — but cannot take
> the box down. The freeze path is closed either way.

**Make the switches required, not optional.** Replace the optional-kwarg constructor with one that
takes a single explicit settings-source object, so a coordinator either has every loader or does
not compile. The two half-wired instances (`router._default_residency`, `worker.py:539`) are then
compile errors rather than silent behaviour differences.

**Collapse the access points, per service.** §A lists 7 loads, 14 unloads (3 of them on the
whisper gateway, not the pool), 17 admission gates and 28 `running()` reads. After W0 there is
exactly one of each behind the gate: `admit`, `load`, `unload`, `resident`. The gateway's own
methods become private to that seam, guarded by an AST test — following the precedent already in
this repo at `tests/unit/test_llm_load_guard_chokepoint.py`, which walks the `src` AST and fails
the build if a `LocalGatewayClient` is built without a probe.

**What the gate covers, and what it cannot.** Of §A.5's ten indirect mechanisms:

| covered by W0 | how |
|---|---|
| I1 request-driven load via the OpenAI endpoint | our code admits before sending a completion for a non-resident model — `router.py:619, 696, 747` already do; W1 makes the intent explicit |
| I2/I3 the two proxies | same, at `api/jcode_llm.py` and `api/external_llm.py` |
| I5 config-rewrite reload | W3 — it is an eviction, and it is narrated |

| **not** covered, and why | consequence |
|---|---|
| I4 a direct `GET /upstream/{model}/…` | anything that can reach the gateway's port can load a model. The gate is in our process, not in llama-swap |
| I6 SIGHUP, I9 group swap, I10 container restart | llama-swap's own lifecycle |
| I7 `Hooks.OnStartup.Preload` (`api.go:375`) | inert today — our generated config emits no `hooks:` key — but it exists, and nothing in the repo mentioned it before this inventory |
| I8 TTL | inert on the main pool (see finding 5); live on whisper |

**"One way in and out" therefore means: one way in and out *for our code*.** That is the honest
claim, and it is the one the AST test can actually hold.

**And stop the gate being fed a stale answer.** A gate that is wired correctly still fails if the
question it asks returns the wrong value, and §E records that happening: `ensure_room`'s fast path
returns early when the model is in `running()` —

```
residency.py:473        with contextlib.suppress(Exception):
residency.py:474            if served_model in await self._gateway.running():
residency.py:475                self._displaced.discard(served_model)
residency.py:476                return
```

— but `running()` includes models llama-swap is **stopping**. Confirmed at the pinned commit:
`internal/router/base.go:366-375` filters only `StateStopped`/`StateShutdown`, and `_parse_running`
(`local_gateway.py:1182-1204`) discards the state field and returns bare names. A model mid-stop
therefore reads as resident, admission returns without admitting, the completion reaches llama-swap,
and llama-swap relaunches it — no guard, no event. It is reachable *through* the door, so neither
the AST test nor any call-site audit finds it. Same shape satisfies `model_already_loaded`
(`preconditions.py:44-71`), which is the precondition that did not defer at 22:41.

**Two steps, in this order, and the first is one line:**

1. **Log the short-circuit with the state llama-swap actually reported.** This is the measurement
   that turns §E's hypothesis into evidence, and it must land before the behaviour change — a
   predecessor plan made this the trigger for a wave that was itself scheduled last, so the
   evidence could never be gathered.
2. **Split `running()` from `ready()`** and make both the fast path and the precondition require
   *ready*. `_parse_running` already receives the field and throws it away.

**The known hazard:** `api/llm_settings.py:156-161`'s `_loaded_ids` is both a call site this changes
**and** the owner's load indicator, so a careless change makes a loading model vanish from the
screen. And billing a mid-stop model's remaining footprint is what reintroduced bug #1186 — the
unguarded branch is `local_gateway.py:757`, the pre-flight `:800-801`.

*Files:* `residency.py`, `local_gateway.py`, `router.py`, `worker.py`, `main.py`,
`workflow/preconditions.py`, `api/llm_settings.py`, plus the AST test.
*Risk:* medium — wide but mechanical, and a missed caller is a build failure rather than a silent
hole; the residency-read change touches the load indicator. *Test:* the AST guard; a constructor
test proving a half-wired coordinator cannot be built; one behavioural test per collapsed access
point; a model reported `stopping` does not satisfy the fast path or the precondition, and
`_loaded_ids` still shows a loading model.

### W1 — Admission learns who is asking ◻️

`admit` grows an intent argument. §C.9's table is the mapping, and it is evidence-based per site —
each classification is backed by a quoted route decorator, `AgentLoop(...)` construction site, or
`worker.py` handler registration, never a filename:

| intent | examples from §C.9 |
|---|---|
| `owner_turn` — a person is waiting | `/chat`, `/intake/chat`, the wiki Talk editor, `/pet/command`, the operator's Load and Prime buttons |
| `tool_in_turn` — inside a person's turn, reaching for a **different** model | the 7 vision tools, the deep-research fan |
| `agent` — no person waiting | the deepest lane, its boot resume (`main.py:1019`), plan continuation, `schedule_restore` |
| `scheduled` — time-driven | the WarmKeeper, the tasks tick, 13 job handlers |

**Then fix the swallow sites**, or W2 is decorative. §C.6 enumerates every place a refusal is
caught and what it catches. Two shapes:
- a sweep that swallows bare `Exception` per item retries the refusal N times and reports nothing —
  it must let `ResidencyError` through to `worker.py:209-216`, which already defers **without
  burning an attempt** (`queue.defer`, `RETRY_AFTER = 5 min`);
- an interactive path must surface the reason, not a transport error.

*Files:* `residency.py`, `router.py`, the 38 demand sites in §C.3, `wiki/lint.py`,
`analysis/pipeline.py`, `ingest/ocr.py`, `gmail/triage.py`, `jpet/brain.py`.
*Risk:* medium — touches many files, each change one line plus a test.
*Test:* every demand site declares an intent (AST-enforced); a refusal reaches the worker's defer
rather than being swallowed; a per-item sweep defers once instead of failing 200 times.

### W2 — Repurpose the existing toggle 🟡

> **Landed so far.** Move 1 only, plus one item this wave did not list.
>
> | item | state |
> |---|---|
> | 1. wire the loader into the worker's coordinator | ✅ `c76288f`; `81ac6b9` then made omitting it a compile error rather than a convention |
> | 2. move the check from `_restore` to `admit` | ◻️ **blocked on W1** — it gates *by intent*, and the intent argument does not exist yet |
> | 3. decide by intent (the table below) | ◻️ same |
> | copy + refusal-reason change | ◻️ not started |
> | *(not in this wave as written)* admit the two debug-console loads | ✅ `2f9904f` — §A's two unadmitted loads were both on the debug console; they are W2's subject even though this wave was written around the toggle |
>
> Moves 2 and 3 are the substance of this wave and neither can start before W1. What is done is
> the wiring they will need.

**No new control.** `PUT /settings/llm/auto-restore` exists (`api/llm_settings.py:1228-1250`),
persists to `LLM_LOCAL_AUTO_RESTORE_KEY` (`settings_store.py:99`) as an `app.settings` row that
survives restart, and is already rendered at `LLMSettingsScreen.tsx:1188-1194`. Its docstring
already states the intent: *"a box that does nothing on its own while the owner is diagnosing it."*

**What it gates today is narrower than its name suggests.** §D.2: two wiring lambdas, both in
`main.py` (`:447` residency, `:1174` WarmKeeper), and three consumers — `residency._auto_restore()`
called from **`_restore` only** (`residency.py:601`, not from `ensure_room`, `free_room` or
`plan_load`), `warm_keeper._auto_restore_allowed()` (`:136`), and a read-only snapshot
(`llm_settings.py:459`). The worker never sees it at all.

**The change is three moves:**
1. Wire the loader into the worker's coordinator — W0 makes omitting it impossible.
2. Move the check from `_restore` to `admit`, so it gates **loads**, not just restores.
3. Decide by intent, per W1. **Owner-decided 2026-08-22:**

| intent | toggle ON | why |
|---|---|---|
| `owner_turn` | **allowed** — loads the model that turn needs | a person is waiting and asked for it |
| `tool_in_turn` | **refused**, with the reason | a second model mid-turn is the co-residency threat; a person is waiting, so it cannot defer |
| `agent` | **deferred** | the morning news and briefing arrive late rather than not at all |
| `scheduled` | **deferred** via `queue.defer`, no attempt burned | the hourly and nightly sweeps |

**The gate is at the model, not at the job.** This is the rule that settles ingestion, and it is
simpler than a per-job-kind list: a job that never needs a model load is never gated. Storing and
embedding a note both run freely — TEI is a separate always-up container with `mem_limit: 1g` and
no load or unload path in our code (§B.2) — so nothing a note needs to be safe on disk waits on a
model. Only the LLM-dependent step (`note.extract`, `integrate.note`) defers, and it defers by the
same mechanism as any other background work. Notes are never silently stuck; the reasoning waits.

Plus a copy change so the label describes what it now does, and the refusal reason names the
toggle rather than code mode — `residency.py:465-467` currently hardcodes *"Code mode is holding
the box…"*, which would misattribute every refusal.

**Not a drain.** Turning it on stops new loads; it does not evict what is resident
(`residency.py:462-464` short-circuits an already-resident model, and no hold path evicts). The
existing per-model unload controls remain how the owner frees the pool. Saying so here because a
predecessor plan assumed the opposite and its test would have passed while the box stayed full.

*Files:* `residency.py`, `worker.py`, `api/llm_settings.py`, `LLMSettingsScreen.tsx` (copy only).
*Risk:* low-medium — one existing control, wider authority. *Test:* with the toggle on, each of
the four intents gets its stated outcome; the worker honours it; a restart preserves it.

### W3 — Account for the other four consumers ◻️

§B: the pool is shared by five things and the gate sees one.

- **Whisper** — unload-only, 8 call sites, no admission before any; a separate gateway whose
  wiring is proven per call site in §B.1. It gets the same seam, its own instance.
- **ComfyUI** — `render.py:205-206` unloads every model on the **main** gateway with no admission,
  and nothing on an LLM path ever frees ComfyUI (`.free()` has two callers: the render itself and a
  PWA button). The asymmetry is the finding; W3 makes both directions go through the gate.
- **TEI** — governed only by `mem_limit: 1g` (`compose:641`); no load/unload exists in our code.
  Recorded, not changed.
- **Kokoro TTS** — *"the warm server holds the model resident"* (`compose:474`), with no memory
  accounting found anywhere in `backend/src`. W3 gives it a footprint number; whether it needs a
  control is a question for after the measurement.

**Also fix the config comment that says the opposite of the deployment.** `config.py:392-393`
describes whisper as *"served by the same llama-swap gateway the local-llm profile runs"*. It is
not. That comment is the kind of artefact that has already caused a withdrawn plan, and it sits in
the file a refactor reads first.

**And expose the GTT counters to the debug console.** `gpu_guard.SupervisorGpuMemProbe` already
reads `gpu_mem` from the supervisor's `/metrics`, and the Ops screen draws it — but `/api/ops/*`
rejects a capability token and no `/api/debug/*` route carries it. So the surface the owner hands
an assistant is blind to resident model memory: with 69 GB resident the `local-llm` container reads
0.23 GiB and no `llama-server` process is listed. During this plan's own research that produced a
wrong diagnosis (§E). A read-only `gpu_mem` passthrough on the debug surface is the fix, and it is
a rule-10 item: the owner cannot diagnose their box through a console that cannot see the number.

*Files:* `image_gen/render.py`, the whisper call sites in §B.1, `config.py`, `box_events.py`,
`api/debug.py`.
*Risk:* low-medium. *Test:* an image render's eviction and a whisper unload both appear at the
gate; Kokoro's footprint is measured and recorded.

### W4 — Delete the cloud providers ◻️

Outright, code included. §D.5 is the manifest, with the exact reproducible commands (C1–C6):

| bucket | files |
|---|---|
| backend source | 7 |
| tests | 61 |
| frontend | 9 |
| docs | 37 |
| deploy | 2 |

Two config fields (`anthropic_api_key:307`, `xai_api_key:308`), two env vars, one `llm_prices`
entry, one `CONTEXT_WINDOWS` block. **No `app.settings` key exists only for cloud**, so nothing
persisted needs migrating.

**Re-point the tiers first, then the tasks.** Per finding 6, `TIER_DEFAULTS` carries the dominant
route. All 23 entries are the identical string, so this is uniform.

**Carve-outs, each proven in §D.8 rather than assumed** — a predecessor plan counted several of
these as blast radius while simultaneously declaring them out of scope:
- `OpenAiCompatClient` is **shared** — `router.py:840` (xai) and `:841` (local) construct the same
  class. Only the xAI construction goes.
- The `grok` **CLI** inside the jcode sandbox stays; it is pointed at the box's own models.
- `external_llm.py` exposes the **on-box** coder over an Anthropic-shaped endpoint. It stays.
- Grokipedia is a **website scraper** — its own source says *"no xAI/Grok API key"*.

**What is lost, stated once.** `tests/eval/` is hard-gated on `xai_api_key` (`run.py:167`, `:220`)
and is the extract → integrate → arbiter harness. It goes with the key. The surviving box-capable
instrument is `backend/evals/box/` — *"the ONLY eval path that calls the box … the same scorers CI
uses"* — driven through the debug console with a minted token, so it works with no terminal.

**One thing W4 must not create.** `provider_choices()` returns `()` when `local_llm_enabled` is
false (`providers.py:61-62`), and that field is boot-time only (§D.7: one `get_settings()` at
`main.py:254`), while the PWA's own install path 409s when hosting is off
(`api/llm_settings.py:652-654`). With cloud deleted, a box in that state has no LLM and no PWA path
back — a rule-10 brick. W4 ships with either a live-settable `local_llm_enabled` or an empty-state
that names the control that fixes it.

*Files:* the manifest above. *Risk:* medium, mechanical, irreversible — so it is last.
*Test:* no cloud provider is constructible; every task and tier resolves to an installed local
model; a hosting-off box shows a way back.

## Open questions the inventory could not answer from source

**Does an explicit operator load outrank code mode's reservation? — ANSWERED 2026-08-22, by
the owner: yes, but not silently.** `free_room` does not consult `_held_names()`, where
`ensure_room` and `_restore` both do, so while code mode holds the box a chat turn is refused
but the Load button evicts the reserved coder — and `free_room` records no restore, so it did
not come back. Raised by adversarial review; pre-existing on the PWA route, and `2f9904f` made
it reachable from the debug console's prime too.

The operator keeps authority (the Load button is the box's, and refusing it would strand an
owner whose coder is holding memory they need back). What is fixed is the SILENCE: evicting a
held model now emits `residency.evicted_held_model` and a vitals reason that names it as code
mode's, so a session that loses its model is traceable to the load that took it. Whether the
non-operator intents should refuse rather than warn — `agent` and `scheduled` clearly should —
stays with W1; this settles only the two owner surfaces.

These need a live read through the debug API before W1 and W4, and are listed rather than guessed:

- the live `app.schedules` rows (the inventory reconstructed 17 from migrations `0036→0169`, but
  the deployed state may differ)
- the live `llm_task_overrides` row — highest-precedence persistent routing
- the deployed `JBRAIN_LLM_TASKS` value — highest precedence of all, and the only thing that would
  make an agent turn use its task entry rather than the tier
- Kokoro's resident footprint (W3 measures it)

## What this plan does NOT do

- It does not change `gpu_guard`'s ceiling arithmetic. The measurements that motivated a fix are
  preserved in the inventory's *facts carried forward*, including a `qwen3.5-4b` load aborted on a
  **0.9%** overshoot. Correcting it is real work and it is not one of the three things asked for
  here; it should be its own plan, written from the inventory the same way this one was.
- It does not add a park pseudo-model. The toggle plus the existing per-model unload controls cover
  the stated need; a catalog entry that serves nothing would need special-casing in
  `llama_swap_config.render`, `footprint_gb`, `_require_provisioned`, the settings model list,
  `smoketest` and `deploy/local-models-sync.sh`.
- It does not touch `ttm.pages_limit`.
- It does not claim to stop a load initiated outside our process (W0's limits table).
