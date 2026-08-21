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

**The caller set, enumerated rather than estimated** — a first draft of this wave got it wrong
in both directions, so it is written out in full:

    residency.py:502,533,589,645     the owner of the budget
    api/llm_settings.py:833,908,1286,1519,1533
    api/jcode.py:164,168,474         code mode taking and releasing the box
    warm_keeper.py:182
    smoketest.py:237                 during a deploy
    cli.py:73                        pre-update, no app process
    image_gen/render.py:206          frees the pool for ComfyUI
    local_gateway.py:808             the watchdog's own abort (see below)

Seventeen sites; thirteen of them bypass the budget entirely.

**Explicitly NOT in scope, and named so nobody "fixes" them:** `agent/transcribetools.py:146`,
`ingest/video.py:441` and `ingest/transcribe_job.py:206` call `unload` on a **different
gateway** — `main.py:789-798` builds them their own `LocalGatewayClient` on
`settings.whisper_url`. That is a separate llama-swap with its own memory, and routing it
through the LLM box's budget would be wrong. The first draft listed `transcribetools` as a
caller to move; it is not one.

`api/jcode.py` is the omission that mattered most: code mode is the subsystem whose box
reservation W1 generalises, and it is itself three direct calls.

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

**It is not a new mechanism, and the first draft of this wave was wrong to propose one.**
`residency._held_names()` already exists:

> *"The served-model names code mode has reserved the box for, or an empty set (not held). Read
> per-load so toggling code mode applies immediately."*

Code mode already reserves the box, and the reservation is already consulted on every load.
**Park is that hold with an empty name set** — nothing may be admitted — set by the owner rather
than by code mode. That inherits a working, already-exercised code path and avoids special-casing
a fake catalog entry in `llama_swap_config.render` (which would try to emit a `cmd` for it),
`footprint_gb`, `_require_provisioned`, the settings model list, `smoketest`, and
`deploy/local-models-sync.sh`.

Two decisions remain, and W1 must make them explicitly:
- **What does a task routed to a parked box get?** With cloud gone there is no fallback, so this
  is W3's question arriving early: fail, or defer. For a background sweep, defer. For an
  interactive turn, a clear "the box is parked" beats a timeout.
- **Does park survive a restart, and can the owner forget it?** It should survive — a park the
  owner set should not evaporate because the api restarted — which means persisting it beside the
  auto-restore switch. And because a forgotten park silently stops all background work, the PWA
  must show it as a standing banner, not a checkbox buried in settings.

*Files:* `local_catalog.py`, `residency.py`, `api/llm_settings.py`, the settings screen.
*Risk:* low-medium — additive, and its failure mode (box parked when it should not be) is
visible and reversible from the PWA. *Test:* with park held, an admission for any real model is
refused with a distinguishable reason, and releasing park admits it.

### W2 — Remove the cloud providers ◻️

Delete `AnthropicClient`, the xAI provider wiring, and their config.

**The blast radius is NOT contained, and a first draft of this wave said it was.** Counted:

    backend source      7 files
    backend tests      50 files
    docs               23 files
    frontend           11 files
    deploy              2 files

**86 files.** The *provider* deletion is seven; the *cloud reference* surface is an order of
magnitude larger, and the 50 test files are the bulk of the work — most fixture routing in this
suite names a cloud provider, which is why W2 is a multi-day wave rather than a deletion.

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

*Files:* the 86 above. *Risk:* **high, and not where it looks** — the deletion is safe and the
tests are mechanical; the re-routing is a quality change across every feature the system has, on
a box that fits two models. *Test:* no cloud provider can be constructed or routed to; each of
the 23 tasks resolves to an installed local model; a task whose model is unavailable defers or
fails per W1's decision rather than hanging. **Quality is the untested part** — `tests/eval/`
exists and is where a regression on the 23 would have to be measured, so W2 should say up front
which of the 23 have eval coverage and which are being re-routed on judgement alone.

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
