# A local-only box with one door

> **Status:** Proposed · **Last verified:** 2026-08-21 · **Waves:** W0◻️ W1◻️ W2◻️ W3◻️ W4◻️

> Reconciled with the root `CLAUDE.md` non-negotiables — every LLM call still goes through the
> adapter (rule 1) and W2 *shrinks* that surface rather than adding to it; no storage or RLS
> surface changes (rules 2–3), and no wave adds a table; every wave names its test (rule 5);
> W3 adds a catalog entry and a settings control, both PWA-operable, and no wave introduces a
> shell-only step (rule 10).

> **Standing rule for this plan, earned the hard way.** Its predecessor
> (`../archive/GPU_ADMISSION_INTEGRITY_PLAN.md`) was withdrawn after four cold adversarial
> reviews. Three of its central claims were a log label, a README summary and an env-var name
> read as evidence; a fourth was a partial source fetch with the remainder inferred. **No claim
> in this plan is load-bearing unless the primary source was opened and quoted.** Where a fact
> could not be checked, it says so instead of rounding up.

## Why now

The owner is removing the Anthropic and xAI cloud providers. That is not a subtraction:

    $ grep -cE '": "xai:|": "anthropic:' backend/src/jbrain/llm/router.py   →  23
    $ grep -cE '": "local:'              backend/src/jbrain/llm/router.py   →   0

**Every one of the 23 task defaults routes to cloud today. None routes local.** So removing
cloud re-routes the entire system onto a box that holds about two models at once, with no
fallback anywhere. Contention stops being a background concern and becomes the main one.

That inverts the predecessor's risk calculus. Its reviews were right that a 10-file semantic
refactor was disproportionate *to a box with a cloud escape hatch*. Once the hatch is gone,
the parts of it that survived scrutiny are no longer optional — but they are also no longer the
whole story, because the interesting question changes from "did this load skip the guard?" to
"who gets the box next, and can the owner take it back?"

## What is already true

Kept short and sourced, because the predecessor's evidence section drifted from the code twice.

| | |
|---|---|
| loads serialise **per process** | `local_gateway.py` `self._global_load_lock = asyncio.Lock()`. The commit that added it said "box-wide"; it is not, and the file says so: *"Per PROCESS, not per box."* Cross-process serialisation is residency's `pg_advisory_xact_lock`, on the routed path only. |
| the device guard is real, at one chokepoint | `refuse_if_no_device_room` + `guarded_load`, both inside `LocalGatewayClient.load`. Anything that reaches llama-swap another way skips both. |
| `unload` no longer races its own success | was a 3 s client timeout against llama-swap's 10 s graceful stop; widened, shipped. |
| `unannounced_load` no longer reports our own loads | shipped on branch; carries `client` and `first_poll` so a cross-process sighting is legible. |
| `/running` carries three states | `starting`, `ready`, `stopping`. `stopped` and `shutdown` are filtered by `internal/router/base.go`. `_parse_running` discards the field. |

## The waves

Ordered so the box is *safer* after each one, and so the riskiest change lands last with the
most scaffolding under it.

### W0 — One door in, one door out ◻️

The enabling wave, and the one the owner asked for: *"make sure there's only one way to load in
and out, and that everything on the box uses these."*

Today `gateway.load()` / `gateway.unload()` are called directly from **at least** these, all
verified present: `residency.py` (×4), `api/llm_settings.py` (×6), `warm_keeper.py`,
`smoketest.py`, `cli.py`, `agent/transcribetools.py`, `image_gen/render.py`,
`local_gateway.py`'s own abort path. Residency owns the budget; six of those callers do not go
through it.

So: **`residency` becomes the only caller of `gateway.load`/`gateway.unload`**, exposing
`acquire(model, *, why)` and `release(model, *, why)`. Every other caller moves to those. The
gateway's methods become private to that seam, and — following the precedent already in this
repo at `test_llm_load_guard_chokepoint.py`, which walks the `src` AST and fails the build if a
`LocalGatewayClient` is constructed without a probe — **an AST test fails CI if anything outside
`residency` calls them.** That is what makes "one door" hold past this PR rather than decaying
on the next one.

Two callers need care rather than mechanical replacement, and the wave is not done until both
are resolved:
- `local_gateway.py`'s `abort=lambda: self.unload(...)` — the watchdog's own abort, *inside*
  the gateway. It cannot route through residency without a cycle. It stays, and the AST test
  gets one named exemption with the reason in the diff.
- `cli.py` — runs pre-update with no app process alive, so residency's DB lock may be
  unavailable. Either it grows a documented degraded path, or the update sequence changes.
  **Unresolved; W0 must answer it before it starts.**

*Files:* `residency.py`, `local_gateway.py`, `api/llm_settings.py`, `warm_keeper.py`,
`smoketest.py`, `cli.py`, `agent/transcribetools.py`, `image_gen/render.py`, plus the AST test.
*Risk:* medium — wide but mechanical, and the AST test makes an omission a build failure rather
than a silent hole. *Test:* the AST guard; plus one behavioural test per moved caller proving it
now evicts-to-budget where it previously did not.

### W1 — The park model ◻️

The owner's idea, and it is a better operator control than anything in the predecessor plan:
**a catalog entry that occupies the box and serves nothing.**

