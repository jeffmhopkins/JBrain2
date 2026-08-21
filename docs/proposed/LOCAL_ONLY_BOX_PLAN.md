# A local-only box with one door

> **Status:** Proposed · **Last verified:** 2026-08-21 · **Waves:** W0◻️ W1◻️ W2◻️ W3◻️ W4◻️ W5◻️ W6◻️

> Reconciled with the root `CLAUDE.md` non-negotiables — every LLM call still goes through the
> adapter (rule 1); no storage or RLS surface changes (rules 2–3), and no wave adds a table;
> every wave names its test (rule 5); W1 adds a settings control and W4 removes two env keys,
> and rule 10 is where this revision changed most — see W4's bootstrap paragraph.

> **Standing rule for this plan, earned the hard way.** Its predecessor
> (`../archive/GPU_ADMISSION_INTEGRITY_PLAN.md`) was withdrawn after four cold adversarial
> reviews. Three of its central claims were a log label, a README summary and an env-var name
> read as evidence; a fourth was a partial source fetch with the remainder inferred. **No claim
> in this plan is load-bearing unless the primary source was opened and quoted.** Where a fact
> could not be checked, it says so instead of rounding up.

> **Revision 2, after three cold reviews (strategic, implementability, adversarial-technical).**
> They agreed on two structural faults, and both are fixed here rather than annotated:
> **(a)** the cloud-removal wave was ordered *before* the wave that makes the box safe to route
> onto, manufacturing the exact window it was meant to close — it is now split and last;
> **(b)** the park wave was built on `_held_names()`, whose empty set means *not held*, so the
> mechanism it inherited was inverted, and a hold blocks admission without freeing a byte.
> Waves are renumbered by safety order; the old numbering is noted at each heading.
> Corrections to specific counts and citations are marked **[r2]** inline.

## Why now

The owner is removing the Anthropic and xAI cloud providers. That is not a subtraction:

    $ grep -cE '": "xai:|": "anthropic:' backend/src/jbrain/llm/router.py   →  23
    $ grep -cE '": "local:'              backend/src/jbrain/llm/router.py   →   0

**[r2]** Those 23 hits are **20 `TASK_DEFAULTS` entries plus 3 `TIER_DEFAULTS`** (`high`, `low`,
`vision` — `router.py:176-178`). A tier is not a task: it is what a prompt's `strength:` resolves
to (`router.py:379-390`). The earlier headline "every one of the 23 task defaults" was false as
phrased. The underlying fact is unchanged and verified: **nothing routes local today** — `": "local:`
returns 0.

**[r2]** And `TASK_DEFAULTS` is not the live routing table. `Router._resolve_live` folds in
per-task DB overrides, which are highest-precedence and PWA-settable. The greps above describe a
*fresh* box, not necessarily this one. W3's table must be built from live settings, not the
constant.

So removing cloud re-routes the entire system onto a box that holds about two models at once,
with no fallback anywhere. Contention stops being a background concern and becomes the main one.

That inverts the predecessor's risk calculus. Its reviews were right that a 10-file semantic
refactor was disproportionate *to a box with a cloud escape hatch*. Once the hatch is gone, the
parts of it that survived scrutiny are no longer optional — but they are also no longer the whole
story, because the interesting question changes from "did this load skip the guard?" to "who gets
the box next, and can the owner take it back?"

### The thing the owner can have today, for free

**[r2]** The owner's ask — *"I no longer want the anthropic or grok cloud services"* — needs no
code. `providers.py:82` already states the posture:

> *"A keyless cloud provider is hidden — offering it would only let a task be pinned to a model
> that fails at call time."*

Unset `JBRAIN_XAI_API_KEY` and `JBRAIN_ANTHROPIC_API_KEY` and the cloud providers vanish from the
PWA and cannot be selected. **That is the switch, and it is W4.** What it does *not* do is make
the box work afterwards, because the 20 tasks still point at specs that now fail at call time.
The work in this plan is the re-routing that has to land *before* the switch is flipped — not the
deletion, which is cosmetic by comparison and is deliberately last (W5).

