# Co-residency without surprises

> **Status:** Proposed · **Last verified:** 2026-08-22 · **Waves:** W0◻️ W1◻️ W2◻️ W3◻️ W4◻️ W5◻️ W6◻️ W7◻️

> Reconciled with the root `CLAUDE.md` non-negotiables — every LLM call still goes through the
> adapter (rule 1); no storage or RLS surface changes (rules 2–3), and no wave adds a table; every
> wave names its test (rule 5); rule 10 is load-bearing throughout and W5 exists only because of
> it. **Three waves add a GUI surface and therefore each owe the `PROCESS.md` GUI gate** — see
> *Process gates* below, which revision 2 omitted entirely.

> **Standing rule for this plan, earned three times over.** Its predecessor
> (`../archive/GPU_ADMISSION_INTEGRITY_PLAN.md`) was withdrawn after four cold reviews found three
> central claims were a log label, a README summary and an env-var name read as evidence, plus a
> partial source fetch with the rest inferred. **Revision 1** of this plan was withdrawn after
> three more. **Revision 2** was withdrawn after three more. No claim here is load-bearing unless
> the primary source was opened and quoted. The *Corrections log* at the bottom is cumulative and
> deliberately unflattering: every row is a claim that survived one round of review and died in the
> next.

## Revision 3 — what changed, and why the plan is now smaller

Three cold reviews (strategic, implementability, adversarial-technical) converged on one verdict:
**most of revision 2's waves were rebuilding mechanisms that already ship, and the owner's actual
goal appeared in none of them.**

Revision 2's central W0 claim was **false**, and it failed in this plan's own signature way. It
said a routed completion loads inside llama-swap with no `LocalGatewayClient.load` call, so the
guard is bypassed. In the wired production configuration it is not: both processes pass a box lock
(`main.py:450`, `worker.py:556`), so `ensure_room` takes the locked path and **loads the model
itself** (`residency.py:479-480` → `_ensure_room_core(load_target=True)` → `:505-506`
`_guarded_load` → `:533` `gateway.load`), which is where `refuse_if_no_device_room`
(`local_gateway.py:801`) and `guarded_load` (`:803`) live. The evidence I cited was a docstring
about *page-cache dropping* which explicitly names residency as a **covered** path
(`local_gateway.py:228-229`) and attributes its unannounced sightings to cross-process
`_loaded_here` bookkeeping (`:256-261`). I read a measured comment about one subject and drew a
structural conclusion about another. That is the exact failure this plan's standing rule names.

**The real hole is different, and better.** `ensure_room`'s fast path returns early when the model
is already in `running()`:

```
residency.py:473        with contextlib.suppress(Exception):
residency.py:474            if served_model in await self._gateway.running():
residency.py:475                self._displaced.discard(served_model)
residency.py:476                return
```

But `running()` includes models llama-swap is **stopping** — verified at the pin
(`internal/router/base.go:366-375` filters only `StateStopped`/`StateShutdown`), and
`_parse_running` (`local_gateway.py:1182-1204`) discards the state field and returns bare names. So
a model mid-stop reads as resident, `ensure_room` returns without admitting, the completion reaches
llama-swap, and llama-swap **relaunches it** — a load with no admission, no
`refuse_if_no_device_room`, no `guarded_load`, and no `box_events` row. It is reachable *through*
the door, so no call-site audit and no AST test can find it. **That is W0.** Revision 2 had it as
W6, "explicitly last and explicitly conditional, correctly never built if no incident occurs."

And the plan's centre of gravity was wrong. The owner asked for co-residency of two named models
without swapping; revision 2 had no wave for it. Meanwhile a second silent evictor was already
diagnosed in-tree and unmentioned — see W1.

## What the owner asked for, and what actually delivers it

| ask | delivered by | status |
|---|---|---|
| *"completely remove the anthropic or grok cloud services"* | W6 (route nothing to cloud), then W7 | **no code needed to start** — `providers.py:86` hides a keyless provider; `api/llm_settings.py:1443-1486` re-points any task from the PWA today |
| *"a 'model' … we can load to ensure no other models can be loaded"* | W4 | ~80% ships already — see W4 |
| *"only one way to load in and out … no surprises"* | W0, W1, W2 | the routed door already holds; the gaps are three **silent** paths |
| *"oss120b + 4.8-27b co-resident without swapping"* | W0, W1, W3 | **measurably almost solved** — `local_catalog.py:213-214` measured the pair at 93.73 GiB against a ~103 GB ceiling (`residency.py:350`), and `llama_swap_config.py:428-438` puts every model in one `swap: false` group so llama-swap never evicts a member |
| *"don't dual load, don't crash the server"* | W0, W1 | the two silent evictors are the remaining risk |
| non-model memory kept growing; hourly triage reloaded through an unload | W3 | root cause fixed; W3 generalises it with the mechanism that already shipped |