Load `park` and the residency budget sees a model whose footprint is the whole box, so nothing
else can be admitted. Unload it and the box is free again. It is the "I want this machine to
myself" switch — for a render, for an update, for debugging, or simply to stop background work
touching the GPU.

Shape, and each of these is a decision W1 must make explicitly rather than inherit:
- **Does it start a process at all?** Cheapest is a pseudo-model residency accounts for with no
  llama-server behind it — no weights, no load time, instant on and off. That means residency
  can hold a reservation for something the gateway has never heard of, which is new.
- **What does a task routed to a parked box get?** With cloud gone there is no fallback, so this
  is W2's question arriving early: fail, or defer. For a background sweep, defer. For an
  interactive turn, a clear "the box is parked" is far better than a timeout.
- **Does park survive a restart?** It should — a park the owner set should not evaporate because
  the api restarted. That means persisting it beside the auto-restore switch.

*Files:* `local_catalog.py`, `residency.py`, `api/llm_settings.py`, the settings screen.
*Risk:* low-medium — additive, and its failure mode (box parked when it should not be) is
visible and reversible from the PWA. *Test:* with park held, an admission for any real model is
refused with a distinguishable reason, and releasing park admits it.

### W2 — Remove the cloud providers ◻️

Delete `AnthropicClient`, the xAI provider wiring, and their config. Blast radius is contained —
`config.py`, `llm/__init__.py`, `llm/router.py`, `llm/providers.py`, `llm/anthropic.py`,
`llm/openai_compat.py`, `llm/model_sampling.py` — but the *routing* change touches all 23 task
defaults.

**The hard part is not deletion, it is what each of the 23 becomes.** They span tiers the local
roster serves unevenly, and the plan will not guess: W2 starts with a table of all 23, the local
model each moves to, and the tier evidence for that choice. Any task with no adequate local
answer is named as such rather than silently pointed at something that will do badly.

`openai_compat.py` is **shared** with the local provider — it is the client the gateway is
spoken to through. It is not deleted; only its xAI construction goes.

Note the jcode sandbox's `grok` CLI **stays**: it is an xAI-authored client pointed at the box's
own models through the residency-aware proxy at `api/jcode_llm.py`. Removing the xAI *provider*
does not touch it. *(Worth stating plainly because the predecessor plan got the jcode wiring
wrong in the opposite direction and built a wave on it.)*

*Files:* the seven above, plus the settings screen's provider list. *Risk:* medium — the
deletion is safe; the re-routing is a quality change across every feature the system has.
*Test:* no cloud provider can be constructed or routed to; each of the 23 tasks resolves to an
installed local model; a task whose model is unavailable fails the way W1 decided, not by
hanging.

### W3 — Make the owner's intent authoritative ◻️

The predecessor's best observation, which it then buried: `triage.classify` is the **only**
registered precondition in the codebase (`grep precondition=` → one hit). `wiki/lint.py`,
`analysis/pipeline.py`, `ingest/ocr.py`, `agent/daily_briefing.py` and `jpet/brain.py` all run
ungated per-item LLM loops, and every one of them can reload a model the owner just unloaded.

With cloud gone, all of them are local, so this stops being a triage bug and becomes the
system's default behaviour.

**One mechanism, not six preconditions:** an owner action — unload, or park — is recorded, and
`residency.acquire` (which after W0 is the only way in) honours it. Background work defers;
interactive work is told plainly. That is one gate at the door W0 built, and it is why W0 comes
first.

*Files:* `residency.py`, `worker.py`, the settings screen. *Risk:* medium — it can starve
background work if the owner forgets a park is held, so the PWA must show it prominently.
*Test:* an owner unload followed by a background sweep defers rather than reloading; the same
with park held; releasing it lets the sweep resume.

### W4 — Re-derive the `running()`/`ready()` split, or drop it ◻️

Explicitly last, and explicitly conditional. The predecessor made this its keystone and cold
review found the keystone hollow: its third category ("bill the remaining footprint") has no
data source on this box, and applying it at `local_gateway.py:762` reintroduces the mid-load
baseline bug #1186 fixed.

Revisit only when **all three** hold:

1. W0 has landed, so there is one door to change rather than 23 call sites.
2. An incident occurs that the device guard did **not** catch. Cheap way to earn that evidence:
   log when the already-resident short-circuit fires, and see whether the window ever opens now
   that `unload` no longer returns early and loads serialise. **That is one line, and it turns
   this from a code-reading argument into a measured one.**
3. The PWA shows model state rather than a boolean, so the distinction is observable before
   anything depends on it — `api/llm_settings.py`'s `_loaded_ids` is both a call site this
   would change *and* the load indicator, so a careless change makes a loading model vanish from
   the owner's screen.

If (2) never happens, this wave is correctly never built.

## What this plan does NOT do

- It does not touch `gpu_guard`'s ceiling arithmetic or `refuse_if_no_device_room`. Those are
  `../plans/MEMORY_ADMISSION_PLAN.md`'s, and that plan records the guard as already too tight to
  load a 4B model — a reason to leave it to the plan that owns it, not to route more traffic
  into it.
- It does not touch `ttm.pages_limit`. Same owner, and the predecessor got its direction
  backwards twice.
- It does not remove the `grok` CLI from the jcode sandbox (see W2).