## What is already true

Kept short and sourced, because the predecessor's evidence section drifted from the code twice.

| | |
|---|---|
| loads serialise **per process** | `local_gateway.py` `self._global_load_lock = asyncio.Lock()`. The commit that added it said "box-wide"; it is not, and the file says so: *"Per PROCESS, not per box."* Cross-process serialisation is residency's `pg_advisory_xact_lock`, on the routed path only. |
| the device guard is real, at one chokepoint | `refuse_if_no_device_room` + `guarded_load`, both inside `LocalGatewayClient.load`. Anything that reaches llama-swap another way skips both. |
| **most loads never call `load()` at all** **[r2]** | `local_gateway.py:224-241`, measured: *"llama-swap loads on REQUEST, so most loads never touch this client. … During one sweep the app's own turns swapped gpt-oss-120b in repeatedly and `Cached` climbed 2.36 → 47.83 GiB … and not one `weights_cache_*` line was logged, because no load we knew about had happened."* This is the single most important correction in r2 and it reshapes W0. |
| `unload` no longer races its own success | was a 3 s client timeout against llama-swap's 10 s graceful stop; widened, shipped. |
| `unannounced_load` no longer reports our own loads | shipped on branch; carries `client` and `first_poll` so a cross-process sighting is legible. |
| `/running` carries three states | `starting`, `ready`, `stopping`. `stopped` and `shutdown` are filtered by `internal/router/base.go:366-375`, confirmed at the pin `60226b63` (`deploy/Dockerfile.local-llm:58`). `_parse_running` discards the field. |
| an admission hold frees **no memory** **[r2]** | `residency.py:462-464` short-circuits a held load when the model is already resident (*"already resident — serving it needs no load"*), and nothing on the hold path evicts. A hold prevents the *next* load; it does not reclaim the ~68 GB already in the pool. W1 was written as if it did. |

## The waves

Ordered so the box is *safer* after each one, and so the riskiest change lands last with the most
scaffolding under it. **[r2]** Revision 1 violated its own ordering rule by putting cloud removal
third of five; it is now split across W4/W5 and both come after the safety waves.

### W0 — One *admission* door ◻️  *(was W0, goal restated)*

The enabling wave, and the one the owner asked for: *"make sure there's only one way to load in
and out, and that everything on the box uses these."*

**[r2] The goal was stated wrongly and the wave would have shipped a false guarantee.** Revision 1
proposed an AST test that fails CI if anything outside `residency` calls `gateway.load`. That test
is structurally blind to the dominant load path on this box: a routed completion to a non-resident
model loads it *inside llama-swap*, with no `LocalGatewayClient.load` call anywhere, hence no
`refuse_if_no_device_room` and no `guarded_load`. Three such passthrough doors, all verified:

    router.py:841-847        the `local` client is a plain OpenAiCompatClient(settings.local_llm_url)
    api/jcode_llm.py:182     client.stream("POST", "/chat/completions", …) — its own comment says
                             "BEFORE the gateway loads it on the forwarded request"
    api/external_llm.py:211  handled correctly today: "The coder must be resident — never trigger
                             an on-demand load for a remote caller"

`external_llm.py:211` is the **precedent this wave should generalise**, not the AST test. So the
door is **admission (`residency.ensure_room`), not `load()`** — every path that can *cause* a
load calls it, whether or not it calls the gateway. The routed path already does (`router.py:619,
696, 747` all call `_admit_local`, verified), which is why W2's gate works at all. The AST test
still earns its place as a *secondary* guard on direct calls; it is no longer the wave's claim.

**The direct-call set, enumerated rather than estimated** — revision 1 got this wrong in both
directions, and r2 found a further miss:

    residency.py:502,533,589,645     the owner of the budget
    api/llm_settings.py:833,908,1286,1519,1533
    api/llm_settings.py:1891         [r2] MISSED — the debug console's prime (api/debug.py:1492),
                                     on the LLM gateway, with nothing calling residency before it
    api/jcode.py:164,168,474         code mode taking and releasing the box
    warm_keeper.py:182
    smoketest.py:237                 during a deploy
    cli.py:73                        pre-update, no app process
    image_gen/render.py:206          frees the pool for ComfyUI
    local_gateway.py:808             the watchdog's own abort (see below)