## What already ships — so it is NOT a wave

Revision 2 proposed building five things this repo already has. Each verified by opening the file:

| mechanism | where | revision 2 called it |
|---|---|---|
| defer a job on refusal, **no attempt burned**, `run_after` enforced DB-side | `worker.py:209-216` → `queue.defer`; `RETRY_AFTER = 5 min` (`preconditions.py:30`) | "W3 spans residency and the worker" |
| a pre-handler gate that defers when the model isn't resident | `workflow/preconditions.py:44-71` `model_already_loaded` — resolves the **live** route, met if non-local, met if resident, else defer | "one mechanism, not six preconditions" |
| drain the whole pool, narrated | `image_gen/render.py:199-211`, six lines, `box_events.because("an image render needs the whole memory pool")` | "park needs a drain step" (as if new) |
| re-point any task to any local model from the PWA, validated server-side incl. vision capability | `api/llm_settings.py:1443-1486` `apply_overrides`, shared by the owner PUT and the debug console | "W2/W3: the substance of the cloud work" |
| hide a keyless cloud provider; render a stranded override as `(unavailable)` | `providers.py:86-105`, `id_for_spec` `:118-124`, `LLMSettingsScreen.tsx:736-738` | correctly identified in r2 |
| cancel an in-flight turn whose model is about to be unloaded | `api/jcode.py:511-518` `cancel_running(reason="cancelled: code mode activated")` | unmentioned; W4 needs it |

**The admission door on the routed path already holds.** `router.py:619, 696, 747` → `_admit_local`
(`:359-361`) → `residency.ensure_room`, and `build_router` always attaches a coordinator
(`:822-827`). There is no unadmitted *routed* local path. W0/W1/W2 are about paths that are
**silent**, not paths that are missing.

## The waves

Renumbered for the third time, because the gap set changed. Mapping from revision 2: r2-W6 →
**W0** (it was the real hole all along); r2-W0 → **W2**, shrunk to two endpoints; r2-W3 → **W3**,
shrunk to registration; r2-W1 → **W4**; r2-W2 → **W6**, demoted to an operating procedure; r2-W4 →
**W6/W7**; r2-W5 → **W7**. **W1** and **W5** are new.

### W0 — Stop a `stopping` model reading as resident ◻️  *(was r2-W6, "probably never built")*

The hole above. One state field, discarded at `local_gateway.py:1182-1204`, lets a load happen with
no admission and no guard.

Two steps, in order:

1. **Earn the measurement first, because it is one line.** Log when the already-resident
   short-circuit fires (`residency.py:473-476`) with the state llama-swap reported. Revision 2 made
   this the trigger for a wave that was itself "explicitly last", so the evidence could never be
   gathered. It ships first instead.
2. **Then split `running()` from `ready()`** and make the fast path require *ready*. `_parse_running`
   already sees the field and throws it away.

**The known hazard, from the predecessor's post-mortem:** `api/llm_settings.py:156-161`'s
`_loaded_ids` is both a call site this changes **and** the owner's load indicator, so a careless
change makes a loading model vanish from the screen. And billing a mid-stop model's remaining
footprint is what reintroduced bug #1186 — the unguarded branch is `local_gateway.py:757`, the
pre-flight `:800-801`. **[r3]** Revision 2 cited `:762` and `:801-802`; `:762` is a comment,
`_drop_weights_cache(model)` is `:764`, and `refuse_if_no_device_room` is `:801` with `:802` the
`try:`. My one correction was itself miscited.

*Files:* `local_gateway.py`, `residency.py`, `api/llm_settings.py`.
*Risk:* medium — touches the load indicator. *Test:* a model reported `stopping` does not satisfy
the fast path and is admitted; `_loaded_ids` still shows a loading model.

### W1 — Narrate the two silent evictors ◻️  *(new — the co-residency wave)*

The owner's stated goal is two named models staying co-resident. Two mechanisms evict the whole
resident set with **no `box_events` row**, so the box appears to lose models for no reason.

**a) A config regen shuts down every running server.** Already diagnosed in-tree, and the docstring
records how long it went unbelieved:

