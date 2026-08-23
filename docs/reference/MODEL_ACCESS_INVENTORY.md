# Model-access inventory

> **Status:** Living · **Last verified:** 2026-08-23

> **What this is.** A factual inventory of every place in this tree that loads, unloads, admits,
> warms, evicts, measures, or *demands* a model — across all five things that consume this box's
> unified memory. It is the evidence base for `LOCAL_MODEL_ACCESS_PLAN.md`. It contains no waves,
> no recommendations, and no judgements.

> **Why it exists.** Four successive plans for this work were withdrawn after cold review. Every
> one died the same way: a claim asserted from a docstring, a log label, an env-var name, or a
> grep count, without the file being opened. The last of them was withdrawn after its own author's
> central premise ("the routed path never calls `load()`") turned out to be contradicted by two
> wiring lines. The rule that produced this document is therefore: **every row carries a
> `file:line` and a verbatim quote of the code.** Where something could not be determined from
> source, it is listed as undetermined rather than inferred. Each section ends with its own
> "things I could not determine" list; those are load-bearing and should be read.

> **How to keep it true.** This is a Living doc under `../DOC_LIFECYCLE.md`. When a wave of the
> plan lands, the rows it changed get re-verified and `Last verified` bumped. A row whose quote no
> longer matches the file is a bug in this document, not a detail.

> ⚠️ **READ THIS BEFORE CITING ANYTHING BELOW. Audited 2026-08-22 by a cold pass told to
> falsify, not confirm.** The rule above was violated on the day it was written. `Last verified`
> was bumped by `be1961a`, but the line numbers throughout describe **`be1961a^`, that commit's
> parent** — and `be1961a` itself edited `residency.py`, `local_gateway.py`, `llm_settings.py`
> and `worker.py`, fixing several defects this document still reported as live. Further
> commits have landed since — re-grep before citing.
>
> Measured drift at the time of the audit: **123 of 503 `file:line` + quote pairs pointed at the
> wrong line (24%)**. The quotes were almost all still correct; only the addressing had rotted.
> Eight rows were factually contradicted by the code and are marked inline below
> (**WRONG WHEN WRITTEN**, **FIXED, and this row is stale**, **CORRECTED**, **DELETED**).
>
> **So: trust the quotes, distrust the line numbers, and re-grep before you cite.** A row that
> says a gate is missing is the most dangerous kind here — three of the eight said exactly that
> about gates which had since been closed, and a plan built on them would re-fix fixed code while
> the real holes went unlisted. The sites this document never listed at all are in §G.

## The five consumers

| # | consumer | reached via | notes |
|---|---|---|---|
| 1 | **LLM gateway** (llama-swap) | `settings.local_llm_url` | the main model pool; §A |
| 2 | **Whisper gateway** (a second llama-swap) | `settings.whisper_url` | separate container `tts-stt`, separate models dir, `ttl: 300`; §B |
| 3 | **Embeddings / TEI** | `settings.embed_url` | `mem_limit: 1g`, CPU image, no load/unload in our code; §B |
| 4 | **ComfyUI / image generation** | `image_gen/` | competes for the same pool; evicts LLMs, is never evicted by them; §B |
| 5 | **Kokoro TTS** | baked into `tts-stt` | *"the warm server holds the model resident"* (`deploy/docker-compose.yml:474`); no memory accounting found in `backend/src`; §B |

Consumer 5 was not in the original scope of this sweep. It was found while proving consumer 2's
wiring, which is the argument for enumerating rather than estimating.

## Contradictions between comment and code, found while building this

Recorded here rather than buried, because these are the exact artefacts that sank the previous
plans. Each is a place where a reader who trusts the prose reaches the wrong conclusion.

| where | the comment says | the code says |
|---|---|---|
| `backend/src/jbrain/config.py:392-393` | whisper is *"served by the same llama-swap gateway the local-llm profile runs"* | a separate llama-swap in the `tts-stt` container on `:8080`, separate models dir (`deploy/docker-compose.yml:492`, `deploy/tts-stt/entrypoint.sh:12`); all six whisper clients are constructed bare — no `gpu_probe`, no `models_dir`, no `config_regen` |
| repo docstrings referring to gateway TTL | the gateway TTL-unloads an idle model | true for **whisper only** (`ttl: 300`, `scripts/whisper-setup.sh:110`). `llama_swap_config.py` emits no `ttl`/`globalTTL`; llama-swap's `GlobalTTL` defaults to `0`, which gates the TTL goroutine off. On the main pool the app is the sole evictor |
| the commit that added `_global_load_lock` | "box-wide" | `local_gateway.py` defines `_global_load_lock` as an `asyncio.Lock()` (the cited line pointed at the comment above it); the file itself says *"Per PROCESS, not per box."* |
| `CODE_MODE_HOLD_KEY = "code_mode_hold_name"` | singular name | stores a **list** |

## §E — Live box state, read 2026-08-22

Read through the owner debug console against the running box (`git_sha 399b15e`, started
2026-08-21T21:34:50Z). These are the four things the source sweeps flagged as undeterminable
from the tree, plus one reproduction. **Live state differs from what the source implies in three
places**, which is why this section exists.

### Routing is already fully local

All 19 selectable tasks resolve to on-box models — 16 to `gpt-oss-120b`, 3 vision to
`qwen3.8-27b-abliterated` — as `llm_task_overrides` rows, which `_resolve_live` ranks above the
tier and the task default. `provider_choices()` returns **12 local models and no cloud entries**.

So the premise that "every task routes to cloud" is true only of `TASK_DEFAULTS` on a fresh box.
It was never true of this box. Any plan sized on that grep is sized wrong.

### The auto-restore toggle is already off

`auto_restore: false` in the live snapshot, and models load anyway — see the reproduction below.
This is direct confirmation that the toggle gates `_restore` and the WarmKeeper only, never
admission.

Also live: `free_ram` is `{"fraction": 0.05, "default": 0.15, "override": 0.05}`. The ceiling is
therefore ~115 GiB, not the ~103 GiB a reader would compute from the default.

### The live schedule set is 16, and differs from the migrations

Enabled triggers joined to enabled schedules, by interval:

| pipeline | interval |
|---|---|
| `reconcile_pending_integration`, `reconcile_pending_notes`, `reconcile_unembedded_notes` | 300 s |
| `geofence_sweep` | 900 s |
| `daily_inbox_triage` | **3600 s** |
| `expire_research_reports`, `nightly_consolidate_predicates`, `nightly_entity_hygiene`, `nightly_purge_deleted_artifacts`, `nightly_reembed_stale`, `nightly_sync_predicates`, `nightly_tag_consolidate`, `nightly_wiki_lint`, `nightly_wiki_prune`, `nightly_wiki_refresh` | 86400 s |
| `wiki_rebuild_all` | on demand |

`nightly_entity_hygiene` and `nightly_reembed_stale` are **enabled live**; a reconstruction from
migrations `0036→0169` concluded the hygiene sweeps ship disabled. The migration-derived answer was
wrong for this box.

### REPRODUCTION — a model the owner unloaded came back, unrecorded

The clearest evidence in this document, and the reason the plan is ordered as it is:

```
08-21 21:42:12  model_unload  gpt-oss-120b   "you unloaded it"        source=api
                auto_restore = false
08-21 22:41:48  daily_inbox_triage → triage_inbox  ok  7,519 tokens
                                     task: triage.classify, model: gpt-oss-120b
08-21 23:42:09  daily_inbox_triage → triage_inbox  ok  0 tokens  (triage_inbox.empty)
08-22 00:42:14  daily_inbox_triage → triage_inbox  ok  0 tokens  (triage_inbox.empty)
08-22 01:19:29  prefill  gpt-oss-120b
08-22 09:04:15  prefill  gpt-oss-120b            ← still resident, 69 GB, at time of reading
```

**No `model_load` event exists anywhere between the unload and now.** The model was unloaded
deliberately, an hourly sweep used it 59 minutes later and billed 7,519 tokens, and it has been
resident since — with the load never narrated.

Three mechanisms that should each have prevented this did not:

1. `auto_restore` was off — but it does not gate admission (above).
2. `triage.classify` carries the **only** registered `precondition=` in the codebase
   (`reasoning_model_loaded`), written for exactly this case. It did not defer. The two later runs
   returning `triage_inbox.empty` with 0 tokens show the machinery runs; at 22:41 there was mail
   and it proceeded.
3. No `model_load` event means the load did not go through `gateway.load`, so nothing narrated or
   guarded it — consistent with the llama-swap load-on-request path (§A.5, I1).

**Cause not established.** A stale residency read is consistent with all three — `running()`
includes `stopping` models. **CORRECTED 2026-08-22:** the state is NOT discarded — `running()` parses it via `_parse_running_states`, caches it, and exposes it as `state_of`, a REQUIRED member of the `LocalGateway` Protocol. `_parse_running` still exists but is dead in `backend/src`. What remains true is that every BEHAVIOURAL consumer branches on the bare name set, so a model on its way
out satisfies the precondition. That is a hypothesis. The reproduction is cheap: unload
`gpt-oss-120b`, wait for the top of an hour with mail in the inbox, observe whether triage defers.

### The debug console cannot see resident model memory

`/api/debug/host/metrics` reports container cgroup memory and per-process RSS. A GTT-resident
model appears in **neither**: with `gpt-oss-120b` resident at 69 GB, the `local-llm` container
reads 0.23 GiB and no `llama-server` process is listed at all. Every process on the box sums to
~3.5 GiB against 83.5 GiB unavailable.

The GTT counters exist — `gpu_guard.SupervisorGpuMemProbe` reads `gpu_mem` from the supervisor's
`/metrics`, and the Ops screen draws them — but `/api/ops/*` rejects a capability token
(`{"detail":"not authenticated"}`), and no `/api/debug/*` route exposes them.

Consequence, recorded because it caused a wrong diagnosis during this very session: a reader
working through the debug console sees ~80 GiB unaccounted and no model, and can reasonably
conclude a leak. The PWA shows `1 resident · 81 GB — GPT-OSS 69G, system 7 GB, cache 5 GB`, which
is correct and complete. **The surface the owner hands an assistant is blind to the single number
that matters most for this class of problem.**

## Facts carried forward from the deleted memory-admission plan

`MEMORY_ADMISSION_PLAN.md` (deleted) was deleted along with `LOCAL_ONLY_BOX_PLAN.md` when this
inventory replaced them. Its *waves* are gone; its *measurements* cost real time on the box and are
preserved verbatim below.

### The eight uncoordinated budgets

| # | constant | value | location |
|---|---|---|---|
| 1 | `MIN_FREE_GTT_GB` | 6.0 | `gpu_guard.py:100` |
| 2 | `RUNAWAY_MULTIPLE` / `+2.0` | 1.75 | `gpu_guard.py:95` |
| 3 | `llm_local_free_ram_fraction` | 0.15 (live) | Settings / `config.py` |
| 4 | ~~residency constructor default~~ | ~~**0.25**~~ | **RESOLVED** — `free_ram_fraction` has no default now (`ResidencyWiring`), so both boxes read budget #3 or fail to build |
| 5 | `LOAD_HEADROOM_GB` | 20.0 | `smoketest.py:85` |
| 6 | `RESERVE_GIB` | 16 | `strix-halo-host-setup.sh:57` |
| 6b | same, hardcoded | `16 * 1024 * 1024` | `update-inner.sh:627` |
| 7 | `HOST_RESERVE_GIB` | 16 | `host_settings.py:35` |
| 8 | ~~`CACHE_RAM_GB`~~ | ~~8.0~~ | **WRONG WHEN WRITTEN.** `local_catalog.py` has `CACHE_RAM_GB = 0.0` — *"ZERO because the gateway now serves `-cram 0`"* — changed in #1185, which had already landed. So this list is six live budgets, not eight. |

### D3 — the smoke test projects a window the gateway does not serve

The llama.cpp upgrade smoke test runs via `cli.py`, which passes neither
`windows_loader` nor `slots_loader` (deliberate: `--no-deps`, no DB). So it
projects the **catalog** window while llama-swap serves the **operator** window.

`qwen3.5-0.8b` has a saved override of 262144 against a catalog default of
32768 — 8× the KV:

| window used to project | projected | ceiling | observed | verdict |
|---|---|---|---|---|
| 32768 (catalog) | 1.57 | 3.57 | 3.78 | **abort** ← smoke test |
| 262144 (actual) | 2.45 | 4.45 | 3.78 | pass ← API path |

Same model, same build, same memory. **We have been rolling back llama.cpp
upgrades for a spurious reason** — and newer llama.cpp is where the `no_alloc`
estimator lives, so the broken test blocks the fix for what it is failing on.

Same class of bug on the live path: `_served_shape` (`local_gateway.py:290-297`)
falls back to the catalog window on *any* settings-read failure, silently.
And `router.py:813` builds a gateway client with `windows_loader` but **no
`slots_loader`**, so a saved `-np 2` is invisible to that pre-flight.


### D4 — the runaway ceiling aborts on noise

### D4 — the runaway ceiling aborts on noise

`ceiling = baseline + max(projected × 1.75, projected + 2.0)`. Crossover where
the two terms swap is exactly **8/3 GiB**. Measured against the real ceiling:

| model | projected | ceiling | observed | margin |
|---|---|---|---|---|
| qwen3.5-0.8b | 1.57 | 3.57 | 3.78 | +6% |
| qwen3.5-4b | 7.25 | 12.69 | 12.80 | **+0.9%** |
| qwen3.8-27b-q4 | 20.59 | 36.05 | 26.10 | −28% |

`qwen3.5-4b` **could not load at the time of this measurement**, aborted on a
0.9% overshoot by a guard whose own docstring says it exists to catch "the
ORDER-OF-MAGNITUDE balloon … not ordinary overshoot". The projection was light
by 5.55 GiB, most of it the warm-up phase: `guarded_load` returned *before*
`_warm()` ran, so KV allocation and graph capture landed after the guard's last
sample — not a one-interval race, an entire unwatched phase (since closed; §G5).
The under-declaration itself was fixed by #1191: a measured
`runtime_overhead_gb=9.5` override (`local_catalog.py:1008`, floor-anchored —
verify against a completed load post-deploy).

Note the direction is the opposite of the intuitive one: the effective allowed
multiple is `max(1.75, 1 + 2/p)`, so the guard is *looser* on small models
(2.27× for the 0.8b), not tighter.

---


### Why not a different runtime (vendor evaluation, preserved)

## 2. What we are NOT doing, and why

Two independent research passes converged on **stay put**. Recording the
rejected options so this is not re-litigated:

| option | why not |
|---|---|
| **Move to Ollama** | gfx1151 support is unofficial (`HSA_OVERRIDE_GFX_VERSION=11.5.1`, and it warns *not* to enable Vulkan) — forcing us off the community-stable backend. Vendored llama.cpp measures ~56% slower on AMD Vulkan (#15601). Open UMA bug #16719: scheduler clamps GPU budget to host free RAM, so a second model evicts the first with 72 GB genuinely free. |
| **Move to LM Studio** | #1471 is our exact bug, still open, no maintainer response. And the GUI's "Load anyway" has **no CLI/REST equivalent** (#1631) — an admission bug we could not override remotely. Fatal against rule 10. |
| **Move to vLLM** | Not upstream-supported on gfx1151; the toolbox is a self-described spare-time project. Architecturally wrong anyway: static KV pool, one hot model, not 16 swapped. |
| **KoboldCpp / Jan / GPT4All / LocalAI / RamaLama** | No multi-model swapping at all (Kobold #2120), or not headless servers, or no memory-aware admission, or no credible gfx1151 evidence. |
| **Replace our estimator with llama.cpp `--fit`** | #22592, *tested on gfx1151*: `--fit` checks the device-side budget while the real run also allocates host buffers **from the same physical pool** — it double-counts on unified memory. It also must risk OOM to decide how to avoid OOM, and its failures are non-fatal. This is why `-fit off` appears in every serious reference config on this platform. |

**llama-swap has no memory accounting at all** — TTL unload, concurrency
groups, manual unload endpoints, but it never asks "is there room". So the layer
we hand-built is not a reimplementation of something purchasable. There is
nothing on the shelf.

`gpu_guard.py` does already compute `min(gtt_total - gtt_used, host_free -
reserve)`, which is the shape LM Studio #1471, Ollama #16719 and llama.cpp
#22592 each get wrong. v1 of this plan concluded from that "we are ahead of all
three". **That line is cut.** By D1, one of those two terms uses a `gtt_total`
that is not a bound we control, and by D0 the `reserve` in the other is one of
eight disagreeing numbers — so the formula being praised is not obviously doing
the work claimed for it, in a system that froze for seven hours and currently
cannot load `qwen3.5-4b`.


### Measured load footprints

### W3 — measure instead of guess ◻️

> **PARTIALLY DELIVERED 2026-08-19, by a different means than this wave proposes.**
> `gpu_guard.measure_footprint` — the GTT+VRAM delta across a load, bracketed by the samples the
> admission guard already takes — is now wired into the load chokepoint and logs
> `local_gateway.footprint_measured` (predicted / measured / drift) on **every** load. That is not
> the `no_alloc` dry-run below, and does not replace it: the delta is **one number**, so it can say
> a catalog entry is wrong but not whether KV or weights is the wrong term. What it does give is a
> standing instrument instead of a one-off study, and the first seven readings follow.
>
> **⚠ These are PEAK-ACROSS-WARM, not resting.** The guard's second sample is taken after
> `_warm()`, so each figure includes warm-time KV growth and context checkpoints that a slot
> releases afterwards. MEASURED on `qwen3.5-0.8b`: **5.13 peak vs 3.83 resting** once the slot
> released. Figures elsewhere in these docs and in `STRIX_HALO_SETUP.md` are resting GTT and are
> **not comparable** — that difference alone accounts for the apparent contradiction the §4 banner
> flags, where gpt-oss reads 67.6 resting against a 68.55 projection (0.95 heavy) but 69.26
> peak-across-warm (0.71 light). Both are correct; they measure different moments.
>
> Peak is the right number for an admission guard: peak is what has to fit.
>
> | model | served shape | predicted | measured (peak) | drift |
> |---|---|---|---|---|
> | qwen3.5-0.8b | 262144×1 | 2.45 | 5.13 | **+2.68** |
> | qwen3.5-4b | 262144×1 | 7.2 | >12.8 | **guard aborted** |
> | qwen3-vl-30b-q4 | 32768×1 | 20.59 | 22.06 | +1.47 |
> | qwen3-vl-30b | 16384×1 | 33.54 | 33.42 | −0.12 |
> | nemotron-3.5-lightning-30b | 500000×**2** | 56.04 | 39.24 | **−16.80** |
> | qwen3-coder-next | 262144×1 | 60.15 | 53.51 | −6.64 |
> | gpt-oss-120b | 131072×1 | 68.55 | 69.26 | +0.71 |
>
> **The error is bidirectional.** Light at the small end (the 0.8b costs 2.1× its projection),
> accurate in the middle, heavy by 16.8 GB on the one running a 500k window across two slots. This
> is the measurement D4b asked for, and it **confirms D4b's own reasoning**: no multiplicative
> margin can fix a sign that changes across the range. Tuning 1.75 and +2.0 harder would make the
> small end safe only by making the large end unloadable.
>
> Two conclusions that do NOT need more data:
> - **The runaway guard works.** It aborted `qwen3.5-4b` (`GTT 12.8 GB, past the 12.7 GB ceiling
>   for a model predicted at 7.2 GB`) and refused `qwen3-coder-next-q8` at pre-flight in 0 s with
>   zero allocation. Both were correct refusals.
> - **D4a is real and measurable.** The warm phase is where the small end's error lives; the
>   0.8b's 5.13 → 3.83 collapse after slot release is that cost, isolated.
>
> Deliberately NOT acted on: the formula is unchanged. Seven points is thin for fitting an error
> with a per-model and a per-context term, and this projection governs the guard that spent
> 2026-08-19 correctly refusing dangerous loads. A wrong fit makes the box less safe, not more.

Adopt llama.cpp's `no_alloc` dry-run as an **install-time cross-check**, not a
replacement (see §2 for why not a replacement). Store the measured
model/context/compute breakdown per model, alert on divergence from the catalog.
`llama_memory_breakdown_print`'s `unaccounted` column is `fixed_overhead`,
measured. Cache it the way vLLM's `startup_plan.py` does: fingerprint the
config, store `{projected, free_memory_baseline}`, and **refuse to reuse a
cached number when current free memory is below the recorded baseline** — that
gate is what makes a stale cache safe.
- Prerequisite: confirm our pinned image has `no_alloc` (it may not; see W5).


---

# §A — The LLM gateway

# Factual inventory — the LLM gateway (llama-swap), branch `claude/model-loading-chat-indicator-tujwms`

Every row carries `file:line` and a verbatim quote. Repo root `/home/user/JBrain2`.
All paths below are absolute. No conclusions, no recommendations.

---

## 0. THE TWO GATEWAYS (read this first)

`LocalGatewayClient` is constructed against **two different base URLs** in this tree.

**Gateway A — the main model pool** (`settings.local_llm_url`), served by the `local-llm`
compose service running `llama-swap` over `local-models/llama-swap.yaml`:

- `/home/user/JBrain2/backend/src/jbrain/config.py:333`
  `    local_llm_url: str = "http://localhost:11434/v1"`
- `/home/user/JBrain2/deploy/docker-compose.yml:117`
  `      JBRAIN_LOCAL_LLM_URL: ${LOCAL_LLM_URL:-http://local-llm:8080/v1}`
- `/home/user/JBrain2/deploy/docker-compose.yml:394-395`
  ```
      command:
        ["llama-swap", "--listen", ":8080", "--config", "/models/llama-swap.yaml", "--watch-config"]
  ```

**Gateway B — the whisper/STT gateway** (`settings.whisper_url`), a SEPARATE llama-swap
process inside the `tts-stt` container, with a config written by a shell script, not by
`llm/llama_swap_config.py`:

- `/home/user/JBrain2/backend/src/jbrain/config.py:401`
  `    whisper_url: str = ""`
- `/home/user/JBrain2/deploy/tts-stt/entrypoint.sh:11-12`
  ```
  if [ -f /models/llama-swap.yaml ]; then
    /app/llama-swap --config /models/llama-swap.yaml --listen :8080 --watch-config &
  ```
- `/home/user/JBrain2/scripts/whisper-setup.sh:92-111` writes that file, ending with
  `    ttl: 300` (line 110). Gateway A's generated config emits no `ttl` and no
  `globalTTL` (see `render`, §7).
- `/home/user/JBrain2/deploy/docker-compose.yml:471`
  `        WHISPER_BASE: ${WHISPER_IMAGE:-ghcr.io/mostlygeek/llama-swap:vulkan}`

Five `LocalGatewayClient(settings.whisper_url)` constructions exist (main.py:799, 839,
861; worker.py:627, 646, 664). They are marked **Gateway B** in the table and never touch
Gateway A's residency budget, locks, or gpu_probe (there are SIX such constructions, not five as an earlier revision of this section said; none is passed a `gpu_probe`,
`config_regen`, `windows_loader`, `slots_loader` or `models_dir`).

---

## 1. Call-site table

Legend for "gateway instance": how the object is obtained at that line.
"Admission gate before it": what runs on the same code path, before the call, in the
same function or its immediate caller — quoted.

### 1a. LOADS (`gateway.load(...)`)

| # | file:line | verbatim | gateway instance (file:line of wiring) | admission gate before it (file:line) | process |
|---|---|---|---|---|---|
| L1 | `backend/src/jbrain/llm/residency.py:533` | `            await self._gateway.load(served_model)` | `self._gateway` ← ctor arg; `residency.py:177` `        self._gateway = gateway`. Instances: api `main.py:429-430` `app.state.residency = ResidencyCoordinator(` / `            app.state.local_gateway,`; worker `worker.py:539-540` `    residency = ResidencyCoordinator(` / `        llm_gateway,`; default `router.py:882-891` | Same function's caller `_ensure_room_core`: `residency.py:493` `        self._refuse_if_over_box(plan)  # raises before we evict anything` and `residency.py:505-506` `        if load_target and not plan.already_resident:` / `            await self._guarded_load(served_model, plan.target_gb)`. Reached only from `ensure_room` slow path, `residency.py:479-480` `        async with self._box_locked():` / `            await self._ensure_room_core(served_model, load_target=True)` | api + worker (both construct a coordinator) |
| L2 | `backend/src/jbrain/llm/residency.py:645` | `                await self._gateway.load(served)` (inside `_restore`) | same as L1 | Not `ensure_room`/`free_room`. In-function gates quoted verbatim: `residency.py:601-603` `        if not await self._auto_restore():` / `            log.info("residency.restore_disabled", displaced=sorted(self._displaced))` / `            return`; `residency.py:609-610` `        if await self._held_names():` / `            return`; `residency.py:634-635` `            if used + fp > ceiling:` / `                continue  # no room without evicting a resident model — leave it for later`; and the suppression `residency.py:636-637` `            with (` / `                contextlib.suppress(LocalGatewayError, gpu_guard.GpuBudgetError),` | api (only `main.py` fires `schedule_restore`; worker comment `worker.py:537` `    # No schedule_restore here: a background job has no end-of-turn steady state to drift`) |
| L3 | `backend/src/jbrain/llm/warm_keeper.py:182` | `                    await self._gateway.load(served)` | `self._gateway` ← kwarg; `warm_keeper.py:66` `        self._gateway = gateway`; wired `main.py:1164-1165` `        app.state.warm_keeper = WarmKeeper(` / `            gateway=app.state.local_gateway,` | `warm_keeper.py:180-181` `                await self._router.admit_local_load(served)` / `                if served not in await self._gateway.running():`. Also `warm_keeper.py:127-128` `        if held and served not in held:` / `            return True  # code mode owns the box; never load outside its reserved set` and `warm_keeper.py:136-140` `            if not await self._auto_restore_allowed():` … `                return True` | api only (`main.py:1176` `        warm_keeper_task = asyncio.create_task(app.state.warm_keeper.run())`) |
| L4 | `backend/src/jbrain/api/llm_settings.py:1519` | `            await gateway.load(model.served_model, warm_system=warm_system, warm_tools=warm_tools)` (in `gateway_load`) | function param `gateway: LocalGatewayClient` (`llm_settings.py:1492`). Callers pass: owner route `llm_settings.py:1050` `    return await gateway_load(model_id, settings, gateway, registry=registry, liveness=liveness)` with `gateway: LocalGatewayDep` → `llm_settings.py:119-120` `def get_local_gateway(request: Request) -> LocalGatewayClient:` / `    return cast(LocalGatewayClient, request.app.state.local_gateway)`; debug route `debug.py:1366-1369` `    return await llm_settings.gateway_load(` … `        _gateway(request),` → `debug.py:102-103` `def _gateway(request: Request) -> Any:` / `    return request.app.state.local_gateway` | Owner route only: `llm_settings.py:1045-1049` `    if residency is not None:` / `        try:` / `            await residency.free_room(model.served_model)  # evict-to-fit, or refuse if impossible` / `        except ResidencyError as exc:` / `            raise HTTPException(status_code=409, detail=str(exc)) from exc`. **FIXED, and this row is stale.** The debug route now passes `residency=getattr(request.app.state, "residency", None)`, and `gateway_load`'s body begins `await _admit_or_409(residency, model.served_model)` with `residency` a REQUIRED keyword — the admission moved INSIDE the shared helper precisely so a route cannot omit it (commit `2f9904f`). | api |
| L5 | `backend/src/jbrain/api/llm_settings.py:1891` | `        await gateway.load(model.served_model, warm_system=warm_system, warm_tools=warm_tools)` (in `gateway_prime`) | function param `gateway: LocalGatewayClient` (`llm_settings.py:1875`); sole caller `debug.py:1492-1495` `    return await llm_settings.gateway_prime(` / `        model_id,` / `        settings,` / `        _gateway(request),` | **FIXED, and this row is stale.** `gateway_prime` now calls `await _admit_or_409(residency, model.served_model)` immediately after `_require_provisioned`, and its sole caller passes `residency=` (commit `2f9904f`). | api |
| L6 | `backend/src/jbrain/api/jcode.py:168` | `        await gateway.load(served)` (in `_warm_model`) | function param `gateway: LocalGateway` (`jcode.py:145`); caller `jcode.py:180-191` `    gateway = getattr(request.app.state, "local_gateway", None)` … `    task = asyncio.create_task(_warm_model(gateway, served, residency))` | none in `_warm_model` (`jcode.py:153-168`); the in-function guard is `jcode.py:158-159` `        if served in resident:` / `            return` and the preceding unload loop `jcode.py:162-165`. `residency` is used only for `jcode.py:167` `            residency.note_evicted(evicted)  # type: ignore[attr-defined]` | api (background task) |
| L7 | `backend/src/jbrain/llm/smoketest.py:237` | `        await gateway.load(smallest.served_model)` | `gateway: SmokeGateway` param (`smoketest.py:200`); constructed `cli.py:225-235` `    gateway = LocalGatewayClient(` / `        settings.local_llm_url,` / `        gpu_probe=gpu_guard.probe_for(settings),` / `        windows_loader=_windows,` / `        slots_loader=_slots,` / `        models_dir=settings.local_models_dir,` — a **fresh client**, not `app.state` | `smoketest.py:234-235` `    if not await _room_for(smallest, gateway, meminfo, messages):` / `        return False, messages` (a MemAvailable check, `smoketest.py:147-184`) — not residency | CLI (`cli.py:292-293` `    if args.command == "local-llm-smoketest":` / `        return asyncio.run(_local_llm_smoketest())`), run by deploy `update-inner.sh:695` `        python -m jbrain.cli local-llm-smoketest; then` |

Notes on the load chokepoint itself (all of L1–L7 pass through it):

- `backend/src/jbrain/llm/local_gateway.py:648-650`
  ```
          lock = self._load_locks.setdefault(served_model, asyncio.Lock())
          queued = lock.locked()
          async with lock:
  ```
- `backend/src/jbrain/llm/local_gateway.py:673-677`
  ```
              async with self._global_load_lock:
                  self._loading_now = served_model
                  self._loading.add(served_model)
                  try:
                      async with box_events.span(box_events.MODEL_LOAD, served_model):
  ```
- `backend/src/jbrain/llm/local_gateway.py:800-809` (the device gate inside `_load_and_warm`)
  ```
          baseline = await self._gpu_probe.sample()
          gpu_guard.refuse_if_no_device_room(baseline, projected_gb, served_model)
          try:
              await gpu_guard.guarded_load(
                  _do_load,
                  probe=self._gpu_probe,
                  projected_gb=projected_gb,
                  target=served_model,
                  abort=lambda: self.unload(served_model),
              )
  ```
- Two branches skip that gate, quoted:
  `local_gateway.py:757-766`
  ```
          if self._gpu_probe is None:  # no probe wired: the prior, unguarded behaviour
              try:
                  await _do_load()
              finally:
                  ...
                  self._drop_weights_cache(model)
              await self._warm(served_model, system=warm_system, tools=warm_tools)
              return
  ```
  `local_gateway.py:783-789`
  ```
          if served_model in await self.running():
              try:
                  await _do_load()
              finally:
                  self._drop_weights_cache(model)
              await self._warm(served_model, system=warm_system, tools=warm_tools)
              return
  ```
- The HTTP call that actually makes llama-swap load: `local_gateway.py:722`
  `                    resp = await client.get(f"{self._root}/upstream/{served_model}/health")`

### 1b. UNLOADS (`gateway.unload(...)`) and anything that stops a model

| # | file:line | verbatim | gateway instance (wiring) | gate before it | process |
|---|---|---|---|---|---|
| U1 | `backend/src/jbrain/llm/residency.py:502` | `                    await self._gateway.unload(served)` (eviction loop in `_ensure_room_core`) | as L1 | `residency.py:493` `        self._refuse_if_over_box(plan)  # raises before we evict anything`; narration `residency.py:499` `        with box_events.because(f"to make room for {served_model}"):` | api + worker |
| U2 | `backend/src/jbrain/llm/residency.py:589` | `                    await self._gateway.unload(served)` (eviction loop in `free_room`) | as L1 | `residency.py:584` `        self._refuse_if_over_box(plan)  # raises before we evict anything` | api |
| U3 | `backend/src/jbrain/llm/local_gateway.py:808` | `                abort=lambda: self.unload(served_model),` — invoked by `gpu_guard.py:385` `                await abort()` | `self` (the same client the load ran on) | it IS the watchdog: `gpu_guard.py:352-355` `            if breach:` / `                log.error("gpu_guard.aborting_load", model=target, reason=breach)` / `                task.cancel()` / `                break` | whichever process ran the load |
| U4 | `backend/src/jbrain/api/llm_settings.py:833` | `                await gateway.unload(model.served_model)` (in `_unload_if_loaded`) | `gateway: LocalGatewayClient` param (`llm_settings.py:825`); callers all pass `LocalGatewayDep`/`_gateway(request)` (see L4) | `llm_settings.py:831-832` `        if model.served_model in await gateway.running():` / `            with box_events.because("its context window changed — it reloads at the new size"):` | api |
| U5 | `backend/src/jbrain/api/llm_settings.py:908` | `            await gateway.unload(name)` (in `reconcile_gateway_config`) | `gateway: LocalGateway` param (`llm_settings.py:844`); boot caller `main.py:1156-1157` `            await llm_settings_api.reconcile_gateway_windows_on_boot(` / `                settings, settings_store, app.state.local_gateway, SYSTEM_CTX` | `llm_settings.py:882-884` `    with contextlib.suppress(OSError):` / `        if path.read_text() == desired:` / `            return False  # already correct — the common case; leave any resident model warm` | api (startup) |
| U6 | `backend/src/jbrain/api/llm_settings.py:1286` | `                    await gateway.unload(model.served_model)` (set-unavailable route) | `LocalGatewayDep` (`llm_settings.py:1265`) | `llm_settings.py:1282-1285` `    if not body.available:` / `        with contextlib.suppress(LocalGatewayError):` / `            if model.served_model in await gateway.running():` / `                with box_events.because("you marked it unavailable"):` | api |
| U7 | `backend/src/jbrain/api/llm_settings.py:1533` | `            await gateway.unload(model.served_model)` (in `gateway_unload`) | param (`llm_settings.py:1526`); callers `llm_settings.py:1023` `    return await gateway_unload(model_id, settings, gateway)` and `debug.py:1380` `    return await llm_settings.gateway_unload(model_id, settings, _gateway(request))` | `llm_settings.py:1530` `    model = _require_provisioned(settings, model_id)`; narration `llm_settings.py:1532` `        with box_events.because("you unloaded it"):` | api |
| U8 | `backend/src/jbrain/api/jcode.py:164` | `                    await gateway.unload(other)` (evict-everything-else loop) | `gateway` param of `_warm_model` ← `jcode.py:180` `    gateway = getattr(request.app.state, "local_gateway", None)` | `jcode.py:161-163` `        with box_events.because("code mode is taking the box"):` / `            for other in resident:` / `                with contextlib.suppress(LocalGatewayError):` — no residency call | api (background task) |
| U9 | `backend/src/jbrain/api/jcode.py:474` | `            await gateway.unload(served)` (power-OFF) | `jcode.py:467` `    gateway = getattr(request.app.state, "local_gateway", None)` | `jcode.py:465-466` `    if not settings.local_llm_enabled:` / `        return`; `jcode.py:470-473` `        with (` / `            contextlib.suppress(LocalGatewayError),` / `            box_events.because("code mode is giving the box back"),` / `        ):` | api |
| U10 | `backend/src/jbrain/image_gen/render.py:206` | `                await gateway.unload(served)` (in `_free_local_llms`, unloads EVERY resident) | `self._local_gateway` ← ctor arg `render.py:256` `        local_gateway: LocalGateway,` / `render.py:266` `        self._local_gateway = local_gateway`; wired `main.py:742` `                app.state.local_gateway,` (and `main.py:760` for the tool handlers) | none; the loop is `render.py:204-207` `        with box_events.because("an image render needs the whole memory pool"):` / `            for served in await gateway.running():` / `                await gateway.unload(served)` / `                freed.append(served)`. Bookkeeping only: `render.py:210-211` `    if freed and on_evicted is not None:` / `        on_evicted(freed)`, wired `main.py:747` `                on_evicted=app.state.residency.note_evicted,` | api |
| U11 | `backend/src/jbrain/cli.py:73` | `            await gateway.unload(served)` | fresh client `cli.py:62` `    gateway = LocalGatewayClient(settings.local_llm_url, gpu_probe=gpu_guard.probe_for(settings))` | `cli.py:59-70` `    if not settings.local_llm_enabled:` … `    if not loaded:` / `        print("[unload] gateway holds no models")` / `        return 0` | CLI, invoked by deploy: `update-inner.sh:240` `    python -m jbrain.cli local-llm-unload \` and `update-inner.sh:498` `  docker compose run --rm --no-deps -T api python -m jbrain.cli local-llm-unload \` |
| U12 | `backend/src/jbrain/agent/transcribetools.py:146` | `            await gateway.unload(model)` | **Gateway B** — `main.py:799` `                gateway=LocalGatewayClient(settings.whisper_url),` | `transcribetools.py:142-145` `    if gateway is None:` / `        return` / `    try:` / `        with box_events.because("transcription is done with it"):` | api |
| U13 | `backend/src/jbrain/ingest/transcribe_job.py:206` | `                await self._gateway.unload(self._model)` | **Gateway B** — `worker.py:627` `            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,` | `transcribe_job.py:202-205` `        if self._gateway is None:` / `            return` / `        try:` / `            with box_events.because("transcription is done with it"):` | worker |
| U14 | `backend/src/jbrain/ingest/video.py:441` | `            await gateway.unload(model)` | **Gateway B** — `main.py:839`, `main.py:861`, `worker.py:646`, `worker.py:664` | `video.py:437-440` `    if gateway is None or not model:` / `        return` / `    try:` / `        with box_events.because("video transcription is done with it"):` | api + worker |

The unload chokepoint itself:
- `backend/src/jbrain/llm/local_gateway.py:320-331`
  ```
          try:
              async with httpx.AsyncClient(
                  timeout=max(self._timeout, 30.0), transport=self._transport
              ) as client:
                  resp = await client.post(f"{self._root}/api/models/unload/{served_model}")
                  resp.raise_for_status()
          except httpx.HTTPError as exc:
              await box_events.record(
                  box_events.MODEL_UNLOAD, served_model, status="failed", detail=str(exc)
              )
              raise LocalGatewayError(str(exc)) from exc
          await box_events.record(box_events.MODEL_UNLOAD, served_model)
  ```

### 1c. ADMISSION / BUDGET DECISIONS

| # | file:line | verbatim | instance | process |
|---|---|---|---|---|
| A1 | `backend/src/jbrain/llm/router.py:359-361` | `    async def _admit_local(self, provider: str, model: str) -> None:` / `        if provider == local_catalog.LOCAL_PROVIDER and self._residency is not None:` / `            await self._residency.ensure_room(model)` | `self._residency` ← ctor `router.py:353` `        self._residency = residency`; api `main.py:478` `            residency=app.state.residency,`; worker `worker.py:570` `        residency=residency,`; fallback `router.py:857-861` `        residency=(` / `            residency` / `            if residency is not None` / `            else _default_residency (**DELETED** — `build_router` now takes `residency` as a required keyword-only argument; the fallback that made a silently weaker gate is gone, commit `c76288f`)(settings, local_windows_loader)` / `        ),` | api + worker |
| A2 | `backend/src/jbrain/llm/router.py:619` | `        await self._admit_local(provider, model)` (in `complete`) | as A1 | api + worker |
| A3 | `backend/src/jbrain/llm/router.py:696` | `        await self._admit_local(provider, model)` (in `converse`) | as A1 | api + worker |
| A4 | `backend/src/jbrain/llm/router.py:747` | `        await self._admit_local(provider, model)` (in `converse_stream`) | as A1 | api + worker |
| A5 | `backend/src/jbrain/llm/router.py:363-372` | `    async def admit_local_load(self, served_model: str) -> None:` … `        await self._admit_local(local_catalog.LOCAL_PROVIDER, served_model)` | as A1; sole caller `warm_keeper.py:180` | api |
| A6 | `backend/src/jbrain/api/jcode_llm.py:174-180` | `                if residency is not None:` / `                    try:` / `                        await residency.ensure_room(served)` / `                    except ResidencyError:` / `                        raise` / `                    except Exception:  # noqa: BLE001 - housekeeping never fails a completion` / `                        log.warning("jcode-llm ensure_room failed model=%s", served, exc_info=True)` | `jcode_llm.py:153` `    residency = getattr(request.app.state, "residency", None)` | api |
| A7 | `backend/src/jbrain/api/llm_settings.py:1047` | `            await residency.free_room(model.served_model)  # evict-to-fit, or refuse if impossible` | `residency: ResidencyDep` → `llm_settings.py:126-129` `def get_residency(request: Request) -> ResidencyCoordinator | None:` … `    return cast(ResidencyCoordinator | None, getattr(request.app.state, "residency", None))` | api |
| A8 | `backend/src/jbrain/api/llm_settings.py:1306` | `    plan = await residency.plan_load(model.served_model) if residency is not None else None` (dry run, no side effects) | `ResidencyDep` (`llm_settings.py:1296`) | api |
| A9 | `backend/src/jbrain/llm/residency.py:460-468` | `        held = await self._held_names()` / `        if held and served_model not in held:` / `            with contextlib.suppress(Exception):` / `                if served_model in await self._gateway.running():` / `                    return  # already resident — serving it needs no load` / `            raise ResidencyError(` / `                f"Code mode is holding the box for {sorted(held)}. Turn code mode off to run "` / `                "other models (chat, vision, or background research)."` / `            )` | coordinator | api + worker |
| A10 | `backend/src/jbrain/llm/residency.py:427-436` | `    def _refuse_if_over_box(self, plan: EvictionPlan) -> None:` … `        if plan.over_box:` / `            raise ResidencyError(` | coordinator | api + worker |
| A11 | `backend/src/jbrain/llm/gpu_guard.py:233-289` | `def refuse_if_no_device_room(` … `    raise GpuBudgetError(` (line 285) | called only at `local_gateway.py:801` | any process that loads |
| A12 | `backend/src/jbrain/llm/gpu_guard.py:292-390` | `async def guarded_load(` … `        ceiling_gb = baseline.gtt_used_gb + max(projected_gb * RUNAWAY_MULTIPLE, projected_gb + 2.0)` (line 330) … `    if breach is not None:` / `        raise GpuBudgetError(` (386-387) | called only at `local_gateway.py:803` | any process that loads |
| A13 | `backend/src/jbrain/llm/residency.py:508-545` | `    async def _guarded_load(self, served_model: str, projected_gb: float) -> None:` … `            await self._gateway.load(served_model)` (533) — the name is historical; the body contains no gate, only `probe = self._gpu_probe` (523) / `baseline = await probe.sample() if probe is not None else None` (524) and the post-load `measure_footprint` (538) | coordinator | api + worker |
| A14 | `backend/src/jbrain/llm/smoketest.py:147-184` | `async def _room_for(` … `    if available_gb >= cost + LOAD_HEADROOM_GB:` / `        return True` (176-177) — a `/proc/meminfo` check, no residency | fresh client from `cli.py:225` | CLI |
| A15 | `backend/src/jbrain/workflow/preconditions.py:64-70` | `    async def check() -> PreconditionResult:` / `        provider, model = await router.effective_spec(task, strength)` / `        if provider != "local":` / `            return PreconditionResult(met=True)` / `        if model in await gateway.running():` / `            return PreconditionResult(met=True)` / `        return PreconditionResult(met=False, reason=f"local model {model!r} not loaded")` | `worker.py:786` `        "reasoning_model_loaded": model_already_loaded(router, llm_gateway, task="triage.classify"),` → `worker.py:516` client | worker |
| A16 | `backend/src/jbrain/api/external_llm.py:221-222` | `    if served not in resident:` / `        raise HTTPException(status_code=503, detail="the coder model is not loaded")` | `external_llm.py:213` `    gateway = getattr(request.app.state, "local_gateway", None)` | api |
| A17 | `backend/src/jbrain/llm/local_gateway.py:391-402` | `    async def _require_resident(self, served_model: str, what: str) -> None:` … `        if served_model not in await self.running():` / `            raise LocalGatewayError(` | the client itself; guards `props`/`slots`/`metrics` | api + worker |
| A18 | `backend/src/jbrain/llm/ledger.py:165` | `    async def charge(self, served_model: str, *, host_gb: float, device_gb: float) -> Charge:` — decide and, if admitted, INSERT the reservation row in one transaction under the box lock; the verdict is `admission.admit` (`admission.py:185`). **AUTHORITATIVE since 2026-08-23** (`docs/plans/LOCAL_MODEL_LEDGER_PLAN.md` L2b): `shadow=False` in BOTH processes, pinned by an AST test. A box-lock timeout or DB hiccup on the charge refuses transiently as `GpuBudgetError` (`ledger.py:191-208`), never a 500 | one `ReservationLedger` per process — `main.py:422-424` (`source="api"`), `worker.py:590-592` (`source="worker"`) — charged through by `LocalGatewayClient` (`main.py:428`, `worker.py:599`) | api + worker |
| A19 | `backend/src/jbrain/llm/residency.py:586` | `    async def _plan_ledger(self, served_model: str, *, narrate_skip: bool) -> EvictionPlan \| None:` — the eviction plan simulates evictions until `admission.admit` says yes, over the ledger's own pools/rows: the SAME arithmetic A18's charge applies, so plan and charge cannot disagree. The measured planner survives only as `_plan_measured`, the fallback for a build with no ledger or a failed ledger read (`residency.py:577-584`) | coordinator; `ledger=` wired `main.py:483` / `worker.py:668` | api + worker |

**A9–A14 are the PRE-LEDGER layer.** They still run, but since the ledger flipped
authoritative (2026-08-23) the admitting arithmetic on the load path is A18/A19: A9/A10
feed from A19's plan, A11/A12 remain the device-side pre-flight/watchdog on the gateway
chokepoint, A13 never was a gate (its row says so), and A14 is the CLI's own
`/proc/meminfo` check, untouched by the ledger until L3.

### 1d. WARM / PRIME / RESTORE

| # | file:line | verbatim | instance | gate | process |
|---|---|---|---|---|---|
| W1 | `backend/src/jbrain/llm/local_gateway.py:835-841` + `901-903` | `    async def _warm(` … `                    resp = await client.post(` / `                        f"{self._root}/upstream/{served_model}/v1/chat/completions", json=body` / `                    )` | inside `load` | runs after every load branch (`local_gateway.py:765`, `788`, `826`) | any |
| W2 | `backend/src/jbrain/llm/local_gateway.py:914-954` | `    async def tool_probe(self, served_model: str) -> None:` … `                resp = await client.post(` / `                    f"{self._root}/upstream/{served_model}/v1/chat/completions", json=body` | fresh CLI client | called `smoketest.py:246` `        await gateway.tool_probe(smallest.served_model)` and `smoketest.py:270` `                await gateway.tool_probe(probe.served_model)` | CLI |
| W3 | `backend/src/jbrain/llm/warm_keeper.py:189-195` | `            await self._router.converse(` / `                AGENT_TURN_TASK,` / `                system=system,` / `                messages=[UserMessage(text="warmup")],` / `                tools=tools,` / `                max_tokens=1,` / `            )` | `self._router` ← `warm_keeper.py:72` `        self._router = router`; wired `main.py:1168` `            router=app.state.llm_router,` | A3 (`router.py:696`) runs inside `converse` | api |
| W4 | `backend/src/jbrain/llm/warm_keeper.py:208-218` | `    async def run(self) -> None:` … `            await asyncio.sleep(self._interval_ready if settled else self._interval_wait)` | task `main.py:1176` | — | api |
| W5 | `backend/src/jbrain/llm/residency.py:255-265` | `    def schedule_restore(self) -> None:` … `        task = asyncio.create_task(self._restore())` | coordinator | `residency.py:259` `        if not self._enabled or self._tasks:` | api |
| W6 | `backend/src/jbrain/api/agent.py:1115-1117` | `                residency = getattr(request.app.state, "residency", None)` / `                if residency is not None:` / `                    residency.schedule_restore()` | app.state | end of chat turn | api |
| W7 | `backend/src/jbrain/api/jcode.py:475-477` | `    residency = getattr(request.app.state, "residency", None)` / `    if residency is not None:` / `        residency.schedule_restore()` | app.state | code power-OFF | api |
| W8 | `backend/src/jbrain/llm/residency.py:243-253` | `    def note_evicted(self, served_names: Iterable[str]) -> None:` … `                self._displaced.add(name)` / `                self._prefix_lost(name)` | coordinator | callers: `main.py:747` `                on_evicted=app.state.residency.note_evicted,`; `jcode.py:167` | api |
| W9 | `backend/src/jbrain/llm/warm_keeper.py:104-113` | `    def note_prefix_lost(self, served_model: str) -> None:` … `        if self._primed is not None and self._primed[0] == served_model:` / `            self._primed = None` | wired `main.py:455` `            on_prefix_lost=_prefix_lost_notifier(app),` → `main.py:245-248` `    def notify(served_model: str) -> None:` / `        keeper = getattr(app.state, "warm_keeper", None)` / `        if keeper is not None:` / `            keeper.note_prefix_lost(served_model)` | — | api |
| W10 | `backend/src/jbrain/llm/prefill.py:54` + `router.py:761-766` | `SlotsReader = Callable[[str], Awaitable[list[dict[str, object]]]]` ; `            prefill.watch(` / `                probe,` / `                model,` / `                prompt_chars=prompt_chars,` / `                on_progress=publish if probe is not None else None,` / `            ) as streaming,` | `main.py:481` `            slots_probe=app.state.local_gateway.slots,`; `worker.py:574` `        slots_probe=llm_gateway.slots,` | reads only; `slots` runs A17 | api + worker |
| W11 | `backend/src/jbrain/llm/kv_prefix.py:198` | `    async def save_after_prime(` — persist the freshly primed slot's KV state to disk, only when the slot's `n_prompt_tokens` exactly equals the prime's own token count (v1 saved garbage; the module docstring is the post-mortem). Caller: `warm_keeper.py:243` | `KvPrefixStore` — api only, `main.py:494` `        app.state.kv_prefix = KvPrefixStore(app.state.local_gateway, settings.local_models_dir)` | best-effort, never raises | api |
| W12 | `backend/src/jbrain/llm/kv_prefix.py:340` | `    async def restore_if_lost(` — stream the saved slot back (~2 s vs ~60 s prefill) when the primed prefix is missing. Callers: the keeper's tick and prime (`warm_keeper.py:171`, `:219`), the router before an `agent.turn` (`router.py:402`), and — added #1195 — the load-time warm hook: `_warm_identity` (`api/llm_settings.py:1536-1571`) wires it as `before_warm`, so a Load's prime meets a restored cache instead of re-prefilling | as W11 | conservative threshold gate: a slot holding at least a prefix-sized cache is never wiped | api |
| W13 | `backend/src/jbrain/llm/kv_prefix.py:123` | `def _fingerprint(` — sha256 over the rendered launch line + system text + tool schema + `reasoning_effort` (effort keyed in by #1195: it is part of the RENDERED prompt, so it must move the filename). A stale file is never matched again and ages out of the byte budget (see §B.5 `MAX_STORE_BYTES`) | module fn | identity, not a gate | api |

### 1e. RESIDENCY-STATE READS (`running()` and friends)

| file:line | verbatim | instance | process |
|---|---|---|---|
| `backend/src/jbrain/llm/local_gateway.py:205-222` | `    async def running(self) -> set[str]:` … `                resp = await client.get(f"{self._root}/running")` … `        self._drop_cache_for_unannounced(resident)` / `        return resident` | the client | any |
| `backend/src/jbrain/llm/local_gateway.py:1182-1204` | `def _parse_running(payload: object) -> set[str]:` … `        items = next(` / `            (payload[k] for k in ("running", "models", "data") if isinstance(payload.get(k), list)),` | module fn | any |
| `backend/src/jbrain/llm/local_gateway.py:224-288` | `    def _drop_cache_for_unannounced(self, resident: set[str]) -> None:` … `        arrived = resident - self._seen_resident` (271) … `        for served in sorted(arrived - self._loaded_here - self._loading):` (274) … `        self._loaded_here &= resident` (288) | the client | any |
| `backend/src/jbrain/llm/residency.py:345` | `        running = await self._gateway.running()` (in `_plan`) | coordinator | api + worker |
| `backend/src/jbrain/llm/residency.py:463` | `                if served_model in await self._gateway.running():` (code-mode hold check) | coordinator | api + worker |
| `backend/src/jbrain/llm/residency.py:474-476` | `            if served_model in await self._gateway.running():` / `                self._displaced.discard(served_model)` / `                return` (lock-free fast path) | coordinator | api + worker |
| `backend/src/jbrain/llm/residency.py:614` | `        running = await self._gateway.running()` (in `_restore`) | coordinator | api |
| `backend/src/jbrain/llm/warm_keeper.py:130` | `            running = await self._gateway.running()` | keeper | api |
| `backend/src/jbrain/llm/warm_keeper.py:181` | `                if served not in await self._gateway.running():` | keeper | api |
| `backend/src/jbrain/api/llm_settings.py:161` | `    return {_SERVED_TO_ID[s] for s in await gateway.running() if s in _SERVED_TO_ID}` (`_loaded_ids`) | `LocalGatewayDep` | api |
| `backend/src/jbrain/api/llm_settings.py:463` | `    loaded = await _loaded_ids(settings, gateway)` (in `_snapshot`) | `LocalGatewayDep` | api |
| `backend/src/jbrain/api/llm_settings.py:831` | `        if model.served_model in await gateway.running():` | `LocalGatewayDep` | api |
| `backend/src/jbrain/api/llm_settings.py:899` | `        running = await gateway.running()` | boot param | api |
| `backend/src/jbrain/api/llm_settings.py:1284` | `            if model.served_model in await gateway.running():` | `LocalGatewayDep` | api |
| `backend/src/jbrain/api/jcode.py:154` | `        resident = await gateway.running()` | app.state | api |
| `backend/src/jbrain/api/jcode.py:274` | `        running = await cast("LocalGateway", gateway).running()` | app.state | api |
| `backend/src/jbrain/api/external_llm.py:218` | `        resident = await gateway.running()` | app.state | api |
| `backend/src/jbrain/image_gen/render.py:205` | `            for served in await gateway.running():` | ctor arg (app.state) | api |
| `backend/src/jbrain/llm/smoketest.py:162` | `        resident = await gateway.running()` (`_room_for`) | CLI client | CLI |
| `backend/src/jbrain/llm/smoketest.py:260` | `            resident = await gateway.running()` | CLI client | CLI |
| `backend/src/jbrain/cli.py:64` | `        loaded = await gateway.running()` | CLI client | CLI |
| `backend/src/jbrain/workflow/preconditions.py:68` | `        if model in await gateway.running():` | `worker.py:516` client | worker |
| `backend/src/jbrain/llm/local_gateway.py:354` | `            for served in sorted(before - await self.running() - {loading}):` (`_narrate_reload_casualties`) | the client | any |
| `backend/src/jbrain/llm/local_gateway.py:397` | `        if served_model not in await self.running():` (`_require_resident`) | the client | any |
| `backend/src/jbrain/llm/local_gateway.py:651` | `            if queued and served_model in await self.running():` | the client | any |
| `backend/src/jbrain/llm/local_gateway.py:738` | `            resident_before = await self.running()` | the client | any |
| `backend/src/jbrain/llm/local_gateway.py:783` | `        if served_model in await self.running():` | the client | any |

### 1f. INDIRECT LOADS — a model becomes resident with no `.load()` in our code

| # | mechanism | our-side quote | llama-swap-side confirmation (pin `60226b63776efac11e15828abe0bb302ec259699`, tag `v250`) |
|---|---|---|---|
| I1 | An ordinary completion POSTed at the gateway's OpenAI endpoint makes llama-swap start the process | `backend/src/jbrain/llm/router.py:841-847` `        "local": OpenAiCompatClient(` / `            settings.local_llm_url,` / `            "",` / `            provider="local",` / `            timeout=settings.local_llm_timeout,` / `            **extra,` / `        ),` and `backend/src/jbrain/llm/openai_compat.py:166` `            f"{self._base_url}/chat/completions",` | `internal/router/base.go:455` `func (b *baseRouter) ServeHTTP(w http.ResponseWriter, req *http.Request) {` → `internal/router/base.go:264-265` `	target := b.processes[modelID]` / `	err := target.EnsureReady(b.shutdownCtx, timeout)` |
| I2 | jcode proxy forwards a completion for the caller's chosen model | `backend/src/jbrain/api/jcode_llm.py:183-185` `                async with client.stream("POST", "/chat/completions", json=payload) as upstream:` / `                    async for chunk in upstream.aiter_raw():` / `                        yield chunk` (base_url `jcode_llm.py:163` `    client = factory(base_url=gateway_url.rstrip("/"), timeout=_TIMEOUT)`) | same as I1 |
| I3 | external (remote coder) proxy forwards a completion | `backend/src/jbrain/api/external_llm.py:240` `            async with client.stream("POST", upstream_path, json=payload) as upstream:` | same as I1; gated by A16 first |
| I4 | any `/upstream/<model>/...` GET, including the load health probe and the diagnostics | `backend/src/jbrain/llm/local_gateway.py:722` `                    resp = await client.get(f"{self._root}/upstream/{served_model}/health")`; `local_gateway.py:410` `                resp = await client.get(f"{self._root}/upstream/{served_model}{path}")` | `internal/server/api.go:479-481` `// handleUpstream proxies ANY request under /upstream/<model>/<path> directly to` / `// the model's process, bypassing model dispatch by body/query inspection.` / `func (s *Server) handleUpstream(w http.ResponseWriter, r *http.Request) {`, then `internal/server/server.go:311` `	mux.Handle("/upstream/{upstreamPath...}", upstreamChain.ThenFunc(s.handleUpstream))` |
| I5 | Rewriting `llama-swap.yaml` makes llama-swap reload, and the reload **stops every running llama-server** | `backend/src/jbrain/llm/llama_swap_config.py:498-500` `    with open(tmp, "w") as f:` / `        f.write(text)` / `    os.replace(tmp, path)` (only reached when content differs: `llama_swap_config.py:494-496` `    with contextlib.suppress(OSError):` / `        if pathlib.Path(path).read_text() == text:` / `            return path`) | poll: `internal/watcher/watcher.go:17` `const DefaultInterval = 2 * time.Second`; `internal/watcher/watcher.go:84` `	return !prev.modTime.Equal(cur.modTime) || prev.size != cur.size`; wired `llama-swap.go:284` `		proxyLog.Info("watching configuration for changes (poll-based, 2s interval)")` and `llama-swap.go:293-296` `				(&configwatcher.Watcher{` / `					Path:     absConfigPath,` / `					Interval: configwatcher.DefaultInterval,` / `					OnChange: reload,`; reload body `llama-swap.go:253-263` `		activeMu.Lock()` / `		old := activeSrv` / … / `		activeSrv = newSrv` / … / `		if err := old.Shutdown(shutdownTimeout); err != nil {` |
| I6 | Same reload can be triggered by SIGHUP | — (no jbrain code sends it) | `llama-swap.go:346-348` `			case syscall.SIGHUP:` / `				proxyLog.Info("received SIGHUP, reloading config")` / `				go reload()` |
| I7 | llama-swap start-up preload | Gateway A's generated config emits only `models:` and `groups:` — see `llama_swap_config.py:221` `    lines = ["# Generated by jbrain.llm.llama_swap_config — do not edit by hand.", "models:"]` and `llama_swap_config.py:436-441` (`groups:` block). No `hooks:` key is emitted anywhere in `render`. | `internal/server/api.go:375-379` `func (s *Server) startPreload() {` / `	models := s.cfg.Hooks.OnStartup.Preload` / `	if len(models) == 0 {` / `		return` / `	}` |
| I8 | TTL idle-unload (a stop, not a load) | Gateway A: `render` emits no `ttl:` / `globalTTL:`. Gateway B: `scripts/whisper-setup.sh:110` `    ttl: 300` | default `internal/config/load.go:47` `		GlobalTTL:          0,`; the TTL goroutine only starts at `internal/process/process_command.go:306` `					if p.config.UnloadAfter > 0 {` |
| I9 | Group auto-eviction (does NOT happen for Gateway A's group) | `backend/src/jbrain/llm/llama_swap_config.py:436-439` `        lines.append("groups:")` / `        lines.append("  resident:")` / `        lines.append("    swap: false")` / `        lines.append("    exclusive: false")` | `internal/router/group.go:84-97` `		switch {` / `		case og == tg && tgCfg.Swap:` / … / `		case og != tg && tgCfg.Exclusive:` — with `Swap:false` and `Exclusive:false` neither arm is taken |
| I10 | Container restart / removal (deploy) | `deploy/update-inner.sh:507` `  docker compose --profile local-llm rm -sf local-llm || true`; `deploy/update-inner.sh:788` `  docker compose --profile local-llm up -d local-llm || true`; `deploy/local-models-sync.sh:258` `  docker compose --profile local-llm up -d`; compose `deploy/docker-compose.yml:389` `    restart: unless-stopped` | — |

Our-side handling of I5 (present in the load path):
- `backend/src/jbrain/llm/local_gateway.py:737-749`
  ```
          if self._config_regen is not None:
              resident_before = await self.running()
              try:
                  await self._config_regen()
              except Exception as exc:  # noqa: BLE001 — a stale config must never FAIL a load
                  ...
                  log.warning("local_gateway.config_regen_failed", model=served_model, error=str(exc))
              else:
                  await self._narrate_reload_casualties(resident_before, served_model)
  ```
- `backend/src/jbrain/api/llm_settings.py:719-739`
  ```
      path = Path(settings.local_models_dir or ".") / "llama-swap.yaml"
      before = path.stat().st_mtime_ns if path.exists() else None
      _try_regenerate(settings, windows, slots, extra, floors)
      after = path.stat().st_mtime_ns if path.exists() else None

      if before != after:
          ...
          await asyncio.sleep(_GATEWAY_RELOAD_SETTLE_S)
  ```
  with `backend/src/jbrain/api/llm_settings.py:781` `_GATEWAY_RELOAD_SETTLE_S = 4.0`.

### 1g. CONFIG / LAUNCH SURFACE (what determines the launch line)

All of Gateway A's per-model command comes from one function:

- `backend/src/jbrain/llm/llama_swap_config.py:193-201`
  ```
  def render(
      models: Sequence[Mapping[str, object]],
      root: str,
      *,
      windows: Mapping[str, int] | None = None,
      slots: Mapping[str, int] | None = None,
      extra_args: Mapping[str, Sequence[str]] | None = None,
      image_min_tokens: Mapping[str, int] | None = None,
  ) -> str:
  ```
- context window / slot arithmetic — `llama_swap_config.py:226` `        window = windows.get(model_id, int(cast(int, m["context_window"])))`;
  `llama_swap_config.py:234` `        n_slots = max(1, slots.get(model_id, 1))`;
  `llama_swap_config.py:264-265` `            "-c",` / `            str(window * n_slots),`;
  `llama_swap_config.py:388` `        cmd += ["-np", str(n_slots)]`
- speculation vs slots — `llama_swap_config.py:250-254`
  ```
          speculative = _is_speculative(catalog_args + operator_args)
          if speculative and n_slots > 1:
              catalog_args = tuple(_drop_speculative(catalog_args))
              operator_args = tuple(_drop_speculative(operator_args))
              speculative = False
  ```
- fixed flags: `"--jinja"` (272), `"-fa"`/`"1"` (275-276), `"--no-mmap"` (277), `"-cram"`/`"0"` (302-303), `"--slots"` (329), `"--metrics"` (330), `"-ub"`/`"1024"` (340-341), `--ctx-checkpoints` (369-370), `--checkpoint-min-step` (374-375), `-ngl 999` (378-379)
- conditional: `llama_swap_config.py:395-396` `        if not m.get("recurrent"):` / `            cmd += ["--cache-reuse", "256"]`; `401-403` reasoning-format; `404-406` `--mmproj`; `409-410` `        if m.get("kv_full_history"):` / `            cmd.append("--swa-full")`; `415-416` `        if floor is not None:` / `            cmd += ["--image-min-tokens", str(floor)]`
- operator args last, de-duplicated: `llama_swap_config.py:417-422`
  ```
          cmd += catalog_args
          # Operator overrides last, so they append to (never reorder) the catalog's own flags —
          # and anything they override is stripped from what came before, so each flag appears
          # exactly once whether its value came from this module, the catalog, or the operator.
          cmd = _drop_operator_overridden(cmd, operator_args)
          cmd += operator_args
  ```
- group/swap semantics: `llama_swap_config.py:434-441`
  ```
      resident = [str(m["served_model"]) for m in models]
      if resident:
          lines.append("groups:")
          lines.append("  resident:")
          lines.append("    swap: false")
          lines.append("    exclusive: false")
          lines.append("    members:")
          lines += [f"      - {name}" for name in resident]
  ```
- ports: `llama_swap_config.py:51` `UPSTREAM_PORT_BASE = 9100`; `llama_swap_config.py:223` `        port = UPSTREAM_PORT_BASE + i`
- writers of the file:
  1. `llama_swap_config.py:636-638` (`_main`, the deploy/CLI path)
     ```
      path = write(
          root, models, windows=windows, slots=slots, extra_args=extra, image_min_tokens=floors
      )
     ```
     invoked at `deploy/local-models-sync.sh:224` `    api python -m jbrain.llm.llama_swap_config /data/local-models` and
     `scripts/local-llm-setup.sh:139` `  api python -m jbrain.llm.llama_swap_config /data/local-models`
  2. `backend/src/jbrain/api/llm_settings.py:799-806` (`_try_regenerate`) `        llama_swap_config.write(` …, reached from `regen_gateway_config` (`llm_settings.py:721` `    _try_regenerate(settings, windows, slots, extra, floors)`), which is wired as the gateway's `config_regen` at `main.py:418` `            config_regen=lambda: llm_settings_api.regen_gateway_config(settings, settings_store),`
  3. `backend/src/jbrain/api/llm_settings.py:885-892` (`reconcile_gateway_config`) `    llama_swap_config.write(` …, reached from `reconcile_gateway_windows_on_boot` (`llm_settings.py:929-937`), called at `main.py:1156-1157`
- read-back for a DB-less caller: `llama_swap_config.py:504` `def served_shape_from_config(root: str) -> dict[str, tuple[int, int]]:`, used at `cli.py:212` `    _shapes = llama_swap_config.served_shape_from_config(settings.local_models_dir)`
- the served (window, slots) the load guard budgets against: `local_gateway.py:570-583` `    async def _served_shape(self, model: local_catalog.LocalModel) -> tuple[int, int]:` … `            if self._windows_loader is not None:` / `                window = (await self._windows_loader()).get(model.id, window)` / `            if self._slots_loader is not None:` / `                slots = (await self._slots_loader()).get(model.id, slots)`, consumed at `local_gateway.py:698-699` `            window, slots = await self._served_shape(model)` / `            projected_gb = local_catalog.load_footprint_gb(model, window, slots=slots)`

---

## 2. Entry points

### 2.1 HTTP — owner settings API (`backend/src/jbrain/api/llm_settings.py`)

| route | line | what it touches |
|---|---|---|
| `@router.get("/settings/llm")` | 940 | `_snapshot` → `_loaded_ids` → `running()` (163, 461) |
| `@router.put("/settings/llm")` | 1902 | `apply_overrides` → `_snapshot` (running()) |
| `@router.put("/settings/llm/jcode-model")` | 958 | `_snapshot` |
| `@router.put("/settings/llm/jcode-planner")` | 989 | `_snapshot` |
| `@router.post("/settings/llm/local-models/{model_id}/unload")` | 1013 | U7 |
| `@router.post("/settings/llm/local-models/{model_id}/load")` | 1026 | A7 then L4 |
| `@router.put("/settings/llm/local-models/{model_id}/context-window")` | 1060 | U4 |
| `@router.put("/settings/llm/local-models/{model_id}/image-min-tokens")` | 1111 | U4 (`llm_settings.py:1139` `    await _unload_if_loaded(settings, gateway, model)`) |
| `@router.put("/settings/llm/local-models/{model_id}/parallel-slots")` | 1156 | U4 (`llm_settings.py:1176`) |
| `@router.put("/settings/llm/free-ram-fraction")` | 1196 | `_snapshot` |
| `@router.put("/settings/llm/auto-restore")` | 1228 | `_snapshot`; the switch read by W5/L2 and by the keeper |
| `@router.put("/settings/llm/local-models/{model_id}/available")` | 1258 | U6 |
| `@router.post("/settings/llm/local-models/{model_id}/plan-load")` | 1290 | A8 (dry run) |
| `@router.post/delete(".../install")`, `.../uninstall` | 1344, 1371, 1388, 1415 | `_snapshot` only |

Route bodies quoted where load/unload happens are in §1a/§1b (L4, U4, U6, U7).

### 2.2 HTTP — owner debug console (`backend/src/jbrain/api/debug.py`)

- `debug.py:1344` `@router.get("/llm")` → `llm_settings.snapshot(..., _gateway(request))` (1346)
- `debug.py:1349` `@router.put("/llm")` → `apply_overrides` (1356-1358)
- `debug.py:1361` `@router.post("/llm/local-models/{model_id}/load")` → L4 with **no residency argument** (1366-1372)
- `debug.py:1375` `@router.post("/llm/local-models/{model_id}/unload")` → U7 (1380)
- `debug.py:1398` `@router.put("/llm/local-models/{model_id}/extra-args")` → `set_local_extra_args` → U4 (`llm_settings.py:1827`)
- `debug.py:1423` `@router.put("/llm/local-models/{model_id}/context-window")` → U4
- `debug.py:1438` `@router.get("/llm/local-models/{model_id}/props")` → A17
- `debug.py:1451` `@router.get("/llm/local-models/{model_id}/slots")` → A17
- `debug.py:1467` `@router.get("/llm/local-models/{model_id}/metrics")` → A17
- `debug.py:1481` `@router.post("/llm/local-models/{model_id}/prime")` → L5
- `debug.py:1109` `        full = await _gateway(request).tail_logs()`
- `debug.py:1144` `        full = await _gateway(request).tail_upstream_logs(stream)`
- `debug.py:1151` `async def drop_page_cache(` … `debug.py:1177` `    freed = _gateway(request).drop_page_cache(ids)`

### 2.3 HTTP — code mode (`backend/src/jbrain/api/jcode.py`)

- `jcode.py:314` `@router.get("/jcode/model")` → `_model_payload` → `running()` (274)
- `jcode.py:324` `@router.post("/jcode/model/warm")` → `jcode.py:330` `    _warm_coder(request, model_id)` → task → U8 + L6
- `jcode.py:480` `@router.post("/jcode/power")` → ON: sets the code-mode hold (`jcode.py:499-500` `        with contextlib.suppress(Exception):` / `            executor = _served_model(await _resolve_model(request, owner.id))`); OFF: `_free_coder_and_restore` → U9 + W7

### 2.4 HTTP — jcode LLM proxy (`backend/src/jbrain/api/jcode_llm.py`)

- `jcode_llm.py:105` `@router.get("/jcode/llm/v1/models")`
- `jcode_llm.py:131` `@router.post("/jcode/llm/v1/chat/completions")` → A6 then I2, both under the swap lock

### 2.5 HTTP — external coder proxy (`backend/src/jbrain/api/external_llm.py`)

- `external_llm.py:201` `async def _proxy(request: Request, sid: str, upstream_path: str, *, meter: bool) -> Response:` → A16 then I3

### 2.6 HTTP — chat turn (`backend/src/jbrain/api/agent.py`)

- the turn itself routes through `LlmRouter` (A2/A3/A4); at the end `agent.py:1117` `                    residency.schedule_restore()`

### 2.7 Startup hooks (`backend/src/jbrain/main.py`, inside `lifespan`)

- `main.py:390-391` `        with suppress(Exception):` / `            await settings_store.set_code_mode_hold_names(SYSTEM_CTX, [])`
- `main.py:1155-1158`
  ```
          with suppress(Exception):
              await llm_settings_api.reconcile_gateway_windows_on_boot(
                  settings, settings_store, app.state.local_gateway, SYSTEM_CTX
              )
  ```
- `main.py:1176` `        warm_keeper_task = asyncio.create_task(app.state.warm_keeper.run())`
- shutdown `main.py:1184` `        warm_keeper_task.cancel()`; `main.py:1209-1210` `        warm_tasks = list(getattr(app.state, "jcode_warm_tasks", set()))` / `        for wt in warm_tasks:`

### 2.8 Worker (`backend/src/jbrain/worker.py`)

- the router (A2/A3/A4) on every background LLM job
- precondition A15 at `worker.py:785-787`
- no `schedule_restore`: `worker.py:537-538` `    # No schedule_restore here: a background job has no end-of-turn steady state to drift` / `    # back to, and the next on-demand load re-admits through ensure_room regardless.`

### 2.9 CLI (`backend/src/jbrain/cli.py`)

- `cli.py:288-289` `    if args.command == "local-llm-unload":` / `        return asyncio.run(_local_llm_unload())` → U11
- `cli.py:292-293` `    if args.command == "local-llm-smoketest":` / `        return asyncio.run(_local_llm_smoketest())` → L7 + W2
- `cli.py:290-291` `    if args.command == "local-llm-auto-update":` / `        return asyncio.run(_print_auto_update())`

### 2.10 Deploy scripts

- `deploy/update-inner.sh:238-242` `release_models() {` / `  run_bounded "$TOGGLE_TIMEOUT_S" docker compose run --rm --no-deps -T api \` / `    python -m jbrain.cli local-llm-unload \` / `    || echo "[update] unload skipped (gateway unreachable?)"` / `}`
- `deploy/update-inner.sh:498` (second unload before stopping the gateway), `:507` (`rm -sf local-llm`), `:685-686` (rebuild + `up -d local-llm`), `:695` (smoketest), `:715-717` (rollback rebuild), `:788` (final `up -d`)
- `deploy/update-inner.sh:210` `    echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true`
- `deploy/local-models-sync.sh:224` (config re-stamp), `:258` `  docker compose --profile local-llm up -d`
- `scripts/local-llm-setup.sh:139` (config write), `:167-168` `docker compose --profile local-llm build local-llm` / `docker compose --profile local-llm up -d`
- `scripts/whisper-setup.sh:92` `cat > "$MODELS_DIR/llama-swap.yaml" <<EOF` (Gateway B)

### 2.11 Scheduled / periodic

- WarmKeeper loop, `warm_keeper.py:211-218`
  ```
          while True:
              settled = True
              try:
                  settled = await self.reconcile_once()
              except Exception:  # noqa: BLE001 — one bad tick must never kill the keeper
                  log.warning("warm_keeper.tick_failed", exc_info=True)
                  settled = False
              await asyncio.sleep(self._interval_ready if settled else self._interval_wait)
  ```
  cadences `warm_keeper.py:63-64` `        interval_ready: float = 60.0,` / `        interval_wait: float = 5.0,` (main.py passes neither, so both defaults apply)
- Worker job queue evaluating A15 with `preconditions.py:30` `RETRY_AFTER = timedelta(minutes=5)`

---

## 3. Wiring

### 3.1 `main.py` (api process) — gateway

```
410	        app.state.local_gateway = LocalGatewayClient(
411	            settings.local_llm_url,
412	            gpu_probe=gpu_probe,
413	            windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
414	            slots_loader=lambda: settings_store.llm_local_parallel_slots(SYSTEM_CTX),
...
418	            config_regen=lambda: llm_settings_api.regen_gateway_config(settings, settings_store),
...
420	            models_dir=settings.local_models_dir,
421	        )
```
Each kwarg resolves to:
- `settings.local_llm_url` → `config.py:333` (compose: `docker-compose.yml:117`)
- `gpu_probe` → `main.py:400-402`
  ```
          gpu_probe = gpu_guard.SupervisorGpuMemProbe(
              lambda: getattr(app.state, "supervisor_client", None), settings.supervisor_token
          )
  ```
  (`app.state.supervisor_client` is created later at `main.py:1180-1182` `        app.state.supervisor_client = httpx.AsyncClient(` / `            base_url=settings.supervisor_url, timeout=30.0` / `        )`)
- `windows_loader` / `slots_loader` → `SqlSettingsStore` methods under `SYSTEM_CTX`; store built `main.py:380-381` `        settings_store = SqlSettingsStore(maker)` / `        app.state.settings_store = settings_store`
- `config_regen` → `llm_settings.py:703` `async def regen_gateway_config(settings: Settings, store: SqlSettingsStore) -> None:`
- `models_dir` → `config.py:378` `    local_models_dir: str = "/data/local-models"`

### 3.2 `main.py` — residency coordinator

```
429	        app.state.residency = ResidencyCoordinator(
430	            app.state.local_gateway,
431	            windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
432	            slots_loader=lambda: settings_store.llm_local_parallel_slots(SYSTEM_CTX),
433	            models_dir=settings.local_models_dir,
434	            enabled=settings.local_llm_enabled,
435	            free_ram_fraction=settings.local_llm_free_ram_fraction,
...
439	            fraction_loader=lambda: settings_store.llm_local_free_ram_fraction(SYSTEM_CTX),
...
444	            hold_loader=lambda: settings_store.code_mode_hold_names(SYSTEM_CTX),
...
447	            auto_restore_loader=lambda: settings_store.llm_local_auto_restore(SYSTEM_CTX),
...
450	            box_lock=pg_box_lock(maker),
...
455	            on_prefix_lost=_prefix_lost_notifier(app),
...
459	            gpu_probe=gpu_probe,
460	        )
```
- ⚠️ **Historical — the coordinator default below was REMOVED on the local-model-access branch**
  (`ResidencyWiring`, no defaults); quoted as read on 2026-08-22 because §3 row 4 cites it.
- `free_ram_fraction` default `config.py:391` `    local_llm_free_ram_fraction: float = 0.15`; coordinator's own default `residency.py:169` `        free_ram_fraction: float = DEFAULT_FREE_RAM_FRACTION,` with `residency.py:68` `DEFAULT_FREE_RAM_FRACTION = 0.15`
- `box_lock` → `residency.py:73` `def pg_box_lock(maker: async_sessionmaker[AsyncSession]) -> BoxLock:`
- `on_prefix_lost` → `main.py:240` `def _prefix_lost_notifier(app: FastAPI) -> Callable[[str], None]:`
- `gpu_probe` is the SAME object handed to the gateway at `main.py:412`

### 3.3 `main.py` — router

```
469	        app.state.llm_router = build_router(
470	            settings,
471	            recorder=SqlUsageRecorder(maker),
472	            overrides_loader=lambda: settings_store.llm_task_overrides(SYSTEM_CTX),
473	            local_windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
...
478	            residency=app.state.residency,
...
481	            slots_probe=app.state.local_gateway.slots,
482	        )
```

### 3.4 `main.py` — warm keeper

```
1164	        app.state.warm_keeper = WarmKeeper(
1165	            gateway=app.state.local_gateway,
1166	            registry=app.state.agent_registry,
1167	            liveness=getattr(app.state, "image_liveness", None),
1168	            router=app.state.llm_router,
1169	            hold_loader=lambda: settings_store.code_mode_hold_names(SYSTEM_CTX),
...
1174	            auto_restore_loader=lambda: settings_store.llm_local_auto_restore(SYSTEM_CTX),
1175	        )
```
- `liveness` ← `main.py:732` `            app.state.image_liveness = ImageGenLiveness(app.state.comfyui_gateway)`
- `registry` ← `main.py:948` `        app.state.agent_registry = build_registry(` (a bare attribute access here, unlike
  the `getattr(..., "agent_registry", None)` used at `debug.py:1370` and `llm_settings.py:138`)
- `interval_ready` / `interval_wait` are NOT passed → defaults 60.0 / 5.0 (`warm_keeper.py:63-64`)

### 3.5 `worker.py` — gateway

```
516	    llm_gateway = LocalGatewayClient(
517	        settings.local_llm_url,
518	        gpu_probe=gpu_guard.SupervisorGpuMemProbe(
519	            lambda: supervisor_client, settings.supervisor_token
520	        ),
...
523	        windows_loader=lambda: worker_settings_store.llm_local_context_windows(queue.SYSTEM_CTX),
524	        slots_loader=lambda: worker_settings_store.llm_local_parallel_slots(queue.SYSTEM_CTX),
...
528	        models_dir=settings.local_models_dir,
529	    )
```
**No `config_regen` kwarg.** With `config_regen=None`, `local_gateway.py:737` `        if self._config_regen is not None:` is False, so the worker's loads do not re-stamp `llama-swap.yaml`.
- `supervisor_client` ← `worker.py:505-509`
  ```
      supervisor_client = (
          httpx.AsyncClient(base_url=settings.supervisor_url, timeout=30.0)
          if settings.supervisor_token
          else None
      )
  ```

### 3.6 `worker.py` — residency coordinator

```
539	    residency = ResidencyCoordinator(
540	        llm_gateway,
541	        windows_loader=lambda: worker_settings_store.llm_local_context_windows(queue.SYSTEM_CTX),
542	        slots_loader=lambda: worker_settings_store.llm_local_parallel_slots(queue.SYSTEM_CTX),
543	        models_dir=settings.local_models_dir,
544	        enabled=settings.local_llm_enabled,
545	        free_ram_fraction=settings.local_llm_free_ram_fraction,
...
548	        fraction_loader=lambda: worker_settings_store.llm_local_free_ram_fraction(queue.SYSTEM_CTX),
...
552	        hold_loader=lambda: worker_settings_store.code_mode_hold_names(queue.SYSTEM_CTX),
...
556	        box_lock=pg_box_lock(maker),
...
559	        gpu_probe=gpu_guard.SupervisorGpuMemProbe(
560	            lambda: supervisor_client, settings.supervisor_token
561	        ),
562	    )
```
**No `auto_restore_loader`, no `on_prefix_lost`.** `residency.py:267-275` shows the absent-loader behaviour: `        if self._auto_restore_loader is None:` / `            return True`.

### 3.7 `worker.py` — router

```
563	    router = build_router(
564	        settings,
565	        recorder=SqlUsageRecorder(maker),
566	        overrides_loader=lambda: worker_settings_store.llm_task_overrides(queue.SYSTEM_CTX),
567	        local_windows_loader=lambda: worker_settings_store.llm_local_context_windows(
568	            queue.SYSTEM_CTX
569	        ),
570	        residency=residency,
...
574	        slots_probe=llm_gateway.slots,
575	    )
```
No WarmKeeper is constructed in `worker.py` (`grep -n "WarmKeeper" backend/src/jbrain/worker.py` → no matches).

### 3.8 The fallback coordinator any other `build_router` caller gets

```
882	    return ResidencyCoordinator(
...
886	        LocalGatewayClient(
887	            settings.local_llm_url,
888	            gpu_probe=gpu_guard.probe_for(settings),
889	            windows_loader=windows_loader,
890	            models_dir=settings.local_models_dir,
891	        ),
892	        windows_loader=windows_loader,
893	        models_dir=settings.local_models_dir,
894	        enabled=settings.local_llm_enabled,
895	        free_ram_fraction=settings.local_llm_free_ram_fraction,
896	    )
```
(`backend/src/jbrain/llm/router.py:882-896`) — no `slots_loader`, no `box_lock`, no `hold_loader`, no `fraction_loader`, no `config_regen`, no `slots_loader` on the client.

### 3.9 The whisper clients (Gateway B)

```
main.py:799	                gateway=LocalGatewayClient(settings.whisper_url),
main.py:839	                gateway=LocalGatewayClient(settings.whisper_url) if settings.whisper_url else None,
main.py:861	                gateway=LocalGatewayClient(settings.whisper_url) if settings.whisper_url else None,
worker.py:627	            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,
worker.py:646	            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,
worker.py:664	            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,
```

---

## 4. Locks

| lock | declaration | acquisition | what it spans |
|---|---|---|---|
| per-model load lock (asyncio, per client instance) | `local_gateway.py:129` `        self._load_locks: dict[str, asyncio.Lock] = {}` | `local_gateway.py:648-650` `        lock = self._load_locks.setdefault(served_model, asyncio.Lock())` / `        queued = lock.locked()` / `        async with lock:` | the whole of `load()` for one served model, including the join check `local_gateway.py:651` `            if queued and served_model in await self.running():` |
| global load lock (asyncio, per client instance) | `local_gateway.py:151` `        self._global_load_lock = asyncio.Lock()` | `local_gateway.py:673` `            async with self._global_load_lock:` (preceded by the queue narration `local_gateway.py:663-665` `            if self._global_load_lock.locked():` / `                ahead = self._loading_now` / `                log.info("local_gateway.load_queued", model=served_model, behind=ahead)`) | `_load_and_warm` — config regen, device pre-flight, guarded load, page-cache drop, warm-up, footprint measurement |
| cross-process box lock (Postgres advisory) | `residency.py:77` `_BOX_LOCK_KEY = admission.BOX_LOCK_KEY` (`admission.py:31` `BOX_LOCK_KEY = 0x6A_42_52_41_4E_4C_4F_41  # "jBRANLOA"`); blocking form `residency.py:97` `            await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _BOX_LOCK_KEY})`; try-lock form `residency.py:135` `                        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _BOX_LOCK_KEY}` | `residency.py:910-911` `        async with self._box_locked():` / `            plan = await self._ensure_room_core(served_model)`; the wrapper is `residency.py:979-998` and degrades: `residency.py:990-992` `        except Exception as exc:  # noqa: BLE001 — lock is best-effort; proceed unlocked` / `            log.warning("residency.box_lock_unavailable", error=repr(exc))` / `            cm = None` | **decision + evictions ONLY, across api and worker — never the load** (inverted 2026-08-23, #1194: holding it across the evict-and-load self-deadlocked against the ledger's own charge). The target's load runs AFTER release, protected by the charge row written at intent — `ledger.charge` takes the same advisory key under a bounded `lock_timeout` and a timeout refuses transiently (`ledger.py:191-208`). `_restore` TRY-locks its plan (`residency.py:1101-1107`, skip-on-busy) and also loads outside the lock. Wired `main.py:467` / `worker.py:653` (`box_lock=pg_box_lock(maker)`). NOT taken on `free_room` or when `box_lock is None` (`residency.py:889-891`) |
| jcode LLM swap lock (asyncio, api process) | `main.py:464` `        app.state.jcode_llm_swap_lock = asyncio.Lock()` | `jcode_llm.py:159` `    swap_lock = getattr(request.app.state, "jcode_llm_swap_lock", None)`; `jcode_llm.py:166-168` `        guard = swap_lock if swap_lock is not None else contextlib.nullcontext()` / `        try:` / `            async with guard:` | `ensure_room` + the entire streamed completion for one jcode request |
| provisioning flocks (shell, not runtime) | `scripts/local-llm-setup.sh:29` `exec 9>"$INSTALL_DIR/.local-llm-setup.lock"`; `scripts/whisper-setup.sh:26` `exec 9>"$INSTALL_DIR/.whisper-setup.lock"` | `if ! flock -n 9; then` (next line in each) | one provisioning run at a time |

Not gateway locks but present nearby, for completeness: `api/ops.py:538` `        self._lock = asyncio.Lock()`; `image_gen/liveness.py:60` `        self._lock = asyncio.Lock()`.

---

## 5. llama-swap behaviour confirmed at the pinned commit

Pin: `deploy/Dockerfile.local-llm:58` `ARG LLAMA_SWAP_VERSION=60226b63776efac11e15828abe0bb302ec259699`
Clone verified: `git log -1 --format="%H %d"` → `60226b63776efac11e15828abe0bb302ec259699  (HEAD, tag: v250)`.

| claim in our tree | our quote | upstream quote |
|---|---|---|
| `/api/models/unload/{model}` blocks until stopped | `local_gateway.py:301-302` `        \`/api/models/unload/{model}\` BLOCKS until the process has actually stopped` / `        (\`internal/router/router.go\`: "It blocks until each targeted process has stopped")` | `internal/router/router.go:44-45` `	// Unload stops the named models, or every running model when none are` / `	// named. It blocks until each targeted process has stopped.`; route `internal/server/server.go:321` `	mux.Handle("POST /api/models/unload/{model...}", apiChain.ThenFunc(s.handleAPIUnloadModel))` |
| `DEFAULT_UNLOAD_TIMEOUT = 10` | `local_gateway.py:303-304` `        it grants each one \`DEFAULT_UNLOAD_TIMEOUT = 10\` seconds of graceful stop before` / `        escalating — a figure the generated config never overrides.` | `internal/config/config.go:12` `const DEFAULT_UNLOAD_TIMEOUT = 10`; default applied `internal/config/load.go:48` `		UnloadTimeout:      DEFAULT_UNLOAD_TIMEOUT,`. `render` emits no `unloadTimeout` key. |
| `/running` reports a stopping model as resident | `local_gateway.py:316` `        process is genuinely still stopping, which is a state \`/running\` reports as resident.` | `internal/router/base.go:366-375` `func (b *baseRouter) RunningModels() map[string]process.ProcessState {` / `	running := make(map[string]process.ProcessState)` / `	for id, p := range b.processes {` / `		st := p.State()` / `		if st == process.StateStopped || st == process.StateShutdown {` / `			continue` / `		}` / `		running[id] = st` … ; payload `internal/server/api.go:350-351` `	w.Header().Set("Content-Type", "application/json")` / `	json.NewEncoder(w).Encode(map[string]any{"running": list})` |
| reload swaps then kills every server | `llama_swap_config.py:470-471` `      -> llama-swap \`reload()\` builds a new server and calls \`old.Shutdown()\`` / `      -> every running llama-server process dies`; `llm_settings.py:767-768` `#     activeSrv = newSrv        <- the SWAP` / `#     old.Shutdown(30s)         <- the old llama-servers are killed here` | `llama-swap.go:253-263` `		activeMu.Lock()` / `		old := activeSrv` … `		activeSrv = newSrv` … `		if err := old.Shutdown(shutdownTimeout); err != nil {`; the `30s` figure is `llama-swap.go:37` `const shutdownTimeout = 30 * time.Second` |
| watcher polls mtime+size at 2 s | `llama_swap_config.py:468-469` `      -> \`os.replace\` lands a file with a fresh mtime, even byte-identical` / `      -> llama-swap's \`--watch-config\` poller compares MTIME + SIZE, so it fires`; `llm_settings.py:772` `# server.New() — neither of which loads a model. So 4 s is the 2 s poll with margin` | `internal/watcher/watcher.go:17` `const DefaultInterval = 2 * time.Second`; `internal/watcher/watcher.go:84` `	return !prev.modTime.Equal(cur.modTime) || prev.size != cur.size`; `llama-swap.go:284` `		proxyLog.Info("watching configuration for changes (poll-based, 2s interval)")` |
| the gateway never self-evicts under `swap: false` / `exclusive: false` | `llama_swap_config.py:428-431` `    # One non-swapping group with EVERY model as a member: \`swap: false\` means llama-swap` / `    # never evicts a member to load another, and \`exclusive: false\` lets an on-demand request` / `    # still load one.` | `internal/router/group.go:84-97` (the `switch` with `case og == tg && tgCfg.Swap:` and `case og != tg && tgCfg.Exclusive:` as the only eviction arms) |
| loading is request-driven | `local_gateway.py:8-10` `  - GET  /upstream/{model}/health      → proxy a request, which makes the gateway` / `                                         load the model (llama-swap has no explicit` / `                                         load endpoint; loading is request-driven)` | `internal/router/base.go:264-265` `	target := b.processes[modelID]` / `	err := target.EnsureReady(b.shutdownCtx, timeout)` inside `doSwap`; and `internal/server/api.go:479-481` for the `/upstream/` passthrough |
| `/logs/stream/...` exists; there is a startup preload hook we do not use | `local_gateway.py:1074-1075` `    llama-swap buffers upstream output separately from the proxy log and exposes it only` / `    at \`/logs/stream/{proxy,upstream,<model>}\`.` | `internal/server/api.go:372-376` `// startPreload fires a background GET / at every model named in` / `// Hooks.OnStartup.Preload so they are warm before the first real request.` / … / `	models := s.cfg.Hooks.OnStartup.Preload` |

---

## 6. Things I could not determine

1. Whether `Hooks.OnStartup.Preload` could ever be populated for Gateway A from somewhere
   outside `llama_swap_config.render` — I confirmed `render` emits only the `models:` and
   `groups:` blocks and that `write` writes exactly `render`'s output, but I did not audit
   for any other writer of `/data/local-models/llama-swap.yaml` beyond the three named in
   §1g (a hand-edited file on the live box would not be visible from this tree).
2. Whether any deployed `.env` sets `WHISPER_URL` to the SAME host:port as `LOCAL_LLM_URL`.
   The defaults differ (`local-llm:8080` vs whatever `jbrain enable-whisper` writes), and
   both containers listen on `:8080` internally, but I could not read the live `.env`.
3. Whether the `local` `OpenAiCompatClient` and `LocalGatewayClient` ever disagree about the
   base URL suffix in practice: the client keeps `/v1` (`openai_compat.py:117`
   `        self._base_url = base_url.rstrip("/")`) while the gateway strips it
   (`local_gateway.py:116` `        self._root = base_url.rstrip("/").removesuffix("/v1")`). Both are
   quoted; I did not test a URL without the `/v1` suffix.
4. Whether `residency._restore`'s suppression of `gpu_guard.GpuBudgetError`
   (`residency.py:637`) is reachable given that `_restore` calls `self._gateway.load` directly
   — I quoted both lines but did not trace whether a `GpuBudgetError` can escape `load()`
   under every branch.

---

# §B — Whisper, embeddings, ComfyUI, and box memory

# Factual inventory — non-main-LLM consumers of the box's unified memory

Repo `/home/user/JBrain2`, branch `claude/model-loading-chat-indicator-tujwms`. Read-only.
Every row carries `file:line` + a verbatim quote. Nothing here is a conclusion; where I
looked for something and found nothing, the row says "none found (looked at …)".

Line numbers are from the working tree as read on 2026-08-22.

---

## 1. The whisper gateway

### 1.1 Wiring proof — which gateway each caller got

The whisper client is the **same Python class** as the main LLM gateway
(`LocalGatewayClient`), constructed against a **different URL setting**.

**Setting definitions (two distinct fields on the same `Settings` object):**

`backend/src/jbrain/config.py:333`
```
    local_llm_url: str = "http://localhost:11434/v1"
```

`backend/src/jbrain/config.py:401`
```
    whisper_url: str = ""
```
Preceding comment, `backend/src/jbrain/config.py:392-395` (verbatim, first lines):
```
    # OPT-IN on-box speech-to-text: whisper.cpp served by the same llama-swap
    # gateway the local-llm profile runs (docs/archive/WHISPER_TRANSCRIPTION_PLAN.md), so
    # it loads on first request and the gateway frees it when idle — and the
    # transcribe job/tool additionally unload it the moment they finish.
```
NOTE — that comment says "the same llama-swap gateway the local-llm profile runs". The
deployment code says otherwise; both are quoted below (§1.2, §5) so the reader can
compare. The comment is not evidence of the shape; the compose file and setup script are.

`backend/src/jbrain/config.py:403-405`
```
    # The served-model name the gateway resolves to a loaded whisper.cpp model
    # (and the name LocalGateway.unload() evicts). A plain default so the request
    # is always concrete; the setup script writes the provisioned name.
```
`backend/src/jbrain/config.py:406`
```
    whisper_model: str = "whisper"
```
`backend/src/jbrain/config.py:411`
```
    whisper_timeout: float = 300.0
```
`backend/src/jbrain/config.py:416`
```
    whisper_max_bytes: int = 100 * 1024 * 1024
```

**The main gateway client (api process) — takes a gpu probe, config regen, models_dir:**

`backend/src/jbrain/main.py:410-421`
```
        app.state.local_gateway = LocalGatewayClient(
            settings.local_llm_url,
            gpu_probe=gpu_probe,
            windows_loader=lambda: settings_store.llm_local_context_windows(SYSTEM_CTX),
            slots_loader=lambda: settings_store.llm_local_parallel_slots(SYSTEM_CTX),
            # Re-stamp the gateway config HERE rather than on every settings edit: rewriting
            # it makes llama-swap reload, and its reload kills every running model, not just
            # the edited one. See LocalGatewayClient._config_regen.
            config_regen=lambda: llm_settings_api.regen_gateway_config(settings, settings_store),
            # Lets a finished load drop the page-cache copy of the weights it just read.
            models_dir=settings.local_models_dir,
        )
```

**The main gateway client (worker process):**

`backend/src/jbrain/worker.py:516-529`
```
    llm_gateway = LocalGatewayClient(
        settings.local_llm_url,
        gpu_probe=gpu_guard.SupervisorGpuMemProbe(
            lambda: supervisor_client, settings.supervisor_token
        ),
```
… `backend/src/jbrain/worker.py:528`
```
        models_dir=settings.local_models_dir,
```

**The whisper gateway clients — every construction site, all bare (no `gpu_probe`, no
`models_dir`, no `windows_loader`/`slots_loader`, no `config_regen`):**

| # | file:line | verbatim |
|---|---|---|
| W1 | `backend/src/jbrain/main.py:799` | `                gateway=LocalGatewayClient(settings.whisper_url),` |
| W2 | `backend/src/jbrain/main.py:839` | `                gateway=LocalGatewayClient(settings.whisper_url) if settings.whisper_url else None,` |
| W3 | `backend/src/jbrain/main.py:861` | `                gateway=LocalGatewayClient(settings.whisper_url) if settings.whisper_url else None,` |
| W4 | `backend/src/jbrain/worker.py:627` | `            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,` |
| W5 | `backend/src/jbrain/worker.py:646` | `            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,` |
| W6 | `backend/src/jbrain/worker.py:664` | `            gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,` |

That is the complete list of `LocalGatewayClient(settings.whisper_url)` constructions in
`backend/src` (grep `LocalGatewayClient` across `backend/src`, `supervisor/src`,
`scripts` returned only these six plus the four `local_llm_url` ones at
`backend/src/jbrain/cli.py:62`, `backend/src/jbrain/cli.py:226`,
`backend/src/jbrain/main.py:411`, `backend/src/jbrain/worker.py:517`).

The constructor's defaults for the omitted arguments —
`backend/src/jbrain/llm/local_gateway.py:104-115`:
```
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 3.0,
        gpu_probe: gpu_guard.GpuMemProbe | None = None,
        config_regen: Callable[[], Awaitable[None]] | None = None,
        windows_loader: Callable[[], Awaitable[Mapping[str, int]]] | None = None,
        slots_loader: Callable[[], Awaitable[Mapping[str, int]]] | None = None,
        models_dir: str = "",
    ):
```
`backend/src/jbrain/llm/local_gateway.py:116`
```
        self._root = base_url.rstrip("/").removesuffix("/v1")
```
`backend/src/jbrain/llm/local_gateway.py:269-270` (the `models_dir` gate on the
unannounced-load page-cache sweep):
```
        if not self._models_dir:
            return
```

**The transcription HTTP client (separate from the gateway client), also on `whisper_url`:**

`backend/src/jbrain/main.py:790-801`
```
            transcribe_handlers = build_transcribe_handlers(
                WhisperCppClient(
                    settings.whisper_url,
                    settings.whisper_model,
                    timeout=settings.whisper_timeout,
                ),
                app.state.blob_store,
                app.state.turn_attachments,
                settings.whisper_model,
                gateway=LocalGatewayClient(settings.whisper_url),
                max_bytes=settings.whisper_max_bytes,
            )
```
`backend/src/jbrain/transcribe.py:93-97`
```
            resp = await client.post(
                "audio/transcriptions",
                files={"file": (filename, audio, media_type)},
                data={"model": self._model, "response_format": "verbose_json"},
            )
```
`backend/src/jbrain/transcribe.py:1` (module docstring first line — quoted as a label, not
as evidence of behaviour):
```
"""Audio transcription client (whisper.cpp via the on-box llama-swap gateway).
```

### 1.2 Deployment wiring — which container `whisper_url` points at

`deploy/docker-compose.yml:144-152` (api service env):
```
      # On-box speech-to-text — served by the always-on `tts-stt` service (below), but the
      # STT MODEL is a heavy opt-in download, so transcribe stays OFF (empty URL) until
      # `jbrain enable-whisper` provisions the model and sets WHISPER_URL in .env
      # (http://tts-stt:8080/v1). Empty here = audio attachments aren't transcribed and
      # jerv's transcribe tool is hidden — never advertising an STT port that isn't loaded.
      # Read-aloud (piper TTS) needs none of this; it works from the default tts-stt service.
      JBRAIN_WHISPER_URL: ${WHISPER_URL:-}
      JBRAIN_WHISPER_ENABLED: ${WHISPER_ENABLED:-false}
      JBRAIN_WHISPER_MODEL: ${WHISPER_MODEL:-whisper}
```

`deploy/docker-compose.yml:105` (embed) and `deploy/docker-compose.yml:117` (main LLM),
for contrast:
```
      JBRAIN_EMBED_URL: http://embed:80
```
```
      JBRAIN_LOCAL_LLM_URL: ${LOCAL_LLM_URL:-http://local-llm:8080/v1}
```

The value `WHISPER_URL` is set to, `scripts/whisper-setup.sh:128`:
```
  echo "WHISPER_URL=http://tts-stt:8080/v1"
```

`scripts/whisper-setup.sh:2-3`:
```
# OPT-IN: provision on-box speech-to-text (whisper.cpp via its own llama-swap) and
# enable it. The audio sibling of scripts/local-llm-setup.sh / comfyui-setup.sh.
```

The second llama-swap process itself, `deploy/tts-stt/entrypoint.sh:11-16`:
```
if [ -f /models/llama-swap.yaml ]; then
  /app/llama-swap --config /models/llama-swap.yaml --listen :8080 --watch-config &
else
  echo "[tts-stt] /models/llama-swap.yaml absent — STT not provisioned (run" \
       "'jbrain enable-whisper'); serving read-aloud (TTS) only" >&2
fi
```

The first llama-swap process (main LLM gateway), `deploy/docker-compose.yml:394-395`:
```
    command:
      ["llama-swap", "--listen", ":8080", "--config", "/models/llama-swap.yaml", "--watch-config"]
```
— a different container (`local-llm`, `deploy/docker-compose.yml:373`) with a different
mount, `deploy/docker-compose.yml:410`:
```
      - ./local-models:/models:ro
```
vs the tts-stt mount, `deploy/docker-compose.yml:492`:
```
      - ./whisper-models:/models:ro
```

The whisper gateway's own config file (written by the setup script),
`scripts/whisper-setup.sh:92-111`:
```
cat > "$MODELS_DIR/llama-swap.yaml" <<EOF
# Generated by scripts/whisper-setup.sh — do not edit by hand.
# Generous cold-load window for the CPU model load (the image's sample uses this too).
healthCheckTimeout: 300
models:
  whisper:
    proxy: http://127.0.0.1:9200
    # whisper-server isn't a llama-server, so llama-swap's default /health probe
    # doesn't gate it; poll GET / (returns 200) until the model finishes loading
    # before forwarding — the same readiness gate the image's sample uses for its
    # sd-server model. Without it llama-swap proxies into the not-yet-bound port
    # during the ~10s CPU load and the first request fails with "connection refused".
    checkEndpoint: /
    cmd: >
      whisper-server --host 127.0.0.1 --port 9200
      --model /models/${GGML_FILE}
      --inference-path /v1/audio/transcriptions
      --convert
    ttl: 300
EOF
```

### 1.3 Whisper call-site table

Process column evidence: `main.py` is the FastAPI app factory (`backend/src/jbrain/main.py`
lifespan wiring, lines quoted above); `worker.py:476` reads
`    box_events.configure(maker, source="worker")`.

| # | file:line | verbatim | service acted on (wiring proof) | action | process | admission/budget check before it |
|---|---|---|---|---|---|---|
| T1 | `backend/src/jbrain/agent/transcribetools.py:146` | `            await gateway.unload(model)` | whisper — the `gateway` arg comes from `main.py:799` `gateway=LocalGatewayClient(settings.whisper_url),` | unload | api | none found. Enclosing fn `_unload` is `backend/src/jbrain/agent/transcribetools.py:139-148`; the only guards are `if gateway is None: return` (`:142-143`) and the `LocalGatewayError` catch (`:147`). No residency/budget call in the file (grep for `residency`, `ensure_room`, `gpu_guard` in `backend/src/jbrain/agent/transcribetools.py` returns nothing). |
| T2 | `backend/src/jbrain/agent/transcribetools.py:91` | `            await _unload(gateway, model)` (in a `finally:` at `:90`) | same as T1 | unload (after a tool transcription) | api | none found (see T1) |
| T3 | `backend/src/jbrain/ingest/transcribe_job.py:206` | `                await self._gateway.unload(self._model)` | whisper — constructed at `backend/src/jbrain/worker.py:627` `gateway=LocalGatewayClient(settings.whisper_url) if transcribe_enabled else None,` | unload | worker | none found. Guard is only `backend/src/jbrain/ingest/transcribe_job.py:202-203` `        if self._gateway is None:` / `            return` |
| T4 | `backend/src/jbrain/ingest/transcribe_job.py:153` | `            await self._unload()` (inside `finally:` at `:149`) | same as T3 | unload after each job | worker | none found |
| T5 | `backend/src/jbrain/ingest/video.py:441` | `            await gateway.unload(model)` | whisper — `backend/src/jbrain/worker.py:646` (worker `analyze_video_attachment`) and `backend/src/jbrain/main.py:839` (api `analyze_video` handlers) | unload | worker + api | none found; guard is `backend/src/jbrain/ingest/video.py:437-438` `    if gateway is None or not model:` / `        return` |
| T6 | `backend/src/jbrain/ingest/video.py:308` | `        await _unload(gateway, model)` (in a `finally:` at `:307`) | same as T5 | unload after a single-shot transcription | worker + api | none found |
| T7 | `backend/src/jbrain/ingest/video.py:388` | `        await _unload(gateway, model)` (in a `finally:` at `:387`) | same as T5 | unload after a **chunked** transcription | worker + api | none found |
| T8 | `backend/src/jbrain/ingest/stream_analysis.py:173-180` | `        return await transcribe_audio_chunked(` … `            gateway,` … | whisper — `backend/src/jbrain/worker.py:664` (`analyze_stream_url`) and `backend/src/jbrain/main.py:861` (`analyze_stream` handlers) | drives T7's unload | worker + api | none found |

**Loads against the whisper gateway: none found.** `grep -rn "\.running()" backend/src/jbrain`
and `grep -rn "gateway.load\|\.load(" backend/src/jbrain` show the only `LocalGateway.load`
/ `LocalGateway.running` call sites are on the `local_llm_url` client
(`cli.py:64`, `warm_keeper.py:130`, `warm_keeper.py:181`, `warm_keeper.py:182`,
`local_gateway.py:354/397/651/738/783`, `residency.py:345/463/474/502/533/589/614/645`,
`llm_settings.py:161/831/899/1284/1519/1533/1891`, `external_llm.py:218`,
`jcode.py:154/164/168/274/474`, `image_gen/render.py:205/206`,
`workflow/preconditions.py:68`, `smoketest.py:237`). None of those objects is built from
`whisper_url` (see the six-row table in §1.1 — every whisper client is passed only as the
`gateway=` argument of a transcribe/video/stream helper, and those helpers call only
`unload`).

Loading of the whisper model is therefore request-driven through the transcription POST
(`backend/src/jbrain/transcribe.py:93-97`, quoted above) plus the gateway's own `ttl: 300`
(`scripts/whisper-setup.sh:110`).

**Whisper unloads write to the same vitals event stream as LLM unloads** —
`backend/src/jbrain/llm/local_gateway.py:331`:
```
        await box_events.record(box_events.MODEL_UNLOAD, served_model)
```
and on failure, `backend/src/jbrain/llm/local_gateway.py:327-329`:
```
            await box_events.record(
                box_events.MODEL_UNLOAD, served_model, status="failed", detail=str(exc)
            )
```
`backend/src/jbrain/box_events.py:52` — `MODEL_UNLOAD = "model_unload"`.

**Gate on enqueueing transcription at all** — `backend/src/jbrain/worker.py:481`:
```
    transcribe_enabled = bool(settings.whisper_url)
```
`backend/src/jbrain/ingest/pipeline.py:341-342`:
```
        if not self._transcribe_enabled:
            return set()
```
`backend/src/jbrain/ingest/pipeline.py:350`:
```
            if att.size_bytes > self._transcribe_max_bytes:
```

**Whisper start/stop from the PWA / debug API / CLI:** none found specific to whisper.
`grep -rn "whisper" frontend/src backend/src/jbrain/api supervisor/src` returns only
`backend/src/jbrain/api/agent.py:782` and `:1276` (comments), and frontend hits that are
unrelated (`EditLayer.tsx`, `VideoAnalysis.tsx` label text). The only whisper-specific
operator entry point found is the shell script:
`deploy/jbrain:102-107`
```
  enable-whisper)
    shift
    # Opt-in, heavy (downloads a GGML model, starts a GPU service). Provisioning
    # lives with the source. Absolute path so it works regardless of cwd.
    JBRAIN_INSTALL_DIR=/opt/jbrain2 bash /opt/jbrain2/src/scripts/whisper-setup.sh "$@"
    ;;
```
`scripts/whisper-setup.sh:137-142`
```
say "Recreating the tts-stt service so it picks up the whisper model"
docker compose build tts-stt
docker compose up -d --force-recreate tts-stt
# Recreate the api + worker so they pick up the new JBRAIN_WHISPER_* env (audio
# attachments now transcribe and jerv gains the transcribe tool).
docker compose up -d api worker
```
The `tts-stt` container can also be stopped/started by the generic Ops per-container
controls — see §6.

---

## 2. Embeddings / TEI

### 2.1 The client

`backend/src/jbrain/embed.py:33-34`
```
class TeiEmbedClient:
    """text-embeddings-inference HTTP client (POST /embed)."""
```
`backend/src/jbrain/embed.py:22-24`
```
# TEI handles long inputs via truncate=true; small batches keep the 1g
# container comfortably inside its memory cap.
EMBED_BATCH = 16
```
`backend/src/jbrain/embed.py:40-50`
```
    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=60.0, transport=self._transport
        ) as client:
            for start in range(0, len(texts), EMBED_BATCH):
                batch = texts[start : start + EMBED_BATCH]
                resp = await client.post("/embed", json={"inputs": batch, "truncate": True})
                resp.raise_for_status()
                vectors.extend(resp.json())
        return vectors
```

### 2.2 Wiring proof

`backend/src/jbrain/config.py:18`
```
    embed_url: str = "http://embed:80"
```
`backend/src/jbrain/config.py:19`
```
    embed_model: str = "BAAI/bge-small-en-v1.5"
```
`deploy/docker-compose.yml:105`
```
      JBRAIN_EMBED_URL: http://embed:80
```

Every construction site of `TeiEmbedClient(settings.embed_url)` (complete list from
`grep -rn "embed_url" backend/src`):

| file:line | verbatim | process |
|---|---|---|
| `backend/src/jbrain/main.py:372` | `            SqlSearchRepo(maker), TeiEmbedClient(settings.embed_url)` | api |
| `backend/src/jbrain/main.py:379` | `        app.state.embed_client = TeiEmbedClient(settings.embed_url)` | api |
| `backend/src/jbrain/main.py:486` | `            MemoryRepo(maker), TeiEmbedClient(settings.embed_url), settings.embed_model` | api |
| `backend/src/jbrain/main.py:995` | `                TeiEmbedClient(settings.embed_url),` | api |
| `backend/src/jbrain/main.py:1001` | `                TeiEmbedClient(settings.embed_url),` | api |
| `backend/src/jbrain/worker.py:488` | `    embedder = NoteEmbedder(maker, TeiEmbedClient(settings.embed_url), settings.embed_model)` | worker |
| `backend/src/jbrain/worker.py:490` | `        maker, TeiEmbedClient(settings.embed_url), settings.embed_model` | worker |
| `backend/src/jbrain/worker.py:493` | `        maker, TeiEmbedClient(settings.embed_url), settings.embed_model` | worker |
| `backend/src/jbrain/worker.py:496` | `        maker, TeiEmbedClient(settings.embed_url), settings.embed_model` | worker |
| `backend/src/jbrain/worker.py:584` | `        embedder=TeiEmbedClient(settings.embed_url),` | worker |
| `backend/src/jbrain/worker.py:712` | `            maker, embedder=TeiEmbedClient(settings.embed_url), embedding_model=settings.embed_model` | worker |
| `backend/src/jbrain/worker.py:728` | `            embed=TeiEmbedClient(settings.embed_url),` | worker |

### 2.3 Separate container; what governs its memory

`deploy/docker-compose.yml:635-644`
```
  embed:
    logging: *logbound
    image: ghcr.io/huggingface/text-embeddings-inference:cpu-latest
    restart: unless-stopped
    command: ["--model-id", "${EMBED_MODEL:-BAAI/bge-small-en-v1.5}"]
    # 4GB host: cap the model server so a bad query can't starve Postgres.
    mem_limit: 1g
    volumes:
      - embed_models:/data
    networks: [internal]
```
Its footprint is governed by the Docker `mem_limit: 1g` (`deploy/docker-compose.yml:641`)
and by the image being `cpu-latest` (`:637`) — no `devices:` / `/dev/dri` block appears in
this service (contrast `local-llm` at `deploy/docker-compose.yml:396-397`, `comfyui` at
`:435-437`, `tts-stt` at `:482-483`).

### 2.4 Load / unload / start / stop from our code

**None found.** `TeiEmbedClient` exposes exactly one method — `backend/src/jbrain/embed.py:40`
`    async def embed(self, texts: list[str]) -> list[list[float]]:` — and the `EmbedClient`
Protocol at `backend/src/jbrain/embed.py:27-30` declares only `embed`. There is no
`free`/`unload`/`load`/`status` on either. Grep for `embed` in
`backend/src/jbrain/api/image_settings.py`, `backend/src/jbrain/api/ops.py`,
`supervisor/src/supervisor/app.py` finds no embed-specific control endpoint; the only
mentions of the service outside compose are the jcode network-isolation comments
(`deploy/docker-compose.yml:508`, `:820`, `:823`).

The one place the embed container is stopped is the generic update quiesce (§6):
`deploy/update-inner.sh:265`
```
QUIESCE_KEEP="db api supervisor proxy cloudflared"
```
— `embed` is not in that keep-list, so it falls into the `QUIESCED` set built at
`deploy/update-inner.sh:302-308`, and `deploy/update-inner.sh:337-339` names it:
```
    # this log because they have no terminal. A restore that fails must SAY so.
    docker compose $ALL_PROFILES start "$_svc" >/dev/null 2>&1 \
      || echo "[update] WARNING: $_svc did not restart — start it from Ops"
```
and `deploy/update-inner.sh:274-277`:
```
    # which by definition does NOT restart something explicitly stopped, so without this a
    # crashed quiesce leaves the box missing its worker, embedder and speech services across
    # reboots. Worse for `comfyui` and the mqtt pair: the next update's `up -d` never names
    # them, so nothing would ever bring them back. The file is the durable half of the trap.
```

---

## 3. ComfyUI / image generation

### 3.1 Wiring proof

`backend/src/jbrain/config.py:341` (comment above it, `:334-340`, names the gate):
```
    comfyui_url: str = ""
```
`backend/src/jbrain/config.py:342` — `    comfyui_enabled: bool = False`
`backend/src/jbrain/config.py:345` — `    comfyui_models: list[str] = []`
`backend/src/jbrain/config.py:351` — `    comfyui_models_dir: str = "/data/comfyui-models"`
`backend/src/jbrain/config.py:357` — `    comfyui_timeout: float = 1800.0`
with `backend/src/jbrain/config.py:352-356`:
```
    # Overall budget for ONE render (cold model load + sampling + tiled VAE decode).
    # On the iGPU a large/high-step image — 1536x1536 at 45 steps — plus a cold model
    # load (we free ComfyUI between renders) runs well past the 1024x1024/20-step base,
    # so this is generous: it's the ceiling for a render that genuinely hung, not the
    # expected duration. Raise it for even larger jobs.
```

`deploy/docker-compose.yml:131` — `      JBRAIN_COMFYUI_URL: ${COMFYUI_URL:-}`
`scripts/comfyui-setup.sh:177` — `  echo "COMFYUI_URL=http://comfyui:8188"`

api-side construction, `backend/src/jbrain/main.py:721-751`:
```
        if settings.comfyui_url:
            image_gen_client = httpx.AsyncClient()
            app.state.image_gen = ComfyUiImageGen(
                settings.comfyui_url, image_gen_client, timeout=settings.comfyui_timeout
            )
            # The management client (status/free) for the owner image-settings surface
            # — the sibling of app.state.local_gateway, wired on the same gate.
            app.state.comfyui_gateway = ComfyUiGatewayClient(settings.comfyui_url)
```
…
```
            app.state.image_render = ImageRenderService(
                app.state.image_gen,
                app.state.blob_store,
                app.state.generated_image_repo,
                maker,
                app.state.local_gateway,
                app.state.comfyui_gateway,
                settings.comfyui_models,
                # Freeing the LLMs for a render is a displacement: record what it evicts so the
                # end-of-turn restore puts the box back to its pre-render steady state.
                on_evicted=app.state.residency.note_evicted,
                # Lets a finished render drop the page-cache copy of the diffusion weights it
                # read; without it that residue reads as a full box to the memory budget.
                models_dir=settings.comfyui_models_dir,
            )
```
The 5th positional argument is `app.state.local_gateway` — the **main LLM** client
(`backend/src/jbrain/main.py:410-411`, quoted in §1.1: `LocalGatewayClient(` /
`settings.local_llm_url,`). The 6th is the ComfyUI client (`main.py:728`).
`ImageRenderService.__init__` binds them, `backend/src/jbrain/image_gen/render.py:256-257`:
```
        local_gateway: LocalGateway,
        comfyui_gateway: ComfyUiMemory,
```

**Process:** api only. `grep -rn "ImageRenderService\|image_render" backend/src/jbrain`
returns hits only in `main.py`, `agent/imagegentools.py`, `api/images_render.py`,
`api/image_settings.py`, `image_gen/render.py`, `box_events.py`. `grep -n "comfy\|Comfy"
backend/src/jbrain/worker.py` returns nothing (only `worker.py:612` `# The vision handler
reads the image-analysis mode setting per job.` matched the word "image").

### 3.2 ComfyUI call-site table

| # | file:line | verbatim | service acted on (wiring proof) | action | process | admission/budget check before it |
|---|---|---|---|---|---|---|
| C1 | `backend/src/jbrain/image_gen/render.py:205-207` | `            for served in await gateway.running():` / `                await gateway.unload(served)` / `                freed.append(served)` | **main LLM gateway** — `gateway` is `self._local_gateway` (`render.py:266` `        self._local_gateway = local_gateway`), fed from `main.py:742` `                app.state.local_gateway,` which is `LocalGatewayClient(settings.local_llm_url, …)` (`main.py:410-411`) | unload EVERY resident LLM ("image gen evicts LLMs") | api | none found — the only wrapping is `backend/src/jbrain/image_gen/render.py:204` `        with box_events.because("an image render needs the whole memory pool"):` and the `except LocalGatewayError` at `:208`. No `ensure_room`/`plan_load`/`gpu_guard` call appears in `image_gen/render.py` (grep for `residency`, `ensure_room`, `gpu_guard`, `refuse_if` in that file returns nothing) |
| C2 | `backend/src/jbrain/image_gen/render.py:324` | `            await _free_local_llms(self._local_gateway, self._on_evicted)` (generate path) | main LLM gateway (as C1) | unload-all before a text→image render | api | none found |
| C3 | `backend/src/jbrain/image_gen/render.py:393` | `            await _free_local_llms(self._local_gateway, self._on_evicted)` (edit path) | main LLM gateway (as C1) | unload-all before an image→image render | api | none found |
| C4 | `backend/src/jbrain/image_gen/render.py:225` | `        await gateway.free(unload_models=True, free_memory=True)` | **ComfyUI** — `gateway: ComfyUiMemory` (`render.py:214`), bound from `main.py:743` `                app.state.comfyui_gateway,` = `ComfyUiGatewayClient(settings.comfyui_url)` (`main.py:728`) | free ComfyUI's resident model after each render ("LLM work reclaims from image gen") | api | none found |
| C5 | `backend/src/jbrain/image_gen/render.py:329` | `            await _free_comfyui_model(self._comfyui_gateway, self._models_dir)` | ComfyUI (as C4) | free after generate | api | none found |
| C6 | `backend/src/jbrain/image_gen/render.py:397` | `            await _free_comfyui_model(self._comfyui_gateway, self._models_dir)` | ComfyUI (as C4) | free after edit | api | none found |
| C7 | `backend/src/jbrain/image_gen/render.py:234-237` | `    if models_dir:` / `        freed = local_weights.drop_image_model_page_cache(models_dir)` / `        if freed:` / `            log.info("image_gen.weights_cache_dropped", freed_gb=freed)` | host page cache over `comfyui_models_dir` (`main.py:750` `                models_dir=settings.comfyui_models_dir,`) | free (page cache) + measure (GiB returned) | api | n/a — housekeeping |
| C8 | `backend/src/jbrain/image_gen/render.py:210-211` | `    if freed and on_evicted is not None:` / `        on_evicted(freed)` | residency bookkeeping for the main gateway; `on_evicted=app.state.residency.note_evicted` (`main.py:747`) | record eviction for later restore | api | n/a |
| C9 | `backend/src/jbrain/api/image_settings.py:123` | `        await gateway.free()` | ComfyUI — `GatewayDep` resolves `request.app.state.comfyui_gateway` (`api/image_settings.py:33`) | free on operator demand (PWA button) | api | `backend/src/jbrain/api/image_settings.py:120-121` `    if not (gateway and _enabled(settings)):` / `        raise HTTPException(status_code=409, detail="image hosting is not enabled")` — an enablement check, not a memory budget |
| C10 | `backend/src/jbrain/api/image_settings.py:137` | `        await gateway.interrupt()` | ComfyUI | stop the in-flight render | api | same 409 enablement check at `:134-135` |
| C11 | `backend/src/jbrain/api/image_settings.py:162-164` | `    resp = await _supervisor(request).post(` / `        f"/{action}", json={"service": COMFYUI_SERVICE}, headers=_sup_headers(settings)` / `    )` | the `comfyui` **container** (`api/image_settings.py:25` `COMFYUI_SERVICE = "comfyui"`) | start / stop the container | api → supervisor | `api/image_settings.py:160-161` `    if not _enabled(settings):` / `        raise HTTPException(status_code=409, detail="image hosting is not enabled")` |
| C12 | `backend/src/jbrain/image_gen/gateway.py:73` | `                resp = await client.get(f"{self._root}/system_stats")` | ComfyUI | **measure** VRAM total/free | api | n/a (read) |
| C13 | `backend/src/jbrain/image_gen/liveness.py:74` | `            status = await self._gateway.status()` | ComfyUI — `ImageGenLiveness(app.state.comfyui_gateway)` (`main.py:732`) | measure reachability (hides tools) | api | n/a |

**The ComfyUI admin surface itself** — `backend/src/jbrain/image_gen/gateway.py:81-94`:
```
    async def free(self, *, unload_models: bool = True, free_memory: bool = True) -> None:
        """Unload cached models and/or free memory. Raises ComfyUiGatewayError on
        any failure (the operator asked for it, so a failure is surfaced)."""
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._root}/free",
                    json={"unload_models": unload_models, "free_memory": free_memory},
                )
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ComfyUiGatewayError(str(exc)) from exc
```
`backend/src/jbrain/image_gen/gateway.py:35-40`:
```
class ComfyUiMemory(Protocol):
    """The free-memory capability the image tool depends on, so it takes the action
    rather than the concrete HTTP client (the in-memory test fake satisfies it — the
    same seam as the `LocalGateway` / `ImageGen` protocols)."""

    async def free(self, *, unload_models: bool = True, free_memory: bool = True) -> None: ...
```
`backend/src/jbrain/image_gen/gateway.py:53-63` (client timeout default):
```
class ComfyUiGatewayClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 3.0,
    ):
        self._root = base_url.rstrip("/")
        self._transport = transport
        self._timeout = timeout
```
Note `main.py:728` passes no `timeout`, so `free`/`status`/`interrupt` use the 3.0 s default.

**Complete list of `ComfyUiMemory.free()` call sites** (grep `\.free(` in `backend/src/jbrain`,
excluding tests): `backend/src/jbrain/api/image_settings.py:123` and
`backend/src/jbrain/image_gen/render.py:225`. **No LLM load path frees ComfyUI** — grep for
`comfyui_gateway` / `ComfyUiGatewayClient` across `backend/src/jbrain` returns only
`main.py:137,728,732,743,764,781`, `agent/imagegentools.py:203,236`,
`api/image_settings.py:22,30,33,36,96,123`, `image_gen/render.py:225,257,267,329,397`,
`image_gen/gateway.py:53,78`. Nothing in `llm/residency.py`, `llm/local_gateway.py`,
`llm/gpu_guard.py` touches it.

**No end-of-turn LLM restore on the direct owner render API.** `grep -rn
"schedule_restore\|note_evicted" backend/src/jbrain` (non-test) returns:
`main.py:426` (comment), `main.py:476` (comment), `main.py:747`, `worker.py:537` (comment),
`llm/warm_keeper.py:5` (comment), `llm/residency.py:21,23,152,154,243,255`,
`api/agent.py:1117`, `api/jcode.py:167,477`, `image_gen/render.py:193` (comment).
`backend/src/jbrain/api/images_render.py` appears nowhere in that list. The chat path's
restore is `backend/src/jbrain/api/agent.py:1115-1117`:
```
                residency = getattr(request.app.state, "residency", None)
                if residency is not None:
                    residency.schedule_restore()
```

**How the evicted set is remembered** — `backend/src/jbrain/llm/residency.py:248-253`:
```
        if not self._enabled:
            return
        for name in served_names:
            if local_catalog.get_by_served(name) is not None:
                self._displaced.add(name)
                self._prefix_lost(name)
```

**Restore budget** — `backend/src/jbrain/llm/residency.py:614-619`:
```
        running = await self._gateway.running()
        mem = read_memory_gb()
        if mem is None:
            return  # can't budget the restore — leave cold members to load on demand
        total, used = mem
        ceiling = total * (1.0 - await self._fraction())
```
`backend/src/jbrain/llm/residency.py:634-635`:
```
            if used + fp > ceiling:
                continue  # no room without evicting a resident model — leave it for later
```

### 3.3 ComfyUI resident-size figures in the catalog (estimates, not measurements)

`backend/src/jbrain/image_gen/catalog.py:69-72`
```
    # Resident unified-memory footprint ESTIMATE (not a measurement) the RAM meter
    # reserves while this model is loaded; on Strix Halo generation itself barely
    # moves RAM beyond the loaded weights (host-observed), so this is ~the load cost.
    vram_gb: float
```
| model id | `size_gb` | `vram_gb` | file:line of `vram_gb` |
|---|---|---|---|
| `qwen-image` (`catalog.py:139`) | `        size_gb=58.0,` (`:144`) | `        vram_gb=58.0,` | `catalog.py:148` |
| `qwen-image-lightning` (`:160`) | `        size_gb=58.9,` (`:168`) | `        vram_gb=58.0,` | `catalog.py:169` |
| `qwen-image-edit` (`:181`) | `        size_gb=51.0,` (`:186`) | `        vram_gb=55.0,` | `catalog.py:190` |
| `qwen-image-edit-lightning` (`:201`) | `        size_gb=51.9,` (`:208`) | `        vram_gb=55.0,` | `catalog.py:209` |
| `dreamshaper` (`:219`) | `        size_gb=6.7,` (`:224`) | `        vram_gb=7.0,` | `catalog.py:227` |

`backend/src/jbrain/image_gen/weights.py:24-27` (the on-disk measurement):
```
def weights_size_gb(models_dir: str, model: ImageModel) -> float | None:
    """Summed size of `model`'s present weight files, in GiB — or None when none of
    them are on this box. scripts/comfyui-setup.sh places each file at
    `<models_dir>/<dest_subdir>/<basename>`."""
```
`backend/src/jbrain/image_gen/weights.py:20-21`:
```
# Weights are GiB-scale; report in GiB to match the catalog's size_gb units.
_BYTES_PER_GIB = 1024**3
```

**These `vram_gb` numbers are not consumed by the LLM residency budget.**
`backend/src/jbrain/llm/residency.py:322-334` (`_footprint`) reads only
`local_catalog.get_by_served(...)`; `grep -rn "image_gen.catalog\|image_gen import catalog"
backend/src/jbrain/llm` returns nothing. The only consumer found is the settings screen:
`backend/src/jbrain/api/image_settings.py:91` `        vram_gb=m.vram_gb,`.

### 3.4 Resolution/step knobs that change the render's memory peak

`backend/src/jbrain/image_gen/render.py:84-93`
```
# resolution → (generate square-edge px, edit total-megapixels). Medium is the model's
# native ~1 MP and the default; `small` cuts the activation/VAE-decode memory peak (the
# weights are fixed, but decode memory scales with pixel count) for headroom on a tight
# unified-memory box; `large` trades that headroom back for more detail.
_RESOLUTIONS: dict[str, tuple[int, float]] = {
    "small": (768, 0.9),
    "medium": (1024, 1.6),
    "large": (1280, 2.5),
}
_DEFAULT_RESOLUTION = "medium"
```

---

## 4. Memory measurements in the tree

| # | file:line | what it reads | units | verbatim |
|---|---|---|---|---|
| M1 | `backend/src/jbrain/host_metrics.py:57` | `/proc/meminfo` `MemTotal`, `MemFree`, `SReclaimable` | kB → GiB | `    wanted = ("MemTotal", "MemFree", "SReclaimable")` |
| M1b | `backend/src/jbrain/host_metrics.py:73-75` | derived total/used | GiB, rounded 1dp | `    total = fields["MemTotal"] / _KB_PER_GIB` / `    free = (fields["MemFree"] + fields.get("SReclaimable", 0)) / _KB_PER_GIB` / `    return round(total, 1), round(total - free, 1)` |
| M1c | `backend/src/jbrain/host_metrics.py:21-22` | unit constant | — | `# /proc/meminfo reports kB; dividing by this yields GiB.` / `_KB_PER_GIB = 1024 * 1024` |
| M1d | `backend/src/jbrain/host_metrics.py:28` | policy statement | — | `    `used = MemTotal - MemFree - SReclaimable`. **Page cache counts as USED.**` |
| M2 | `backend/src/jbrain/host_metrics.py:97-98` | `/proc/meminfo` `Cached` | kB → GiB, 1dp | `                if key == "Cached":` / `                    return round(int(rest.strip().split()[0]) / _KB_PER_GIB, 1)` |
| M3 | `backend/src/jbrain/host_metrics.py:118` | `/sys/class/drm/card*/device/gpu_busy_percent` | 0-100 | `        cards = sorted(drm.glob("card*/device/gpu_busy_percent"))` |
| M4 | `supervisor/src/supervisor/host_metrics.py:244-249` | amdgpu `mem_info_{gtt,vram}_{used,total}` | bytes | `    fields = (` / `        "mem_info_gtt_used",` / `        "mem_info_gtt_total",` / `        "mem_info_vram_used",` / `        "mem_info_vram_total",` / `    )` |
| M4b | `supervisor/src/supervisor/host_metrics.py:394-395` | `MemTotal`, **`MemAvailable`** | bytes | `        mem_total_bytes=mem.get("MemTotal", 0) * 1024,` / `        mem_available_bytes=mem.get("MemAvailable", 0) * 1024,` |
| M4c | `supervisor/src/supervisor/host_metrics.py:122-135` | curated meminfo slice | kB → bytes (`:392`) | `_MEMINFO_BREAKDOWN = (` … `"MemFree", "Buffers", "Cached", "Shmem", "AnonPages", "Mapped", "Slab", "SReclaimable", "SUnreclaim", "KReclaimable", "Unevictable", "Mlocked",` … `)` |
| M4d | `supervisor/src/supervisor/host_metrics.py:392` | breakdown scaling | kB→bytes | `    breakdown = {k: mem[k] * 1024 for k in _MEMINFO_BREAKDOWN if k in mem}` |
| M5 | `supervisor/src/supervisor/gateway.py:232-234` | per-container docker stats | bytes | `            mem = stats.get("memory_stats", {})` / `            usage = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)` / `            usages.append(ContainerMemory(service=service, mem_bytes=max(usage, 0)))` |
| M6 | `supervisor/src/supervisor/gateway.py:250` | per-process RSS via `docker top` | KiB (comment `:249`) | `                top = cast("dict", container.top(ps_args="-eo pid,rss,args"))` |
| M7 | `backend/src/jbrain/llm/gpu_guard.py:218-221` | supervisor `/metrics` `gpu_mem` | bytes → GB (`_BYTES_PER_GB = 1024**3`, `:84`) | `            gtt_used_gb=float(raw["gtt_used_bytes"]) / _BYTES_PER_GB,` / `            gtt_total_gb=float(raw["gtt_total_bytes"]) / _BYTES_PER_GB,` / `            vram_used_gb=float(raw.get("vram_used_bytes") or 0) / _BYTES_PER_GB,` / `            vram_total_gb=float(raw.get("vram_total_bytes") or 0) / _BYTES_PER_GB,` |
| M8 | `backend/src/jbrain/llm/gpu_guard.py:158` | HTTP read of supervisor metrics | — | `            resp = await client.get("/metrics", headers=headers)  # type: ignore[attr-defined]` |
| M9 | `backend/src/jbrain/llm/gpu_guard.py:262-263` | host free pages inside the device pre-flight | GB | `        mem = read_memory_gb()` / `        host_free_gb = None if mem is None else mem[0] - mem[1]` |
| M10 | `backend/src/jbrain/llm/residency.py:346` | host memory for the eviction plan | GiB | `        mem = read_memory_gb()` |
| M11 | `backend/src/jbrain/llm/residency.py:615` | host memory for the restore budget | GiB | `        mem = read_memory_gb()` |
| M12 | `backend/src/jbrain/llm/local_gateway.py:485` | page cache before a load | GiB | `        cache_before = host_metrics.read_page_cache_gb()` |
| M13 | `backend/src/jbrain/llm/local_gateway.py:496` | page cache during a load (poll) | GiB | `                now = host_metrics.read_page_cache_gb()` |
| M14 | `backend/src/jbrain/llm/local_gateway.py:520` | page cache after each sweep | GiB | `                last = host_metrics.read_page_cache_gb() or 0.0` |
| M15 | `backend/src/jbrain/llm/local_gateway.py:529` | page cache at end of load | GiB | `                    cache_after_gb=host_metrics.read_page_cache_gb(),` |
| M16 | `backend/src/jbrain/llm/local_weights.py:143` (fn `_cached_pages`) via `_SYS_CACHESTAT` | `cachestat(2)` per-fd cached pages | pages → GiB (`_PAGE_SIZE`, `:23`) | `_SYS_CACHESTAT = {"x86_64": 451, "aarch64": 451}.get(platform.machine())` (`:140`) |
| M17 | `backend/src/jbrain/llm/smoketest.py:112` | `MemFree + SReclaimable` | kB → GB (`:118`) | `        if key in ("MemFree", "SReclaimable"):` |
| M17b | `backend/src/jbrain/llm/smoketest.py:118` | derived | GB | `    return (free_kb["MemFree"] + free_kb.get("SReclaimable", 0)) / (1024 * 1024)` |
| M18 | `backend/src/jbrain/host_settings.py:62-71` | `MemTotal` for the ttm check | KiB → GiB | `def _mem_total_gib(proc: Path) -> float | None:` / `    text = _read(proc / "meminfo")` … `                return int(parts[1]) / _KIB_PER_GIB` |
| M19 | `backend/src/jbrain/image_gen/gateway.py:126-129` | ComfyUI `/system_stats` `vram_total`/`vram_free` | bytes → GB (`_BYTES_PER_GB = 1024**3`, `:28`) | `        vt, vf = dev.get("vram_total"), dev.get("vram_free")` / `        if isinstance(vt, int | float) and isinstance(vf, int | float):` / `            total += float(vt)` / `            free += float(vf)` |
| M20 | `backend/src/jbrain/api/llm_settings.py:568-572` | drawer meter | GiB | `    mem = read_memory_gb()` / `    if mem is None:` / `        return None` / `    total, used = mem` / `    return HostMemory(total_gb=total, used_gb=used, cache_gb=read_page_cache_gb())` |
| M21 | `backend/src/jbrain/ops_metrics.py:205-216` | persisted host sample | bytes | `                "mem_total": metrics["mem_total_bytes"],` / `                "mem_avail": metrics["mem_available_bytes"],` … `                "mem_free": _breakdown(metrics).get("MemFree"),` / `                "mem_cached": _breakdown(metrics).get("Cached"),` / `                "mem_sreclaimable": _breakdown(metrics).get("SReclaimable"),` / `                "gtt_used": _gpu_mem(metrics).get("gtt_used_bytes"),` … |
| M22 | `deploy/wall/serve.py:197-203` | `MemTotal` / **`MemAvailable`** | bytes | `            if len(parts) >= 2 and parts[0] == "MemTotal:":` / `                total = int(parts[1]) * 1024` / `            elif len(parts) >= 2 and parts[0] == "MemAvailable:":` / `                avail = int(parts[1]) * 1024` … `    return (total - avail, total)` |
| M23 | `deploy/wall/serve.py:216-220` | amdgpu VRAM only (no GTT) | bytes | `        u = _read_first_float(dev / "mem_info_vram_used")` / `        t = _read_first_float(dev / "mem_info_vram_total")` |
| M24 | `deploy/update-inner.sh:118-121` | **`MemAvailable`** in the updater | GB (integer) | `mem_available_gb() {` / `  awk '/^MemAvailable:/ { printf "%d", $2 / 1048576; f = 1; exit }` / `       END { if (!f) printf "?" }' /proc/meminfo 2>/dev/null || echo "?"` / `}` |
| M25 | `backend/src/jbrain/vitals_ring.py:29` | GPU busy only (no memory) | 0-100 | `from jbrain.host_metrics import read_gpu_busy_percent` |
| M26 | `backend/src/jbrain/llm/gpu_guard.py:405` | post-load device delta | GB | `    delta = after.device_used_gb - before.device_used_gb` |

The repo named the disagreement itself — `backend/src/jbrain/llm/residency.py:167-168`, **as
read on 2026-08-22; these lines no longer exist**, the default having been removed rather than
re-tuned:
```
        # the one the guard and the meter use — one of eight disagreeing memory budgets
        # this repo carried (see MEMORY_ADMISSION_PLAN.md (deleted), D0).
```

Explicit disagreement, self-documented at `backend/src/jbrain/host_metrics.py:30-33`:
```
    This deliberately does NOT use `MemAvailable`, which is what it used to do, on the
    reasoning that MemAvailable "excludes reclaimable page cache, so it reflects real memory
    pressure". On this box that reasoning is exactly inverted, and it cost a seven-hour
    livelock and a power cycle on 2026-08-19.
```
and at `backend/src/jbrain/llm/smoketest.py:94-97`:
```
    Reads `MemFree + SReclaimable`, NOT `MemAvailable`, for the reason
    `host_metrics.read_memory_gb` documents at length: `--no-mmap` leaves a page-cache copy
    of every model's weights behind, MemAvailable counts that as free, and reclaiming it
    while the iGPU pins most of RAM as GTT is the livelock this gate exists to avoid.
```
M4b, M22 and M24 read `MemAvailable`; M1, M17 read `MemFree + SReclaimable`. Both sets are
listed above with their `file:line`; reconciling them is out of scope for this inventory.

---

## 5. Hardcoded budget constants

| file:line | name & value | comment (verbatim, abbreviated where marked) |
|---|---|---|
| `backend/src/jbrain/config.py:391` | `    local_llm_free_ram_fraction: float = 0.15` | `backend/src/jbrain/config.py:379-384`: `    # The fraction of physical RAM the residency budget keeps FREE. The app is the box's sole` / `    # model evictor (the gateway never swaps on its own — every model is a llama-swap` / `    # `swap: false` member): before a model loads, jbrain.llm.residency.ensure_room evicts the` / `    # FEWEST resident models needed to keep >= this fraction free after it's resident (weights` / `    # + KV, biggest-first, staged last), so any model loads by unloading others until it fits.` / `    # Measured against live /proc/meminfo `used`, so image-gen and OS pressure count too. …` |
| `backend/src/jbrain/llm/residency.py:68` | `DEFAULT_FREE_RAM_FRACTION = 0.15` | `residency.py:66-67`: `# `Settings.local_llm_free_ram_fraction`; the operator override rides the settings` / `# store and is threaded in via `fraction_loader`.` |
| `backend/src/jbrain/llm/residency.py:169` (**since removed**) | `        free_ram_fraction: float = DEFAULT_FREE_RAM_FRACTION,` | `residency.py:164-168` (verbatim, all five lines): `        # Matches `Settings.local_llm_free_ram_fraction` (0.15). It was 0.25, so any` / `        # construction path that did not pass the fraction explicitly reserved 30 GiB` / `        # instead of 18.2 on this box and planned against a ceiling 12 GiB tighter than` / `        # the one the guard and the meter use — one of eight disagreeing memory budgets` / `        # this repo carried (see MEMORY_ADMISSION_PLAN.md (deleted), D0).` |
| `backend/src/jbrain/llm/residency.py:350` | `        ceiling = total * (1.0 - await self._fraction())  # keep used at/under this` | inline |
| `backend/src/jbrain/llm/residency.py:619` | `        ceiling = total * (1.0 - await self._fraction())` | inline (restore path) |
| `backend/src/jbrain/llm/gpu_guard.py:89` | `SAMPLE_INTERVAL_S = 1.0` | `gpu_guard.py:86-88`: `# How often the watchdog samples the device pool while a load runs. A model load is tens of` / `# seconds of I/O, so a 1s sample gives many chances to catch a climb; polling faster buys` / `# little and costs a supervisor round-trip each time.` |
| `backend/src/jbrain/llm/gpu_guard.py:95` | `RUNAWAY_MULTIPLE = 1.75` | `gpu_guard.py:91-94`: `# How far past its predicted footprint a load may push GTT before it is judged a runaway.` / `# Generous on purpose: llama.cpp's real device usage exceeds the weights (compute buffers,` / `# the graph, alignment), so a tight multiple would abort healthy loads. What it catches is` / `# the ORDER-OF-MAGNITUDE balloon that takes the host, not ordinary overshoot.` |
| `backend/src/jbrain/llm/gpu_guard.py:100` | `MIN_FREE_GTT_GB = 6.0` | `gpu_guard.py:97-99`: `# GTT the box must still have free for the host to stay alive. The freeze mode here is a` / `# reclaim livelock, not a clean OOM kill — the machine stops answering rather than losing a` / `# process — so this is a hard floor, held even when a load's own prediction says it fits.` |
| `backend/src/jbrain/llm/gpu_guard.py:330` | `    ceiling_gb = baseline.gtt_used_gb + max(projected_gb * RUNAWAY_MULTIPLE, projected_gb + 2.0)` | (the `+ 2.0` GB floor on the ceiling is inline, uncommented) |
| `backend/src/jbrain/llm/smoketest.py:85` | `LOAD_HEADROOM_GB = 20.0` | `smoketest.py:65,80-84`: `# Free RAM a load needs BEYOND the model's own resident cost, in GB.` … `# The number is the app's OWN steady-state floor, not a smaller one invented here. Every` / `# ordinary turn loads through `jbrain.llm.residency`, which keeps` / `# `local_llm_free_ram_fraction` (0.15) of RAM free — ~19.5 GB on this 130 GB box. …` |
| `backend/src/jbrain/llm/local_gateway.py:60-62` | `_SWEEP_POLL_S = 0.25` / `_SWEEP_INTERVAL_S = 2.0` / `_SWEEP_GROWTH_GB = 1.0` | `local_gateway.py:50-59` (abbrev.): `# How the in-flight page-cache sweep is paced (`_sweep_page_cache_during_load`).` … `# 1 GiB bounds the transient at roughly a second of read; 2 s bounds it when growth is slow.` / `# The poll itself is one small /proc/meminfo read, so it can be much finer than either.` |
| `backend/src/jbrain/llm/local_gateway.py:77` | `_WEIGHTS_SHARE = 0.9` | `local_gateway.py:64-76` (abbrev.): `# How the load span's one percentage is split between its two phases.` … |
| `backend/src/jbrain/llm/local_gateway.py:79` | `_PROGRESS_STEP = 0.01` | (uncommented) |
| `backend/src/jbrain/llm/local_gateway.py:84` | `_FOOTPRINT_DRIFT_GB = 1.0` | `local_gateway.py:81-83`: `# A catalog entry wrong by this much is worth waking someone for: the two found on` / `# 2026-08-19 were light by 1.4 and >5.5 GiB, and the smaller of those was enough to abort` / `# a healthy load. Below it, drift is ordinary per-build variation and is logged at info.` |
| `backend/src/jbrain/llm/local_gateway.py:322` | `                timeout=max(self._timeout, 30.0), transport=self._transport` | `local_gateway.py:300-305` (abbrev.): `        The timeout is WIDENED, like every other slow call on this client. llama-swap's` / `        `/api/models/unload/{model}` BLOCKS until the process has actually stopped` … |
| `backend/src/jbrain/config.py:357` | `    comfyui_timeout: float = 1800.0` | see §3.1 |
| `backend/src/jbrain/image_gen/comfyui.py:47` | `DEFAULT_TIMEOUT = 600.0` | (module default; `main.py:724` overrides with `settings.comfyui_timeout`) |
| `backend/src/jbrain/image_gen/render.py:88-92` | `_RESOLUTIONS` `small (768, 0.9)` / `medium (1024, 1.6)` / `large (1280, 2.5)` | see §3.4 |
| `backend/src/jbrain/image_gen/render.py:51-52` | `_FAST_STEPS = 4` / `_DREAMSHAPER_STEPS = 6  # DreamShaper XL Lightning's sweet spot in its tiny 4–8 band; not tunable` | `render.py:47-50` |
| `backend/src/jbrain/image_gen/render.py:65-67` | `_QUALITY_MIN_STEPS = 20` / `_QUALITY_MAX_STEPS = 40` / `_DEFAULT_QUALITY_STEPS = _QUALITY_MIN_STEPS` | see `render.py:47-50` |
| `backend/src/jbrain/embed.py:24` | `EMBED_BATCH = 16` | `embed.py:22-23`: `# TEI handles long inputs via truncate=true; small batches keep the 1g` / `# container comfortably inside its memory cap.` |
| `backend/src/jbrain/config.py:416` | `    whisper_max_bytes: int = 100 * 1024 * 1024` | `config.py:412-415`: `    # Per-attachment size budget (the docs/reference/ANALYSIS.md "Dispatcher-level policy"` / `    # cap, OCR's MAX_OCR_BYTES sibling): ingest skips enqueueing transcription for` / `    # larger files, with a logged warning and no cache row, so a smaller re-upload` / `    # transcribes normally. 100 MB ~ a long lossy recording.` |
| `backend/src/jbrain/config.py:411` | `    whisper_timeout: float = 300.0` | `config.py:407-410` |
| `backend/src/jbrain/ingest/video.py:343` | `WHISPER_CHUNK_S = 4 * 60.0` | `video.py:338-342`: `# Split a long transcription into pieces this long so no single whisper call runs past` / `# the client's request timeout (a 30-min clip in one call would; ~4-min chunks each` / `# finish in well under it), and the owner sees per-chunk progress. Chunking does not` / `# speed transcription up (one GPU, whisper already windows internally) — it makes a` / `# long transcription reliable and observable, and keeps partial text if a chunk fails.` |
| `scripts/whisper-setup.sh:110` | `    ttl: 300` | `whisper-setup.sh:83-84`: `# Write the one-model llama-swap config. The gateway loads whisper-server on the` / `# first request and frees it after `ttl` idle (and the app unloads it explicitly` |
| `scripts/whisper-setup.sh:95` | `healthCheckTimeout: 300` | `whisper-setup.sh:94`: `# Generous cold-load window for the CPU model load (the image's sample uses this too).` |
| `deploy/docker-compose.yml:641` | `    mem_limit: 1g` | `deploy/docker-compose.yml:640`: `    # 4GB host: cap the model server so a bad query can't starve Postgres.` |
| `deploy/oom-hardening.sh:51` | `echo 'EARLYOOM_ARGS="-r 60 -m 30 -m 20 -s 10 -s 5 --prefer ^llama-server$ --avoid ^(sshd|systemd|systemd-.*|dockerd|containerd|postgres|supervisor)$"' \` | `oom-hardening.sh:35-50` (abbrev.): `# The thresholds are 30/20 (SIGTERM/SIGKILL) rather than the 10/5 they were, and that is` / `# NOT a paranoia knob — it is what makes the trigger reachable at all.` … |
| `deploy/oom-hardening.sh:59-61` | `vm.min_free_kbytes = 2097152` / `vm.watermark_scale_factor = 200` / `vm.swappiness = 10` | `oom-hardening.sh:56-57`: `# Start reclaiming earlier and thrash into swap less, so the killer gets CPU to act BEFORE` / `# a unified-memory reclaim livelock — the failure mode earlyoom alone can lose to.` |
| `deploy/update-inner.sh:196` | `EARLYOOM_ARGS_LINE='EARLYOOM_ARGS="-r 60 -m 30 -m 20 -s 10 -s 5 --prefer ^llama-server$ --avoid ^(sshd|systemd|systemd-.*|dockerd|containerd|postgres|supervisor)$"'` | `update-inner.sh:191-195` |
| `scripts/strix-halo-host-setup.sh:76` | `RESERVE_GIB=16` | `strix-halo-host-setup.sh:72-75`: `# This used to hardcode 32505856 pages = 124 GiB ≈ 100% of RAM on this box, which DISABLES` / `# the backstop it exists to provide (kernel 6.18 has no physical-RAM sanity cap of its own;` / `# that lands in v7.2). It is now derived: MemTotal minus a 16 GiB host reserve, so the` / `# kernel refuses the allocation while there is still enough RAM left to stay responsive.` |
| `scripts/strix-halo-host-setup.sh:77-81` | `PAGES_LIMIT="$(awk -v reserve="$RESERVE_GIB" '` … `        pages = (kb - reserve * 1048576) / 4; if (pages < 1048576) pages = 1048576;` | as above |
| `deploy/update-inner.sh:265` | `QUIESCE_KEEP="db api supervisor proxy cloudflared"` | `update-inner.sh:259-264` |
| `backend/src/jbrain/ops_metrics.py:52` | `_SAMPLE_INTERVAL_SECONDS = 30` | (host-metrics sampling cadence) |
| `backend/src/jbrain/image_gen/liveness.py:52` | `        ttl_s: float = 30.0,` | `liveness.py:10-13`: `One cached bool with a short TTL keeps the per-turn cost at zero on the hot path` … |
| `backend/src/jbrain/llm/kv_prefix.py:96` | `MAX_STORE_BYTES = 25 * 1024**3` | `kv_prefix.py:91-95` (abbrev.): `# The whole \`.kvslots\` tree's disk allowance — the trade the owner chose on 2026-08-23` / `# (25 GiB of hard drive for prompt caches; changing it is a release, there is no knob).` — a DISK budget, not memory: LRU by mtime, least-recently-USED slot file evicted first (`kv_prefix.py:304-338`); files are ~2.2 GiB each. See §A.1d W11–W13 for the save/restore behaviour |
| `deploy/docker-compose.yml:418` | `      - ./local-models/.kvslots:/models/.kvslots` | `docker-compose.yml:411-417` (abbrev.): `      # The ONE writable carve-out in the otherwise read-only weights mount: llama-server` / `      # saves/restores KV-slot files here (jbrain.llm.kv_prefix, --slot-save-path).` … `      # The api holds the tree to its 25 GiB budget through its own rw mount` |

---

## 6. Supervisor / container topology

Compose services, from `grep -n "^  [a-z-]*:" deploy/docker-compose.yml` (top-level
`services:` children):

`proxy` (`:30`), `cloudflared` (`:75`), `api` (`:84`), `migrate` (`:234`), `wipe` (`:252`),
`worker` (`:270`), `db` (`:289`), `supervisor` (`:309`), `wall` (`:346`), `local-llm`
(`:373`), `comfyui` (`:423`), `tts-stt` (`:463`), `jcode` (`:515`), `jlaunch` (`:605`),
`embed` (`:635`), `searxng` (`:657`), `reader` (`:698`), `byparr` (`:718`), `rapidocr`
(`:736`), `htmlrender` (`:765`), `mqtt` (`:788`), `mqtt-ingest` (`:803`).
(Networks follow at `:816` `edge:`, `:817` `internal:`, `:821` `jcode:`, `:824` `jlaunch:`;
volumes at `:830` `render:`, `:834` `blobs:`, `:838` `tiles:` — the same grep pattern
matches those, so they are named here to avoid being mistaken for services.)

### Memory-relevant services

| service | line | profile | device access | mounts | command |
|---|---|---|---|---|---|
| `local-llm` | `:373` | `    profiles: [local-llm]` (`:375`) | `      - /dev/dri:/dev/dri` (`:397`) | `      - ./local-models:/models:ro` (`:410`) | `      ["llama-swap", "--listen", ":8080", "--config", "/models/llama-swap.yaml", "--watch-config"]` (`:395`) |
| `comfyui` | `:423` | `    profiles: [comfyui]` (`:425`) | `      - /dev/kfd:/dev/kfd` (`:436`), `      - /dev/dri:/dev/dri` (`:437`) | `      - ./comfyui-models:/opt/ComfyUI/models:ro` (`:448`) | `      ["python", "/opt/ComfyUI/main.py", "--listen", "0.0.0.0", "--port", "8188",` / `       "--preview-method", "auto"]` (`:433-434`) |
| `tts-stt` | `:463` | none (comment `:454`: `# wall's /tts forward). DEFAULT-ON (no profile) — read-aloud must not depend on enabling`) | `      - /dev/dri:/dev/dri` (`:483`) | `      - ./whisper-models:/models:ro` (`:492`), `      - ./src/deploy/tts-stt:/tts:ro` (`:493`) | `    entrypoint: ["/bin/sh", "/tts/entrypoint.sh"]` (`:480`), `    command: []` (`:481`) |
| `embed` | `:635` | none | none declared | `      - embed_models:/data` (`:643`) | `    command: ["--model-id", "${EMBED_MODEL:-BAAI/bge-small-en-v1.5}"]` (`:639`) |

`deploy/docker-compose.yml:451-462` (tts-stt header comment, verbatim):
```
  # tts-stt — the box's speech I/O in one always-on container: whisper.cpp speech-to-text
  # (llama-swap on :8080, the api reaches it at http://tts-stt:8080) AND warm Kokoro
  # text-to-speech (tts_server.py on :8801, reached by the api's read-aloud proxy and the
  # wall's /tts forward). DEFAULT-ON (no profile) — read-aloud must not depend on enabling
  # STT. Built on the official llama-swap unified image (llama-swap + ffmpeg, Vulkan) with
  # whisper.cpp's server + Python/Kokoro + the baked Kokoro weights layered on
  # (deploy/Dockerfile.tts-stt). /dev/dri + the GIDs give whisper the iGPU; Kokoro is CPU.
  # The CONTAINER is default-on (TTS always available); STT's GGML model is a heavy opt-in
  # download provisioned by `jbrain enable-whisper` (scripts/whisper-setup.sh), which also
  # sets WHISPER_URL so the api starts using it. Until then the entrypoint runs TTS only
  # (no whisper config -> no llama-swap), so a stock box serves read-aloud, not a dead STT
  # port. Internal only — neither port is published.
```

### Who can start/stop what

**Supervisor is the only holder of the Docker socket** — `backend/src/jbrain/api/image_settings.py:156-159`:
```
async def _toggle_service(request: Request, settings: Settings, action: str) -> ServiceActionOut:
    """Proxy a start/stop of the comfyui compose service to the supervisor (the
    only holder of the Docker socket). 409 when image hosting is off; 404 when the
    service was never provisioned (no container to toggle)."""
```

`supervisor/src/supervisor/gateway.py:200-207`:
```
    def start(self, service: str) -> None:
        # Acts on the EXISTING (created, stopped) container — the profile-gated
        # comfyui service is created by comfyui-setup.sh's `compose up`, so toggling
        # it on/off is a plain container start/stop. Unknown (never-created) 404s.
        self._find(service).start()

    def stop(self, service: str) -> None:
        self._find(service).stop()
```
`supervisor/src/supervisor/app.py:250-260`:
```
    @authed.post("/start", status_code=202)
    def start_service(body: ServiceRequest) -> ServiceActionResponse:
        # Toggle an existing-but-stopped service on (the comfyui profile service).
        # An unknown/never-created service raises UnknownServiceError -> 404.
        gateway.start(body.service)
        return ServiceActionResponse(service=body.service, action="start")

    @authed.post("/stop", status_code=202)
    def stop_service(body: ServiceRequest) -> ServiceActionResponse:
        gateway.stop(body.service)
        return ServiceActionResponse(service=body.service, action="stop")
```
(No allowlist appears on `/start` / `/stop`; the `known` set check at
`supervisor/src/supervisor/app.py:242-243` is inside `/restart` only:
```
        if body.service not in known:
            raise UnknownServiceError(body.service)
```
)

**Generic per-container power controls (any service, incl. `tts-stt`, `embed`, `comfyui`):**
`backend/src/jbrain/api/ops.py:365-391`:
```
# Start/stop a single existing container — the per-container power controls next to
# restart. Both proxy the supervisor's fixed-command gateway (docker start/stop on the
# existing container); an unknown/never-created service 404s.
async def _lifecycle(
    action: str, service: str, request: Request, settings: Settings
) -> dict[str, object]:
    resp = await _client(request).post(
        f"/{action}", json={"service": service}, headers=_headers(settings)
    )
```
PWA side, `frontend/src/api/client.ts:2968-2976`:
```
  /** Stop / start a single container (docker stop/start on the existing container) —
   * the per-container power controls next to Restart. */
  async opsStop(service: string): Promise<void> {
    await request("/api/ops/stop", jsonInit("POST", { service }));
  },

  async opsStart(service: string): Promise<void> {
    await request("/api/ops/start", jsonInit("POST", { service }));
  },
```
`frontend/src/screens/OpsScreen.tsx:1272-1278`:
```
  // Power a single container off/on. Stop is disruptive, so it confirms; Start is safe.
  const lifecycle = useCallback(
    async (service: string, action: "start" | "stop") => {
      if (action === "stop" && !window.confirm(`Stop ${service}?`)) return;
      setError(null);
      try {
        await (action === "stop" ? api.opsStop(service) : api.opsStart(service));
```

**ComfyUI-specific PWA controls** — `frontend/src/api/client.ts:2480-2494`:
```
  /** Unload cached models + free the service's VRAM; returns the refreshed snapshot. */
  async freeImageMemory(): Promise<ImageSettings> {
    const response = await request("/api/settings/image/free", { method: "POST" });
    return (await response.json()) as ImageSettings;
  },

  /** Start the (provisioned) ComfyUI service via the supervisor. */
  async startImageService(): Promise<void> {
    await request("/api/settings/image/service/start", { method: "POST" });
  },

  /** Stop the ComfyUI service via the supervisor (frees its memory by halting it). */
  async stopImageService(): Promise<void> {
    await request("/api/settings/image/service/stop", { method: "POST" });
  },
```
and the render-stop, `frontend/src/api/client.ts:2151`:
```
    await request("/api/settings/image/interrupt", { method: "POST" });
```

**Debug API** — `backend/src/jbrain/api/debug.py:1150-1183` `POST /llm/drop-page-cache`
(host page cache for LLM weights) and `backend/src/jbrain/api/debug.py:1366`/`:1380`
(`llm_settings.gateway_load` / `gateway_unload`) act on the **main LLM gateway**
(`_gateway(request)`); grep for `whisper` and `comfy` in `backend/src/jbrain/api/debug.py`
returns nothing.

**Deploy scripts.** `deploy/update-inner.sh:205-214` (global page-cache drop):
```
drop_page_cache() {
  _before="$(mem_available_gb)"
  echo "[update] dropping page cache ($_before GB available before)"
  if [ -n "$HOST_UPDATE" ]; then
    sync
    echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
  else
    docker run --rm --privileged --network none "$HELPER_IMAGE" \
      sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches' >/dev/null 2>&1 || true
  fi
```
`deploy/update-inner.sh:238-242` (controlled LLM unload before the memory-heavy phase):
```
release_models() {
  run_bounded "$TOGGLE_TIMEOUT_S" docker compose run --rm --no-deps -T api \
    python -m jbrain.cli local-llm-unload \
    || echo "[update] unload skipped (gateway unreachable?)"
}
```
which reaches `backend/src/jbrain/cli.py:62-74`:
```
    gateway = LocalGatewayClient(settings.local_llm_url, gpu_probe=gpu_guard.probe_for(settings))
    try:
        loaded = await gateway.running()
    except Exception as exc:  # noqa: BLE001
        print(f"[unload] gateway unreachable ({exc}) — nothing to unload")
        return 0
    if not loaded:
        print("[unload] gateway holds no models")
        return 0
    for served in sorted(loaded):
        try:
            await gateway.unload(served)
            print(f"[unload] released {served}")
```
— `local_llm_url`, i.e. **the main gateway only**; no whisper/ComfyUI equivalent exists
(grep `whisper|comfy` in `backend/src/jbrain/cli.py` returns nothing).

`deploy/update-inner.sh:295-322` quiesce (stops everything not in `QUIESCE_KEEP`) and
`:331-340` unquiesce, quoted in §2.4.

`deploy/update-inner.sh:772-775` (image-model sync during update):
```
    JBRAIN_INSTALL_DIR=/opt/jbrain2 sh src/scripts/comfyui-setup.sh $_ids \
      || echo "[update] image-model sync skipped (retry: sudo bash scripts/comfyui-setup.sh)"
```
`scripts/comfyui-setup.sh:184`:
```
docker compose --profile comfyui up -d comfyui
```
`scripts/comfyui-setup.sh:176-177`:
```
  echo "COMFYUI_ENABLED=true"
  echo "COMFYUI_URL=http://comfyui:8188"
```

`deploy/jbrain:14-15` (the profile the CLI wrapper adds):
```
if grep -q '^LOCAL_LLM_ENABLED=true' "$ENV_FILE" 2>/dev/null; then
  PROFILE+=(--profile local-llm)
```
`deploy/jbrain:17-18`:
```
# so it needs no --profile flag; `enable-whisper` just provisions its GGML model.
```

---

## 7. Things I could not determine

1. **Whether the tts-stt llama-swap and the local-llm llama-swap are ever the same
   endpoint in practice.** The code path is unambiguous in the tree (`WHISPER_URL` is
   written as `http://tts-stt:8080/v1` at `scripts/whisper-setup.sh:128`, and the compose
   file's `api` env defaults `JBRAIN_WHISPER_URL: ${WHISPER_URL:-}` at
   `deploy/docker-compose.yml:150`), but `WHISPER_URL` ultimately comes from the live box's
   `/opt/jbrain2/.env`, which is not in this repo. I could not read the deployed `.env`.
   The docstring at `backend/src/jbrain/config.py:392-393` still says "served by the same
   llama-swap gateway the `local-llm` profile runs"; I record both statements without
   reconciling them.
2. **Actual measured ComfyUI resident footprint.** `catalog.py:69` labels `vram_gb` an
   "ESTIMATE (not a measurement)". No measurement of a ComfyUI load is persisted anywhere I
   found — `gpu_guard.measure_footprint` (`backend/src/jbrain/llm/gpu_guard.py:393`) is only
   called from the LLM residency path (`grep -rn "measure_footprint" backend/src/jbrain`
   → `gpu_guard.py:393` and `residency.py` call sites only).
3. **Whether `ttm.pages_limit` is currently binding on the live box.** The setup script
   derives it (`scripts/strix-halo-host-setup.sh:77-81`) and the checker
   (`backend/src/jbrain/host_settings.py:74-90`) reads `/sys/module/ttm`, but the runtime
   value is a host fact not in the repo. `backend/src/jbrain/llm/gpu_guard.py:20-22` states
   `and this box currently sets it to ~100% of RAM, which disables that boundary` while
   `scripts/strix-halo-host-setup.sh:72-75` says the hardcoded 124 GiB value was replaced by
   a derived one; which is deployed, I cannot tell from the tree.
4. **Whether `/free` on ComfyUI actually returns memory to the pool.** I quoted only the
   HTTP call (`image_gen/gateway.py:88-92`); ComfyUI's own implementation is not in this
   repo.
5. **Kokoro TTS memory.** `deploy/tts-stt/entrypoint.sh:27` `exec "$TTS_PY" /tts/tts_server.py`
   runs a warm resident model in the same container as whisper
   (`deploy/docker-compose.yml:474` `      # Kokoro (TTS) — weights baked at /opt/kokoro; the warm server holds the model resident.`).
   I found no memory measurement, cap, or unload path for it anywhere
   (grep `kokoro` / `BRAIN_TTS` across `backend/src`, `supervisor/src` returns nothing);
   it is a fourth resident consumer with no accounting I could locate.
6. **Whether `/proc/meminfo`'s `used` (M1) actually reflects ComfyUI's GTT.** The config
   comment claims it (`backend/src/jbrain/config.py:384` `    # Measured against live /proc/meminfo `used`, so image-gen and OS pressure count too. This`),
   and `gpu_guard.py:49-51` states GTT pages come from the buddy allocator; I did not find
   code that verifies this, only the two comments.

---

# §C — Demand: everything that can cause a model to be needed

# Inventory: every DEMAND for a model

Repo `/home/user/JBrain2`, branch `claude/model-loading-chat-indicator-tujwms`.
All paths below are relative to `/home/user/JBrain2/backend/src/` unless prefixed with
`backend/migrations/`. Every row carries `file:line` and a verbatim quote. Facts only.

---

## 1. `TASK_DEFAULTS` — every entry, verbatim

`jbrain/llm/router.py:50` opens the table:

```
TASK_DEFAULTS: dict[str, str] = {
```

| # | line | entry (verbatim) |
|---|------|------------------|
| 1 | `jbrain/llm/router.py:51` | `    "note.extract": "xai:grok-4.3",` |
| 2 | `jbrain/llm/router.py:52` | `    "entity.disambiguate": "xai:grok-4.3",` |
| 3 | `jbrain/llm/router.py:53` | `    "fact.adjudicate": "xai:grok-4.3",` |
| 4 | `jbrain/llm/router.py:54` | `    "correction_note.extract": "xai:grok-4.3",` |
| 5 | `jbrain/llm/router.py:55` | `    "vision.ocr": "xai:grok-4.3",` |
| 6 | `jbrain/llm/router.py:56` | `    "vision.caption": "xai:grok-4.3",` |
| 7 | `jbrain/llm/router.py:59` | `    "agent.turn": "xai:grok-4.3",` |
| 8 | `jbrain/llm/router.py:64` | `    "agent.vision": "xai:grok-4.3",` |
| 9 | `jbrain/llm/router.py:68` | `    "integrate.note": "xai:grok-4.3",` |
| 10 | `jbrain/llm/router.py:73` | `    "intake.materialize": "xai:grok-4.3",` |
| 11 | `jbrain/llm/router.py:78` | `    "video.summarize": "xai:grok-4.3",` |
| 12 | `jbrain/llm/router.py:88` | `    "research.title": "xai:grok-4.3",` |
| 13 | `jbrain/llm/router.py:94` | `    "wiki.rewrite": "xai:grok-4.3",` |
| 14 | `jbrain/llm/router.py:95` | `    "wiki.ground": "xai:grok-4.3",` |
| 15 | `jbrain/llm/router.py:100` | `    "wiki.lint.contradiction": "xai:grok-4.3",` |
| 16 | `jbrain/llm/router.py:101` | `    "wiki.lint.stale": "xai:grok-4.3",` |
| 17 | `jbrain/llm/router.py:106` | `    "triage.classify": "xai:grok-4.3",` |
| 18 | `jbrain/llm/router.py:111` | `    "pet.turn": "xai:grok-4.3",` |
| 19 | `jbrain/llm/router.py:112` | `    "pet.thought": "xai:grok-4.3",` |
| 20 | `jbrain/llm/router.py:117` | `    "pet.statue": "xai:grok-4.3",` |

Twenty entries; every default spec is the identical string `"xai:grok-4.3"`.

### Task names present in `TASK_DEFAULTS` with no in-`src` `router.*` call site
Grep of `"<task>"` / `'<task>'` across `backend/src`, excluding `jbrain/llm/router.py`:

- `pet.thought` — only hit is `jbrain/api/llm_settings.py:84`: `    "pet.thought": "JPet — idle thought",` (a settings-screen label).
- `fact.adjudicate` — only hit is `jbrain/api/llm_settings.py:70`: `    "fact.adjudicate": "Fact adjudicate",`.
- `correction_note.extract` — only hit is `jbrain/api/llm_settings.py:73`: `    "correction_note.extract": "Correction extract",`.

### Tiers — `TIER_DEFAULTS`, verbatim (`jbrain/llm/router.py:175-179`)

```
TIER_DEFAULTS: dict[str, str] = {
    "high": "xai:grok-4.3",
    "low": "xai:grok-4.3",
    "vision": "xai:grok-4.3",
}
```

`jbrain/llm/router.py:181`: `PROVIDERS = ("anthropic", "xai", "local")`

### Reasoning buckets (`jbrain/llm/router.py:127-154`, verbatim entries)

```
TASK_REASONING_BUCKET: dict[str, str] = {
    # High reasoning
    "integrate.note": "high",
    "fact.adjudicate": "high",
    "wiki.ground": "high",
    "wiki.lint.contradiction": "high",
    "wiki.lint.stale": "high",
    "pet.statue": "high",
    # Medium reasoning
    "agent.turn": "medium",
    "note.extract": "medium",
    "correction_note.extract": "medium",
    "video.summarize": "medium",
    "wiki.rewrite": "medium",
    "intake.materialize": "medium",
    # Low reasoning
    "entity.disambiguate": "low",
    "research.title": "low",
    "triage.classify": "low",
    "pet.turn": "low",
    "pet.thought": "low",
}
```

`jbrain/llm/router.py:155-157`:
```
TASK_REASONING_DEFAULTS: dict[str, str] = {
    task: effort for task, effort in TASK_REASONING_BUCKET.items() if effort != "medium"
}
```

`jbrain/llm/router.py:160`: `_PRIMARY_MODEL_TASK = "agent.turn"`
`jbrain/llm/router.py:168`: `_FOLLOW_PRIMARY_MODEL = frozenset({"research.title"})`

---

## 2. The FULL precedence chain that decides which model serves a task

### Step 0 — construction (`jbrain/llm/router.py:808`, `build_router`)

`jbrain/llm/router.py:846-861` (verbatim excerpt):
```
    return LlmRouter(
        clients,
        resolve_tasks(settings.llm_tasks),
        recorder=recorder,
        tiers=resolve_tiers(settings.llm_tiers),
        pinned=frozenset(settings.llm_tasks),
        overrides_loader=overrides_loader,
```
So `self._pinned` is exactly the key set of the env var `JBRAIN_LLM_TASKS`
(`jbrain/config.py:487`: `    llm_tasks: dict[str, str] = {}`, documented at
`jbrain/config.py:485-486`: `    # JSON object of per-task "provider:model" overrides, merged over the` /
`    # adapter defaults — see jbrain.llm.router.TASK_DEFAULTS.`).

`resolve_tasks` (`jbrain/llm/router.py:243-248`):
```
    merged = dict(TASK_DEFAULTS)
    for task, spec in overrides.items():
        if task not in TASK_DEFAULTS:
            raise LlmError(f"unknown LLM task in overrides: {task!r}")
        merged[task] = spec
    return {task: _split_spec(task, spec) for task, spec in merged.items()}
```

### Step 1 — static resolution, `_resolve` (`jbrain/llm/router.py:374-389`) — verbatim body

```
        if task in self._pinned:
            return self._tasks[task]
        if strength is not None:
            try:
                return self._tiers[strength]
            except KeyError:
                raise LlmError(f"unknown LLM strength tier: {strength!r}") from None
        try:
            return self._tasks[task]
        except KeyError:
            raise LlmError(f"unknown LLM task: {task!r}") from None
```

Order inside `_resolve`: **(a) env pin (`JBRAIN_LLM_TASKS`) → (b) the caller's
`strength` tier → (c) the merged task table (`TASK_DEFAULTS` + env overrides).**
A caller that passes a non-None `strength` and whose task is NOT env-pinned never
reaches the task table at all.

### Step 2 — live resolution, `_resolve_live` (`jbrain/llm/router.py:428-500`)

`jbrain/llm/router.py:447`: `        provider, model = self._resolve(task, strength)`
`jbrain/llm/router.py:450`: `        reasoning_effort: str | None = TASK_REASONING_DEFAULTS.get(task)`
`jbrain/llm/router.py:451-452`:
```
        if self._overrides_loader is not None:
            overrides = await self._overrides_loader()
```
`jbrain/llm/router.py:458-465` (the follow-task branch):
```
            followed = (
                task in _FOLLOW_PRIMARY_MODEL
                and strength is None
                and task not in self._pinned
                and _PRIMARY_MODEL_TASK in self._tasks
            )
            if followed:
                provider, model = self._followed_primary_model(overrides)
```
`jbrain/llm/router.py:466-484` (the stored per-task spec):
```
            entry = overrides.get(task) or {}
            spec = entry.get("spec")
            ...
            if spec is not None and not followed:
                try:
                    sp, sm = _split_spec(task, spec)
                except LlmError:
                    log.warning("llm.override_bad_spec", task=task, spec=spec)
                else:
                    # Ignore a local override the operator can no longer serve.
                    if sp == "local" and not self._local_enabled:
                        log.warning("llm.local_override_ignored", task=task, spec=spec)
                    else:
                        provider, model = sp, sm
```
`jbrain/llm/router.py:485-487`:
```
            stored_effort = entry.get("reasoning_effort")
            if stored_effort and not followed:
                reasoning_effort = stored_effort
```
`jbrain/llm/router.py:488-497` (the per-call override):
```
        if spec_override is not None:
            try:
                sp, sm = _split_spec(task, spec_override)
            except LlmError:
                log.warning("llm.call_override_bad_spec", task=task, spec=spec_override)
            else:
                if sp == "local" and not self._local_enabled:
                    log.warning("llm.local_call_override_ignored", task=task, spec=spec_override)
                else:
                    provider, model = sp, sm
```
`jbrain/llm/router.py:498-500`:
```
        if not _reasoning_capable(provider, model):
            reasoning_effort = None
        return provider, model, reasoning_effort
```

The docstring's own statement of precedence (`jbrain/llm/router.py:431-436`):
```
        """Resolve (provider, model, reasoning_effort) folding in the live DB
        overrides. A stored `spec` is the HIGHEST-precedence PERSISTENT selector —
        above an env pin, the strength tier, and the task default — because the
        settings screen is the operator's live control surface and must win over any
        deploy-time config.
```

**Assembled chain, last writer wins over earlier ones:**

| order | selector | code |
|---|---|---|
| 1 (lowest) | `TASK_DEFAULTS[task]` merged into `self._tasks` | `router.py:243`, `router.py:386` |
| 2 | `strength` tier from `TIER_DEFAULTS`/`settings.llm_tiers` (only when `task not in self._pinned`) | `router.py:380-383` |
| 3 | env pin `JBRAIN_LLM_TASKS` (short-circuits 1 and 2) | `router.py:379-380` |
| 4 | DB `llm_task_overrides[task]["spec"]` — skipped when `followed` | `router.py:474-484` |
| 4a | for `research.title` only: `_followed_primary_model(overrides)` = the resolved `agent.turn` route + its DB spec | `router.py:464-465`, `router.py:406-426` |
| 5 (highest) | per-call `spec_override` | `router.py:488-497` |
| gate | any `local:` spec at steps 4/4a/5 is discarded when `self._local_enabled` is false | `router.py:481-482`, `router.py:494-495` |

`_followed_primary_model` (`jbrain/llm/router.py:410-426`) verbatim body:
```
        provider, model = self._resolve(_PRIMARY_MODEL_TASK, None)
        spec = (overrides.get(_PRIMARY_MODEL_TASK) or {}).get("spec")
        if spec is not None:
            try:
                sp, sm = _split_spec(_PRIMARY_MODEL_TASK, spec)
            except LlmError:
                log.warning("llm.override_bad_spec", task=_PRIMARY_MODEL_TASK, spec=spec)
            else:
                if sp == "local" and not self._local_enabled:
                    log.warning("llm.local_override_ignored", task=_PRIMARY_MODEL_TASK, spec=spec)
                else:
                    provider, model = sp, sm
        return provider, model
```

The DB override source (`jbrain/settings_store.py:580-603`, `llm_task_overrides`), keyed
`jbrain/settings_store.py:57`: `LLM_TASK_OVERRIDES_KEY = "llm_task_overrides"`. Wired at
`jbrain/main.py:472`: `            overrides_loader=lambda: settings_store.llm_task_overrides(SYSTEM_CTX),`
and `jbrain/worker.py:566`: `        overrides_loader=lambda: worker_settings_store.llm_task_overrides(queue.SYSTEM_CTX),`.

### Step 3 — admission before a LOCAL call

Every one of `complete` / `converse` / `converse_stream` calls `_admit_local` after
resolution and before the client call:

- `jbrain/llm/router.py:619`: `        await self._admit_local(provider, model)` (in `complete`)
- `jbrain/llm/router.py:696`: `        await self._admit_local(provider, model)` (in `converse`)
- `jbrain/llm/router.py:747`: `        await self._admit_local(provider, model)` (in `converse_stream`)

`jbrain/llm/router.py:359-361`:
```
    async def _admit_local(self, provider: str, model: str) -> None:
        if provider == local_catalog.LOCAL_PROVIDER and self._residency is not None:
            await self._residency.ensure_room(model)
```

`jbrain/llm/router.py:363-372` — the caller-side admission for a self-loading caller:
```
    async def admit_local_load(self, served_model: str) -> None:
        """Make room for a local model a CALLER is about to load itself, through the same
        admission a routed completion gets.
        ...
        await self._admit_local(local_catalog.LOCAL_PROVIDER, served_model)
```

### Resolvers that do NOT call the model but do resolve a route

| surface | line | note (verbatim) |
|---|---|---|
| `spec()` | `jbrain/llm/router.py:549-555` | `        """The (provider, model) a task resolves to from STATIC config alone — env` … `        pin, prompt tier, or task default. It does NOT see the live DB overrides` |
| `effective_spec()` | `jbrain/llm/router.py:557-573` | `        return (await self._resolve_live(task, strength, spec_override))[:2]` |
| `context_window()` | `jbrain/llm/router.py:502-520` | `        provider, model, _ = await self._resolve_live(task, strength, spec_override)` |
| `supports_vision()` | `jbrain/llm/router.py:522-536` | `        provider, model, _ = await self._resolve_live(task, strength, spec_override)` |
| `effective_reasoning_effort()` | `jbrain/llm/router.py:538-547` | `        return (await self._resolve_live(task, strength, spec_override))[2]` |
| `primary_local_served_model()` | `jbrain/llm/router.py:391-404` | `        provider, model = self._followed_primary_model(overrides)` / `        return model if provider == local_catalog.LOCAL_PROVIDER else None` |
| `context_window_for_spec()` (module fn, no router) | `jbrain/llm/router.py:204-218` | `    provider, _, model = spec.partition(":")` |

---

## 3. Demand-site table — every caller that can reach a model

Classification key: **(a)** human-initiated turn; **(b)** tool call inside a human-initiated
turn; **(c)** agent-initiated, no human waiting; **(d)** scheduled/background.

### 3.1 Router `complete` / `converse` / `converse_stream` call sites

| # | file:line | verbatim call head | task passed | class | trace evidence (quoted) |
|---|---|---|---|---|---|
| D1 | `jbrain/agent/loop.py:1054` | `            async for part in self._router.converse_stream(` … `                self._task,` … `                strength=SYSTEM_STRENGTH,` | `self._task` (default `"agent.turn"`, `loop.py:547` `        self._task = task`; default arg `loop.py:539` `        task: str = "agent.turn",`) | a / c / d — depends on the constructing caller (rows D1a–D1e) | see below |
| D1a | `jbrain/api/agent.py:736` | `    loop = AgentLoop(` (args: `router,` `get_agent_registry(request),` `recorder=tally,` `guardrails=guardrails,` `model_override=model_override,`) | `agent.turn` | **(a)** | route: `jbrain/api/agent.py:647` `@router.post("/chat")` and `:648` `async def chat(request: Request, principal: OwnerDep, body: ChatRequest) -> StreamingResponse:` — an owner-authenticated HTTP POST carrying `body.message` |
| D1b | `jbrain/tasks/runner.py:138` | `        loop = AgentLoop(` (`self.router,` `self.registry,` `recorder=tally,` `guardrails=guardrails,`) | `agent.turn` | **(d)** | `jbrain/tasks/scheduler.py:57-61` `    due = await repo.claim_due(owner_ctx, now=now)` / `    for task in due:` / `        info = await runner.run(task_owner, task, trigger="schedule")`; driven by `jbrain/tasks/scheduler.py:76-81` `        while True:` … `            await tasks_tick(maker, repo, runner)` … `        await asyncio.sleep(interval)` with `:28` `TICK_INTERVAL_SECONDS = 60.0`; started at `jbrain/main.py:1096` `        tasks_loop_task = asyncio.create_task(` `            run_tasks_loop(maker, app.state.task_repo, app.state.task_runner)` |
| D1c | `jbrain/agent/spawn.py:1001` | `            loop = AgentLoop(` (`self._router,` `self._registry,` `recorder=tally,`) | `agent.turn` (`spawn.py:81` `_CHILD_TASK = "agent.turn"`) | **(b)** when the fan is spawned inside a `/chat` turn; **(c)/(d)** when the root is a headless run | `jbrain/agent/spawn.py:1078-1109` `                result = await asyncio.wait_for(` `                    loop.run(` … `                    timeout=child_timeout,` ; the fan root is gated on `ctx.tree` — `jbrain/tasks/runner.py:150` `        tree = TreeState.rooted(guardrails.max_cost_tokens) if root_tree else None` and `jbrain/tasks/runner.py:48` `    return "news" in task.name.casefold()` |
| D1d | `jbrain/api/intake.py:459` | `    loop = AgentLoop(router_llm, registry, guardrails=guardrails)` | `agent.turn` | **(a)** (a non-owner human) | route `jbrain/api/intake.py:429` `@router.post("/intake/chat")` / `:430-435` `async def intake_chat(` … `principal: PrincipalDep,` ; body `:436-438` `    """One interview turn for a redeemed link, streamed as SSE.` |
| D1e | `jbrain/wiki/editor.py:111` | `    loop = AgentLoop(router, registry, recorder=tally)` | `agent.turn` | **(a)** | route `jbrain/api/wiki.py:181` `@router.post("/wiki/{article_id}/talk/topics/{topic_id}/editor", status_code=201)` / `:183` `    article_id: str, topic_id: str, body: EditorRequest, owner: OwnerDep, request: Request` → `jbrain/api/wiki.py:201` `    reply = await run_editor_turn(` |
| D2 | `jbrain/agent/loop.py:1463` | `            turn = await self._router.converse(` `                self._task,` … `                strength=SYSTEM_STRENGTH,` | as D1 | as D1 | non-streaming twin, same `AgentLoop` instances |
| D3 | `jbrain/agent/loop.py:626` | `            return await self._router.converse(` `                self._task,` … `                effort_override=reasoning_effort,` | as D1 | as D1c mostly | `_converse_turn` — `loop.py:610-614` `        if on_text is None and on_reasoning is None:` |
| D4 | `jbrain/agent/loop.py:641` | `        async for part in self._router.converse_stream(` `            self._task,` … `            effort_override=reasoning_effort,` | as D1 | as D1c | same `_converse_turn`, streaming branch |
| D5 | `jbrain/agent/deep_research.py:2401` | `            result = await self._router.complete(` `                _TASK,` | `agent.turn` (`deep_research.py:114` `_TASK = "agent.turn"`) | **(b)** or **(c)** | reached from `_plan` (`deep_research.py:2415` `        result = await self._complete_json(`) and `_reflect` (`deep_research.py:2844` `        result = await self._complete_json(`) |
| D6 | `jbrain/agent/deep_research.py:2970` | `        async for part in self._router.converse_stream(` `            _TASK,` `            system=_SYNTH.render(),` | `agent.turn` | **(b)** or **(c)** | called at `deep_research.py:2083` `            report = await self._synthesize(`, `:2111` `                candidate = await self._synthesize(`, `:2179` `                    report = await self._synthesize(` |
| D7 | `jbrain/agent/daily_briefing.py:501` | `        async for part in self._router.converse_stream(` `            _TASK,` `            system=_SYNTH.render(),` | `agent.turn` (`daily_briefing.py:46` `_TASK = "agent.turn"`) | **(b)** or **(c)** | constructed only at `jbrain/agent/deep_research.py:1350` `        self._briefing = DailyBriefingBuilder(router, feeds=feeds, searxng=searxng, fetcher=fetcher)` |
| D8 | `jbrain/llm/warm_keeper.py:189` | `            await self._router.converse(` `                AGENT_TURN_TASK,` `                system=system,` `                messages=[UserMessage(text="warmup")],` `                tools=tools,` `                max_tokens=1,` | `agent.turn` (`warm_keeper.py:50` `AGENT_TURN_TASK = "agent.turn"`) | **(d)** | `warm_keeper.py:211-218` `        while True:` `            settled = True` `            try:` `                settled = await self.reconcile_once()` … `            await asyncio.sleep(self._interval_ready if settled else self._interval_wait)`; started detached at `jbrain/main.py:1176` `        warm_keeper_task = asyncio.create_task(app.state.warm_keeper.run())` |
| D9 | `jbrain/analysis/pipeline.py:261` | `            result = await router.complete(` `                "note.extract",` … `                strength=NOTE_EXTRACT_STRENGTH,` | `note.extract` | **(d)** | handler `jbrain/worker.py:611` `        "integrate_note": analyzer.integrate_note,`, claimed by `jbrain/worker.py:160` `    job = await queue.claim(maker, queue.SYSTEM_CTX)` inside `process_one`, driven by `jbrain/worker.py:453-457` `            if not held and await process_one(` |
| D10 | `jbrain/analysis/integrate.py:50` | `        result = await self._router.complete(` `            "integrate.note",` … `            strength=INTEGRATE_STRENGTH,` | `integrate.note` | **(d)** | same `integrate_note` job as D9 |
| D11 | `jbrain/analysis/pipeline.py:1194` | `                result = await self._router.complete(` `                    DISAMBIGUATE_TASK,` … `                    strength=DISAMBIGUATE_STRENGTH,` | `entity.disambiguate` (`jbrain/analysis/entities.py:531` `DISAMBIGUATE_TASK = _DISAMBIGUATE.name`; `jbrain/analysis/prompts/entity_disambiguate.prompt:2` `name: entity.disambiguate`) | **(d)** | same job as D9 |
| D12 | `jbrain/ingest/ocr.py:181` | `        result = await router.complete(` `            "vision.ocr",` … `            strength=OCR_STRENGTH,` | `vision.ocr` | **(d)** | in `ocr_pdf_pages`; handler `jbrain/worker.py:613` `        "ocr_attachment": OcrPipeline(` |
| D13 | `jbrain/ingest/ocr.py:274` | `                result = await self._router.complete(` `                    "vision.ocr",` | `vision.ocr` | **(d)** | inside `async def _vlm_ocr() -> str:` (`ocr.py:272`), awaited at `ocr.py:287` `            vlm_text, rapid = await asyncio.gather(_vlm_ocr(), self._rapid_ocr(data, media_type))` |
| D14 | `jbrain/ingest/ocr.py:315` | `            description = await self._router.complete(` `                "vision.caption",` | `vision.caption` | **(d)** | same handler |
| D15 | `jbrain/ingest/video.py:243` | `        caption = await router.complete(` `            FRAME_CAPTION_TASK,` | `agent.vision` (`video.py:98` `FRAME_CAPTION_TASK = "agent.vision"`) | **(d)** | handler `jbrain/worker.py:647` `        ).analyze_video_attachment,` |
| D16 | `jbrain/ingest/video.py:275` | `    summary = await router.complete(` `        SUMMARY_TASK, system=SUMMARY_SYSTEM, user_text=timeline, max_tokens=SUMMARY_MAX_TOKENS` | `video.summarize` (`video.py:99` `SUMMARY_TASK = "video.summarize"`) | **(d)** | same |
| D17 | `jbrain/gmail/triage.py:250` | `            result = await self._router.complete(` `                task="triage.classify",` | `triage.classify` | **(d)** | handler `jbrain/worker.py:721` `        "triage_inbox": triage_inbox_handler(gmail_provider.client, router, maker),`; fired by the seeded hourly schedule (§4) |
| D18 | `jbrain/wiki/rewriter.py:145` | `        result = await self._router.complete(` `            "wiki.rewrite", system=system, user_text=user_text, json_schema=_REWRITE_SCHEMA` | `wiki.rewrite` | **(d)** | handler `jbrain/worker.py:726-731` `        **wiki_handlers(` … `            rewriter=LlmRewriter(router, settings=worker_settings_store, ctx=queue.SYSTEM_CTX),` |
| D19 | `jbrain/wiki/rewriter.py:172` | `        result = await self._router.complete(` `            "wiki.ground", system=system, user_text=user_text, json_schema=_GROUND_SCHEMA` | `wiki.ground` | **(d)** | same |
| D20 | `jbrain/wiki/lint.py:744` | `            result = await self._router.complete(` `                task, system=system, user_text=user_text, json_schema=schema` | `wiki.lint.contradiction` (`lint.py:520`) and `wiki.lint.stale` (`lint.py:658`) | **(d)** | handler `jbrain/worker.py:737-742` `        "wiki_lint": wiki_lint_handler(` |
| D21 | `jbrain/external/report_titler.py:87` | `        result = await self._router.complete(` `            _TASK,` … `            spec_override=str(model_spec) if model_spec is not None else None,` | `research.title` (`report_titler.py:30` `_TASK = "research.title"`) | **(d)** | handler `jbrain/worker.py:609` `        "title_research_report": research_report_titler.title_research_report,` |
| D22 | `jbrain/intake/materialize.py:64` | `    result = await router.complete(` `        "intake.materialize",` … `        strength=_PROMPT.strength,` | `intake.materialize` | **(a)** | route `jbrain/api/intake.py:267` `@router.post("/intake/submissions/{submission_id}/materialize", status_code=201)` → `:275` `    proposal_id = await materialize_submission(` |
| D23 | `jbrain/jpet/brain.py:473` | `    result = await router.complete(` `        "pet.turn",` | `pet.turn` | **(a)** | `jbrain/api/pet.py:259` `@router.post("/command")` → `:268` `        info = await _say(request, ctx, domain, state, (body.text or "").strip())` → `jbrain/api/pet.py:324` `            reply = await pet_turn(`; also `jbrain/api/pet.py:384` `@internal_router.post("/say")` → `:402` `    info = await _say(request, ctx, domain, state, text[:500], remember=False)` |
| D24 | `jbrain/jpet/brain.py:492` | `    result = await router.complete(` `        "pet.statue",` | `pet.statue` | **(a)** | `jbrain/api/pet.py:415` `@internal_router.post("/statue")` → `:429` `        voxels = await statue_voxels(_router(request), subject=subject[:80])` |
| D25 | `jbrain/ingest/emr/pathology.py:77` | `        result = await router.complete(` `            PATHOLOGY_TASK,` | `emr.pathology_diagnosis` (`pathology.py:34` `PATHOLOGY_TASK = _PROMPT.name`; `jbrain/ingest/emr/prompts/pathology_diagnosis.prompt:2` `name: emr.pathology_diagnosis`) — **not in `TASK_DEFAULTS`** | **(d)** | handler `jbrain/worker.py:677` `        "emr_parse": EmrImportPipeline(maker, blobs, analyzer).parse,`; guarded by `pathology.py:72` `        router.spec(PATHOLOGY_TASK)  # routability probe; unrouted -> skip the LLM` |
| D26 | `jbrain/agent/visiontools.py:129` | `            result = await router.complete(` `                "agent.vision",` … `                spec_override=vision_spec,` | `agent.vision` | **(b)** | tool `compare_images`; `visiontools.py:126` `        vision_spec = await vision_read_spec(router, ctx.model_override)` — `ctx.model_override` is set from the loop's turn (`jbrain/agent/loop.py:718` `            model_override=self._model_override,`) |
| D27 | `jbrain/agent/imagegentools.py:396` | `                result = await router.complete(` `                    "agent.vision",` … `                    spec_override=vision_spec,` | `agent.vision` | **(b)** | tool `analyze_image` (`imagegentools.py:370` `    async def analyze_image_tool(arguments: dict, ctx: ToolContext) -> str:`) |
| D28 | `jbrain/agent/imagegentools.py:421` | `                ocr = await router.complete(` `                    "vision.ocr",` … `                    spec_override=vision_spec,` | `vision.ocr` | **(b)** | same tool, second pass, gated by `imagegentools.py:417` `        if has_text:` |
| D29 | `jbrain/agent/croptools.py:239` | `            result = await router.complete(` `                "agent.vision",` … `                strength="vision",` | `agent.vision` with `strength="vision"` | **(b)** | `croptools.py:227` `    async def _ground(` reached from `crop_regions` tool (`croptools.py:275` `    return {"crop_regions": crop_regions_tool}`) |
| D30 | `jbrain/agent/drawtools.py:199` | `            result = await router.complete(` `                "agent.vision",` … `                strength="vision",` | `agent.vision` / `"vision"` | **(b)** | `drawtools.py:184` `    async def _look(ctx: ToolContext, scene: Scene, question: str) -> str:` reached from `canvas_tool` |
| D31 | `jbrain/agent/htmltools.py:103` | `            result = await router.complete(` `                "agent.vision",` … `                strength="vision",` | `agent.vision` / `"vision"` | **(b)** | `htmltools.py:98` `    async def _look(png: bytes, question: str, ctx: ToolContext) -> str:` reached from `render_html_tool` |
| D32 | `jbrain/agent/grabtools.py:161` | `            result = await router.complete(` `                "agent.vision", system=_VISION_SYSTEM, user_text=question, images=[image]` | `agent.vision` | **(b)** | `grabtools.py:158` `    async def _vision_read(frame: bytes, question: str) -> str:` reached from `grab_frame_tool` |
| D33 | `jbrain/api/debug.py:405` | `        result = await router_.complete(` `            task,` … `            strength=strength,` | `body.task` or `"debug.complete"` (`debug.py:373` `    task = body.task or "debug.complete"`) | **(a)** | route `jbrain/api/debug.py:429` `@router.post("/complete")` |
| D34 | `jbrain/api/debug.py:348` | `    async for part in router_.converse_stream(` `        task,` | as D33 | **(a)** | same route, `debug.py:385` `        if body.stream:` |
| D35 | `jbrain/api/debug.py:509` | `        turn = await router_.converse(` `            body.task,` | `body.task` | **(a)** | route `jbrain/api/debug.py:480` `@router.post("/tool-probe")` |
| D36 | `jbrain/api/debug.py:593` | `        result = await router_.complete(` `            body.task,` … `            strength="vision",` | `body.task` (`_VISION_DEFAULTS`-keyed) | **(a)** | route `jbrain/api/debug.py:759` `@router.post("/vision")` |
| D37 | `jbrain/api/debug.py:705` | `        result = await router_.complete(` `            "agent.vision",` … `            strength="vision",` | `agent.vision` | **(a)** | route `jbrain/api/debug.py:664` `@router.post("/grounding")` |
| D38 | `jbrain/evals/runner.py:269`, `jbrain/evals/disambiguate_runner.py:97`, `jbrain/evals/integrate_runner.py:210` | `            out = await router.complete(` | `note.extract` / `entity.disambiguate` / `integrate.note` | not wired into the running app | no import of `jbrain.evals` exists outside `backend/tests/` and `backend/evals/` (grep of `jbrain.evals` over the repo returns only those two trees); `backend/tests/unit/test_no_evals_boot.py:13` `` `jbrain.evals.runner`, so the analysis eval scoring runs in production. The`` |

### 3.2 Route-resolution-only sites (no completion, but they read the live route)

| file:line | verbatim | task | class |
|---|---|---|---|
| `jbrain/api/agent.py:712` | `    effort = await router.effective_reasoning_effort("agent.turn", spec_override=model_override)` | `agent.turn` | (a) |
| `jbrain/api/agent.py:717` | `    context_window = await router.context_window("agent.turn", spec_override=model_override)` | `agent.turn` | (a) |
| `jbrain/api/agent.py:757` | `    can_see_images = await router.supports_vision("agent.turn", spec_override=model_override)` | `agent.turn` | (a) |
| `jbrain/api/agent.py:768` | `                spec=await router.effective_spec("agent.turn", spec_override=model_override),` | `agent.turn` | (a) |
| `jbrain/agent/readtools.py:172` | `        _provider, model = await router.effective_spec("agent.turn", spec_override=model_override)` | `agent.turn` | (a) via `canvas_hidden_tools` |
| `jbrain/agent/chat_images.py:66` | `    if await router.supports_vision("agent.vision", spec_override=model_override):` | `agent.vision` | (b) |
| `jbrain/agent/loop.py:585` | `            provider, _model = await self._router.effective_spec(self._task, SYSTEM_STRENGTH)` | loop task | a/b/c/d |
| `jbrain/agent/spawn.py:909` | `        provider, _model = await self._router.effective_spec(_CHILD_TASK)` | `agent.turn` | (b) |
| `jbrain/agent/spawn.py:948` | `            child_window = await self._router.context_window("agent.turn")` | `agent.turn` | (b) |
| `jbrain/agent/spawn.py:954` | `            child_provider, child_model = await self._router.effective_spec(_CHILD_TASK)` | `agent.turn` | (b) |
| `jbrain/agent/deep_research.py:2935` | `            await self._router.context_window(_TASK),` | `agent.turn` | (b)/(c) |
| `jbrain/tasks/runner.py:123` | `        effort = await self.router.effective_reasoning_effort("agent.turn")` | `agent.turn` | (d) |
| `jbrain/tasks/runner.py:128` | `        context_window = await self.router.context_window("agent.turn")` | `agent.turn` | (d) |
| `jbrain/api/intake.py:456-457` | `    effort = await router_llm.effective_reasoning_effort("agent.turn")` / `    context_window = await router_llm.context_window("agent.turn")` | `agent.turn` | (a) |
| `jbrain/analysis/pipeline.py:426` | `        provider, model = await self._router.effective_spec("integrate.note", INTEGRATE_STRENGTH)` | `integrate.note` | (d) |
| `jbrain/analysis/pipeline.py:1177` | `            self._router.spec(DISAMBIGUATE_TASK)` | `entity.disambiguate` | (d) |
| `jbrain/ingest/ocr.py:288`, `:323`, `:352` | `            spec = await self._router.effective_spec("vision.ocr", OCR_STRENGTH)` / `            spec = await self._router.effective_spec("vision.caption", DESCRIPTION_STRENGTH)` / `        spec = await self._router.effective_spec("vision.ocr", OCR_STRENGTH)` | vision tasks | (d) |
| `jbrain/ingest/video.py:286` | `        tool=":".join(await router.effective_spec(FRAME_CAPTION_TASK)),` | `agent.vision` | (d) |
| `jbrain/ingest/emr/import_handler.py:205` | `        provider, model = await self._pipeline._router.effective_spec(VISION_OCR_TASK, OCR_STRENGTH)` | `vision.ocr` | (d) |
| `jbrain/agent/croptools.py:236`, `jbrain/agent/drawtools.py:229` | `            _provider, model = await router.effective_spec(` `                "agent.vision", "vision", spec_override=spec` `            )` | `agent.vision` | (b) |
| `jbrain/workflow/preconditions.py:65` | `        provider, model = await router.effective_spec(task, strength)` | `triage.classify` (bound at `jbrain/worker.py:786`) | (d) |
| `jbrain/api/debug.py:386`, `:389`, `:416`, `:504`, `:592`, `:704` | e.g. `        provider, model = await router_.effective_spec(task, strength)` | various | (a) |
| `jbrain/llm/warm_keeper.py:120` | `        served = await self._router.primary_local_served_model()` | `agent.turn` | (d) |
| `jbrain/llm/warm_keeper.py:180` | `                await self._router.admit_local_load(served)` | `agent.turn` served name | (d) |

### 3.3 Model demand that does NOT go through `LlmRouter`

| file:line | verbatim | what it demands | admission before it? |
|---|---|---|---|
| `jbrain/api/jcode_llm.py:176` | `                        await residency.ensure_room(served)` | the caller-chosen served model, then `jbrain/api/jcode_llm.py:183` `                async with client.stream("POST", "/chat/completions", json=payload) as upstream:` | yes — `ensure_room` is called explicitly, inside `jbrain/api/jcode_llm.py:167` `            async with guard:` |
| `jbrain/api/external_llm.py:221-222` | `    if served not in resident:` / `        raise HTTPException(status_code=503, detail="the coder model is not loaded")` | nothing — refuses when not resident. `jbrain/api/external_llm.py:211` `    # 2. The coder must be resident — never trigger an on-demand load for a remote caller.` | n/a |
| `jbrain/transcribe.py:1` | `"""Audio transcription client (whisper.cpp via the on-box llama-swap gateway).` and `jbrain/transcribe.py:96` `                data={"model": self._model, "response_format": "verbose_json"},` | the whisper served model on the same gateway | no `ensure_room` call in `jbrain/transcribe.py` or `jbrain/ingest/transcribe_job.py` (grep for `ensure_room` returns no hit in either) |
| `jbrain/image_gen/render.py:202-205` | `            for served in await gateway.running():` / `                await gateway.unload(served)` / `                freed.append(served)` | frees every resident LLM for a render; the reply then reloads: `jbrain/image_gen/render.py:187-190` `    the diffusion model (~39 GB bf16 on ROCm) can't both fit alongside the` … `    We free the LLM now; the agent loop's NEXT call —` `    to compose the reply after this tool returns — transparently reloads it via` `    llama-swap's on-demand loading` | n/a (unload path) |
| `jbrain/api/agent.py:1116-1117` | `                if residency is not None:` / `                    residency.schedule_restore()` | reloads the displaced set after a `/chat` turn | `jbrain/llm/residency.py:255-265` `    def schedule_restore(self) -> None:` … `        task = asyncio.create_task(self._restore())` |
| `jbrain/api/jcode.py:477` | `        residency.schedule_restore()` | re-warms the hot set when code mode powers off | same |
| `jbrain/api/llm_settings.py:1519` | `            await gateway.load(model.served_model, warm_system=warm_system, warm_tools=warm_tools)` | operator-pressed Load | (a) |
| `jbrain/api/llm_settings.py:1891` | `        await gateway.load(model.served_model, warm_system=warm_system, warm_tools=warm_tools)` | operator-pressed Prime (`jbrain/api/debug.py:1481` `@router.post("/llm/local-models/{model_id}/prime")`) | (a) |
| `jbrain/llm/warm_keeper.py:181-182` | `                if served not in await self._gateway.running():` / `                    await self._gateway.load(served)` | the `agent.turn` local model | preceded by `warm_keeper.py:180` `                await self._router.admit_local_load(served)` |
| `jbrain/embed.py:34` | `    """text-embeddings-inference HTTP client (POST /embed)."""` | a separate TEI container — `jbrain/embed.py:3-4` `Embeddings come from the local TEI container (bge-small-en-v1.5, 384 dims) —` | n/a |

---

## 4. Schedules — everything time-driven that can reach a model

### 4.1 The tick loops

| loop | cadence (verbatim) | started at |
|---|---|---|
| workflow scheduler tick | `jbrain/workflow/scheduler.py:466` `TICK_SECONDS = 30.0`; gate `jbrain/worker.py:393` `        if not held and registry is not None and now - last_tick >= scheduler.TICK_SECONDS:` → `:394` `            await scheduler.run_tick_safely(maker, registry)` | worker `run_loop` |
| workflow dispatcher tick | `jbrain/workflow/dispatcher.py:667` `TICK_SECONDS = 2.0` | `jbrain/worker.py:411` `            await dispatcher.run_tick_safely(maker, registry, settings=settings, run_log=run_log)` |
| job claim | `jbrain/worker.py:79` `POLL_SECONDS = 2.0`; `jbrain/worker.py:462` `        await asyncio.sleep(POLL_SECONDS)` | worker `run_loop` |
| tasks tick | `jbrain/tasks/scheduler.py:28` `TICK_INTERVAL_SECONDS = 60.0` | `jbrain/main.py:1096` |
| plan continuation sweep | `jbrain/agent/continuation.py:62` `SWEEP_INTERVAL_S = 15` | `jbrain/main.py:1116` `        plan_continuation_task = asyncio.create_task(` |
| warm keeper | `jbrain/llm/warm_keeper.py:63-64` `        interval_ready: float = 60.0,` / `        interval_wait: float = 5.0,` | `jbrain/main.py:1176` |
| jpet tick | `jbrain/jpet/scheduler.py:27` `TICK_INTERVAL_SECONDS = 30.0` — `jbrain/jpet/scheduler.py:8` `queue, so the pet always takes second seat to real processing: it resolves the single` / `:9` `owner principal, ensures the pet, and touches `last_tick_at`. No LLM, no behaviour, no` | `jbrain/main.py:1137` |
| intake reaper | `jbrain/intake/sweep.py:21` `REAP_INTERVAL_SECONDS = 15 * 60` — no router call in the module (`grep LlmRouter jbrain/intake/sweep.py` → no hit) | `jbrain/main.py:1121` |

Code-mode pause on the worker (`jbrain/worker.py:452-456`):
```
            # Claim + run one job — UNLESS code mode holds the box (then no job runs, so nothing
            # loads a model that would contend with the coder; the job waits in the queue).
            if not held and await process_one(
                maker, handlers, registry=registry, preconditions=preconditions
            ):
```

### 4.2 Seeded `app.schedules` rows still present at the migration head as of 2026-08-22 (see `backend/migrations/versions/` for the current head)

Derived from the migration set; `enabled` is the value after every later `UPDATE`.
Nothing after `0121` touches any of these schedule ids (grep of the ids over
`backend/migrations/versions` lists only the migrations named below).

| pipeline | action(s) | interval | seeded time | enabled at head | precondition on action | seed migration(s) |
|---|---|---|---|---|---|---|
| `nightly_consolidate_predicates` | `consolidate_predicates` | `86400` | 02:00 UTC | yes (no `enabled` column given → table default) | none | `0038_seed_nightly_sweeps.py:89-91` `            INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at)` / `            VALUES ('{schedule_id}', 86400, 'UTC', {_NEXT_RUN_SQL})` |
| `nightly_sync_predicates` | `sync_predicates` | `86400` | 02:00 UTC | yes | none | `0038` |
| `nightly_purge_deleted_artifacts` | `purge_deleted_artifacts` | `86400` | 02:00 UTC | yes | none | `0038` |
| `reconcile_pending_notes` | `reconcile_pending_notes` | `0041_seed_reconciler_sweeps.py:50` `_INTERVAL_SECONDS = 300` | `now()` | yes | none | `0041` |
| `reconcile_pending_integration` | `reconcile_pending_integration` | `300` | `now()` | yes | none | `0041` |
| `reconcile_unembedded_notes` | `reconcile_unembedded_notes` | `0042_seed_unembedded_reconciler_sweep.py:48` `_INTERVAL_SECONDS = 300` | `now()` | yes | none | `0042` |
| `nightly_wiki_refresh` | `wiki_refresh` | `86400` | `"3 hours 30 minutes"` | **yes** (`0047` false → `0048` true → `0088` false → `0121` true) | none | `0047_seed_wiki_actions.py:105-106` (`            "INSERT INTO app.schedules (id, interval_seconds, timezone, next_run_at, enabled)"` / `            f" VALUES ('{sched_id}', 86400, 'UTC', {_next_run_sql(interval)}, {on})"`), `0121_enable_wiki_nightly_schedules.py:43` `    op.execute(f"UPDATE app.schedules SET enabled = true WHERE id IN {_IDS}")` |
| `nightly_wiki_prune` | `wiki_prune` | `86400` | `"3 hours 45 minutes"` | **yes** (same chain) | none | `0047`, `0121` |
| `wiki_rebuild_all` | `wiki_rebuild` (`{"target": "all"}`) | `86400` | `"4 hours"` | no | none | `0047` |
| `wiki_reindex_all` | `wiki_reindex` | `86400` | `"4 hours"` | no | none | `0047` |
| `geofence_sweep` | `geofence_sweep` | `0064_seed_geofence_sweep.py:41` `_INTERVAL_SECONDS = 900` | `now()` | yes | none | `0064` |
| `nightly_entity_hygiene` | `entity_hygiene` | `86400` | hour 2 | no (`0066_seed_hygiene_sweeps.py:85` `        # enabled=false: ships off; the owner turns it on from Ops when they want it.`) | none | `0066` |
| `nightly_reembed_stale` | `reembed_stale` | `86400` | hour 2 | no | none | `0066` |
| `nightly_tag_consolidate` | `tag_consolidate` | `86400` | hour 2 | no | none | `0066` |
| `daily_inbox_triage` | `triage_inbox` | **`3600`** (`0101_hourly_inbox_triage.py:25` `_HOURLY = 3600`) | reset to `now()` | **yes** (`0101_hourly_inbox_triage.py:36` `        f" SET interval_seconds = {_HOURLY}, enabled = true, next_run_at = now()"`) | **`reasoning_model_loaded`** (`jbrain/gmail/triage.py:120` `    precondition="reasoning_model_loaded",`) | `0096`, `0101` |
| `nightly_wiki_lint` | `wiki_lint` | `86400` | hour 4 | **yes** (`0119` false → `0121` true) | none | `0119`, `0121` |
| `expire_research_reports` | `expire_research_reports` | `0162_seed_research_report_expiry_sweep.py:36` `_INTERVAL_SECONDS = 86_400` | `now()` | yes | none | `0162` |

Deleted by `0091_drop_self_improvement_seeds.py:111-112`
(`    for _pipeline, schedule_id, _trigger_id in _SEEDS:` / `        op.execute(f"DELETE FROM app.schedules WHERE id = '{schedule_id}'")`):
`nightly_eval_run`, `nightly_skill_distill`, `nightly_skill_sweep`,
`nightly_predicate_review`, `nightly_correction_mine`, `nightly_prompt_self_edit`.

### 4.3 Which of those schedules reach a model

| pipeline | model demand | site |
|---|---|---|
| `nightly_wiki_refresh` | `wiki.rewrite` + `wiki.ground`, once per dirty entity | D18/D19 |
| `wiki_rebuild_all` (manual only) | same, once per active article | D18/D19 |
| `nightly_wiki_lint` | `wiki.lint.contradiction`, `wiki.lint.stale` | D20 |
| `daily_inbox_triage` | `triage.classify`, one call per email | D17 |
| `nightly_reembed_stale` / `wiki_reindex_all` | embeddings only — `jbrain/worker.py:713-715` `        "reembed_stale": reembed_handler(` `            maker, embedder=TeiEmbedClient(settings.embed_url), embedding_model=settings.embed_model` | TEI, not the gateway |
| all others in §4.2 | `jbrain/worker.py:706-708` `        # Phase-6 hygiene sweeps (docs/archive/HYGIENE_SWEEPS_PLAN.md): core-data` / `        # maintenance, no LLM,` | none |
| `reconcile_pending_notes` / `reconcile_pending_integration` | no direct model call; they ENQUEUE `ingest_note` / `integrate_note` jobs, which do (D9–D11) | `jbrain/workflow/scheduler.py:88-98` (`RECONCILE_PENDING_NOTES_ACTION` … `    description="Re-enqueue ingest for notes still pending.",`) |

### 4.4 The one registered `precondition=` in the codebase

`jbrain/workflow/registry.py:71`: `    precondition: str | None = None`
Only one `ActionSpec` sets it — `jbrain/gmail/triage.py:115-120`:
```
    # Run only when the model triage routes to is already resident: classification
    # makes one local LLM call per email, so firing while that model is cold would
    # swap out whatever the owner is actively using (a code model, an image session).
    # When unmet the run defers (5m, no attempt burned); the scheduler coalesces
    # re-fires so deferred runs never pile up. Inert on a cloud route (always met).
    precondition="reasoning_model_loaded",
```
Registered at `jbrain/worker.py:785-787`:
```
    preconditions: dict[str, Precondition] = {
        "reasoning_model_loaded": model_already_loaded(router, llm_gateway, task="triage.classify"),
    }
```
Check body (`jbrain/workflow/preconditions.py:215-221`):
```
    async def check() -> PreconditionResult:
        provider, model = await router.effective_spec(task, strength)
        if provider != "local":
            return PreconditionResult(met=True)
        if model in await gateway.running():
            return PreconditionResult(met=True)
        return PreconditionResult(met=False, reason=f"local model {model!r} not loaded")
```
Every other `ActionSpec` in the repo leaves `precondition` at its `None` default —
the 6 in `jbrain/workflow/registry.py:153-220`, the 6 in `jbrain/workflow/scheduler.py`
(`PURGE_ACTION:62`, `RECONCILE_PENDING_NOTES_ACTION:88`, `RECONCILE_PENDING_INTEGRATION_ACTION:100`,
`RECONCILE_UNEMBEDDED_NOTES_ACTION:118`, `GEOFENCE_SWEEP_ACTION:139`, `EXPIRE_RESEARCH_REPORTS_ACTION:158`),
`jbrain/wiki/actions.py:24,35,46,57`, `jbrain/wiki/lint.py:167`,
`jbrain/ingest/video.py:69`, `jbrain/ingest/stream_analysis.py:83`,
`jbrain/ingest/transcribe_job.py:50`, `jbrain/ingest/emr/import_handler.py:70`,
`jbrain/ingest/emr/intake_handler.py:38`, `jbrain/analysis/reembed.py:35`,
`jbrain/analysis/tagconsolidate.py:30`, `jbrain/analysis/hygiene.py:28`,
`jbrain/external/research_corpus.py:41,55`, `jbrain/external/corpus.py:41`.

### 4.5 Non-schedule background starters that reach a model

| starter | file:line | verbatim | class |
|---|---|---|---|
| boot resume of deepest-research runs | `jbrain/main.py:1019` | `            asyncio.create_task(app.state.deepest.resume_interrupted())` | (c) |
| its body | `jbrain/agent/deepest_tool.py:112` | `            if self._lane.launch(row.run_id, _run, wall_clock_s=wall_clock_s):` | (c) |
| detached deepest lane | `jbrain/agent/deepest_lane.py:71` | `        task = asyncio.create_task(self._supervise(run_id, run, wall_clock_s))` | (c) — kicked off by a tool inside a human turn, continues after it |
| plan continuation | `jbrain/agent/continuation.py:177-178` | `        for plan_id, sid in due:` / `            await self._run_one(plan_id, sid, pid, owner_ctx)` | (c) |
| its arming | `jbrain/agent/continuation.py:63` | `CONTINUATION_DELAY_S = 60` and `:64` `MAX_CONTINUATIONS = 20` | |
| warm keeper | `jbrain/llm/warm_keeper.py:188-195` | `            await self._router.converse(` `                AGENT_TURN_TASK,` … `                max_tokens=1,` | (d) |

---

## 5. Per-item loops that call the router repeatedly

| loop | file:line | items per job | verbatim exception handling |
|---|---|---|---|
| wiki refresh over dirty entities | `jbrain/wiki/builder.py:316-326` | one `_build_entity` per row of `SELECT id FROM app.entities WHERE NOT wiki_built ORDER BY created_at` (`builder.py:311`); each `_build_entity` → `builder.py:447` `        plan = await self._rewriter.plan(sourced)` → **2** router calls (`wiki.rewrite` + `wiki.ground`) | `            except WikiBudgetExceeded:` / `                break  # out of budget for today — leave the rest dirty for the next window` / `            except WikiGroundingError:` / `                continue  # one entity's verifier failed — leave it dirty, keep building the rest` — any other exception propagates out of `refresh()` |
| wiki rebuild | `jbrain/wiki/builder.py:357-366` | one per active article (`target == "all"`) | `            except WikiBudgetExceeded:` / `                break` / `            except WikiGroundingError:` / `                continue` |
| wiki lint contradictions | `jbrain/wiki/lint.py:512-526` | `jbrain/wiki/lint.py:69` `VERIFY_BATCH = 20  # candidate pairs per adapter call`; one call per batch per domain group | inside `_verify_batch`, `jbrain/wiki/lint.py:747-750`: `        except Exception:  # noqa: BLE001 — a verifier failure must not abort the whole sweep` / `            log.warning("wiki_lint_verify_failed", task=task)` / `            return None`; the loop then `jbrain/wiki/lint.py:525-526` `                if verdicts is None:` / `                    return filed  # budget refused → fail-closed, stop` |
| wiki lint stale claims | `jbrain/wiki/lint.py:651-661` | same batch of 20 | same `_verify_batch`; `jbrain/wiki/lint.py:660-661` `                if verdicts is None:` / `                    return filed` |
| inbox triage | `jbrain/gmail/triage.py:249-256` | one call **per email**; up to `jbrain/gmail/triage.py:73` `_SEARCH_CAP = 200` per fire (`triage.py:180` `        ids = await gmail.search(_SEARCH_QUERY, max_results=_SEARCH_CAP)`) | **none in the loop** — `jbrain/gmail/triage.py:249` is `        for i, msg in enumerate(batch):` / `            result = await self._router.complete(` … with no `try`; `InboxTriage.run` (`triage.py:178-206`) has no `try` either, so it propagates to `jbrain/worker.py:216` `        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker` |
| PDF page OCR | `jbrain/ingest/ocr.py:178-191` | one `vision.ocr` call per page: `jbrain/ingest/ocr.py:178` `    for number, png in enumerate(images, start=1):` | none — propagates to the worker's `except Exception` |
| video frame captioning | `jbrain/ingest/video.py:239-252` | one `agent.vision` call per sampled frame: `jbrain/ingest/video.py:239` `    for i, frame in enumerate(frames, start=1):` | none in the loop; the enclosing job handler catches only `jbrain/ingest/video.py:522` `        except ResidencyError as exc:` |
| note extraction over chunk groups | `jbrain/analysis/pipeline.py:259-271` (`        for group in groups:`) | one `note.extract` call per group; groups from `jbrain/analysis/prompt.py:60` `def group_texts(texts: list[str], budget: int = GROUP_CHAR_BUDGET) -> list[list[str]]:` or `:80` `def group_texts_by_source(` | `jbrain/analysis/pipeline.py:273-274`: `    except (LlmBadResponseError, ExtractionError, SchemaError) as exc:` / `        raise PermanentJobError(f"note.extract unusable for note {note_id}: {exc}") from exc` |
| sub-agent fan | `jbrain/agent/spawn.py:895-898` | `            await asyncio.gather(` `                *(_run_and_collect(i, plan, child) for i, (plan, child) in enumerate(minted))` ; concurrency capped by `jbrain/agent/tree.py:22` `MAX_PARALLEL = 4  # the most children that run concurrently within a fan`, forced to 1 on a local route (`spawn.py:909-910` `        provider, _model = await self._router.effective_spec(_CHILD_TASK)` / `        return 1 if provider == "local" else n`). Each child is a full `AgentLoop` (many `converse_stream` calls) | `jbrain/agent/spawn.py:1143-1159`: `            except Exception as exc:  # noqa: BLE001 — a child failure degrades, not crashes` / `                log.warning("subagent.child_failed", persona=persona, label=label, error=repr(exc))` / `                await _settle("error", "error")` … `                return _ChildResult(label, persona, f"ERROR: {exc}", ok=False, session_id=child.id)`; also `:1122` `            except TimeoutError:` and `:1117` `            except asyncio.CancelledError:` |
| deep-research orchestration one-shots | `jbrain/agent/deep_research.py:2400-2410` | `_plan` (1), `_reflect` (1 per gap round), `_synthesize` (1..3 — call sites `:2083`, `:2111`, `:2179`) | `        except LlmBadResponseError:` / `            log.warning("deep_research.json_degraded", task=_TASK)` / `            return None` |
| tasks tick over due tasks | `jbrain/tasks/scheduler.py:59-63` | one full agent turn per due task | none in the tick; each turn is wrapped at `jbrain/tasks/runner.py:292-294`: `        except Exception as exc:  # noqa: BLE001 — a task failure is a recorded run, not a crash` / `            log.warning("task.run_failed", task_id=task.id, error=repr(exc))` / `            error = str(exc)` |
| plan continuation sweep over due plans | `jbrain/agent/continuation.py:177-178` | one agent turn per due plan | `jbrain/agent/continuation.py:353-354`: `            except Exception as exc:  # noqa: BLE001 — a continuation failure is a recorded run` / `                log.warning("plan.continuation_failed", session_id=sid, error=repr(exc))` |
| the agent ReAct loop itself | `jbrain/agent/loop.py:1028` | `        for _step in range(self._g.max_steps):` — one `converse_stream` per step. `jbrain/agent/loop.py:141` `STEPS_BY_EFFORT: dict[str, int] = {"high": 60, "medium": 50}`; `:162-163` `SUPERVISED_MAX_STEPS = 500` / `SUPERVISED_MAX_COST_TOKENS = 2_000_000` | no per-step `try` around the router call in `run_stream`; the `/chat` caller catches (see §6) |

---

## 6. Every place an exception from a refused/failed model call is caught

### 6.1 Class hierarchy evidence

`jbrain/llm/errors.py`:
```
class LlmError(Exception):
    """Base for every adapter failure."""


class LlmAuthError(LlmError):
class LlmRateLimitError(LlmError):
class LlmTransientError(LlmError):
class LlmBadResponseError(LlmError):
class LlmContextOverflowError(LlmBadResponseError):
```
(lines 10, 14, 18, 22, 26, 32 respectively.)

`jbrain/llm/residency.py:109`: `class ResidencyError(Exception):` — subclasses `Exception`, **not** `LlmError`.
Its docstring, `jbrain/llm/residency.py:110-115`:
```
    """A deliberate refusal to load a model — distinct from the best-effort housekeeping
    errors that are swallowed. Raised when a model can't physically fit the box even after
    evicting everything (its footprint alone exceeds total RAM): loading it would drive the
    box into an out-of-memory hard-freeze, so the load is refused rather than attempted. The
    caller surfaces it (a 409 on the manual load, a failed completion on the router path)."""
```
`jbrain/llm/gpu_guard.py:227`: `class GpuBudgetError(Exception):` — subclasses `Exception`, and carries a keyword-only `permanent: bool = False`. It is the ONE refusal type callers branch on rather than merely surface. Exactly two sites set it, and both derive it from a total capacity they actually READ rather than from the size of the shortfall: the ledger, on an `admission.Outcome.INFEASIBLE`; and `refuse_if_no_device_room`, which runs BEFORE the charge and is therefore what decides this on a probe-wired box. Being out of room right now is transient however far out of room it is. Known gap, recorded in the class docstring: the runaway-watchdog abort reports transient and, for a mis-catalogued model, is not.
`jbrain/llm/local_gateway.py:87`: `class LocalGatewayError(Exception):` — subclasses `Exception`.
`jbrain/jcode/client.py:19`: `class JcodeError(RuntimeError):`

The refusal a busy/held box raises (`jbrain/llm/residency.py:460-468`):
```
        held = await self._held_names()
        if held and served_model not in held:
            with contextlib.suppress(Exception):
                if served_model in await self._gateway.running():
                    return  # already resident — serving it needs no load
            raise ResidencyError(
                f"Code mode is holding the box for {sorted(held)}. Turn code mode off to run "
                "other models (chat, vision, or background research)."
            )
```
and the over-box refusal (`jbrain/llm/residency.py:493`): `        self._refuse_if_over_box(plan)  # raises before we evict anything`.

Because `ResidencyError` is not an `LlmError`, every `except LlmError` site below does
**not** match it; every bare `except Exception` site does.

### 6.2 Catch sites

| file:line | catches | effect (verbatim) |
|---|---|---|
| `jbrain/worker.py:216` | `        except (ResidencyError, gpu_guard.GpuBudgetError) as exc:` — **this row was stale.** It read `except ResidencyError as exc:` at `:209`, which asserted that a `GpuBudgetError` fell through to the generic `except Exception` below and FAILED the job. It no longer does: `GpuBudgetError` is caught in the SAME clause as `ResidencyError` (`LOCAL_MODEL_LEDGER_PLAN.md` L1 item 2). | `                await queue.defer(maker, queue.SYSTEM_CTX, job.id, RETRY_AFTER, reason=repr(exc))` / `                    "worker.job_deferred_no_room",` — a defer, so **no attempt is burned** (§7). CONDITIONAL since the INFEASIBLE split: the clause first tests `isinstance(exc, gpu_guard.GpuBudgetError) and exc.permanent` and, when true, takes `queue.fail(…, permanent=True)` + `worker.job_failed_never_fits` instead — a refusal that exceeds the pool's whole usable capacity cannot be waited out, so deferring it would re-attempt a condition that cannot arrive. That branch also records `box_events.JOB_REFUSED_NO_ROOM` (a directly-enqueued job has no run step, so `_finalize_run_step` returns early and `app.jobs.last_error` is projected nowhere) and deliberately SKIPS `_after_exhaustion`, whose content fallbacks would discard an attachment's text over a capacity refusal the owner can fix in Settings. |
| `jbrain/worker.py:250` | `        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker` | `            exhausted = await queue.fail(maker, queue.SYSTEM_CTX, job.id, repr(exc))` |
| `jbrain/worker.py:209` | `        except queue.PermanentJobError as exc:` | `            exhausted = await queue.fail(maker, queue.SYSTEM_CTX, job.id, repr(exc), permanent=True)` |
| `jbrain/ingest/video.py:522` | `        except ResidencyError as exc:` | `            raise queue.PermanentJobError(str(exc)) from exc` |
| `jbrain/api/jcode_llm.py:177-180` | `                    except ResidencyError:` / `                        raise` / `                    except Exception:  # noqa: BLE001 - housekeeping never fails a completion` | `                        log.warning("jcode-llm ensure_room failed model=%s", served, exc_info=True)` |
| `jbrain/api/llm_settings.py:1517-1518` | `    except ResidencyError as exc:` / `        raise HTTPException(status_code=409, detail=str(exc)) from exc` (inside `_admit_or_409`, `:1485`) — **this row was stale.** It cited an inline `except ResidencyError as exc:` at `:1048`; the admission moved INSIDE the shared warm helper (commit `2f9904f`, §A rows L4/L5), so the catch lives here and both `gateway_load` and `gateway_prime` get it. | HTTP 409 (manual load path) |
| `jbrain/api/llm_settings.py:1561` (in `gateway_load`) and `:1943` (in `gateway_prime`) | `    except gpu_guard.GpuBudgetError as exc:` | `        raise HTTPException(status_code=409, detail=str(exc)) from exc` — with the comment `        # 409, not an uncaught 500. A device refusal is the same class of answer as the` (`:1562`). A device refusal is **no longer a 500** on either owner-reachable warm path. |
| `jbrain/api/agent.py:1134` | `        except LlmContextOverflowError as exc:` | `            status, stop_reason = "error", "context_overflow"` / `            live.emit(b'data: {"type": "done", "stop_reason": "context_overflow"}\n\n')` |
| `jbrain/api/agent.py:1142` | `        except Exception as exc:  # noqa: BLE001 — surface a terminal event, never a 500 mid-stream` | `            log.warning("agent.chat_failed", run_id=run_id, error=repr(exc))` / `            live.emit(b'data: {"type": "done", "stop_reason": "error"}\n\n')` |
| `jbrain/api/agent.py:1118` | `        except TimeoutError:` | `            status, stop_reason = "error", "turn_timeout"` |
| `jbrain/tasks/runner.py:292` | `        except Exception as exc:  # noqa: BLE001 — a task failure is a recorded run, not a crash` | `            error = str(exc)` — the run row records `status = "error"` |
| `jbrain/tasks/scheduler.py:79` | `        except Exception as exc:  # noqa: BLE001 — the tick must not kill the loop` | `            log.warning("tasks.tick_error", error=repr(exc))` |
| `jbrain/agent/continuation.py:353` | `            except Exception as exc:  # noqa: BLE001 — a continuation failure is a recorded run` | `                log.warning("plan.continuation_failed", session_id=sid, error=repr(exc))` |
| `jbrain/agent/continuation.py:468` | `            log.warning("plan.continuation_sweep_failed", error=repr(exc))` preceded by `        except Exception as exc:  # noqa: BLE001 — one bad sweep must not end the loop` | loop continues |
| `jbrain/agent/continuation.py:220` | `        except Exception as exc:  # noqa: BLE001 — one bad kick must not crash the caller's task` | `            log.warning("plan.continuation_kick_failed", error=repr(exc))` |
| `jbrain/agent/spawn.py:1143` | `            except Exception as exc:  # noqa: BLE001 — a child failure degrades, not crashes` | returns `_ChildResult(..., ok=False, ...)` with `f"ERROR: {exc}"` |
| `jbrain/agent/deep_research.py:2408` | `        except LlmBadResponseError:` | `            return None` |
| `jbrain/agent/visiontools.py:136` | `        except LlmError as exc:` | `            return "I couldn't compare those images right now — the vision model didn't respond."` |
| `jbrain/agent/imagegentools.py:403` | `            except LlmError as exc:` | `                log.warning("analyze_image_failed", error=str(exc))` / `                return None` → `                return "I couldn't analyze that image right now — the vision model didn't respond."` (`imagegentools.py:413`) |
| `jbrain/agent/imagegentools.py:434` | `            except LlmError as exc:` | `                log.warning("analyze_image_ocr_failed", error=str(exc))` — transcription silently empty |
| `jbrain/agent/croptools.py:247` | `        except LlmError as exc:` | `            return f"Couldn't look at that image: {exc}"` |
| `jbrain/agent/drawtools.py:207` | `        except LlmError as exc:` | `            return f"\n\nCouldn't look at the canvas: {exc}"` |
| `jbrain/agent/htmltools.py:111` | `        except LlmError as exc:` | `            return f"\n\nCouldn't look at it: {exc}"` |
| `jbrain/agent/grabtools.py:164` | `        except LlmError as exc:` | `            log.warning("grab_frame_vision_failed", error=str(exc))` / `            return ""` |
| `jbrain/agent/loop.py:586` | `        except Exception:  # noqa: BLE001 - a routing hiccup must never break a turn` | `            return False` (in `_hide_tool_round_text`) |
| `jbrain/agent/loop.py:566` | `        except Exception:  # noqa: BLE001 — liveness is best-effort; a probe error hides nothing` | `            return ()` |
| `jbrain/agent/readtools.py:173` | `    except Exception:  # noqa: BLE001 — a routing probe failure must not cost the turn` | `        return gated` |
| `jbrain/analysis/pipeline.py:273` | `    except (LlmBadResponseError, ExtractionError, SchemaError) as exc:` | `        raise PermanentJobError(...)` |
| `jbrain/analysis/pipeline.py:1178` | `        except LlmError:` | `            log.info("analysis.disambiguate_unrouted", note_id=str(note_id))` |
| `jbrain/analysis/pipeline.py:1204` | `            except (LlmError, LlmBadResponseError) as exc:` | `                log.warning("analysis.disambiguate_failed", note_id=str(note_id), error=repr(exc))` |
| `jbrain/ingest/emr/pathology.py:73` | `    except LlmError:` | `        log.info("emr.pathology_unrouted")` / `        return []` |
| `jbrain/ingest/emr/pathology.py:88` | `    except (LlmError, LlmBadResponseError) as exc:` | `        log.warning("emr.pathology_extract_failed", error=repr(exc))` / `        return []` |
| `jbrain/wiki/lint.py:747` | `        except Exception:  # noqa: BLE001 — a verifier failure must not abort the whole sweep` | `            return None` |
| `jbrain/wiki/editor.py:125` | `    except Exception:  # noqa: BLE001 — best-effort turn; a committed lever still reports below` | `        log.warning("wiki_editor_turn_failed", article_id=article_id)` |
| `jbrain/api/pet.py:334` | `        except Exception as exc:  # noqa: BLE001 — the LLM must not break "say"` | `            log.warning("jpet.say_llm_error", error=repr(exc))` then a canned babble |
| `jbrain/api/pet.py:430` | `    except Exception as exc:  # noqa: BLE001 — a failed/unconfigured model must be a clean error` | `        raise HTTPException(status_code=502, detail="could not imagine that statue") from exc` |
| `jbrain/llm/warm_keeper.py:196` | `        except Exception as exc:  # noqa: BLE001 — gateway down/cold/no-room: retry, never raise` | `            log.info("warm_keeper.prime_failed", model=served, error=str(exc))` / `            return False` |
| `jbrain/llm/warm_keeper.py:183` | `            except Exception as exc:  # noqa: BLE001 — no room / gateway down: the prime retries` | `                log.info("warm_keeper.preload_failed", model=served, error=str(exc))` |
| `jbrain/llm/warm_keeper.py:215` | `            except Exception:  # noqa: BLE001 — one bad tick must never kill the keeper` | `                log.warning("warm_keeper.tick_failed", exc_info=True)` |
| `jbrain/llm/router.py:401` | `            except Exception:  # noqa: BLE001 — a settings read hiccup must not wedge the keeper` | `                overrides = {}` |
| `jbrain/llm/router.py:594` | `        except Exception as exc:  # noqa: BLE001 - accounting must never fail or slow a call` | `            log.warning("llm.usage_record_failed", task=task, error=repr(exc))` |
| `jbrain/api/debug.py:414`, `:517`, `:601`, `:713` | `    except LlmError as exc:` | HTTP 400 / an `error=` field on the probe response |
| `jbrain/llm/residency.py:488` | `        except Exception as exc:  # noqa: BLE001 — housekeeping hiccup: best-effort, no-op` | `            log.warning("residency.ensure_room_failed", model=served_model, error=repr(exc))` / `            return` |

---

## 7. Defer / requeue / retry — mechanism and whether an attempt is consumed

| mechanism | file:line | verbatim | attempt consumed? |
|---|---|---|---|
| precondition defer | `jbrain/worker.py:265-267` | `    await report_progress(f"deferred: {result.reason}")` / `    await queue.defer(maker, queue.SYSTEM_CTX, job.id, RETRY_AFTER, reason=result.reason)` / `    log.info("worker.job_deferred", job_id=job.id, kind=job.kind, reason=result.reason)` | **no** — `jbrain/queue.py:505-506` `    """Reschedule a claimed (running) job to run again after `delay`, WITHOUT burning` / `    an attempt — the worker's "not now, try again soon" for an unmet precondition.` ; SQL at `:518-527` sets only `status`, `locked_at`, `last_error`, `run_after` |
| defer delay | `jbrain/workflow/preconditions.py:181` | `RETRY_AFTER = timedelta(minutes=5)` | — |
| no-room defer (code-mode residency **and** device budget) | `jbrain/worker.py:216` | `        except (ResidencyError, gpu_guard.GpuBudgetError) as exc:` … `                await queue.defer(maker, queue.SYSTEM_CTX, job.id, RETRY_AFTER, reason=repr(exc))` | **no** for a transient refusal (same `defer`) — **yes, terminally**, for a `GpuBudgetError` carrying `permanent=True` (`admission.Outcome.INFEASIBLE`), which takes `queue.fail(…, permanent=True)` instead. **This row was stale**: it named `ResidencyError` alone, i.e. a `GpuBudgetError` burning an attempt via the generic fail. A job that could not get memory waits for memory — unless the memory could never exist. |
| ordinary failure retry | `jbrain/queue.py:470-492` | `        attempts = row.attempts + 1` / `        exhausted = permanent or attempts >= row.max_attempts` … `                    run_after = now() + make_interval(secs => :delay),` | **yes** |
| backoff schedule | `jbrain/queue.py:146-151` | `def backoff(attempts: int) -> timedelta:` / `    """Retry delay after the Nth failed attempt: 2^N minutes, capped."""` … `    return min(timedelta(minutes=2 ** min(attempts, 10)), BACKOFF_CAP)`; `jbrain/queue.py:26` `BACKOFF_CAP = timedelta(hours=1)` | — |
| retry budget | `backend/migrations/versions/0003_jobs_chunks_ingest_state.py:40` | `            max_attempts int NOT NULL DEFAULT 5,` | — |
| permanent failure | `jbrain/worker.py:202-205` | `        except queue.PermanentJobError as exc:` … `            exhausted = await queue.fail(maker, queue.SYSTEM_CTX, job.id, repr(exc), permanent=True)` | budget short-circuited |
| exhaustion fallback | `jbrain/worker.py:314-318` | `    if not exhausted or job.kind not in ("ocr_attachment", "transcribe_attachment"):` / `        return` … `        await ocr.enqueue_analysis_fallback(maker, str(attachment_id))` — which enqueues `integrate_note` (`jbrain/ingest/ocr.py:128` `    job_id = await queue.enqueue(maker, SYSTEM_CTX, "integrate_note", {"note_id": nid})`) | new job |
| scheduler coalescing for preconditioned actions | `jbrain/workflow/scheduler.py:299-301` | `        if spec.precondition and await queue.has_active_kind(maker, queue.SYSTEM_CTX, spec.handler):` / `            log.info("scheduler.step_coalesced", action=step.action, kind=spec.handler)` / `            continue` | no new job |
| manual re-fire supersedes a deferred run | `jbrain/workflow/scheduler.py:342` | `    superseded = await supersede_running_runs(maker, queue.SYSTEM_CTX, pipeline=pipeline.name)` | — |
| plan continuation busy re-arm | `jbrain/agent/continuation.py:200-204` | `    async def _rearm(self, plan_id: str, owner_ctx: SessionContext) -> None:` … `                await PlanRepo().schedule_continuation(s, plan_id, delay_s=BUSY_RETRY_DELAY_S)`; `:70` `BUSY_RETRY_DELAY_S = 0` | counts against `MAX_CONTINUATIONS = 20` only when a turn runs (`continuation.py:64`) |
| router JSON re-ask (a second billed call) | `jbrain/llm/router.py:635-650` | `        if json_schema is not None and result.parsed is None:` / `            log.warning("llm.json_reask", task=task, provider=provider, model=model)` / `            result = await client.complete(` … `                raise LlmBadResponseError(` | n/a — same job attempt |
| warm-keeper retry cadence | `jbrain/llm/warm_keeper.py:218` | `            await asyncio.sleep(self._interval_ready if settled else self._interval_wait)` | n/a |
| deepest-run boot resume | `jbrain/agent/deepest_tool.py:99-100` | `            if wall_clock_s <= 0:` / `                continue  # already past its deadline — a terminal reconcile, not a resume` | n/a |

---

## 8. Model-name → served-name resolution (what the gateway sees)

| file:line | verbatim |
|---|---|
| `jbrain/llm/local_catalog.py:1086` | `_BY_SERVED = {m.served_model: m for m in CATALOG}` |
| `jbrain/llm/local_catalog.py:1112-1119` | `def id_for_served(served_model: str) -> str | None:` / `    """Catalog id for a served-model name (the gateway loads/reports served names,` / `    but per-model settings — overrides, staging — key off the catalog id), or None` / `    for a served name outside the catalog."""` / `    model = _BY_SERVED.get(served_model)` / `    return model.id if model else None` |
| `jbrain/llm/router.py:359-361` | `_admit_local` passes the bare `model` half of the spec straight to `ensure_room` — `            await self._residency.ensure_room(model)` |
| `jbrain/llm/router.py:511-513` | `                cat_id = local_catalog.id_for_served(model)` / `                if cat_id is not None and cat_id in windows:` / `                    return windows[cat_id]` |
| `jbrain/api/external_llm.py:145-149` | `def _served_model(model_id: str) -> str:` / `    """The gateway's served-model name for a catalog id (they match for the coder, but` / `    resolve via the catalog to be correct)."""` / `    m = local_catalog.get(model_id)` / `    return m.served_model if m else model_id` |
| `jbrain/api/jcode.py:119` | `    return _served_model(stored or settings.jcode_planner_model)` |
| `jbrain/llm/providers.py:33-42` | `def active_local_override(model: local_catalog.LocalModel) -> dict[str, str]:` … `    entry = {"spec": model.spec}` / `    if model.supports_reasoning:` / `        entry["reasoning_effort"] = REASONING_DEFAULT` |
| `jbrain/llm/router.py:391-404` | `primary_local_served_model` — `        return model if provider == local_catalog.LOCAL_PROVIDER else None` |
| `jbrain/llm/local_catalog.py:1081-1083` | `REASONING_SERVED_MODELS: frozenset[str] = frozenset(` / `    m.served_model for m in CATALOG if m.supports_reasoning` / `)` — consumed by `jbrain/llm/router.py:232-234` `    return provider == "xai" or (` / `        provider == "local" and model in local_catalog.REASONING_SERVED_MODELS` / `    )` |
| `jbrain/api/chat_attachments.py:189-190` | `    overrides = await store.llm_task_overrides(ctx_for(principal))` / `    spec = (overrides.get("agent.turn") or {}).get("spec") or TASK_DEFAULTS["agent.turn"]` |

---

## 9. Interactive vs background, per demand site (the explicit table)

| site | class | one-line evidence |
|---|---|---|
| `/chat` turn (D1a, D2, and every tool it dispatches) | **(a)** | `jbrain/api/agent.py:647` `@router.post("/chat")` |
| `/intake/chat` turn (D1d) | **(a)** | `jbrain/api/intake.py:429` `@router.post("/intake/chat")` |
| wiki Talk Editor turn (D1e) | **(a)** | `jbrain/api/wiki.py:181` `@router.post("/wiki/{article_id}/talk/topics/{topic_id}/editor", status_code=201)` |
| `/intake/submissions/{id}/materialize` (D22) | **(a)** | `jbrain/api/intake.py:267` `@router.post("/intake/submissions/{submission_id}/materialize", status_code=201)` |
| `/pet/command` say + `/pet/say` (D23) | **(a)** | `jbrain/api/pet.py:259` `@router.post("/command")`; `jbrain/api/pet.py:384` `@internal_router.post("/say")` |
| `/pet/statue` (D24) | **(a)** | `jbrain/api/pet.py:415` `@internal_router.post("/statue")` |
| debug `/complete`, `/tool-probe`, `/vision`, `/grounding` (D33–D37) | **(a)** | `jbrain/api/debug.py:429`, `:480`, `:759`, `:664` |
| operator Load / Prime | **(a)** | `jbrain/api/debug.py:1481` `@router.post("/llm/local-models/{model_id}/prime")` |
| Ops "run now" | **(a)** initiating a **(d)** job | `jbrain/api/ops.py:290` `@router.post("/triggers/{trigger_id}/run", status_code=202)` → `:302` `        fired = await scheduler.fire_trigger(maker, registry, trigger_id, require_manual=True)` |
| `analyze_image` / `compare_images` / `crop_regions` / canvas look / `render_html` look / `grab_frame` (D26–D32) | **(b)** | each is a `ToolHandler` taking `ctx: ToolContext`, dispatched inside `AgentLoop`; e.g. `jbrain/agent/imagegentools.py:370` `    async def analyze_image_tool(arguments: dict, ctx: ToolContext) -> str:` |
| `deep_research` / `deep_produce` orchestration + fan (D5, D6, D7, D1c) | **(b)** while the turn runs | `jbrain/agent/deep_research.py:3367` `    """Late-bound handler for the `deep_produce` tool.` ; the fan needs `ctx.tree`, seeded only for a rooted turn |
| `deepest_research` background run | **(c)** | `jbrain/agent/deepest_lane.py:71` `        task = asyncio.create_task(self._supervise(run_id, run, wall_clock_s))`; `jbrain/agent/deepest_lane.py:9-11` `(the caller — the `deepest_research` kickoff tool — enqueues and gets its turn back), the` / `run proceeds in the background` |
| deepest boot resume | **(c)** | `jbrain/main.py:1019` `            asyncio.create_task(app.state.deepest.resume_interrupted())` |
| plan continuation turn | **(c)** | `jbrain/agent/continuation.py:458-465` `async def run_plan_continuation_loop(` … `        await asyncio.sleep(interval_s)` / `            await runner.tick()`. A foreground client may be present but is not required: `jbrain/agent/continuation.py:194-198` `    def _client_present(self, session_id: str) -> bool:` … `        return seen is not None and (time.monotonic() - seen) < PRESENCE_TTL_S` |
| scheduled task turn (D1b) | **(d)** | `jbrain/tasks/scheduler.py:61` `        info = await runner.run(task_owner, task, trigger="schedule")` |
| WarmKeeper prime (D8) | **(d)** | `jbrain/llm/warm_keeper.py:211` `        while True:` |
| `integrate_note` job — `note.extract`, `integrate.note`, `entity.disambiguate` (D9–D11) | **(d)** | claimed in `jbrain/worker.py:160` `    job = await queue.claim(maker, queue.SYSTEM_CTX)` |
| `ocr_attachment` job (D12–D14) | **(d)** | `jbrain/worker.py:613` `        "ocr_attachment": OcrPipeline(` |
| `analyze_video_attachment` / `analyze_stream_url` (D15, D16) | **(d)** | `jbrain/worker.py:647` `        ).analyze_video_attachment,`; `jbrain/worker.py:665` `        ).analyze_stream_url,` — origin is a chat tool: `jbrain/ingest/stream_analysis.py:81-82` `# In-code only (NOT an app.actions seed — 0035's seed-lockstep is untouched), the URL` / `# sibling of analyze_video_attachment: kicked on demand by a deferred analyze_stream,` |
| `triage_inbox` (D17) | **(d)** | `jbrain/worker.py:721` `        "triage_inbox": triage_inbox_handler(gmail_provider.client, router, maker),` + hourly schedule |
| `wiki_refresh` / `wiki_rebuild` (D18, D19) | **(d)** | `jbrain/worker.py:726` `        **wiki_handlers(` |
| `wiki_lint` (D20) | **(d)** | `jbrain/worker.py:737` `        "wiki_lint": wiki_lint_handler(` |
| `title_research_report` (D21) | **(d)** | `jbrain/worker.py:609` `        "title_research_report": research_report_titler.title_research_report,` |
| `emr_parse` pathology (D25) | **(d)** | `jbrain/worker.py:677` `        "emr_parse": EmrImportPipeline(maker, blobs, analyzer).parse,` |
| jcode `/jcode/llm/v1/chat/completions` | **(a)** from the sandbox's operator, or **(c)** from an agent in the sandbox | `jbrain/api/jcode_llm.py:131` `@router.post("/jcode/llm/v1/chat/completions")`; `jbrain/api/jcode_llm.py:19-21` `Internal-only: reachable from the jcode sandbox over the `jcode` docker network, Bearer-` |
| external LLM proxy | **(a)** remote client; refuses rather than loading | `jbrain/api/external_llm.py:222` `        raise HTTPException(status_code=503, detail="the coder model is not loaded")` |
| `residency.schedule_restore()` after a `/chat` turn | **(c)** | `jbrain/api/agent.py:1117` `                    residency.schedule_restore()` |

---

## 10. Things I could not determine

1. **The live DB state of `app.schedules`.** §4.2 is reconstructed from the migration
   chain only. `enabled`, `interval_seconds`, and `next_run_at` are all mutable at runtime
   through `jbrain/api/ops.py` (`jbrain/api/ops.py:274` `    ok = await _automations_reader(request).update_schedule(` with `:278` `        schedule_kind=body.schedule_kind,` and `:279` `        interval_seconds=body.interval_seconds,`). I did not query a database.
2. **The live `llm_task_overrides` row.** Every route in §2 step 4 depends on it; it is a
   `app.settings` value, not code. I could not read it.
3. **The live `JBRAIN_LLM_TASKS` / `JBRAIN_LLM_TIERS` env values**, which determine
   `self._pinned` and therefore whether the tier branch in `_resolve` is ever reached for
   `agent.turn`. `jbrain/config.py:487` shows only the default `{}`.
4. **Whether `pet.thought`, `fact.adjudicate`, `correction_note.extract` are reachable at
   runtime.** No `router.*` call site exists in `backend/src` for any of the three; I did
   not search the frontend, `jcode/`, `jlaunch/`, `spikes/`, or `research/` trees.
5. **`emr.pathology_diagnosis` behaviour on a live box.** `jbrain/ingest/emr/pathology.py:72`
   probes with `router.spec(PATHOLOGY_TASK)`; that task is absent from `TASK_DEFAULTS`, so
   `_resolve` would take `jbrain/llm/router.py:388-389` (`        except KeyError:` /
   `            raise LlmError(f"unknown LLM task: {task!r}") from None`). I did not execute it.
6. **Actual call counts per fire** for `wiki_refresh`, `wiki_lint`, and `triage_inbox` — each
   is a function of live row counts (`SELECT id FROM app.entities WHERE NOT wiki_built`,
   the candidate-pair set, the inbox size up to `_SEARCH_CAP = 200`).
7. **Whether `residency` is non-None in a given process.** `jbrain/llm/router.py:857-861`
   shows `build_router` always attaches one (`        residency=(` / `            residency` / `            if residency is not None` /
   `            else _default_residency(settings, local_windows_loader)` / `        ),`), but
   `_default_residency` passes `        enabled=settings.local_llm_enabled,` — I did not read
   the deployed `.env`.
8. **The frontend's role in demand.** I inventoried `backend/` only; any PWA polling that
   POSTs to a model-reaching route (e.g. `/api/pet/command`) is out of what I read.
9. **`analyze_stream_url` / `analyze_video_attachment` origin classification.** The specs say
   "kicked on demand by a deferred analyze_stream" (`jbrain/ingest/stream_analysis.py:82`);
   I did not trace the deferred-tool enqueue path end-to-end, so the (b)→(d) hand-off is
   asserted from that comment plus the worker handler registration, not from a quoted enqueue
   call site.

---

# §D — Controls, config, and the cloud-provider deletion manifest

# Inventory: model control surfaces, configuration, cloud-provider deletion manifest

Repo `/home/user/JBrain2`, branch `claude/model-loading-chat-indicator-tujwms`, HEAD `f2b5423`.
Working tree clean; nothing modified. All paths absolute below the repo root are written
relative to `/home/user/JBrain2/`.

Every row carries a `file:line` and a verbatim quote. Where a claim could not be closed with a
quote it is in "Things I could not determine".

---

## 1. Controls that affect whether a model loads, unloads, stays resident, or is routed to

### 1.1 Backend HTTP endpoints (owner PWA surface, `/api/settings/llm/*`)

Router file: `backend/src/jbrain/api/llm_settings.py`.

| # | Endpoint (verbatim decorator) | Handler | What it does (verbatim from handler) | Persistence | Frontend control |
|---|---|---|---|---|---|
| 1 | `backend/src/jbrain/api/llm_settings.py:940` `@router.get("/settings/llm")` | `read_llm_settings` | `    return await _snapshot(settings, store, ctx_for(principal), gateway)` (`:947`) | read-only | `frontend/src/screens/LLMSettingsScreen.tsx` (snapshot load) |
| 2 | `:958` `@router.put("/settings/llm/jcode-model")` | `set_jcode_model` | `    await store.set_jcode_model(ctx, body.model)` (`:976`) | `JCODE_MODEL_KEY = "jcode_model"` (`backend/src/jbrain/settings_store.py:255`) | `frontend/src/screens/LLMSettingsScreen.tsx:421` `function setJcodeModel(model: string) {`; client `frontend/src/api/client.ts:2422` `async setJcodeModel(model: string): Promise<LlmSettings> {` |
| 3 | `:989` `@router.put("/settings/llm/jcode-planner")` | `set_jcode_planner` | `    await store.set_jcode_planner_model(ctx, body.planner)` (`:1009`) | `JCODE_PLANNER_MODEL_KEY = "jcode_planner_model"` (`settings_store.py:273`) | `LLMSettingsScreen.tsx:435` `function setJcodePlanner(planner: string) {`; client `client.ts:2429` `async setJcodePlanner(planner: string): Promise<LlmSettings> {` |
| 4 | `:1013` `@router.post("/settings/llm/local-models/{model_id}/unload")` | `unload_local_model` | `    """Evict one resident model from the gateway's memory. 404 for a model that` (`:1020`) → `    return await gateway_unload(model_id, settings, gateway)` (`:1023`) | none (runtime action) | `LLMSettingsScreen.tsx:297` `function unloadModel(id: string) {` |
| 5 | `:1026` `@router.post("/settings/llm/local-models/{model_id}/load")` | `load_local_model` | `            await residency.free_room(model.served_model)  # evict-to-fit, or refuse if impossible` (`:1047`) then `    return await gateway_load(model_id, settings, gateway, registry=registry, liveness=liveness)` (`:1050`) | none (runtime action) | `LLMSettingsScreen.tsx:310` `function loadModel(id: string) {` |
| 6 | `:1060` `@router.put("/settings/llm/local-models/{model_id}/context-window")` | `set_local_context_window` → `set_local_context_window_value` | `    await store.set_llm_local_context_window(ctx, model_id=model_id, window=window)` (`:1094`); `    await _unload_if_loaded(settings, gateway, model)` (`:1095`) | `LLM_LOCAL_CONTEXT_WINDOWS_KEY = "llm_local_context_windows"` (`settings_store.py:66`) | `LLMSettingsScreen.tsx:336` `function setContextWindow(id: string, window: number | null) {` |
| 7 | `:1111` `@router.put("/settings/llm/local-models/{model_id}/image-min-tokens")` | `set_local_image_min_tokens` | `    await store.set_llm_local_image_min_tokens(ctx, model_id=model_id, tokens=tokens)` (`:1138`); `    await _unload_if_loaded(settings, gateway, model)` (`:1139`) | `LLM_LOCAL_IMAGE_MIN_TOKENS_KEY = "llm_local_image_min_tokens"` (`settings_store.py:71`) | `LLMSettingsScreen.tsx:364` `function setImageMinTokens(id: string, tokens: number | null) {` |
| 8 | `:1156` `@router.put("/settings/llm/local-models/{model_id}/parallel-slots")` | `set_local_parallel_slots` | `    await store.set_llm_local_parallel_slots(ctx, model_id=model_id, slots=body.slots)` (`:1175`); `    await _unload_if_loaded(settings, gateway, model)` (`:1176`) | `LLM_LOCAL_PARALLEL_SLOTS_KEY = "llm_local_parallel_slots"` (`settings_store.py:70`) | `LLMSettingsScreen.tsx:350` `function setParallelSlots(id: string, slots: number | null) {` |
| 9 | `:1196` `@router.put("/settings/llm/free-ram-fraction")` | `set_free_ram_fraction` | `    await store.set_llm_local_free_ram_fraction(ctx, body.fraction)` (`:1218`) | `LLM_LOCAL_FREE_RAM_FRACTION_KEY = "llm_local_free_ram_fraction"` (`settings_store.py:85`) | `LLMSettingsScreen.tsx:378` `function setFreeRam(fraction: number | null) {` |
| 10 | `:1228` `@router.put("/settings/llm/auto-restore")` | `set_auto_restore` | `    await store.set_llm_local_auto_restore(ctx, body.enabled)` (`:1248`) | `LLM_LOCAL_AUTO_RESTORE_KEY = "llm_local_auto_restore"` (`settings_store.py:99`) | `LLMSettingsScreen.tsx:393` `mark("auto-restore");` / control at `:1177` `function AutoRestoreControl({` — see §2 |
| 11 | `:1258` `@router.put("/settings/llm/local-models/{model_id}/available")` | `set_local_available` | `    await store.set_llm_local_unavailable(ctx, unavailable)` (`:1280`); `                    await gateway.unload(model.served_model)` (`:1286`) | `LLM_LOCAL_UNAVAILABLE_KEY = "llm_local_unavailable"` (`settings_store.py:91`) | `LLMSettingsScreen.tsx:406` `function setAvailable(id: string, on: boolean) {` |
| 12 | `:1290` `@router.post("/settings/llm/local-models/{model_id}/plan-load")` | `plan_load_local_model` | `    plan = await residency.plan_load(model.served_model) if residency is not None else None` (`:1306`) | none (dry-run) | `LLMSettingsScreen.tsx:324` `function previewStage(id: string) {` |
| 13 | `:1344` `@router.post("/settings/llm/local-models/{model_id}/install")` | `queue_local_install` | `        await store.set_llm_local_provision_requested(ctx, requested)` (`:1362`) | `LLM_LOCAL_PROVISION_REQUESTED_KEY = "llm_local_provision_requested"` (`settings_store.py:105`) | `LLMSettingsScreen.tsx:450` `function queueInstall(id: string, on: boolean) {` |
| 14 | `:1371` `@router.delete("/settings/llm/local-models/{model_id}/install")` | `cancel_local_install` | `    await store.set_llm_local_provision_requested(ctx, requested)` (`:1384`) | same key | same (`on === false` branch) |
| 15 | `:1388` `@router.post("/settings/llm/local-models/{model_id}/uninstall")` | `queue_local_uninstall` | `        await store.set_llm_local_remove_requested(ctx, removing)` (`:1406`) | `LLM_LOCAL_REMOVE_REQUESTED_KEY = "llm_local_remove_requested"` (`settings_store.py:112`) | `LLMSettingsScreen.tsx:467` `function queueUninstall(id: string, on: boolean) {` |
| 16 | `:1415` `@router.delete("/settings/llm/local-models/{model_id}/uninstall")` | `cancel_local_uninstall` | `    await store.set_llm_local_remove_requested(ctx, removing)` (`:1428`) | same key | same |
| 17 | `:1902` `@router.put("/settings/llm")` | `update_llm_settings` | `    return await apply_overrides(body, settings, store, ctx_for(principal), gateway)` (`:1910`); inside: `    await store.upsert(ctx, LLM_TASK_OVERRIDES_KEY, overrides)` (`:1485`) | `LLM_TASK_OVERRIDES_KEY = "llm_task_overrides"` (`settings_store.py:57`) | `LLMSettingsScreen.tsx` per-task picker; client `client.ts:2338` `// Per-task LLM routing: the provider each task runs on, plus grok's reasoning` |

Read sites for the routing overrides key (`llm_task_overrides`), i.e. what #17 gates:
- `backend/src/jbrain/main.py` wires an overrides loader into `build_router` (see §2.3 for the
  identical `SYSTEM_CTX` lambda pattern); the router consumes it at
  `backend/src/jbrain/llm/router.py:466` `            entry = overrides.get(task) or {}` and
  `:467` `            spec = entry.get("spec")`.
- Local specs are dropped when hosting is off:
  `router.py:481` `                    if sp == "local" and not self._local_enabled:`.
- `backend/src/jbrain/cli.py:172` `        raw = await store.get(SYSTEM_CTX, LLM_TASK_OVERRIDES_KEY, {})` and
  `:175` `        await store.upsert(SYSTEM_CTX, LLM_TASK_OVERRIDES_KEY, overrides)`.

### 1.2 Owner debug API endpoints (`/api/debug/*`)

Router file: `backend/src/jbrain/api/debug.py`.

| Endpoint | Handler body (verbatim) | Persistence |
|---|---|---|
| `:1344` `@router.get("/llm")` | `    return await llm_settings.snapshot(settings, _store(request), _OWNER_CTX, _gateway(request))` (`:1346`) | read-only |
| `:1349` `@router.put("/llm")` | `    return await llm_settings.apply_overrides(` (`:1356`) | `llm_task_overrides` |
| `:1361` `@router.post("/llm/local-models/{model_id}/load")` | `    return await llm_settings.gateway_load(` (`:1366`) | none |
| `:1375` `@router.post("/llm/local-models/{model_id}/unload")` | `    return await llm_settings.gateway_unload(model_id, settings, _gateway(request))` (`:1380`) | none |
| `:1398` `@router.put("/llm/local-models/{model_id}/extra-args")` | `    return await llm_settings.set_local_extra_args(` (`:1415`) | `LLM_LOCAL_EXTRA_ARGS_KEY = "llm_local_extra_args"` (`settings_store.py:77`) |
| `:1423` `@router.put("/llm/local-models/{model_id}/context-window")` | `    return await llm_settings.set_local_context_window_value(` (`:1435`) | `llm_local_context_windows` |
| `:1438` `@router.get("/llm/local-models/{model_id}/props")` | `    return await llm_settings.gateway_props(model_id, settings, _gateway(request))` (`:1448`) | read-only |
| `:1451` `@router.get("/llm/local-models/{model_id}/slots")` | `    return await llm_settings.gateway_slots(model_id, settings, _gateway(request))` (`:1464`) | read-only |
| `:1467` `@router.get("/llm/local-models/{model_id}/metrics")` | `    return await llm_settings.gateway_metrics(model_id, settings, _gateway(request))` (`:1478`) | read-only |
| `:1481` `@router.post("/llm/local-models/{model_id}/prime")` | `    return await llm_settings.gateway_prime(` (`:1493`) — loads + times a jerv prime | none |
| `:1150` `@router.post("/llm/drop-page-cache")` | `    freed = _gateway(request).drop_page_cache(ids)` (`:1177`) | none |

Debug-surface reachability gate (env, boot-time):
`backend/src/jbrain/api/deps.py:136` `    if not settings.debug_access_enabled:` / `:137` `        raise HTTPException(status_code=404, detail="not found")`.
Field: `backend/src/jbrain/config.py:317` `    debug_access_enabled: bool = False`.

Debug-console web client (the same `/api/debug` routes from a browser):
`frontend/src/debug-console/Console.tsx:292` `        path = `/api/debug/llm/local-models/${encodeURIComponent(modelId)}/${modelAction}`;`
and `:137` `  const [modelAction, setModelAction] = useState<"load" | "unload">("load");`.

### 1.3 Code-mode (jcode) controls

`backend/src/jbrain/api/jcode.py`:

| Endpoint | Effect on models (verbatim) |
|---|---|
| `:324` `@router.post("/jcode/model/warm")` | `    _warm_coder(request, model_id)` (`:330`) → `_warm_model` at `:145`: `                    await gateway.unload(other)` (`:164`), `            residency.note_evicted(evicted)  # type: ignore[attr-defined]` (`:167`), `        await gateway.load(served)` (`:168`) |
| `:480` `@router.post("/jcode/power")` | ON: `            await _store(request).set_code_mode_hold_names(` (`:502`); OFF: `            await _store(request).set_code_mode_hold_names(_owner_ctx(owner.id), [])` (`:523`), then `        await _free_coder_and_restore(request, owner.id)` (`:525`) → `            await gateway.unload(served)` (`:474`) and `        residency.schedule_restore()` (`:477`) |
| `:431` `@router.get("/jcode/power")` | read-only status |
| `:314` `@router.get("/jcode/model")` | read-only residency status |

Frontend: `frontend/src/screens/JcodeScreen.tsx:229` `            onClick={() => setPowerAction(power.on ? "off" : "on")}`;
`:418` `        latest.current = await api.jcodeSetPower(true);`;
`:461` `        latest.current = await api.jcodeSetPower(false);`;
`:441` `        setModel(await api.jcodeWarmModel());`;
`frontend/src/screens/JcodeSessionScreen.tsx:450` `      setModel(await api.jcodeWarmModel());`.
Client: `frontend/src/api/client.ts:3796` `    return (await request("/api/jcode/model/warm", { method: "POST" })).json();`;
`:3807` `    return (await request("/api/jcode/power", jsonInit("POST", { on }))).json();`.

### 1.4 Ops endpoints that move models

`backend/src/jbrain/api/ops.py`:

| Endpoint | Body (verbatim) | Frontend |
|---|---|---|
| `:1119` `@router.post("/update", status_code=202)` | `    resp = await _client(request).post("/update", headers=_headers(settings))` (`:1121`) | `frontend/src/screens/OpsScreen.tsx:260` `function UpdateControl() {` |
| `:1178` `@router.post("/local-provision", status_code=202)` | `    resp = await _client(request).post("/provision", headers=_headers(settings))` (`:1180`) | `frontend/src/screens/LLMSettingsScreen.tsx:487` `  function startDownload() {` → `    api.opsLocalProvisionStart().catch((e) => {` (`:490`) |
| `:1187` `@router.get("/local-provision/status")` | `        "/provision/status", params={"tail": 80}, headers=_headers(settings)` (`:1189`) | `LLMSettingsScreen.tsx:506` `        .opsLocalProvisionStatus()` |
| `:352` `@router.post("/restart", status_code=202)` | `        "/restart", json={"service": body.service}, headers=_headers(settings)` (`:357`) | Ops per-container controls |
| `:380` `@router.post("/start", status_code=202)` / `:387` `@router.post("/stop", status_code=202)` | `    return await _lifecycle("start", body.service, request, settings)` (`:384`) / `    return await _lifecycle("stop", body.service, request, settings)` (`:391`) | Ops per-container controls |

Supervisor side (what those proxy to), `supervisor/src/supervisor/gateway.py`:
`:35` `    "apk add --no-cache git >/dev/null 2>&1 && exec sh src/deploy/update-inner.sh"`
`:46` `PROVISION_COMMAND = "exec sh src/deploy/local-models-sync.sh"`

The gateway-auto-update toggle sits on the Ops screen next to Update:
`backend/src/jbrain/api/settings.py:187` `        await store.upsert(ctx, LOCAL_LLM_AUTO_UPDATE_KEY, body.local_llm_auto_update)`;
key `backend/src/jbrain/settings_store.py:214` `LOCAL_LLM_AUTO_UPDATE_KEY = "local_llm_auto_update"`,
default `:215` `LOCAL_LLM_AUTO_UPDATE_DEFAULT = True`.
Frontend: `frontend/src/screens/OpsScreen.tsx:212` `function GatewayAutoUpdateToggle() {`,
`:235` `      await api.updateSettings({ local_llm_auto_update: next });`.
Read site: `backend/src/jbrain/cli.py:94` `        return 0 if await store.local_llm_auto_update(SYSTEM_CTX) else 1`.
Consumer of that exit code: `deploy/update-inner.sh:664` `    python -m jbrain.cli local-llm-auto-update; then`.

### 1.5 Image-generation controls that free/hold the same RAM pool

`backend/src/jbrain/api/image_settings.py`:
- `:116` `@router.post("/free")` → `        await gateway.free()` (`:122`)
- `:129` `@router.post("/interrupt", status_code=202)` → `        await gateway.interrupt()` (`:136`)
- `:171` `@router.post("/service/start", status_code=202)` → `    return await _toggle_service(request, settings, "start")` (`:174`)
- `:177` `@router.post("/service/stop", status_code=202)` → `    return await _toggle_service(request, settings, "stop")` (`:180`)

Frontend: `frontend/src/screens/LLMSettingsScreen.tsx:522` `  function freeImage() {` (and
`onFreeImage={freeImage}` / `onStartImageService={startImageService}` at `:688`/`:689`).

A render evicts every resident LLM: `backend/src/jbrain/image_gen/render.py:206`
`                await gateway.unload(served)`, and records the displacement at
`:210` `        on_evicted(freed)`, wired at `backend/src/jbrain/main.py:747`
`                on_evicted=app.state.residency.note_evicted,`.

### 1.6 Non-endpoint controls (process-internal) that load/unload

| Site | Verbatim |
|---|---|
| Router admission before every local completion | `backend/src/jbrain/llm/router.py:361` `            await self._residency.ensure_room(model)` |
| End-of-turn restore, chat path | `backend/src/jbrain/api/agent.py:1117` `                    residency.schedule_restore()` |
| jcode LLM proxy (sandbox `/model` swap) | `backend/src/jbrain/api/jcode_llm.py:176` `                        await residency.ensure_room(served)` |
| WarmKeeper preload | `backend/src/jbrain/llm/warm_keeper.py:180` `                await self._router.admit_local_load(served)` and `:182` `                    await self._gateway.load(served)` |
| WarmKeeper prime | `warm_keeper.py:189` `            await self._router.converse(` |
| Transcription tool unload-after | `backend/src/jbrain/agent/transcribetools.py:146` `            await gateway.unload(model)` |
| Video pipeline unload-after | `backend/src/jbrain/ingest/video.py:441` `            await gateway.unload(model)` |
| Transcribe job unload-after | `backend/src/jbrain/ingest/transcribe_job.py:206` `                await self._gateway.unload(self._model)` |
| Pre-update unload (CLI) | `backend/src/jbrain/cli.py:73` `            await gateway.unload(served)` |
| Smoke test load | `backend/src/jbrain/llm/smoketest.py:237` `        await gateway.load(smallest.served_model)` |
| Device-memory pre-flight (refuses a load) | `backend/src/jbrain/llm/local_gateway.py:801` `        gpu_guard.refuse_if_no_device_room(baseline, projected_gb, served_model)` |
| Runaway watchdog (aborts a load) | `local_gateway.py:803` `            await gpu_guard.guarded_load(` |

Guard constants, `backend/src/jbrain/llm/gpu_guard.py`:
`:95` `RUNAWAY_MULTIPLE = 1.75`, `:100` `MIN_FREE_GTT_GB = 6.0`, `:89` `SAMPLE_INTERVAL_S = 1.0`.

The gateway never self-evicts, which is what makes the app's evictor the only one:
`backend/src/jbrain/llm/llama_swap_config.py:438` `        lines.append("    swap: false")`,
`:439` `        lines.append("    exclusive: false")`.

### 1.7 Restart survival

All PWA/debug-set values in this section are rows in `app.settings`:
`backend/migrations/versions/0012_app_settings.py:27` `        CREATE TABLE app.settings (`,
`:28` `            key text PRIMARY KEY,`, `:29` `            value jsonb NOT NULL,`.
Write path: `backend/src/jbrain/settings_store.py:409` `                    "INSERT INTO app.settings (key, value)"`.
So they survive a process restart unless something clears them.

The one value cleared at boot is the code-mode hold:
`backend/src/jbrain/main.py:390` `        with suppress(Exception):`
`backend/src/jbrain/main.py:391` `            await settings_store.set_code_mode_hold_names(SYSTEM_CTX, [])`
preceded by `main.py:382` `        # Clear any stranded code-mode box reservation at startup. The flag is persisted, but a`.

Env-var-backed values (`Settings`) are read once per process:
`backend/src/jbrain/main.py:254` `    settings = settings or get_settings()` and
`main.py:1237` `    app.state.settings = settings`; every request re-reads that same object via
`backend/src/jbrain/api/deps.py:13` `    return cast(Settings, request.app.state.settings)`.

### 1.8 Reachability

| Control | PWA | Debug API | CLI | Host shell only | Evidence |
|---|---|---|---|---|---|
| load / unload one model | yes | yes | no | no | `llm_settings.py:1013`,`:1026`; `debug.py:1361`,`:1375` |
| plan-load (dry run) | yes | no | no | no | `llm_settings.py:1290` only |
| context window | yes | yes | no | no | `llm_settings.py:1060`; `debug.py:1423` |
| parallel slots | yes | no | no | no | `llm_settings.py:1156` only |
| image-min-tokens | yes | yes (via extra-args allowlist) | no | no | `llm_settings.py:1111`; `EXTRA_ARG_FLAGS` contains `        "--image-min-tokens",` (`llm_settings.py:1636`) |
| extra llama-server args | no | yes | no | no | only `debug.py:1398`; grep for `set_local_extra_args` in `frontend/` returns nothing |
| free-RAM fraction | yes | no | no | no | `llm_settings.py:1196` only |
| auto-restore | yes | no | no | no | `llm_settings.py:1228` only |
| available/unavailable | yes | no | no | no | `llm_settings.py:1258` only |
| install / uninstall queue | yes | no | queue read/clear only | no | `llm_settings.py:1344`/`:1388`; `cli.py:247`–`:250` |
| gateway auto-update toggle | yes | no | read-only exit code | `.env` still wins | `api/settings.py:187`; `update-inner.sh:661` `if grep -q '^LOCAL_LLM_AUTO_UPDATE=false' .env; then` |
| drop page cache | no | yes | no | no | `debug.py:1150` only |
| jcode power / warm | yes | no | no | no | `jcode.py:480`,`:324` |
| local hosting on/off (`LOCAL_LLM_ENABLED`) | no | no | no | yes | `config.py:314` `    local_llm_enabled: bool = False`; set only by `deploy/local-models-sync.sh` / `scripts/local-llm-setup.sh` writing `.env`; no endpoint writes it |
| `local_llm_timeout` | reported, not settable | no | no | yes | `llm_settings.py:382` `    # REPORTED, not settable: it is env-only, so an operator cannot change it without a host` |

---

## 2. Deep-dive: the auto-restore toggle

### 2.1 The key and its store accessors

```
backend/src/jbrain/settings_store.py:99
LLM_LOCAL_AUTO_RESTORE_KEY = "llm_local_auto_restore"
```
Preceding comment, `settings_store.py:92`–`:98`:
```
# Whether the evictor may put back models a transient displacement (image render, code
# session, a big one-off) removed, once the turn that displaced them ends. ON by default —
# it is what keeps the box drifting back to its steady state instead of cold-loading on the
# next turn. The operator switch exists because a restore is a MODEL LOAD the owner did not
# ask for at that moment: while diagnosing the box, or while deliberately holding it near
# empty, "nothing loads unless I say so" has to be reachable from the PWA. A load that does
# happen is guarded either way (jbrain.llm.gpu_guard); this is about surprise, not safety.
```

Reader, `settings_store.py:886`–`:890`:
```
    async def llm_local_auto_restore(self, ctx: SessionContext) -> bool:
        """Whether the evictor may restore displaced models at end of turn. Defaults to True
        (the long-standing behaviour) — only an explicit stored `False` turns it off, so a
        garbled value can never silently leave the box refusing to restore."""
        return await self.get(ctx, LLM_LOCAL_AUTO_RESTORE_KEY, True) is not False
```
Writer, `settings_store.py:892`–`:895`:
```
    async def set_llm_local_auto_restore(self, ctx: SessionContext, enabled: bool) -> bool:
        clean = bool(enabled)
        await self.upsert(ctx, LLM_LOCAL_AUTO_RESTORE_KEY, clean)
        return clean
```

### 2.2 The endpoint

`backend/src/jbrain/api/llm_settings.py:1222`–`:1225`:
```
class AutoRestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
```
`:1228`–`:1230`:
```
@router.put("/settings/llm/auto-restore")
async def set_auto_restore(
    body: AutoRestoreIn,
```
`:1247`–`:1249`:
```
    ctx = ctx_for(principal)
    await store.set_llm_local_auto_restore(ctx, body.enabled)
    return await _snapshot(settings, store, ctx, gateway)
```
Snapshot read that populates the GET, `:459`:
```
    auto_restore = await store.llm_local_auto_restore(ctx)
```
and `:505` `        auto_restore=auto_restore,`. Response field, `:379` `    auto_restore: bool = True`.

### 2.3 Every wiring lambda that supplies it

Exactly two, both in `backend/src/jbrain/main.py`:

1. Into the **api process's** `ResidencyCoordinator` (`main.py:429` `        app.state.residency = ResidencyCoordinator(`):
```
backend/src/jbrain/main.py:445
            # The operator's end-of-turn restore switch (Settings → LLM). Read per restore,
backend/src/jbrain/main.py:446
            # so flipping it takes effect on the next turn with no restart.
backend/src/jbrain/main.py:447
            auto_restore_loader=lambda: settings_store.llm_local_auto_restore(SYSTEM_CTX),
```

2. Into the **WarmKeeper** (`main.py:1164` `        app.state.warm_keeper = WarmKeeper(`):
```
backend/src/jbrain/main.py:1170
            # The same operator switch the coordinator reads. The keeper is the OTHER auto-load
backend/src/jbrain/main.py:1171
            # path on this box, and without this it reloaded the primary model every 5s no
backend/src/jbrain/main.py:1172
            # matter what the setting said — so turning auto-reload off stopped restores while
backend/src/jbrain/main.py:1173
            # a 68 GiB model kept coming straight back, which is not what the switch claims.
backend/src/jbrain/main.py:1174
            auto_restore_loader=lambda: settings_store.llm_local_auto_restore(SYSTEM_CTX),
```

The **worker** process's `ResidencyCoordinator` is constructed at
`backend/src/jbrain/worker.py:539` `    residency = ResidencyCoordinator(` and its keyword
arguments run `:540`–`:562`; the full list is `llm_gateway`, `windows_loader`, `slots_loader`,
`models_dir`, `enabled`, `free_ram_fraction`, `fraction_loader`, `hold_loader`, `box_lock`,
`gpu_probe`. There is no `auto_restore_loader=` among them — reproduce with:
`grep -n "auto_restore" backend/src/jbrain/worker.py` → no output.

### 2.4 Every consumer that reads it

**Consumer 1 — `ResidencyCoordinator`** (`backend/src/jbrain/llm/residency.py`).

Constructor parameter, `:172`:
```
        auto_restore_loader: Callable[[], Awaitable[bool]] | None = None,
```
Stored, `:205` `        self._auto_restore_loader = auto_restore_loader`, with the comment at
`:199`–`:204`:
```
        # The operator's end-of-turn RESTORE switch (Settings → LLM), read live so a flip
        # applies with no restart. Off means the box stops putting back what a displacement
        # took — models come back only when a turn actually needs them. It exists because a
        # restore is a model load the owner did not ask for at that moment, and while
        # diagnosing the box "nothing loads unless I say so" has to be reachable from the
        # PWA. Absent loader → on, the long-standing behaviour.
```
Accessor, `:267`–`:274`:
```
    async def _auto_restore(self) -> bool:
        """The live end-of-turn restore switch. Defaults to ON when no loader is wired or the
        read fails: a settings-store hiccup must not silently leave the box refusing to drift
        back to its steady state."""
        if self._auto_restore_loader is None:
            return True
        with contextlib.suppress(Exception):
            return await self._auto_restore_loader()
        return True
```
The **only** call site of `_auto_restore()` is inside `_restore()`, `:601`–`:603`:
```
        if not await self._auto_restore():
            log.info("residency.restore_disabled", displaced=sorted(self._displaced))
            return
```
(`grep -n "_auto_restore()" backend/src/jbrain/llm/residency.py` → `601` only.)
`_restore()` is reached only via `schedule_restore()`, `:266`:
```
        task = asyncio.create_task(self._restore())
```
`ensure_room` (`:279`), `free_room` (`:568`) and `plan_load` (`:411`) do not consult it —
reproduce with `grep -n "_auto_restore" backend/src/jbrain/llm/residency.py` → `172, 205, 267,
271, 274, 601`.

**Consumer 2 — `WarmKeeper`** (`backend/src/jbrain/llm/warm_keeper.py`).

Constructor parameter, `:62` `        auto_restore_loader: Callable[[], Awaitable[bool]] | None = None,`.
Stored, `:81` `        self._auto_restore_loader = auto_restore_loader`, comment `:74`–`:80`:
```
        # The operator's "automatically reload models" switch. The keeper is the SECOND
        # auto-load path on this box — residency restore is the other — and it used to ignore
        # this setting entirely, so turning it off stopped restores while the keeper went on
        # reloading the primary model every interval_wait seconds. An operator who switches
        # auto-reload off and watches a 68 GiB model reappear within five seconds has been
        # told something untrue by the UI. It gates LOADING only: a model already resident is
        # still kept primed, because holding a warm prefix costs nothing and is not a load.
```
Accessor, `:93`–`:102`:
```
    async def _auto_restore_allowed(self) -> bool:
        """Default OPEN when unwired (no loader) or on a settings read failure: this gate only
        suppresses a convenience reload, and a box that silently stopped keeping its model warm
        because a settings query hiccupped would be a worse failure than one extra load."""
        if self._auto_restore_loader is None:
            return True
        try:
            return await self._auto_restore_loader()
        except Exception:  # noqa: BLE001 — a settings hiccup must not wedge the keeper
            return True
```
Its single call site, `:133`–`:140`:
```
        cold = served not in running
        if cold:
            self._primed = None  # evicted (or never loaded) → the cache no longer holds our prime
            if not await self._auto_restore_allowed():
                # Off: the operator asked for nothing to be loaded behind their back. SETTLED,
                # not "retry soon" — returning False here would spin the eager 5s cadence
                # forever against a switch that is never going to flip on its own.
                return True
```

**Consumer 3 — the settings snapshot** (read for display only):
`backend/src/jbrain/api/llm_settings.py:459` `    auto_restore = await store.llm_local_auto_restore(ctx)`.

Full-repo reproduction:
`grep -rn "llm_local_auto_restore" --include="*.py" backend/src` →
`main.py:447`, `main.py:1174`, `settings_store.py:99,886,892`, `api/llm_settings.py:459`.
No other production read sites exist.

### 2.5 The frontend control

Handler, `frontend/src/screens/LLMSettingsScreen.tsx:390`–`:402`:
```
  // Turn the end-of-turn restore on or off. A box-wide knob like the free-RAM floor, so it
  // gets its own fixed busy id rather than a model id.
  function setAutoRestore(enabled: boolean) {
    mark("auto-restore");
    const seq = ++putSeq.current;
    api
      .setAutoRestore(enabled)
      .then((s) => {
        if (seq === putSeq.current) setSettings(s);
      })
      .catch(() => {})
      .finally(() => unmark("auto-restore"));
  }
```
Wiring into the card, `:669`–`:671`:
```
        autoRestore={settings.auto_restore}
        onSetAutoRestore={setAutoRestore}
        autoRestoreBusy={busy.has("auto-restore")}
```
Widget, `:1177`–`:1204`:
```
function AutoRestoreControl({
  enabled,
  busy,
  onChange,
}: {
  enabled: boolean;
  busy: boolean;
  onChange: (enabled: boolean) => void;
}) {
  return (
    <div className="onbox-freeram">
      <label className="onbox-freeram-label" htmlFor="auto-restore-toggle">
        Auto-restore models
      </label>
      <input
        id="auto-restore-toggle"
        type="checkbox"
        className="onbox-autorestore-toggle"
        checked={enabled}
        disabled={busy}
        onChange={(e) => onChange(e.target.checked)}
      />
```
API client, `frontend/src/api/client.ts:2413`–`:2418`:
```
  /** Turn the end-of-turn restore on or off. Off means the box stops putting back models a
   * displacement evicted, so nothing loads until a turn needs it. Returns the snapshot. */
  async setAutoRestore(enabled: boolean): Promise<LlmSettings> {
    const response = await request("/api/settings/llm/auto-restore", jsonInit("PUT", { enabled }));
    return (await response.json()) as LlmSettings;
  },
```

### 2.6 Restart survival

Persisted as an `app.settings` row (§1.7). Nothing in the repo clears
`llm_local_auto_restore` at boot — `grep -rn "llm_local_auto_restore" backend/src` returns
only the six lines listed in §2.4, none of which is a boot-time reset.

---

## 3. Deep-dive: the code-mode hold (`CODE_MODE_HOLD_KEY`)

### 3.1 The key

```
backend/src/jbrain/settings_store.py:265
CODE_MODE_HOLD_KEY = "code_mode_hold_name"
```
Comment, `:257`–`:264`:
```
# Box-exclusivity flag for code mode: while non-empty, it holds the served-model name the
# coder (jcode) has reserved the unified-memory box for. Set on jcode power ON, cleared on
# OFF. Read box-wide (via SYSTEM_CTX) by the residency coordinator — which then refuses to
# load ANY other model (no eviction of the coder, no contending big-model load) — and by the
# worker loop, which pauses its background job/scheduler work. This makes code mode fully
# exclusive: on a 128 GB Strix Halo box two ~60 GB models (gpt-oss-120b + the coder) can't
# co-reside, and a background deep-research load contending with an open jcode session OOM'd
# the box; reserving the box for the coder removes that contention by construction.
```
Note the stored value is a **list** despite the singular key name:
`settings_store.py:705` `        raw = await self.get(ctx, CODE_MODE_HOLD_KEY, [])`.

Reader, `settings_store.py:699`–`:709`:
```
    async def code_mode_hold_names(self, ctx: SessionContext) -> frozenset[str]:
        """The served-model names code mode has reserved the box for while it's ON (jcode's own
        executor + planner), or an empty set when code mode is off. While non-empty, residency
        refuses to load any OTHER model and the worker pauses its job loop — code mode owns the
        unified-memory box, which prevents the OOM from a background load contending with it.
        Defensive: a non-list store, or any non-string / empty entry, is dropped."""
        raw = await self.get(ctx, CODE_MODE_HOLD_KEY, [])
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(x for x in raw if isinstance(x, str) and x)
```
Writer, `settings_store.py:710`–`:714`:
```
    async def set_code_mode_hold_names(
        self, ctx: SessionContext, served_models: Sequence[str]
    ) -> None:
        """Reserve the box for jcode's models (power ON), or release it with [] (OFF / boot)."""
        await self.upsert(ctx, CODE_MODE_HOLD_KEY, sorted({m for m in served_models if m}))
```

### 3.2 Every write site

1. **jcode power ON** — `backend/src/jbrain/api/jcode.py:500`–`:504`:
```
            executor = _served_model(await _resolve_model(request, owner.id))
            planner = await _resolve_planner_model(request, owner.id)  # "" for single-model
            await _store(request).set_code_mode_hold_names(
                _owner_ctx(owner.id), [executor, planner]
            )
```
2. **jcode power OFF** — `backend/src/jbrain/api/jcode.py:520`–`:523`:
```
        # Release the box FIRST, so `_free_coder_and_restore` can reload the general hot set
        # (gpt-oss + vision) — a restore load would be refused while the hold is still set.
        with contextlib.suppress(Exception):
            await _store(request).set_code_mode_hold_names(_owner_ctx(owner.id), [])
```
3. **Boot reset** — `backend/src/jbrain/main.py:391`:
```
            await settings_store.set_code_mode_hold_names(SYSTEM_CTX, [])
```

### 3.3 Every wiring lambda that supplies it

1. api-process residency — `backend/src/jbrain/main.py:440`–`:444`:
```
            # Code-mode box reservation (jcode power ON writes it): while set, ensure_room
            # refuses to load any model outside code mode's reserved set, so nothing evicts its
            # models or co-loads past physical RAM. Read per load (SYSTEM_CTX), identically
            # wired in the worker.
            hold_loader=lambda: settings_store.code_mode_hold_names(SYSTEM_CTX),
```
2. WarmKeeper — `backend/src/jbrain/main.py:1169`:
```
            hold_loader=lambda: settings_store.code_mode_hold_names(SYSTEM_CTX),
```
3. worker-process residency — `backend/src/jbrain/worker.py:549`–`:552`:
```
        # Code-mode box reservation: while jcode holds the box, a background job's model load
        # is refused here too (belt-and-suspenders with the run_loop pause below), so it can
        # never evict code mode's models or co-load past physical RAM.
        hold_loader=lambda: worker_settings_store.code_mode_hold_names(queue.SYSTEM_CTX),
```

### 3.4 Every consumer that reads it

**Consumer 1 — `ResidencyCoordinator._held_names()`**, `backend/src/jbrain/llm/residency.py:309`–`:319`:
```
    async def _held_names(self) -> frozenset[str]:
        """The served-model names code mode has reserved the box for, or an empty set (not
        held). Read per-load so toggling code mode applies immediately; any hiccup degrades to
        empty — a housekeeping failure must never block a legitimate load (best-effort, like the
        rest of this coordinator)."""
        if self._hold_loader is None:
            return frozenset()
        with contextlib.suppress(Exception):
            names = await self._hold_loader()
            if names:
                return frozenset(names)
        return frozenset()
```
Its two call sites (`grep -n "_held_names()" backend/src/jbrain/llm/residency.py` → `460, 609`):

(a) `ensure_room`, `residency.py:453`–`:468`:
```
        # Code-mode exclusivity: while the box is reserved for code mode, refuse to load ANY
        # model outside its reserved set (jcode's executor + planner). A model already resident
        # may keep serving (no new load, no memory pressure), but a NON-resident, non-reserved
        # model is refused — so nothing evicts code mode's models and no second large model
        # co-loads past physical RAM (the unified-memory OOM this guards). The reserved models
        # are exempt (they must be able to (re)load). Checked before the box lock so a refused
        # load never contends for it.
        held = await self._held_names()
        if held and served_model not in held:
            with contextlib.suppress(Exception):
                if served_model in await self._gateway.running():
                    return  # already resident — serving it needs no load
            raise ResidencyError(
                f"Code mode is holding the box for {sorted(held)}. Turn code mode off to run "
                "other models (chat, vision, or background research)."
            )
```
(b) `_restore`, `residency.py:604`–`:609`:
```
        # While code mode holds the box, do NOT opportunistically reload displaced members — a
        # restore load bypasses ensure_room's refusal, so this is where a stray model could
        # slip in beside code mode's own. Skip; the members stay displaced and restore once the
        # hold clears (jcode OFF clears the flag before it fires its own restore, so that path
        # is unaffected).
        if await self._held_names():
            return
```
`free_room` (`residency.py:568`) and `plan_load` (`:411`) contain no `_held_names` call —
reproduce with `grep -n "_held_names\|_hold_loader" backend/src/jbrain/llm/residency.py` →
`171, 198, 309, 314, 317, 461, 607`.

**Consumer 2 — `WarmKeeper.reconcile_once`**, `backend/src/jbrain/llm/warm_keeper.py:123`–`:128`:
```
        try:
            held = set(await self._hold_loader() or ())
        except Exception:  # noqa: BLE001 — a settings read hiccup must not wedge the keeper
            held = set()
        if held and served not in held:
            return True  # code mode owns the box; never load outside its reserved set
```

**Consumer 3 — the worker run loop**, `backend/src/jbrain/worker.py:385`–`:388`:
```
        held = False
        if settings is not None:
            with contextlib.suppress(Exception):
                held = bool(await settings.code_mode_hold_names(queue.SYSTEM_CTX))
```
with the preceding comment `worker.py:379`–`:384`:
```
        # Code-mode box reservation: while jcode holds the box for the coder, PAUSE the
        # background work that would load a model — the scheduler tick (which enqueues research
        # like daily_news), the dispatcher, and the job claim below. Nothing then contends with
        # the coder for unified memory (the OOM this guards; residency also refuses such a load
        # as a backstop). Heartbeat + host-metrics keep running so the box stays observable
        # through a code session; the paused work resumes the moment code mode toggles off.
```
and the gate it feeds, `worker.py:393`:
```
        if not held and registry is not None and now - last_tick >= scheduler.TICK_SECONDS:
```

Full-repo reproduction:
`grep -rn "code_mode_hold\|CODE_MODE_HOLD" --include="*.py" backend/src` →
`settings_store.py:265,699,710`; `main.py:391,444,1169`; `worker.py:388,552`;
`api/jcode.py:502,523`. No other production sites.

### 3.5 Reachability and restart survival

- Set/cleared only through `POST /api/jcode/power` (PWA switch, §1.3). No debug-API route
  writes it: `grep -n "code_mode_hold" backend/src/jbrain/api/debug.py` → no output.
- Persisted in `app.settings`, but **explicitly cleared at every api-process boot** —
  `backend/src/jbrain/main.py:391` (quoted above), whose rationale line is `main.py:386`:
  `        # shows code mode OFF (it reads live service state, not the flag). Boot is a safe reset —`.
- The worker process does not clear it: `grep -n "set_code_mode_hold_names" backend/src/jbrain/worker.py` → no output.

---

## 4. Config fields in `backend/src/jbrain/config.py` that govern models

`Settings` is a `pydantic_settings.BaseSettings` with
`config.py:9` `        env_prefix="JBRAIN_", env_file=".env", extra="ignore", env_ignore_empty=True`
— so each field `foo` is the env var `JBRAIN_FOO`.

**Boot-time vs live**: every field below is read off the single `Settings` instance created at
`main.py:254` and stashed at `main.py:1237`; requests re-read that same object
(`api/deps.py:13`). No code path re-instantiates `Settings` inside a request — reproduce with
`grep -rn "get_settings()" backend/src/jbrain` → `main.py:254`, plus `cli.py:33,58,90,105,118,131,144,168,192` (each a fresh CLI process). So all are **boot-time-only** for the api/worker; the CLI re-reads per invocation.

| Field | Default (verbatim line) | Read sites (file:line) |
|---|---|---|
| `local_llm_enabled` | `config.py:314` `    local_llm_enabled: bool = False` | 20 non-comment sites: `cli.py:59,193`; `main.py:434`; `worker.py:544`; `llm/router.py:863,894`; `llm/providers.py:61`; `api/llm_settings.py:159,481,519,549,558,566,586,639,652,673,921`; `api/jcode.py:178,273,304,465` |
| `local_llm_url` | `config.py:333` `    local_llm_url: str = "http://localhost:11434/v1"` | `cli.py:62,226`; `main.py:411`; `worker.py:517`; `llm/router.py:842,887` |
| `local_llm_model` | `config.py:367` `    local_llm_model: str = "local"` | `llm/providers.py:67` only |
| `local_models` | `config.py:372` `    local_models: list[str] = []` | `cli.py:193,236`; `llm/providers.py:63`; `api/llm_settings.py:519,586,642,657,679,798,925` |
| `local_models_dir` | `config.py:378` `    local_models_dir: str = "/data/local-models"` | `cli.py:212,234`; `main.py:420,433`; `worker.py:528,543`; `llm/router.py:890,893`; `api/llm_settings.py:551,560,719,800,930` |
| `local_llm_timeout` | `config.py:363` `    local_llm_timeout: float = 600.0` | `llm/router.py:845`; `api/llm_settings.py:507` |
| `local_llm_free_ram_fraction` | `config.py:391` `    local_llm_free_ram_fraction: float = 0.15` | `main.py:435`; `worker.py:545`; `llm/router.py:895`; `api/llm_settings.py:501,502` |
| `whisper_url` | `config.py:401` `    whisper_url: str = ""` | `main.py:789,792,799,831,835,839,853,857,861`; `worker.py:481,624,627,640,646,658,664` |
| `whisper_enabled` | `config.py:402` `    whisper_enabled: bool = False` | **none in `backend/src`** — `grep -rn "whisper_enabled" backend/src` returns only `config.py:399,400,402` (two comment lines + the definition) |
| `whisper_model` | `config.py:406` `    whisper_model: str = "whisper"` | `main.py:793,798,832,838,854,860`; `worker.py:624,626,640,645,658,663` |
| `whisper_timeout` | `config.py:411` `    whisper_timeout: float = 300.0` | `main.py:794,833,855`; `worker.py:624,640,658` |
| `whisper_max_bytes` | `config.py:416` `    whisper_max_bytes: int = 100 * 1024 * 1024` | `main.py:800`; `worker.py:486` |
| `comfyui_url` | `config.py:341` `    comfyui_url: str = ""` | `main.py:721,724,728,1294`; `api/image_settings.py:77`; `api/chat_attachments.py:192` |
| `comfyui_enabled` | `config.py:342` `    comfyui_enabled: bool = False` | **none in `backend/src`** — `grep -rn "comfyui_enabled" backend/src` returns only `config.py:339,340,342` |
| `comfyui_models` | `config.py:345` `    comfyui_models: list[str] = []` | `main.py:744,771`; `api/image_settings.py:81` |
| `comfyui_models_dir` | `config.py:351` `    comfyui_models_dir: str = "/data/comfyui-models"` | `main.py:750`; `api/image_settings.py:82` |
| `comfyui_timeout` | `config.py:357` `    comfyui_timeout: float = 1800.0` | `main.py:724` |
| `jcode_model` | `config.py:444` `    jcode_model: str = "qwen3-coder-next"` | `api/llm_settings.py:538,539`; `api/jcode.py:87` |
| `jcode_planner_model` | `config.py:452` `    jcode_planner_model: str = "gpt-oss-120b"` | `api/llm_settings.py:533,541`; `api/jcode.py:119` |
| `jcode_url` | `config.py:441` `    jcode_url: str = ""` | `main.py:808`; `api/jcode_preview.py:107,118,158,192`; `api/jcode_terminal.py:123,148` |
| `jcode_enabled` | `config.py:442` `    jcode_enabled: bool = False` | `api/llm_settings.py:536` |
| `jcode_gateway_token` | `config.py:463` `    jcode_gateway_token: str = ""` | `backend/src/jbrain/api/jcode_llm.py:66` `    token = getattr(request.app.state.settings, "jcode_gateway_token", "") or ""` |
| `llm_tasks` | `config.py:487` `    llm_tasks: dict[str, str] = {}` | `llm/router.py:851,854` |
| `llm_tiers` | `config.py:492` `    llm_tiers: dict[str, str] = {}` | `llm/router.py:853` |
| `llm_prices` | `config.py:495`–`:497` `    llm_prices: dict[str, dict[str, float]] = {` / `        "xai:grok-4.3": {"input_per_m": 1.25, "output_per_m": 2.50}` / `    }` | `api/ops.py:1116` |
| `anthropic_api_key` | `config.py:307` `    anthropic_api_key: str = ""` | `llm/router.py:839`; `llm/providers.py:97` |
| `xai_api_key` | `config.py:308` `    xai_api_key: str = ""` | `llm/router.py:840`; `llm/providers.py:91` |
| `embed_url` | `config.py:18` `    embed_url: str = "http://embed:80"` | `main.py:372,379,486,995,1001`; `worker.py:488,490,493,496,584,712,728` (the embedding sidecar, not the completion router) |
| `embed_model` | `config.py:19` `    embed_model: str = "BAAI/bge-small-en-v1.5"` | `main.py:486`; `worker.py:488,490,493,496,585,712,729,739` |
| `debug_access_enabled` | `config.py:320` `    debug_access_enabled: bool = False` | `api/deps.py:136` |
| `supervisor_url` / `supervisor_token` | `config.py:13` `    supervisor_url: str = "http://supervisor:9000"` / `:14` `    supervisor_token: str = ""` | `main.py:401` (`gpu_guard.SupervisorGpuMemProbe(... settings.supervisor_token)`), `llm/gpu_guard.py:202`–`:203` |

Reproduce the read-site lists with, e.g.:
`grep -rnE "\.local_llm_enabled\b" --include="*.py" backend/src`

Exact commands used for the two zero-read fields:
```
grep -rn "whisper_enabled"  --include="*.py" backend/src
grep -rn "comfyui_enabled"  --include="*.py" backend/src
```
Both return only `config.py` lines. Their env vars are still written by deploy:
`deploy/docker-compose.yml:151` `      JBRAIN_WHISPER_ENABLED: ${WHISPER_ENABLED:-false}` and
`:132` `      JBRAIN_COMFYUI_ENABLED: ${COMFYUI_ENABLED:-false}`;
`scripts/whisper-setup.sh:127` `  echo "WHISPER_ENABLED=true"`;
`scripts/comfyui-setup.sh:176` `  echo "COMFYUI_ENABLED=true"`.
The only consumer of the *shell* variable `COMFYUI_ENABLED` is
`deploy/update-inner.sh:768` `if [ -n "$HOST_UPDATE" ] && grep -q '^COMFYUI_ENABLED=true' .env; then`.

Note on `local_llm_enabled` as a **deploy-time** gate, separate from the Python read sites:
`deploy/update-inner.sh:489` `if grep -q '^LOCAL_LLM_ENABLED=true' .env; then` and
`deploy/local-models-sync.sh:34` `grep -q '^LOCAL_LLM_ENABLED=true' .env || { say "hosting off — skipping model sync"; exit 0; }`.

---

## 5. Cloud-provider deletion manifest (Anthropic + xAI)

### 5.0 Reproducible commands

Run from `/home/user/JBrain2`:

```
# C1 — backend source that defines/constructs a cloud provider or names its ids
grep -rlnE '"(anthropic|xai)"|anthropic:|xai:|anthropic_api_key|xai_api_key|AnthropicClient|XAI_BASE_URL' --include="*.py" backend/src

# C2 — tests that reference a cloud provider id
grep -rlnE '"(anthropic|xai)"|anthropic:|xai:|anthropic_api_key|xai_api_key|AnthropicClient|\bxai\b' --include="*.py" backend/tests

# C3 — frontend
grep -rlnE '"(anthropic|xai)"|xai:grok|anthropic:|\bclaude\b' --include="*.ts" --include="*.tsx" frontend/src

# C4 — anything naming the API-key env vars
grep -rn "ANTHROPIC_API_KEY\|XAI_API_KEY" --include="*" . | grep -v node_modules | grep -v "\.venv" | grep -v "\.git/"

# C5 — docs
grep -rlniE "\b(xai|anthropic)\b" docs/

# C6 — modules importing the Anthropic client
grep -rn "from jbrain.llm.anthropic\|jbrain.llm.anthropic" --include="*.py" .
```

Counts observed: C1 → 7 files; C2 → 61 files; C3 → 9 files; C5 → 37 files; C6 → 3 lines.

### 5.1 Files that define or construct a cloud provider client

| File | Quote |
|---|---|
| `backend/src/jbrain/llm/anthropic.py` (325 lines) | `:1`–`:2` `"""Anthropic Messages API client over raw httpx (no SDK — fewer deps, and the` / `TEI embed client set the transport-injection precedent for tests)."""`; `:31` `API_VERSION = "2023-06-01"`; `:32` `DEFAULT_TIMEOUT = 120.0` |
| `backend/src/jbrain/llm/router.py` | `:26` `from jbrain.llm.anthropic import AnthropicClient`; `:48` `XAI_BASE_URL = "https://api.x.ai/v1"`; `:839` `        "anthropic": AnthropicClient(settings.anthropic_api_key, **extra),`; `:840` `        "xai": OpenAiCompatClient(XAI_BASE_URL, settings.xai_api_key, provider="xai", **extra),`; `:181` `PROVIDERS = ("anthropic", "xai", "local")` |
| `backend/src/jbrain/llm/__init__.py` | `:7` `from jbrain.llm.anthropic import AnthropicClient`; `:41` `    "AnthropicClient",` |
| `backend/src/jbrain/llm/providers.py` | `:91`–`:96` `    if settings.xai_api_key:` / `        cloud.append(` / `            ProviderChoice(` / `                "grok", "Grok 4.3", "xai:grok-4.3", supports_reasoning=True, supports_vision=True` / `            )` / `        )`; `:97`–`:106` `    if settings.anthropic_api_key:` … `                "anthropic:claude-sonnet-4-6",`; `:146` `    if provider in ("anthropic", "xai"):` |
| `backend/src/jbrain/llm/model_sampling.py` | `:41`–`:43` `CLOUD_SAMPLING: dict[tuple[str, str], Sampling] = {` / `    ("xai", "grok-4.3"): Sampling(temperature=0.7, top_p=0.95),` / `}` |

### 5.2 Config fields, env vars, and settings keys that exist only for cloud

| Item | Quote |
|---|---|
| `anthropic_api_key` | `backend/src/jbrain/config.py:307` `    anthropic_api_key: str = ""` |
| `xai_api_key` | `backend/src/jbrain/config.py:308` `    xai_api_key: str = ""` |
| `llm_prices` default entry | `backend/src/jbrain/config.py:496` `        "xai:grok-4.3": {"input_per_m": 1.25, "output_per_m": 2.50}` |
| env var `JBRAIN_ANTHROPIC_API_KEY` | `deploy/docker-compose.yml:101` `      JBRAIN_ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}` |
| env var `JBRAIN_XAI_API_KEY` | `deploy/docker-compose.yml:102` `      JBRAIN_XAI_API_KEY: ${XAI_API_KEY:-}` |
| `.env` writers | `deploy/install.sh:121` `ANTHROPIC_API_KEY=$ANTHROPIC_KEY`; `:122` `XAI_API_KEY=$XAI_KEY` |
| install prompts | `deploy/install.sh:103` `  read -rp "Anthropic API key (blank to skip): " ANTHROPIC_KEY`; `:104` `  read -rp "xAI API key (blank to skip): " XAI_KEY` |
| cloud context windows | `backend/src/jbrain/llm/router.py:190`–`:195` `    # Anthropic Claude 4.x family.` / `    "claude-opus-4-8": 200_000,` / `    "claude-sonnet-4-6": 200_000,` / `    "claude-haiku-4-5-20251001": 200_000,` / `    # xAI Grok.` / `    "grok-4.3": 256_000,` |

**No `app.settings` key exists only for cloud.** `LLM_TASK_OVERRIDES_KEY` stores whatever spec
is chosen (`settings_store.py:57`) and is used for local specs too
(`backend/src/jbrain/cli.py:174` `        overrides[_PRIMARY_MODEL_TASK] = active_local_override(model)`).

### 5.3 Task defaults and tier defaults naming a cloud spec

All 20 entries of `TASK_DEFAULTS` (`backend/src/jbrain/llm/router.py:50`–`:119`) name
`"xai:grok-4.3"`. Verbatim, in file order:
```
backend/src/jbrain/llm/router.py:50   TASK_DEFAULTS: dict[str, str] = {
:51       "note.extract": "xai:grok-4.3",
:52       "entity.disambiguate": "xai:grok-4.3",
:53       "fact.adjudicate": "xai:grok-4.3",
:54       "correction_note.extract": "xai:grok-4.3",
:55       "vision.ocr": "xai:grok-4.3",
:56       "vision.caption": "xai:grok-4.3",
:59       "agent.turn": "xai:grok-4.3",
:64       "agent.vision": "xai:grok-4.3",
:68       "integrate.note": "xai:grok-4.3",
:73       "intake.materialize": "xai:grok-4.3",
:78       "video.summarize": "xai:grok-4.3",
:89       "research.title": "xai:grok-4.3",
:95       "wiki.rewrite": "xai:grok-4.3",
:96       "wiki.ground": "xai:grok-4.3",
:101      "wiki.lint.contradiction": "xai:grok-4.3",
:102      "wiki.lint.stale": "xai:grok-4.3",
:107      "triage.classify": "xai:grok-4.3",
:112      "pet.turn": "xai:grok-4.3",
:113      "pet.thought": "xai:grok-4.3",
:118      "pet.statue": "xai:grok-4.3",
:119  }
```
Tier defaults, `router.py:175`–`:179`:
```
TIER_DEFAULTS: dict[str, str] = {
    "high": "xai:grok-4.3",
    "low": "xai:grok-4.3",
    "vision": "xai:grok-4.3",
}
```
Reproduce: `grep -c '"xai:grok-4.3"' backend/src/jbrain/llm/router.py` → 23 (20 tasks + 3 tiers).

Reasoning capability is keyed on the provider name:
`router.py:232` `    return provider == "xai" or (`.

### 5.4 Tests, fixtures, and eval harnesses depending on a cloud provider or its key

**Gated on a real key (would stop running without one):**
- `backend/tests/eval/run.py:167`–`:169`:
```
    if not Settings().xai_api_key:
        print("JBRAIN_XAI_API_KEY not set — the real-Grok eval is opt-in.")
        return 2
```
- `backend/tests/eval/run.py:220`–`:222`:
```
    if not settings.xai_api_key:
        print("JBRAIN_XAI_API_KEY not set — the real-Grok eval is opt-in.")
        return 2
```
- `scripts/grok-eval.sh:5` `# Needs JBRAIN_XAI_API_KEY (the eval calls real Grok; ~$0.5 for the full corpus).`;
  `:13` `exec uv run python -m tests.eval.run "$@"`.
- `backend/tests/eval/README.md:12` `    JBRAIN_XAI_API_KEY=... scripts/grok-eval.sh            # full corpus`
- `backend/tests/eval/run.py:35` `_PRICE_IN, _PRICE_OUT = 1.25, 2.50  # $/M tokens, grok-4.3`

**Directly exercise the Anthropic client:**
- `backend/tests/unit/test_llm_providers.py:11` `    AnthropicClient,`; `:48` `    client = AnthropicClient("sk-ant-test", transport=capture_transport(seen, ANTHROPIC_OK))`; `:52` `    assert str(request.url) == "https://api.anthropic.com/v1/messages"`; `:54` `    assert request.headers["anthropic-version"] == "2023-06-01"`. Test functions at `:46, :65, :82, :95, :220, :237, :265, :466`.
- `backend/tests/unit/test_llm_sampling.py:14` `from jbrain.llm.anthropic import AnthropicClient`; `:177` `    AnthropicClient._apply_sampling(`; `:186` `    AnthropicClient._apply_sampling(payload, Sampling(top_p=0.9))`; `:192` `    AnthropicClient._apply_sampling(payload, Sampling())`; `:110` `    assert model_sampling.default_sampling("anthropic", "claude-sonnet-4-6", None).is_empty`.
- `backend/tests/unit/test_llm_stream.py:65` `async def test_anthropic_stream_text_chunks_then_assembled_tool_turn() -> None:`; `:90` `async def test_anthropic_stream_plain_text_end_turn() -> None:`.

**Construct settings with cloud keys (fixtures):**
- `backend/tests/unit/test_llm_settings_api.py:32`–`:33` `    kw.setdefault("xai_api_key", "test-xai")` / `    kw.setdefault("anthropic_api_key", "test-anthropic")`; also `:320, :323, :327, :333, :368, :369, :384, :385`.
- `backend/tests/unit/test_llm_local_catalog.py:634`–`:635` (same two lines).
- `backend/tests/unit/test_debug_api.py:175`–`:176` (same two lines).
- `backend/tests/unit/test_llm_router.py:217`–`:218` `        anthropic_api_key="ant-key",` / `        xai_api_key="xai-key",`; `:200` `        if request.url.host == "api.anthropic.com":`; `:238` `    assert hosts == ["api.anthropic.com", "localhost", "api.x.ai"]`.

**Use `"xai"` as a fake-client key / route (the largest group).** Full list of 61 files is
command C2 above; the harness entry points are:
- `backend/tests/harness/runner.py:59` `        {"xai": FakeLlmClient([extraction_json, intent_json])},`; `:60` `        {"note.extract": ("xai", "grok-4.3"), "integrate.note": ("xai", "grok-4.3")},`; `:180`–`:181` same shape.
- `backend/tests/integration/test_settings_pg.py:317` `            "note.extract": {"spec": "anthropic:claude-sonnet-4-6"},`; `:325` assertion.
- `backend/tests/unit/test_llm_routing.py:40`–`:41`, `:68`, `:70`, `:72`.
- `backend/tests/unit/test_usage.py:82` `    assert cost_usd("anthropic", "claude-sonnet-4-6", 1_000_000, 0, PRICES) is None`.

**Not key-gated, provider-agnostic:** `backend/evals/run.py` — `:16`–`:17`
```
It routes to whatever provider/model your config points note.extract at
(JBRAIN_LLM_TASKS, provider keys / base URLs), so the same cases score Claude,
```
`grep -n "xai_api_key\|anthropic_api_key" backend/evals/run.py` → no output.
`backend/evals/README.md:39`–`:40` names the env var in prose only:
`> via `Settings` (`JBRAIN_XAI_API_KEY`, etc.). Pass it inline at call time`.

### 5.5 Frontend references

| File | Quote |
|---|---|
| `frontend/src/api/mock.ts` | `:395` `    { id: "grok", label: "Grok 4.3", supports_reasoning: true, supports_vision: true },`; `:396` `    { id: "claude", label: "Claude Sonnet 4.6", supports_reasoning: false, supports_vision: true },`; `:616` `    patch.provider === "grok"`; `:656`/`:664` `      tool: "xai:grok-4.3",`; `:1376` `const EXTRACTOR = "xai:grok-4.3";`; `:4335`–`:4336` `            provider: "anthropic",` / `            model: "claude-opus-4-6",` |
| `frontend/src/api/client.ts` | `:657` ` * "grok" | "claude" are always present; enabling local hosting adds one id per`; `:666` `  /** Whether this provider/model honors a reasoning level (grok, or a local`; `:678` `  /** null whenever provider !== "grok" — the wire mirrors the UI's disabling. */` |
| `frontend/src/screens/LLMSettingsScreen.tsx` | `:597` `      const provider = u.provider ?? task?.provider ?? "grok";`; `:706`–`:710` `        // Claude gets its own wording; any other non-reasoning provider (local` / … / `          provider === "claude"` / `            ? "Claude manages thinking on its own."` |
| `frontend/src/screens/LLMSettingsScreen.test.tsx` | `:38` `      { id: "grok", label: "Grok 4.3", supports_reasoning: true, supports_vision: true },`; `:40`–`:41` `        id: "claude",` / `        label: "Claude Sonnet 4.6",`; `:468` `  it("hides reasoning and shows the Claude note when a tier moves off grok", async () => {` |
| `frontend/src/screens/VitalsScreen.test.tsx` | `:47`–`:48` `      provider: "anthropic",` / `      model: "claude-opus-4-6",`; `:121` `    expect(await screen.findByText(/claude-opus-4-6 · anthropic/)).toBeInTheDocument();` |
| `frontend/src/components/AnalysisTab.test.tsx` | `:52` `  extractor: "xai:grok-4.3",`; `:100` `    tool: "xai:grok-4.3",`; `:421` `    expect(screen.getByText("ocr · xai:grok-4.3 · 70%")).toBeInTheDocument();` |
| `frontend/src/screens/NoteScreen.test.tsx` | `:94` `  extractor: "xai:grok-4.3 · note.extract v2",`; `:344`, `:357` |

### 5.6 Doc references

Command C5 lists 37 files. The non-archive, non-mock ones:
- `docs/reference/ANALYSIS.md:335` `All domains may use cloud LLMs (Anthropic/xAI) during development — recorded`; `:350` `default when unset is `low`) is sent **only** to the xAI/Grok provider; it is`; `:514` `` `anthropic | xai | local` (+ model), defaulting to **`xai:grok-4.3` for ``; `:565` `seeded with **grok-4.3 at $1.25/M in, $2.50/M out** (xAI docs, June`
- `docs/reference/SERVICES.md:85` `Stock deploys route LLM calls to the cloud (Anthropic / xAI) through the LLM`
- `docs/reference/ARCHITECTURE.md:62` `Anthropic-native, and OpenAI-compatible (xAI on a stock deploy; opt-in **on-box`
- `docs/reference/MODEL_PROMPTING.md:65`–`:67`, `:397`, `:406`–`:408` (e.g. `:406` `Cloud: `xai:grok-4.3` → temp 0.7 / top_p 0.95 (documented default; penalties dropped —`)
- `docs/reference/DESIGN.md:733` `    (`ocr · xai:grok-4.3 · 70%`). A row lacking a description in ocr mode`; `:743`
- `docs/ROADMAP.md:95` `LLM adapter (Anthropic + OpenAI-compatible). Fact and entity extraction on`
- `docs/runbooks/STRIX_HALO_SETUP.md:101` `- **Anthropic / xAI keys** — paste, or leave blank to run fully local.`
- `docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md:282`, `:416`; `docs/plans/VIDEO_IMAGE_TOOLS_PLAN.md:275`; `docs/plans/AGENT_CANVAS_PLAN.md:353`
- `LOCAL_ONLY_BOX_PLAN.md` (deleted) (an existing plan document on this same removal — e.g. `:384` `` `openai_compat.py` is **shared** (`router.py:840` xai / `:841-847` local) — only the xAI ``); `docs/plans/TOOL_CATALOG_PLAN.md:50`, `:53`; `docs/proposed/TEACHER_MODE_AGENTS_PLAN.md:423`

Archive/mocks (17 further files) are listed by C5 and not re-quoted here.

### 5.7 Deploy / env-template references

- `deploy/docker-compose.yml:101`, `:102` (quoted in §5.2) — the only compose lines.
- `deploy/install.sh:103`, `:104`, `:121`, `:122` (quoted in §5.2).
- No `.env.example` or template file exists: `find . -name "*.env*" -not -path "*/node_modules/*" -not -path "*/.venv/*"` returns nothing.
- `scripts/grok-eval.sh:5` (quoted in §5.4).

### 5.8 EXPLICITLY NOT the cloud providers (matches a cloud-ish grep, but is something else)

| Thing | Why it is not the provider — verbatim |
|---|---|
| **Grokipedia scraper** — `backend/src/jbrain/web/grokipedia.py`, `backend/src/jbrain/agent/grokipediatools.py` | `web/grokipedia.py:3`–`:4` `Grokipedia is xAI's AI-generated encyclopedia. This client reaches it over the` / `**open internet only** — no xAI/Grok API key — via the site's own first-party`. Wired at `backend/src/jbrain/main.py:654` `        web_handlers.update(build_grokipedia_handlers(GrokipediaClient(), emit=brain_emit))`. Base URL is not `xai_api_key`-dependent. |
| **`grok` CLI inside the jcode sandbox** (an external binary, not a client of xAI's API) | `deploy/docker-compose.yml:528`–`:531` `      # Grok Build (`grok`, xAI's official CLI) is the session harness. A login-shell hook` / `      # (/etc/profile.d/grok-config.sh) renders ~/.grok/config.toml from these vars so` / `      # `grok` targets the on-box models over an OpenAI-compatible base_url. base_url` / `      # points at the api's residency-aware jcode proxy (NOT the gateway directly): the`. Model env: `:526` `      JCODE_MODEL: ${JCODE_MODEL:-qwen3-coder-next}`; `:527` `      JCODE_MODEL_BASE_URL: ${JCODE_MODEL_URL:-http://local-llm:8080}`. |
| **`GET /api/install/grok.ps1`** — installs that same CLI | `backend/src/jbrain/api/install.py:91` `@router.get("/install/grok.ps1")`; `:93` `    """Windows PowerShell setup script for the Grok Build CLI.`; `:20` `#   irm https://hopkinsbrain.com/api/install/grok.ps1 | iex`. Frontend counterpart `frontend/src/screens/ExternalSessionScreen.tsx:41` `  const grokCmd = `OPENAI_BASE_URL=${url}/v1 \\\n  OPENAI_API_KEY=${tok} \\\n  grok`;` — the base URL is the box itself. |
| **`jcode_llm.py` proxy** ("grok" throughout) | `backend/src/jbrain/api/jcode_llm.py:1` `"""Residency-aware, multi-model proxy for the jcode sandbox's grok CLI (live `/model`).`; `:2`–`:3` `The sandbox's grok CLI lists every installed tool-capable local model and lets the owner`. It forwards to the on-box gateway: `:182` `                async with client.stream("POST", "/chat/completions", json=payload) as upstream:` with `:163` `    client = factory(base_url=gateway_url.rstrip("/"), timeout=_TIMEOUT)`. |
| **`external_llm.py`** — exposes the *on-box* coder over an Anthropic-shaped endpoint | `backend/src/jbrain/api/external_llm.py:1` `"""External LLM sessions: a token-gated public proxy to the on-box coder.`; `:15` `configured coder (settings.jcode_model) — pinned, never the caller's choice.`. UI copy: `frontend/src/screens/JcodeScreen.tsx:886` `            A token-gated public endpoint exposing your loaded coder over the Anthropic API. Off`. |
| **Terminal `claude` inside a jcode session** | `frontend/src/screens/JcodeSessionScreen.tsx:703` `                    The terminal's <code>claude</code> runs the coder on-box (~`; `backend/src/jbrain/llm/local_catalog.py:880` `        "native 256k window: jcode's terminal `claude` wants the whole context, and the "` |
| **`OpenAiCompatClient` — shared by `xai` AND `local`** | `backend/src/jbrain/llm/openai_compat.py:3`–`:4` `Serves two providers: xAI (https://api.x.ai/v1) and the local escape hatch` / `(Ollama-style server at JBRAIN_LOCAL_LLM_URL). Keeping them on one client is`. Both constructions sit side by side: `router.py:840` (`provider="xai"`) and `router.py:841`–`:847` (`provider="local"`). Provider-specific branches inside the class: `openai_compat.py:209` `        if reasoning_effort is None or self.provider not in ("xai", "local"):` and `:237` `        if self.provider != "local":`. |
| **`.claude/` directory + `CLAUDE.md`** | Claude Code harness config: `.claude/hooks/session-start.sh:4` `# Only bootstrap automatically in Claude Code on the web; local checkouts`; `.claude/settings.json:8` `            "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/session-start.sh"` |
| **Debug-access UI labelled "Claude"** | `frontend/src/screens/SettingsScreen.tsx:1292` `        <h2 className="settings-label">Debug access (Claude)</h2>`; `:376` `      .mintDebugToken(debugLabel.trim() || "Claude debug", debugTtl)` — mints a capability token for this box's own debug API. |
| **`tests/conftest.py`** | `:75` `    """Integration tests need a daemon; Claude Code web sessions lack one."""` |
| **`.github/workflows/ci.yml`** | `:76` `  # It is where the RLS firewall is proven (CLAUDE.md #3), and a path heuristic that` — a doc reference, no key or provider. |
| **`scripts/prefill-experiment.py:163`** | `    "grokipedia": "Look a topic up in Grokipedia, xAI's encyclopedia — background on a subject, not current news.",` — a tool-description string for the prefill benchmark. |

---

## 6. Deploy and CLI surface

### 6.1 CLI subcommands (`python -m jbrain.cli <cmd>`, `backend/src/jbrain/cli.py`)

| Command (verbatim `add_parser`) | What it runs | DB needed | On failure |
|---|---|---|---|
| `:247` `    sub.add_parser("local-provision-ids", help="print the local-model install queue")` | `_print_provision_ids` → `:109` `        for model_id in await store.llm_local_provision_requested(SYSTEM_CTX):` | yes (`create_async_engine(settings.database_url)`, `:106`) | caller uses `|| true` — `deploy/local-models-sync.sh:41` `requested="$(docker compose run --rm -T api python -m jbrain.cli local-provision-ids || true)"` |
| `:248` `    sub.add_parser("local-provision-clear", help="empty the local-model install queue")` | `:122` `        await store.set_llm_local_provision_requested(SYSTEM_CTX, [])` | yes | `deploy/local-models-sync.sh:307` `docker compose run --rm -T api python -m jbrain.cli local-provision-clear || true` |
| `:249` `    sub.add_parser("local-remove-ids", help="print the local-model uninstall queue")` | `:135` `        for model_id in await store.llm_local_remove_requested(SYSTEM_CTX):` | yes | `local-models-sync.sh:46` `… local-remove-ids || true)"` |
| `:250` `    sub.add_parser("local-remove-clear", help="empty the local-model uninstall queue")` | `:148` `        await store.set_llm_local_remove_requested(SYSTEM_CTX, [])` | yes | `local-models-sync.sh:308` `… local-remove-clear || true` |
| `:251`–`:255` `local-activate <model_id>` | `_local_activate` → `:174` `        overrides[_PRIMARY_MODEL_TASK] = active_local_override(model)`; `:175` `        await store.upsert(SYSTEM_CTX, LLM_TASK_OVERRIDES_KEY, overrides)` | yes | `local-models-sync.sh:301` `  docker compose run --rm -T api python -m jbrain.cli local-activate "$activate" || true` |
| `:256`–`:259` `local-llm-unload` | `_local_llm_unload` → `:64` `        loaded = await gateway.running()`; `:73` `            await gateway.unload(served)` | **no** — no engine created; `:59` `    if not settings.local_llm_enabled:`; contract `:53`–`:54` `    Best-effort by contract: a gateway that is already down, unreachable, or holding` / `    nothing is a success, not a failure. It must never abort an update.` | always exits 0 (`:61,:67,:70,:79 return 0`) |
| `:260`–`:263` `local-llm-auto-update` | `_print_auto_update` → `:94` `        return 0 if await store.local_llm_auto_update(SYSTEM_CTX) else 1` | yes; `:95`–`:96` `    except Exception:  # noqa: BLE001` / `        return 0` | unreachable DB ⇒ exit 0 ("ON"), stated at `:87`–`:89` `    Unreachable DB reads as ON: this gates a safety net (rebuild onto the newest llama.cpp,` / `    then verify it can still serve), and failing closed would silently freeze the gateway` / `    on an old base every time the database hiccuped during an update.` |
| `:264`–`:267` `local-llm-smoketest` | `_local_llm_smoketest` → `:236` `    ok, messages = await run_smoketest(settings.local_models, gateway)` | **no** — `:186`–`:187` `    the installed set + gateway URL from settings (env-wired in the api container); no` / `    DB needed, so it runs under `docker compose run --rm --no-deps -T api`.` Window/slot loaders read the yaml instead: `:212` `    _shapes = llama_swap_config.served_shape_from_config(settings.local_models_dir)` | `return 0 if ok else 1` (`:239`); caller rolls back |
| `:245` `init` / `:246` `reset-owner-key` | `_rotate` | yes | n/a |

### 6.2 Deploy script steps that load / unload / prime / smoke-test / reconfigure

`deploy/update-inner.sh`:

| Line | Step | DB available? | On failure |
|---|---|---|---|
| `:239`–`:241` `  run_bounded "$TOGGLE_TIMEOUT_S" docker compose run --rm --no-deps -T api \` / `    python -m jbrain.cli local-llm-unload \` / `    || echo "[update] unload skipped (gateway unreachable?)"` | `release_models()` — controlled unload | **no** (`--no-deps`) | echoes and continues |
| `:498`–`:499` `  docker compose run --rm --no-deps -T api python -m jbrain.cli local-llm-unload \` / `    || echo "[update] unload skipped (gateway unreachable?)"` | pre-build unload, gated by `:489` `if grep -q '^LOCAL_LLM_ENABLED=true' .env; then` | **no** (`--no-deps`) | echoes and continues |
| `:507` `  docker compose --profile local-llm rm -sf local-llm || true` | stop + remove the gateway | n/a | `|| true` |
| `:513`–`:520` bounded wait loop (`:514` `  while [ "$_waited" -lt 60 ]; do`) | wait for the gateway container to disappear | n/a | warning only |
| `:661` `if grep -q '^LOCAL_LLM_AUTO_UPDATE=false' .env; then` … `:663`–`:664` `elif ! run_bounded "$TOGGLE_TIMEOUT_S" docker compose run --rm --no-deps -T api \` / `    python -m jbrain.cli local-llm-auto-update; then` | read the PWA toggle | **no** (`--no-deps`); documented at `:741`–`:742` `# what kept them up there was the Settings read below running on the new image…` / `# or unreadable DB returns "auto-update ON" by design (jbrain.cli._print_auto_update),` | non-zero ⇒ `:665`–`:666` `  echo "[update] gateway auto-update is OFF (Settings) — keeping the pinned base, no model load"` / `  AUTO_UPDATE_ON=''` |
| `:681` `  pause_api` | stop the api so the WarmKeeper prime cannot race the load. Definition `:354`–`:356`: `pause_api() {` / `  echo "[update] pausing api for the model load (its keep-warm prime would race it)"` / `  docker compose stop -t 30 api >/dev/null 2>&1 || echo "[update] could not pause api"` | n/a | echoes |
| `:684`–`:686` `  if run_bounded "$PULL_TIMEOUT_S" env LOCAL_LLM_BASE="$FLOATING" \` / `      docker compose --profile local-llm build --pull local-llm \` / `      && docker compose --profile local-llm up -d local-llm; then` | float the gateway onto newest llama.cpp | n/a | else-branch sets `SMOKE_FAILED=1` |
| `:694`–`:695` `    elif drop_page_cache && run_bounded "$SMOKE_TIMEOUT_S" docker compose run --rm --no-deps -T api \` / `        python -m jbrain.cli local-llm-smoketest; then` | load smallest tool-capable model + tool probe | **no** (`--no-deps`) | `SMOKE_FAILED=1` → `:712` `    release_models` then `:715`–`:717` rollback rebuild on the pinned base with `|| echo "[update] WARNING: gateway rollback rebuild failed — check 'jbrain logs local-llm'"` |
| `:730`–`:731` `  release_models` / `  echo "[update] gateway emptied before the recreate ($(mem_available_gb) GB available)"` | empty the gateway before recreate | **no** | best-effort |
| `:746` `docker compose run --rm migrate` | migrations | yes | `set -e` aborts |
| `:749` `docker compose $JCODE_PROFILE $TUNNEL_PROFILE up -d` | recreate stack | n/a | `set -e` |
| `:762` `sh src/deploy/local-models-sync.sh || echo "[update] local-model sync skipped (will retry next update)"` | provision models | see below | echoes, update continues |
| `:787`–`:788` `  echo "[update] restarting local-llm gateway"` / `  docker compose --profile local-llm up -d local-llm || true` | bring the gateway back | n/a | `|| true` |

Timeout ceilings, `deploy/update-inner.sh:53`–`:57`:
```
SMOKE_TIMEOUT_S=600
TOGGLE_TIMEOUT_S=120
PULL_TIMEOUT_S=1800
```
`run_bounded` maps a ceiling hit onto a normal failure: `:87`–`:88` `  if [ "$_rc" -eq 124 ] || [ "$_rc" -eq 143 ]; then` / `    echo "[update] TIMEOUT after ${_limit}s: $*"`.

`deploy/local-models-sync.sh`:

| Line | Step | DB available? |
|---|---|---|
| `:34` `grep -q '^LOCAL_LLM_ENABLED=true' .env || { say "hosting off — skipping model sync"; exit 0; }` | gate | n/a |
| `:37` `catalog() { docker compose run --rm --no-deps -T api python "$@"; }` | pure-catalog reads — **no DB** (`--no-deps`), stated at `:36` `# Catalog reads run in the api image (pure Python; --no-deps skips the database).` | no |
| `:41` `requested="$(docker compose run --rm -T api python -m jbrain.cli local-provision-ids || true)"` | install queue read — **DB required**, and this omits `--no-deps`; `:39`–`:40` `# 1. The PWA install queue (owner-scoped DB read). This runs after `up -d`, so the` / `#    db is up. A clean empty result is the normal "nothing queued" case.` (compose brings `db` up via `deploy/docker-compose.yml:221`–`:223` `    depends_on:` / `      db:` / `        condition: service_healthy`) | yes |
| `:46` `removing="$(docker compose run --rm -T api python -m jbrain.cli local-remove-ids || true)"` | uninstall queue read | yes |
| `:115` `  manifest="$(catalog -m jbrain.llm.local_catalog $ids)"` — `:116` `  [ -n "$manifest" ] || { say "empty manifest — aborting sync"; exit 1; }` | render manifest | no |
| `:241`–`:242` `sed -i '/^LOCAL_MODELS=/d' .env` / `echo "LOCAL_MODELS=$json" >> .env` | rewrite the served roster | n/a |
| `:254`–`:258` `if [ -n "${JBRAIN_SKIP_GATEWAY_START:-}" ]; then` / `  say "update in progress — leaving the gateway down; it is restarted after the rebuild"` / `  docker compose up -d api` / `else` / `  docker compose --profile local-llm up -d` | restart gateway + api | n/a |
| `:267` `KEEP="$ids" sh src/deploy/prune-local-weights.sh "$PWD/local-models" $(cat "$remove_file") || true` | delete uninstalled weights | n/a; `|| true` |
| `:301` `  docker compose run --rm -T api python -m jbrain.cli local-activate "$activate" || true` | re-point `agent.turn` | yes; `|| true` |
| `:307`–`:308` `docker compose run --rm -T api python -m jbrain.cli local-provision-clear || true` / `docker compose run --rm -T api python -m jbrain.cli local-remove-clear || true` | clear both queues | yes; `|| true` |

Supervisor one-shot commands (what the PWA buttons trigger),
`supervisor/src/supervisor/gateway.py`:
```
:35       "apk add --no-cache git >/dev/null 2>&1 && exec sh src/deploy/update-inner.sh"
:46   PROVISION_COMMAND = "exec sh src/deploy/local-models-sync.sh"
```
Mutual exclusion, `supervisor/src/supervisor/app.py:407`–`:408`:
```
            raise HTTPException(
                status_code=409, detail="another one-shot is running"
```

`deploy/jbrain:65` `    docker compose run --rm api python -m jbrain.cli reset-owner-key` and
`deploy/install.sh:173` `  docker compose run --rm api python -m jbrain.cli init` are the only other
CLI invocations (`grep -rn "jbrain.cli" deploy scripts supervisor/src`).

---

## 7. Things I could not determine

1. **Whether the supervisor process holds its own copy of any model-affecting setting.** I
   audited `backend/src/jbrain/config.py` only; `supervisor/src/supervisor/config.py` was not
   read.
2. **Whether any runtime path re-reads `.env` after boot.** I established that `Settings` is
   instantiated once per api process (`main.py:254`) and per CLI invocation, but I did not
   audit the supervisor process for its own `Settings`-like reload.
3. **The full 61-file test list for the cloud manifest.** I have the file list (command C2 in
   §5.0 reproduces it) but did not open every file, so I quoted gating lines only for the
   subset in §5.4. The remaining ~45 files match on `{"xai": FakeLlmClient(...)}`-shaped
   router fixtures; I verified that shape in 12 of them.
4. **Whether `llm_local_unavailable` is consulted anywhere in the routing path.** The
   repo-wide grep (`grep -rn "llm_local_unavailable" --include="*.py" --include="*.ts" --include="*.tsx" --include="*.sh" .`)
   returns production hits only in `backend/src/jbrain/settings_store.py` (`:91,:897,:902`) and
   `backend/src/jbrain/api/llm_settings.py` (`:460,:1275,:1280`). I did not trace whether the
   `available` field it produces (`api/llm_settings.py:588` `    available = enabled and not unavailable`)
   reaches any server-side load/route decision beyond the snapshot and the unload at `:1286`.
5. **Android client.** `/home/user/JBrain2/android` exists but I did not search it for model
   controls or cloud references.
6. **`docs/mocks/*.html` and `docs/archive/**`** cloud matches — enumerated by command C5 but
   not individually quoted.

---

# §G — Sites this inventory never listed

> Added 2026-08-22 from a cold falsification pass. Everything here is absent from §A–§F, and
> two entries are the most consequential findings in the document. Cited by function name
> rather than line number, deliberately: §A's numbers rotted 24% in four commits, and a name
> survives a refactor that a number does not.

## G1 — The gateway's OWN already-resident short circuits

§A's call-site table tracks residency's four short circuits and none of `local_gateway`'s
three. They are downstream of residency and **cannot be fixed there**:

| where | the test | what it skips |
|---|---|---|
| `_load_and_warm`'s already-resident branch | `if served_model in await self.running():` | **`refuse_if_no_device_room` AND `guarded_load`, both** — then calls `_do_load()`, whose `/upstream/<model>/health` GET makes llama-swap launch the process |
| the queued-load join in `load` | `if queued and served_model in await self.running():` | returns "already loaded" and records a `MODEL_LOAD` span for a load that did not happen |
| `_require_resident` (diagnostic reads) | `if served_model not in await self.running():` | guards `props`/`slots`/`metrics`; its own docstring says reaching them on a cold model *"froze this host to a power cycle"* |

**G1a WAS the hole; CLOSED 2026-08-22.** `running()` lists a model llama-swap is STOPPING. So a
load targeting a mid-stop model took the branch written on the premise that *nothing will be
allocated* — and allocated the entire model, with no pre-flight and no watchdog. This is the
same window `residency._note_if_not_ready` was added to measure; nobody traced it down to the
gateway's own short circuit. Seven already-resident short circuits exist across the two layers
and all seven read the same state-blind set.

`_load_and_warm` now calls `_settle_a_stopping_model` before it picks a branch: it WAITS for the
stop to land rather than re-spelling the state test, because routing a stopping model to the
guarded path would ask for room while the dying model's footprint is still charged to the
device pool — the double-count three earlier attempts made. Once the stop lands, "resident" and
"absent" mean what they say and every existing number is right. A stop still unlanded after 20 s
(llama-swap grants 10 s of graceful stop before escalating) is a retryable `LocalGatewayError`,
because the only other fallthrough is the unguarded load. **The other two rows are unchanged and
still read the state-blind set** — they are diagnostic/join paths, not loads.

## G2 — Loads that run outside the cross-process box lock

**OVERTAKEN 2026-08-23 (#1194).** NO load runs under the box lock any more — `ensure_room` now
decides+evicts under it and loads outside it, the rule `_restore` already followed. The
protection across the load is the ledger's charge row, written at intent (see §A.1c A18 and
`../plans/LOCAL_MODEL_LEDGER_PLAN.md` L1 item 5). The paragraphs below record the pre-ledger
shape.

Only `ensure_room`'s slow path holds `_box_locked()` across its load. Outside it: the owner's
Load button, the debug prime, `_restore`, `jcode._warm_model`, the warm-keeper fallback, and
`smoketest`. `_restore` is the likeliest instantiator — it fires at the end of every displaced
turn, aims at exactly the memory a concurrent evict just freed, and takes its budget snapshot
outside any lock. See `../plans/LOCAL_MODEL_LEDGER_PLAN.md` L1 item 5 for why this is recorded
rather than fixed, and for the measurement (inconclusive: no worker loads in the window).

A non-memory hazard of the same gap: `gateway.load` calls `_config_regen`, and a config change
makes llama-swap reload, **killing every running llama-server**. Run outside the lock, the
owner's Load can therefore destroy a model the worker admitted and loaded *under* it.

## G3 — A fourth budget, and it is the one with teeth

§3's "eight uncoordinated budgets" (six, after the `CACHE_RAM_GB` correction) omits
`host_settings.HOST_RESERVE_GIB = 16` — what the host script reserves when deriving
`ttm.pages_limit`. It is a **kernel-level** bound on the same physical pool, set outside the
app, unreconciled with the others. `gpu_guard` records that the box currently sets
`ttm.pages_limit` to ~100% of RAM, i.e. **the only bound with enforcement teeth is disabled in
practice**.

Related: `MIN_FREE_GTT_GB` is subtracted from the device headroom *and* from the host headroom
inside one function, so it is also a second host-RAM reserve stacked on residency's fraction —
one constant answering two different physical questions.

## G4 — Two `/proc/meminfo` readers with different formulas

`host_metrics.read_memory_gb` returns `(total, total - (MemFree + SReclaimable))`;
`smoketest.mem_available_gb` returns `(MemFree + SReclaimable)` directly, via its own parser.
Same semantics, two implementations, no shared helper — they agree by maintenance, not
construction. `smoketest`'s keeps a name that no longer describes what it reads.

## G5 — The warm phase runs outside the watchdog

**G5 WAS the gap; CLOSED 2026-08-22 (#1188).** `guarded_load` returned, and only then did
`_load_and_warm` call `_warm(...)` — measured in that file at **118 s of a 198 s gpt-oss-120b
load**. So ~60% of a cold load allocated KV and graph-capture buffers with nothing watching
for a runaway. The warm now runs INSIDE `guarded_load` via `_load_then_warm`
(`local_gateway.py:1145`); the pre-flight had already admitted weights + KV + projector, so
the ceiling covered the warm all along — only the watching stopped early.

## G6 — The measured-footprint series is biased

`_record_measured_footprint` runs only on the fully-guarded path. Both the no-probe branch and
the already-resident branch return before it. So the predicted-vs-measured series that the
catalog is meant to be corrected from is **missing exactly the loads that took a shortcut** —
which matters to any wave proposing "measure instead of predict".

## G7 — `dbless_coordinator` is a sanctioned half-wired gate

`ResidencyWiring`'s whole premise is that a half-wired coordinator should be a type error. The
DB-less path builds one with no window sizing, no operator floor, no code-mode hold and no
cross-process lock. Its docstring is honest about it, but it is an exemption from the invariant
the type exists to enforce, and it is the kind of thing cited as precedent for the next one.