**Eighteen** sites, not seventeen. **[r2] And "thirteen of them bypass the budget" was arithmetic,
not analysis — the real number is four.** Of the fourteen non-residency sites: **nine are unloads**
(`llm_settings 833, 908, 1286, 1533`; `jcode 164, 474`; `cli 73`; `render 206`; `local_gateway 808`)
and freeing memory cannot bypass an admission budget; `warm_keeper.py:182` is admitted one line
earlier at `:181` (`await self._router.admit_local_load(served)`); `llm_settings.py:1519` is
admitted on one of its two routes (`llm_settings.py:1047` calls `free_room` first; `debug.py:1366`
does not).

**The genuinely unadmitted loads are four:**

    api/jcode.py:168
    smoketest.py:237
    api/llm_settings.py:1519   via debug.py:1366 only
    api/llm_settings.py:1891   via debug.py:1492

Two of the four are the owner's debug console — the rule-10 surface. That is a much smaller, much
better-targeted wave than "move thirteen callers", and it also shows why an AST test on call sites
cannot carry the argument: it has no way to express *"1519 is safe from one caller and not the
other."*

**Explicitly NOT in scope, and named so nobody "fixes" them:** `agent/transcribetools.py:146`,
`ingest/video.py:441` and `ingest/transcribe_job.py:206` call `unload` on a **different gateway** —
built on `settings.whisper_url` (`config.py:401`, separate from `config.py:333` `local_llm_url`) at
`main.py:799` and `main.py:839/861`, plus `worker.py:627,646,664`. **[r2]** Revision 1 cited
`main.py:789-798`; the constructor is at 799 and the worker's three were omitted. The exclusion
itself is right, and matches the existing whisper exemption at
`test_llm_load_guard_chokepoint.py:60`.

**Two callers need care rather than mechanical replacement, and the wave is not done until both
are resolved:**

- `local_gateway.py:808`'s `abort=lambda: self.unload(...)` — the watchdog's own abort, *inside*
  the gateway. **[r2] The hazard is worse than "a cycle".** `pg_box_lock` (`residency.py:73-86`)
  opens a **new session per acquisition**, and `ensure_room` holds that lock across
  `_ensure_room_core → _guarded_load → gateway.load` (`residency.py:478-480, 508-533`). Routing the
  abort through `residency.release` would open a second session on the same advisory key and block
  forever — a cross-session **self-deadlock**, turning "abort the runaway load" into "hang the
  process holding the box-wide lock". So the rule W0 needs is broader than one exemption:
  **nothing reachable from inside `_box_locked()` may go through the door**, and W0 must check the
  other movers against that rule, not just this one.
- **[r2] `cli.py:73` *and* `smoketest.py:237` — revision 1 flagged only the first.** Both run from
  the CLI with no DB. The smoketest's own docstring (`cli.py:186-187`): *"Reads the installed set +
  gateway URL from settings (env-wired in the api container); **no DB needed**, so it runs under
  `docker compose run --rm --no-deps -T api`."* `deploy/update-inner.sh:695` runs exactly that, and
  `:240` / `:498` run `local-llm-unload` the same way — `--no-deps` in all three, with `:498` firing
  after the stack is quiesced. `_box_locked()` degrades gracefully rather than hanging
  (`residency.py:548-560` logs `box_lock_unavailable` and proceeds unlocked), so the failure mode is
  not a freeze — it is that **"one door" becomes a door that silently opens itself during an
  update**, which is the moment the guard matters most. W0 answers for both or ships neither.