> *"DIAGNOSED on the box 2026-08-20, after the owner reported — repeatedly, and was repeatedly told
> it was a display artifact — that staging a model in the PWA unloaded gpt-oss-120b. … llama-swap
> `reload()` builds a new server and calls `old.Shutdown()` → every running llama-server process
> dies. … The unload happens inside llama-swap, so no `box_events` row is written and the vitals
> surface stays silent — the app genuinely does not know it happened."*
> — `llama_swap_config.py:462-481`

This runs inside `gateway.load`, i.e. **inside the box lock**, so one admitted load can silently
destroy the eviction plan the budget just computed. It is the single most direct threat to the
owner's co-residency goal, and revision 2 never mentioned it.

**b) A refusal is invisible.** `box_events.py:51-66` defines `model_load`, `model_unload`,
`image_render`, `prefill`, `gateway_config_stale` — **no refusal kind**, and the refusal sites emit
nothing at all (`residency.py:461-468`, `:433-436`: raise with no `log`, no `box_events.record`).
The only trace anywhere is a structured log at `worker.py:216` and `last_error = 'deferred: …'` on
the Ops run. For an owner with no terminal, *"a load was refused and why"* is unobservable — and
W3's deferral and W4's park both assume it is visible.

*Files:* `box_events.py`, `residency.py`, `llama_swap_config.py`, `api/llm_settings.py`, the vitals
surface. *Risk:* low — additive narration, no behaviour change. *Test:* a config regen that kills
the resident set emits an event naming the cause; a refused admission emits one with its reason.
**GUI gate applies** (vitals surface).

### W2 — Admit the two debug-console loads ◻️  *(was r2-W0, 18 sites → 2)*

**[r3] The count went 17 → 4 → 2, and each cut was a correction of the previous cut.** Revision 1
said thirteen callers bypass the budget. Revision 2 cut it to four. Two of those four defend
themselves:

- `api/jcode.py:168` — preceded by `:162-166`, which unloads **every** resident model first. It
  cannot over-commit.
- `smoketest.py:237` — gated at `:234` by `_room_for` (`:147-167`), its own admission check with its
  own budget (`LOAD_HEADROOM_GB`). Routing it through the door is **budget consolidation**, which
  belongs to `../plans/MEMORY_ADMISSION_PLAN.md`'s D0, not here — and doing it carelessly risks a
  spurious gateway rollback (`update-inner.sh:694-702` sets `SMOKE_FAILED=1` → rollback;
  `cli.py:203-212` records one such rollback already, on 2026-08-19, which then blocked the upstream
  fix for the thing it was failing on).

**The genuinely naked set is two, and both are the owner's debug console:**

    api/llm_settings.py:1519   via api/debug.py:1366 only (the PWA route admits at :1047)
    api/llm_settings.py:1891   via api/debug.py:1492 (gateway_prime — never admitted)

So today the owner can over-commit their own box from the one surface they reach when the box is
already in trouble. That is the shape of the seven-hour freeze. A ~10-line change with two tests.

**[r3] Two things revision 2 got wrong here.** Its "three passthrough doors, all verified"
paragraph listed sites that are all *already* admitted (`jcode_llm.py:176-177` calls `ensure_room`
and re-raises `ResidencyError` before the forward at `:182`; `external_llm.py:211-222` refuses
outright), which contradicts the paragraph below it — a builder could not tell what was in scope.
And its self-deadlock analysis described a hazard of a design not yet chosen: there is no
`residency.release`; the only unload path, `free_room` (`:568-590`), takes **no** box lock, so
routing an abort through it would not deadlock. The `_box_locked()` re-entrancy **rule** is still
worth stating — no `lock_timeout` is set anywhere, so a second acquisition on the same key would
block forever — but both examples chosen to motivate it (`cli.py:73`, `local_gateway.py:808`) are
**unloads**, which this plan's own taxonomy puts outside admission. Neither gates this wave.

One thing to carry: `gateway.load` runs `regen_gateway_config` inside the box lock, including a
deliberate `asyncio.sleep(_GATEWAY_RELOAD_SETTLE_S)` (`api/llm_settings.py:718-739`). The
cross-process lock is held across a sleep. Related to W1(a); worth a note, not a fix here.

Also: `api/llm_settings.py:833` is inside `_unload_if_loaded` (`:824`), which has **four** call
sites (`:1095, :1139, :1176, :1827`) — "one test per caller" must know that.

*Files:* `api/debug.py`, `api/llm_settings.py`. *Risk:* low. *Test:* a debug-console load and a
debug-console prime each evict to budget, and each refuses rather than over-committing.