*Files:* `residency.py`, `local_gateway.py`, `api/llm_settings.py`, `api/jcode.py`, `api/debug.py`,
`warm_keeper.py`, `smoketest.py`, `cli.py`, `image_gen/render.py`, plus the AST test.
*Risk:* medium — narrower than revision 1 believed (four real gaps, not thirteen), but the
box-lock reentrancy rule is subtle and must be tested, not just documented.
*Test:* the AST guard as a secondary check; one behavioural test per unadmitted caller proving it
now evicts-to-budget; and one test that a load initiated from inside `_box_locked()` does not
re-enter the door.

### W1 — The park model ◻️  *(was W1, rewritten)*

The owner's idea, and it is a better operator control than anything in the predecessor plan:
**a way to occupy the box so nothing else can be admitted.** *"A 'model' that is a dead endpoint we
can load when we want to ensure no other models can be loaded."* For a render, for an update, for
debugging, or simply to stop background work touching the GPU.

**[r2] Revision 1 said park "is not a new mechanism" and inherited `_held_names()`. That was
wrong, and it was wrong in the most dangerous direction — the mechanism is inverted.**
`residency.py:309-320`:

```python
async def _held_names(self) -> frozenset[str]:
    """... or an empty set (not held)..."""
    if self._hold_loader is None:
        return frozenset()
    with contextlib.suppress(Exception):
        names = await self._hold_loader()
        if names:
            return frozenset(names)
    return frozenset()
```

`if names:` collapses an empty result into the same `frozenset()` as "no loader configured" and
"the read failed". Every consumer then reads emptiness as **unheld**:

    residency.py:460-461   held = await self._held_names(); if held and served_model not in held:
                           → empty ⇒ no refusal, everything admitted
    residency.py:609       if await self._held_names(): return   → empty ⇒ restore proceeds
    worker.py:388          held = bool(await settings.code_mode_hold_names(...))
                           → empty ⇒ background loop keeps running
    warm_keeper.py:127     if held and served not in held:       → same

"Park is that hold with an empty name set — nothing may be admitted" is **exactly backwards**: an
empty set is the one value that means *admit everything*.

**Three further breakages, all primary-sourced, and each fatal on its own:**

1. **Park cannot survive a restart while sharing the key.** `main.py:390-391` clears the hold on
   every boot (`await settings_store.set_code_mode_hold_names(SYSTEM_CTX, [])`), with a documented
   rationale: a stranded hold wedges the box. W1 requires a park the owner set to survive a
   restart. Irreconcilable on one key.
2. **Code mode would clobber park.** `api/jcode.py:502-504` overwrites the hold with
   `[executor, planner]` on power-on; `:523` clears it to `[]` on power-off. A code session would
   silently release the owner's park — and W1's own release path would silently un-reserve code
   mode.
3. **The refusal message is hardcoded to blame code mode.** `residency.py:463-466` raises
   *"Code mode is holding the box for {sorted(held)}. Turn code mode off…"*. With an empty set that
   prints `[]` and misattributes the refusal.

**So W1 builds a mechanism rather than inheriting one**, and its cost is honest:

- a **separate settings key** beside `CODE_MODE_HOLD_KEY` (`settings_store.py:265`), not shared
  with it, so boot-clear and jcode's toggle cannot touch it;
- a **distinct sentinel** for "held against everything", since empty already means unheld — and
  `_held_names()`'s return type changes accordingly, so all four consumers above change with it;
- a **refusal reason** carried with the hold rather than hardcoded, so park, code mode and any
  future holder each explain themselves.

**[r2] And the largest gap: a hold blocks admission but frees no memory.** `residency.py:462-464`
short-circuits an already-resident model, and nothing on the hold path evicts. Parking for a render
would leave ~68 GB resident and ComfyUI would still have nothing to work with — the exact scenario
the owner named. **Park therefore needs a drain step**: set the hold *then* evict the resident set
through W0's door. That is what makes park a real operator control instead of a sign on a full
car park, and it is why park depends on W0 rather than standing alone.

**Two decisions W1 must make explicitly:**

- **What does a task routed to a parked box get?** With cloud gone there is no fallback, so this is
  W2's question arriving early: fail, or defer. For a background sweep, defer. For an interactive
  turn, a clear "the box is parked" beats a timeout.