### W3 — Register the owner's intent on the sweeps ◻️  *(was r2-W3, wave → registration)*

The mechanism ships (`workflow/preconditions.py:44-71`); `precondition=` has exactly **one**
registered use (`gmail/triage.py:120`, registered `worker.py:786`). The work is registering it on
the sweeps that lack it — plus two fixes review found:

**[r3] The swallow set is not one site.** Revision 2 named only `api/jcode_llm.py:174-180`.
`ResidencyError` is a bare `Exception` subclass (`residency.py:109`), and `wiki/lint.py:747` catches
bare `Exception` (*"a verifier failure must not abort the whole sweep"*), so the sweep advances,
calls `complete` again, is refused again — silently, per item — and `worker.py:209` never sees it.
By contrast `analysis/pipeline.py:1204` catches `(LlmError, LlmBadResponseError)` only, so it does
propagate. **W3 must enumerate the swallow set, not assume one.**

**[r3] Two of the five named "ungated per-item loops" are not that.**
`agent/daily_briefing.py:501` makes exactly one router call, not a loop. `jpet/brain.py:473, 492`
are **interactive** paths (a child talking to the wall), where "defer" is the wrong remedy — they
need to fail with a message. Three of the five are sweeps.

**[r3] And the gate must run *before* the handler.** `worker.py:209`'s catch wraps `_invoke` at
`:201`, so a refusal lands mid-handler: the sweep has already done partial, non-idempotent,
token-spending work, and will redo it every five minutes for as long as the hold is held. The
pre-handler seam is `worker.py:192-193` → `_deferred_on_precondition` (`:240-269`) — which is the
precondition mechanism, i.e. the thing revision 2 dismissed as *"six preconditions"*. Revision 2
also cited `:240-270` as the place that *catches* the refusal; it catches nothing.

Do **not** gate owner-initiated ingest — `model_already_loaded` on `ingest_note` would stall note
integration indefinitely on a parked box. Sweeps only.

*Files:* `wiki/lint.py`, `analysis/pipeline.py`, `ingest/ocr.py`, `jpet/brain.py`, the action specs,
`api/jcode_llm.py`. *Risk:* low-medium — the failure mode is a stalled sweep, visible after W1.
*Test:* an owner unload followed by each registered sweep defers before the handler runs, burning no
attempt and doing no partial work; an interactive path fails with a message instead.

### W4 — Park ◻️  *(was r2-W1, "build a mechanism" → "reuse one")*

*"A 'model' that is a dead endpoint we can load when we want to ensure no other models can be
loaded."*

**[r3] Revision 2's design defeats itself in five seconds, and its own test would not catch it.**
It specified a **separate** settings key so boot-clear and jcode could not touch park. But both
auto-load paths read the **code-mode** key specifically — `main.py:444` (residency's restore guard)
and `main.py:1169` (the warm keeper) — and the keeper's `interval_wait` is `5.0`
(`warm_keeper.py:64`). Sequence on the live box: park set → drain frees ~68 GB → five seconds later
the keeper reads an empty set for *its* key and loads the primary model straight back, while the PWA
still shows "parked". Revision 2's test (*"the drain leaves the pool free"*) passes with no keeper
attached and fails on the box.

**A non-empty sentinel name in the existing key works with every consumer unchanged**, because
emptiness is the only value that means *unheld*:

    residency.py:460-461   if held and served_model not in held:   → refuses every real model ✓
    residency.py:609       if await self._held_names(): return     → restore skipped ✓
    worker.py:385-393      bool(...) → pauses the scheduler tick   ✓
    warm_keeper.py:127     if held and served not in held:         → keeper stands down ✓

**[r3]** So revision 2's *"needs a distinct sentinel, a changed return type, and four consumer
changes"* was half right and half overcorrection: the sentinel is needed, the type change and the
consumer rewrites are not. Note also that `worker.py:388` reads `settings.code_mode_hold_names`
**directly**, not `_held_names()`, so changing that method's return type would never have reached
it — revision 2's "four consumers of `_held_names()`" grouping was wrong.

Residual work, all small and all real:
- **Tag the hold** so `main.py:390-391`'s boot-clear skips a park-tagged one, and `api/jcode.py:502`
  **merges** rather than overwrites (`:523` likewise).
- **Stop hardcoding the reason** — `residency.py:465-467` raises *"Code mode is holding the box…"*,
  which would misattribute a park refusal.
- **Reuse the drain** from `image_gen/render.py:199-211`.
- **Cancel the in-flight turn** using the precedent at `api/jcode.py:511-518`; without it,
  `gateway.unload` kills the llama-server mid-stream and the turn dies with an opaque transport
  error.
- **A mandatory TTL**, not an option. `main.py:382-389` documents that a stranded hold wedges the
  box, which is why boot clears it; a park that survives restart deliberately reintroduces that
  failure mode. `worker.py:385-393` shows the hold pauses the scheduler tick, so a forgotten park
  silently stops all background work — on a box with no terminal, that is a rule-10 **regression**,
  so the TTL and the banner ship in the same PR as park itself.

**[r3] Two hazards revision 2 did not ask about.** The hold is checked *before* the box lock, on
purpose (`residency.py:458-459`: *"so a refused load never contends for it"*), so a load already
past `:461` completes regardless of park — park is not a barrier against in-flight loads. And the
drain's evictions run outside that lock (`free_room` takes none), so the drain can race a
cross-process load it cannot see: park half-applies, hold set, pool not free. **Revision 2's drain
test is inherently flaky, not deterministic.** W4 must state the ordering it guarantees. Eviction
itself is safe — nothing on the unload path consults `_held_names()`, so the drain can always evict.

*Files:* `residency.py`, `settings_store.py`, `main.py`, `api/jcode.py`, `warm_keeper.py`,
`api/llm_settings.py`, the settings screen. **Not** `local_catalog.py` — a fake catalog entry would
need special-casing in `llama_swap_config.render`, `footprint_gb`, `_require_provisioned`, the
settings model list, `smoketest` and `deploy/local-models-sync.sh`.
*Risk:* medium — it can wedge the box remotely if the TTL or banner slip. *Test:* park refuses every
real model with a reason naming **park**; the drain leaves the pool free **with the keeper running**;
a restart preserves park; toggling code mode leaves park intact; the TTL auto-releases and says so.
**GUI gate applies** (park control + standing banner).

### W5 — Make local hosting recoverable from the PWA ◻️  *(new — extracted from r2-W4)*

**[r3] Revision 2 buried this as a precondition line inside a wave it called "zero code". It is
neither.** `local_llm_enabled` is a boot-time `Settings` field (`config.py:314`) read at 25+ sites,
and it gates the PWA's own install path: `api/llm_settings.py:652-654` — *"409 when hosting is off
(the gateway/GPU env is a one-time host setup **the PWA can't bootstrap**)"*. If it is ever false,
`provider_choices()` returns `()` and every recovery control 409s: **there is no PWA path back.**

Worse, on a hosting-off box a stored `local:` override is **silently discarded** and falls back to
the resolved default — `router.py:481-482` `log.warning("llm.local_override_ignored", …)`. So an
owner who had set local routes sees them silently revert. Revision 2's proposed remedy ("an
actionable message pointing at the control that fixes it") pointed at a control that does not
exist; an actionable message with no action behind it is a rule-10 brick with better copy.

**[r3]** Revision 2 also cited `config.py:372` `local_models: list[str] = []` as a second cause of
the empty tuple. It is not: an empty `local_models` with hosting **on** returns one choice
(`providers.py:64-69`). Only `local_llm_enabled == False` yields `()`. (`local_models` itself *is*
PWA-operable — `deploy/local-models-sync.sh:241-245` rewrites `LOCAL_MODELS=` and restarts, driven
from Ops → Update.)

*Files:* `config.py`, `providers.py`, `api/llm_settings.py`, the settings screen, `deploy/`.
*Risk:* medium — a boot-time field becoming live-settable raises a process-restart question.
*Test:* with hosting off, the PWA offers a working control to turn it on; a stored `local:` override
survives rather than silently reverting. **GUI gate applies.**

### W6 — Route nothing to cloud ◻️  *(was r2-W2/W4, wave → operating procedure)*

**[r3] This is not a code wave.** `apply_overrides` (`api/llm_settings.py:1443-1486`) already
re-points any task to any local provider from the PWA or the debug console, validated server-side
including the vision check (`:1473-1477`), and `_resolve_live` (`router.py:428-470`) makes a stored
spec the highest-precedence persistent selector. So the re-routing is **19 PWA toggles and an eval
habit**: one task at a time, reversible per task, measurable between each, fully rule-10 compliant.

**19, not 20 — and not 23. [r3]** `TASK_DEFAULTS` holds 20 entries (`router.py:50-124`); the other
3 of revision 1's "23" are `TIER_DEFAULTS` (`:176-178`), a different kind of thing. And one of the
20 cannot be set from the control surface at all: `research.title` is in `_HIDDEN_TASKS`
(`api/llm_settings.py:96`), excluded from the snapshot (`:479`) and rejected on PUT (`:1459-1461`)
because it follows `agent.turn` at the router (`:462-470`). So revision 2's *"a table of all 20
built from live settings"* is unbuildable as stated: 19 are live-settable, the 20th is derived.
**[r3]** `TASK_DEFAULTS` also contains **no coding task** — code generation runs through jcode's own
model (`settings.jcode_model`), which W7 keeps. The real span is text reasoning, extraction and
three vision tasks.

**Then, and only at the end, edit the `TASK_DEFAULTS` constant.** Until that happens a cleared
override, a fresh box, or a rejected PUT falls back to a cloud default. **[r3] Which matters
because unsetting the keys makes cloud unselectable but NOT unroutable:** `build_router` constructs
both cloud clients unconditionally (`router.py:839-840`) and spec resolution validates only the
provider *name* (`:223-224`), so all 20 defaults still resolve to `xai:grok-4.3` after the keys go —
failing fast and cleanly (`retry.py:95-96` raises `LlmAuthError` on 401/403), but failing. Revision
2's W4 test (*"no task resolves to a cloud spec"*) could not have passed.

**Keep the keys until the local roster is proven.** Removing them is reversible only in the sense
that a key can be re-added; what cannot be recovered is the comparison. The keys buy: a known-good
baseline during the exact period 19 routes are changing, and a cheap answer to *"is this the model
or the box?"* Billing stops either way once nothing routes there. **[r3] Revision 2 named the wrong
eval instrument for the second time in a row.** Revision 1 said `tests/eval/`, which is hard-gated
on the key being removed (`run.py:167`, `:220`). Revision 2 said `backend/evals/run.py`, which
builds `build_router(Settings())` with **no overrides loader** (`:41`) and so routes off
`TASK_DEFAULTS` — the very table this plan says is not live. The right instrument is
`backend/evals/box/`: *"the ONLY eval path that calls the box … the same scorers CI uses"*
(`evals/box/README.md:3-8`), three layers (`extract | integrate | disambiguate`), driven through the
debug console with a minted token — the one eval surface that works with no terminal. Plus
`scripts/wiki-lint-eval.sh`. Coverage is ~4-5 of 20, not 2.

*Files:* none to start; `router.py`'s `TASK_DEFAULTS` at the end. *Risk:* the risk is quality, and
it is paced by the owner one task at a time. *Test:* `evals/box` per layer between changes.

### W7 — Retire the keys, then (optionally) the code ◻️  *(was r2-W4/W5)*

Once nothing routes to cloud and the roster is proven: unset `JBRAIN_XAI_API_KEY` and
`JBRAIN_ANTHROPIC_API_KEY`. Blocked on **W5** (bootstrap) and on W6 having edited `TASK_DEFAULTS`.

Deleting the provider code is **optional housekeeping and nothing depends on it.** It is last
because it is the only irreversible step and it destroys the baseline in W6.

**[r3] Revision 2's blast-radius table reproduces exactly** (all 20 cells, with the command below),
and its critique of revision 1's 86-file figure stands. Two fixes: its body says **127** and its own
corrections log says 128 — 15 + 64 + 40 + 5 + 3 = **127**; and the count is only meaningful with the
carve-outs applied, since the `grok`-derived frontend 11 includes `JcodeScreen.tsx`,
`ExternalSessionScreen.tsx` and `jcode/types.ts` — the grok-CLI surface that **stays** — and the
backend sweep catches `web/grokipedia.py` and `agent/tools/grokipedia.tool`, a website scraper.

| pattern | src | tests | docs | frontend | deploy |
|---|---|---|---|---|---|
| `anthropic` | 8 | 12 | 23 | 3 | 2 |
| `xai` | 13 | 63 | 28 | 3 | 3 |
| `anthropic\|xai` | 15 | 64 | 40 | 5 | 3 |
| `grok` | 22 | 72 | 40 | 11 | 2 |

    for d in backend/src backend/tests docs frontend/src deploy; do
      grep -rliE "$pat" "$d" | grep -v __pycache__ | grep -v node_modules | wc -l
    done

`openai_compat.py` is **shared** (`router.py:840` xai / `:841-847` local) — only the xAI
construction goes. The jcode sandbox's `grok` CLI stays (`api/jcode_llm.py:1`).

*Risk:* the key removal is low and reversible; the deletion is mechanical but irreversible.
*Test:* no cloud provider is constructible; the suite passes with fixtures re-pointed.

## Prerequisites this plan does not own

**`../plans/MEMORY_ADMISSION_PLAN.md`'s guard fix.** That plan records, measured, that
`qwen3.5-4b` **cannot load today** — *"aborted on a 0.9% overshoot by a guard whose own docstring
says it exists to catch the ORDER-OF-MAGNITUDE balloon … not ordinary overshoot"* (`:219`). Every
model sits in one `swap: false` group (`llama_swap_config.py:428-438`), so any cross-tier task
forces an evict+load through that guard.

**[r3] Revision 2 named this a prerequisite of its biggest wave and left it in a `Draft` document
with nothing built** (`MEMORY_ADMISSION_PLAN.md:3`: `W0◻️ W1◻️ W2◻️ W3◻️ W4❌ W5◻️`), without
identifying *which* wave is "the fix". That left this plan's back half with no start date and its
dependency graph open at the root. Two honest options, and the roadmap must pick one: schedule that
plan's W0 explicitly ahead of W6 here, or accept that W6 proceeds one task at a time on tiers that
do not force a swap. **This plan does not proceed past W4 until the roadmap says which.**

Reviewers also noted the prerequisite may be too *weak*: even with the guard fixed, a cross-tier
switch costs a full evict+load of a 59–78 GB model, and no wave here prices that latency.

## Process gates

**[r3] Revision 2 omitted both.**

- **The GUI gate** (`../reference/PROCESS.md:60-66`): any wave adding or changing a GUI surface
  needs **three interactive mock HTML artifacts presented to the owner to choose from before
  implementation begins** — *"a critical-decision interruption by design"* — with the chosen mock
  landing in `docs/mocks/`. **W1, W4, W5** each add a surface, and none exists today (no hold or
  park banner appears in `LLMSettingsScreen.tsx`, `OpsScreen.tsx` or `ControlScreen.tsx`). So three
  waves cannot be planned as "one PR, start to finish", and each owes the owner a mock round.
- **Doc lifecycle** (`../DOC_LIFECYCLE.md:115-116`, R5 at `:180`): *"Ideate → write a Proposed doc
  … **No code.**"* then *"Commit to the roadmap → flip to `Scheduled`, `git mv` from `proposed/`"*.
  **W0 cannot ship while this file sits in `docs/proposed/`.** Shipping code against a Proposed doc
  is an R5 violation the `docs` gate is meant to catch.

## What this plan does NOT do

- It does not touch `gpu_guard`'s ceiling arithmetic or `refuse_if_no_device_room` — see
  *Prerequisites*, which is now a scheduling question rather than a disclaimer.
- It does not touch `ttm.pages_limit`. Same owner, and the predecessor got its direction backwards
  twice.
- It does not remove the `grok` CLI from the jcode sandbox (W7).
- It does not add a catalog entry for park (W4's *Files:*).
- It does not consolidate `smoketest._room_for`'s budget with residency's — that is
  `MEMORY_ADMISSION_PLAN`'s D0 (W2).

## Corrections log

Cumulative across three revisions. Every row survived one round of cold review and died in the next;
that is the point of keeping it.

### Revision 2 → 3

| revision 2 said | primary source says |
|---|---|
| the dominant load path never calls `load()`, so the guard is bypassed | with a box lock wired (`main.py:450`, `worker.py:556`) `ensure_room` loads under the lock — `residency.py:479-480` → `:505-506` → `:533` → the guard at `local_gateway.py:801,803`. The cited docstring is about page-cache dropping and names residency as **covered** (`:228-229`) |
| W6 (running/ready) is conditional and probably never built | it is the real hole: `running()` includes `stopping` (`base.go:366-375` at the pin; `_parse_running` discards state), so `ensure_room`'s fast path (`residency.py:473-476`) returns and llama-swap relaunches unadmitted |
| four genuinely unadmitted loads | two — `jcode.py:168` unloads everything first (`:162-166`); `smoketest.py:237` is gated by `_room_for` (`:234`, `:147-167`) |
| "three passthrough doors, all verified" | all three are already admitted (`jcode_llm.py:176-177`, `external_llm.py:211-222`, `router.py:619/696/747`) — the paragraph contradicts the one below it |
| routing the abort through residency is a self-deadlock **today** | there is no `residency.release`; `free_room` (`:568-590`) takes no box lock. The rule is sound, the hazard is of a design not yet chosen, and both motivating examples are unloads |
| park needs its own settings key | that defeats the drain in 5 s — the keeper and the restore guard read the **code-mode** key (`main.py:1169`, `:444`; `warm_keeper.py:64` `interval_wait=5.0`) |
| park needs a changed return type and four consumer rewrites | a non-empty sentinel in the existing key works with all four unchanged; and `worker.py:388` reads `code_mode_hold_names` directly, so the type change never reached it |
| deferral lives at `worker.py:240-270` and must be built | `:240-269` is `_deferred_on_precondition` and catches nothing; the refusal defer already ships at `:209-216` with no attempt burned |
| one swallow site (`jcode_llm.py:174-180`) | `wiki/lint.py:747` catches bare `Exception` and `ResidencyError` is one (`residency.py:109`) — refused silently, per item, forever |
| five ungated per-item loops | three — `daily_briefing.py:501` is a single call; `jpet/brain.py:473,492` are interactive, so "defer" is the wrong remedy |
| a table of all 20 tasks from live settings | 19 — `research.title` is in `_HIDDEN_TASKS` (`api/llm_settings.py:96`, `:479`, `:1459-1461`) |
| unsetting the keys means no task resolves to a cloud spec | unselectable ≠ unroutable: `router.py:839-840` builds both clients unconditionally and `:223-224` validates only the name; all 20 still resolve to `xai:grok-4.3` and fail at call time |
| `backend/evals/run.py` is the surviving instrument | it has no overrides loader (`:41`), so it routes off `TASK_DEFAULTS`. The box-capable one is `backend/evals/box/` (`README.md:3-8`), via the debug console |
| `local_models: list[str] = []` also yields empty choices | only `local_llm_enabled == False` does; empty `local_models` with hosting on returns one choice (`providers.py:64-69`) |
| the bootstrap gap is a precondition line on a zero-code wave | 25+ read sites, gates the PWA's own install path (`api/llm_settings.py:652-654`), and a stored local override silently reverts (`router.py:481-482`). Own wave (W5) |
| the `MEMORY_ADMISSION` guard fix is a named prerequisite | named, but left in a `Draft` doc with nothing built and no wave identified (`:3`) — the dependency graph was open at the root |
| corrections log: `anthropic\|xai` totals 128 | 127, contradicting revision 2's own body |
| `local_gateway.py:762` → `:757` / `:801-802` | `:762` is a comment, `_drop_weights_cache` is `:764`, the pre-flight is `:800-801`. The correction was itself miscited |
| `providers.py:82` states the posture | `:82` is the `def`; the quoted sentence is `:86-87` |
| `warm_keeper.py:181` is the admission | `:180` is; `:181` is the `running()` check |
| `residency.py:463-466` raises the code-mode message | the `raise` is `:465`, message `:466-467` |
| `test_llm_load_guard_chokepoint.py:60` is the whisper exemption | `:68` |
| — (unmentioned) | a config regen calls `Shutdown()` on **every** running server, invisibly, inside the box lock (`llama_swap_config.py:462-481`) — the most direct threat to the co-residency goal |
| — (unmentioned) | there is no `box_events` refusal kind and the refusal sites emit nothing (`box_events.py:51-66`, `residency.py:461-468`) — W3 and W4 both assume the owner can see it |
| — (unmentioned) | the `PROCESS.md:60-66` GUI gate applies to three waves; `DOC_LIFECYCLE.md:115-116` blocks shipping code from `docs/proposed/` |

### Revision 1 → 2

| revision 1 said | primary source says |
|---|---|
| "every one of the 23 task defaults" | 20 tasks + 3 tiers (`router.py:50-124`, `:176-178`); and `_resolve_live` folds in DB overrides, so `TASK_DEFAULTS` is not the live table |
| park = `_held_names()` with an empty set | empty means **not held** (`residency.py:309-320`) |
| a hold reserves the box | a hold blocks the next load and frees nothing (`residency.py:462-464`) |
| 17 load/unload sites, 13 bypassing the budget | 18 sites (`api/llm_settings.py:1891` missed); nine are unloads |
| only `cli.py` has the no-DB problem | `smoketest.py:237` too (`update-inner.sh:695`, `:240`, `:498`, all `--no-deps`) |
| remove cloud before honouring owner intent | inverted — it manufactured the condition the next wave fixed |
| `tests/eval/` measures the re-routing | gated on `xai_api_key` (`run.py:167`, `:220`) |
| 86 files of blast radius | not reproducible from any single pattern |
| removing cloud requires code | `providers.py:86` already hides a keyless provider |