- **Can the owner forget it?** A forgotten park silently stops all background work, so the PWA must
  show it as a standing banner, not a checkbox buried in settings. Consider a TTL that auto-releases
  and says so, since the owner operates this box remotely with no terminal (rule 10).

*Files:* **[r2] corrected** — `residency.py`, `settings_store.py`, `worker.py`, `warm_keeper.py`,
`main.py`, `api/jcode.py`, `api/llm_settings.py`, the settings screen. **Not** `local_catalog.py`:
revision 1's *Files:* line listed it while the wave body argued at length that park must avoid a
catalog entry, and the body is right — a fake entry would need special-casing in
`llama_swap_config.render`, `footprint_gb`, `_require_provisioned`, the settings model list,
`smoketest` and `deploy/local-models-sync.sh`.
*Risk:* medium — higher than revision 1 claimed, because it changes a shared type and four
consumers, one of which (`worker.py:388`) gates all background work.
*Test:* with park held, an admission for any real model is refused with a reason naming park (not
code mode); the drain leaves the pool free; a restart preserves park; toggling code mode on and off
leaves park intact; releasing park admits again.

### W2 — Make the owner's intent authoritative ◻️  *(was W3 — moved up, ahead of re-routing)*

The predecessor's best observation, which it then buried: `triage.classify` is the **only**
registered precondition in the codebase (verified: `precondition=` → one hit, `gmail/triage.py:120`,
`"reasoning_model_loaded"`, registered at `worker.py:786`). `wiki/lint.py`, `analysis/pipeline.py`,
`ingest/ocr.py`, `agent/daily_briefing.py` and `jpet/brain.py` all run ungated per-item LLM loops —
all five verified to exist — and every one of them can reload a model the owner just unloaded.

**One mechanism, not six preconditions:** an owner action — unload, or park — is recorded, and
admission honours it. Background work defers; interactive work is told plainly. One gate at the
door W0 built, which is why W0 comes first.

**[r2] Two corrections to how it must be built:**

- **The refusal must be a `ResidencyError` specifically.** `api/jcode_llm.py:174-180` re-raises
  `ResidencyError` and swallows everything else (`except Exception: # noqa: BLE001 — housekeeping
  never fails a completion`), then forwards to llama-swap, **which loads on demand**. A new
  owner-intent exception type would be silently discarded and the load would happen anyway. Either
  raise `ResidencyError` or add a second re-raise — and test that path, because it is the difference
  between a gate and a log line.
- **The gate lives at admission, not at the load.** Per W0: the routed completion never calls
  `gateway.load`. `ensure_room` is the only thing on that path, so it is the only place this can
  work.
- **[r2]** Revision 1 said residency should also handle deferral. It cannot: residency can only
  raise. Deferral lives in the worker (`worker.py:240-270`), which catches the refusal and requeues.
  The wave spans both.

**[r2]** Revision 1 justified this wave with *"with cloud gone, all of them are local, so this
stops being a triage bug and becomes the system's default behaviour."* That is true — and it is
precisely why this wave must land **before** cloud goes, not after. Revision 1 had the order
inverted: it manufactured the condition in W2 and fixed it in W3.

*Files:* `residency.py`, `worker.py`, `api/jcode_llm.py`, the settings screen.
*Risk:* medium — it can starve background work if the owner forgets a park is held, so the PWA must
show it prominently (see W1).
*Test:* an owner unload followed by a background sweep defers rather than reloading; the same with
park held; releasing it lets the sweep resume; and a jcode completion to a gated model is refused
rather than falling through to an on-demand load.

### W3 — Re-route the task defaults to local ◻️  *(the substance of the old W2)*

**[r2] This, not deletion, is the cloud work.** Revision 1 bundled re-routing with deleting the
provider code and called the result one wave; the deletion is cosmetic and the re-routing is a
quality change across every feature the system has. They are separated so the hard one gets its own
gate and the easy one cannot smuggle it through.

**The hard part is what each of the 20 tasks becomes.** They span tiers the local roster serves
unevenly, and the plan will not guess: W3 starts with a table of all 20 tasks — built from **live
settings, not `TASK_DEFAULTS`** (see *Why now*) — the local model each moves to, and the tier
evidence for that choice. The 3 tiers get their own short table; they are a different kind of thing.
Any task with no adequate local answer is named as such rather than silently pointed at something
that will do badly.

**[r2] Named prerequisite: `../plans/MEMORY_ADMISSION_PLAN.md`'s guard fix.** Every model sits in
one `swap: false` group (`llama_swap_config.py:23, 428-438`), so llama-swap never auto-evicts, and
the 20 tasks span text-reasoning (`gpt-oss-120b` 59 GB, `nemotron-3-super-120b` 78 GB), vision
(`vision.ocr`, `vision.caption`, `agent.vision`) and coding. Any cross-tier task therefore forces an
evict+load cycle through the device guard — and that plan records, measured, that the guard
**cannot load even a 4B model today** (`MEMORY_ADMISSION_PLAN.md:219`: *"`qwen3.5-4b` cannot load
today, aborted on a 0.9% overshoot by a guard whose own docstring says it exists to catch the
ORDER-OF-MAGNITUDE balloon … not ordinary overshoot"*). Revision 1 cited that fact in *What this
plan does NOT do* as a reason to keep clear of the guard — and then routed all traffic into it.
**W3 does not start until that fix has landed.**

**[r2] The quality instrument was named backwards.** Revision 1 said *"`tests/eval/` exists and is
where a regression on the 23 would have to be measured."* `tests/eval/` is the **real-Grok** harness
(`tests/eval/README.md:1`), hard-gated on the provider being removed — `run.py:167` and `:220` both
`if not Settings().xai_api_key: … return 2` — and it routes through `TASK_DEFAULTS` to xai. It
measures nothing after W4 and rebuilding it is part of this wave's cost, not its safety net. The two
harnesses that **survive** were never mentioned: `backend/evals/run.py`, which is provider-agnostic
(*"whatever provider/model your config points `note.extract` at"*, `scripts/prompt-eval.sh:5-6`),
and `scripts/wiki-lint-eval.sh`, which routes through the debug console with no provider key. Those
are the instruments. **Capture a cloud baseline on `backend/evals/` before W4 removes the keys** —
after that the comparison is unavailable, and coverage today is 2 of 20 tasks, so most of the 20
are being re-routed on judgement alone. W3 must say which is which.

*Files:* `router.py`, the per-task settings surface, `backend/evals/`, `tests/eval/`.
*Risk:* **high, and not where it looks** — no deletion happens here at all; the risk is entirely
that 18 of 20 tasks change model with no measurement.
*Test:* each of the 20 tasks resolves to an installed local model; a task whose model is unavailable
defers or fails per W1/W2 rather than hanging; `backend/evals/` run against the pre-W4 baseline.

### W4 — Retire the cloud keys ◻️  *(new — the owner's actual ask)*

Unset `JBRAIN_XAI_API_KEY` and `JBRAIN_ANTHROPIC_API_KEY`. **Zero code.** `providers.py:82` already
hides a keyless cloud provider, and a stored override to one reverse-maps via `id_for_spec` so the
screen surfaces it as unavailable rather than crashing. This is the switch; W3 is what makes it
safe to flip.

**It is also fully reversible**, which is the reason it is a wave of its own and the reason W5 is
separate: if W3's re-routing turns out to have hurt a task badly, restoring a key restores the
comparison. Deleting the code destroys that.

**[r2] Bootstrap and rule 10 — the gap revision 1 left.** `provider_choices()` returns
`(*cloud, *_local_choices(settings))`. With no keys, `cloud` is empty; and `_local_choices` returns
`()` when hosting is off — `config.py:314` `local_llm_enabled: bool = False`, `config.py:372`
`local_models: list[str] = []`. A fresh or hosting-disabled box would then have **no working LLM and
no PWA control to fix it**, which is a rule-10 brick: `local_llm_enabled` is `.env`-only, and the
owner has no terminal. W4 does not ship until either `local_llm_enabled` is PWA- or
debug-API-settable, or the empty-choices state renders as an actionable message pointing at the
control that fixes it. Naming this is the whole reason W4 is a wave rather than a line in a runbook.

*Files:* `deploy/` env template, `providers.py` (bootstrap message only), the settings screen.
*Risk:* low, and reversible by restoring a key.
*Test:* with both keys unset, no cloud provider appears in `provider_choices()` and no task resolves
to a cloud spec; a box with hosting off shows an actionable message rather than an empty list.

### W5 — Delete the cloud provider code ◻️  *(optional, last)*

Delete `AnthropicClient`, the xAI provider wiring, and their config. **Nothing depends on this
happening** — W4 already achieved what the owner asked for. It is housekeeping, and it is last
because it is the only irreversible step.

**[r2] The blast radius in revision 1 is not reproducible and understates the total.** It claimed
7 source / 50 tests / 23 docs / 11 frontend / 2 deploy = 86 files. No single consistent pattern
yields those numbers. Consistent scans (excluding `__pycache__`/`node_modules`):

| pattern | src | tests | docs | frontend | deploy |
|---|---|---|---|---|---|
| `anthropic` | 8 | 12 | **23** | 3 | **2** |
| `xai` | 13 | 63 | 28 | 3 | 3 |
| `anthropic\|xai` | 15 | 64 | 40 | 5 | 3 |
| `grok` | 22 | 72 | 40 | **11** | 2 |

Reproduce with, for each pattern and each of `backend/src backend/tests docs frontend/src deploy`:

    grep -rliE "$pat" "$d" | grep -v __pycache__ | grep -v node_modules | wc -l

`docs: 23` and `deploy: 2` match an `anthropic`-only scan; `frontend: 11` matches a `grok`-only
scan. Those cannot both be "the cloud reference surface". Nothing produces `src: 7` or `tests: 50`.
On a consistent `anthropic|xai` scan the total is **127**, not 86.

Worse, the `grok`-derived frontend count sweeps in files this wave explicitly carves out —
`frontend/src/screens/JcodeScreen.tsx`, `ExternalSessionScreen.tsx`, `jcode/types.ts` are the
grok-CLI surface that **stays** — and on the backend it catches `web/grokipedia.py` and
`agent/tools/grokipedia.tool`, which are a website scraper, not a provider. **Revision 1 counted as
blast radius the exact files it carved out.** W5 recounts from scratch with one stated pattern, or
does not quote a number.

`openai_compat.py` is **shared** with the local provider — `router.py:840` (xai) and `:841-847`
(local) construct the same class. It is not deleted; only its xAI construction goes.

The jcode sandbox's `grok` CLI **stays**: `api/jcode_llm.py:1` — *"Residency-aware, multi-model
proxy for the jcode sandbox's grok CLI"* — pointed at the box's own models. Removing the xAI
*provider* does not touch it. *(Worth stating plainly because the predecessor plan got the jcode
wiring wrong in the opposite direction and built a wave on it.)*

`llm/model_sampling.py`'s `CLOUD_SAMPLING` table and `llm/providers.py`'s choice list hold only
preference-level coupling — mechanical, no correctness dependency.

*Files:* recount at wave start; the seven-file provider deletion plus a test-fixture sweep that is
the actual bulk.
*Risk:* medium and entirely mechanical — but irreversible, so it goes last.
*Test:* no cloud provider can be constructed; the suite passes with cloud fixtures re-pointed.

### W6 — Re-derive the `running()`/`ready()` split, or drop it ◻️  *(was W4)*

Explicitly last, and explicitly conditional. The predecessor made this its keystone and cold review
found the keystone hollow: its third category ("bill the remaining footprint") has no data source on
this box, and applying it reintroduces the mid-load baseline bug #1186 fixed.

**[r2] Citation corrected.** Revision 1 said *"at `local_gateway.py:762`"*, carried verbatim from
the predecessor (`GPU_ADMISSION_INTEGRITY_PLAN.md:7, 150, 167`) and never re-verified — the exact
failure this plan's standing rule targets. Line 762 today is `self._drop_weights_cache(model)`; the
unguarded branch opens at **757** (`if self._gpu_probe is None:`) and the pre-flight is at
**801-802**. (`local_gateway.py:808` and `llm_settings.py:156-161` were both cited correctly.)

Revisit only when **all three** hold:

1. W0 has landed, so there is one door to change.
2. An incident occurs that the device guard did **not** catch. Cheap way to earn that evidence: log
   when the already-resident short-circuit fires, and see whether the window ever opens now that
   `unload` no longer returns early and loads serialise. **That is one line, and it turns this from
   a code-reading argument into a measured one.**
3. The PWA shows model state rather than a boolean, so the distinction is observable before anything
   depends on it — `api/llm_settings.py`'s `_loaded_ids` is both a call site this would change *and*
   the load indicator, so a careless change makes a loading model vanish from the owner's screen.

If (2) never happens, this wave is correctly never built.

## What this plan does NOT do

- It does not touch `gpu_guard`'s ceiling arithmetic or `refuse_if_no_device_room`. Those are
  `../plans/MEMORY_ADMISSION_PLAN.md`'s. **[r2]** Revision 1 cited that plan's "cannot load a 4B
  model" finding as a reason to keep clear — and then W2 routed all traffic into it. That fix is now
  a **named prerequisite of W3**, not a disclaimer.
- It does not touch `ttm.pages_limit`. Same owner, and the predecessor got its direction backwards
  twice.
- It does not remove the `grok` CLI from the jcode sandbox (see W5).
- It does not add a catalog entry for park (see W1's *Files:*).

## Corrections log

Kept because this plan's predecessor died of unverified claims, and a reader deserves to know which
statements have already failed review once.

| revision 1 said | primary source says |
|---|---|
| "every one of the 23 task defaults" | 20 tasks + 3 tiers (`router.py:50-124`, `:176-178`); and `TASK_DEFAULTS` is not the live table — `_resolve_live` folds in DB overrides |
| park = `_held_names()` with an empty set | empty means **not held** (`residency.py:309-320`, `if names:`); four consumers read it that way |
| park inherits a working mechanism | needs its own key, a sentinel, a carried reason, and reconciliation with `main.py:391` and `api/jcode.py:502/523` |
| a hold reserves the box | a hold blocks the *next* load and frees nothing (`residency.py:462-464`); park needs a drain |
| 17 load/unload sites | 18 — `api/llm_settings.py:1891` missed |
| "thirteen bypass the budget" | four; nine are unloads, one is admitted a line earlier, one is admitted on one of two routes |
| one door = an AST test on `.load()` | the dominant load path never calls `load()` (`local_gateway.py:224-241`, measured); the door is `ensure_room` |
| only `cli.py` has the no-DB problem | `smoketest.py:237` too — `--no-deps` at `update-inner.sh:695`, `:240`, `:498` |
| the abort lambda "cannot route through residency without a cycle" | a cross-session self-deadlock on the advisory key; the rule is *nothing inside `_box_locked()` goes through the door* |
| remove cloud (W2) before honouring owner intent (W3) | inverted — W2 manufactured the condition W3 fixes; now W2 then W3/W4 |
| `tests/eval/` measures the re-routing | it is the real-Grok harness, gated on `xai_api_key` (`run.py:167,220`); `backend/evals/` and `scripts/wiki-lint-eval.sh` survive |
| 86 files of blast radius | not reproducible from any single pattern; a consistent `anthropic\|xai` scan gives 128, and the quoted frontend count includes files the wave carves out |
| removing cloud requires code | two env keys — `providers.py:82` already hides a keyless provider |
| `local_gateway.py:762` | `:757` (unguarded branch) / `:801-802` (pre-flight); `:762` is `_drop_weights_cache` |
