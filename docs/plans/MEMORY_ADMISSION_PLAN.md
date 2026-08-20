# Memory Admission on Unified Memory — Design Spec

> **Status:** Draft (v2 — rewritten after adversarial review) · **Last verified:** 2026-08-20 · **Waves:** W0◻️ W1◻️ W2◻️ W3◻️ W4❌ W5◻️

> Reconciled with the root `CLAUDE.md` non-negotiables — no LLM or storage
> surface changes (rules 1–2); tests land with the code (rule 5); rule 10 is
> load-bearing and every step is named as PWA-operable, debug-API-operable, or
> host-bound with no pretence otherwise.

> **v2 supersedes v1 wholesale.** Three adversarial reviews and four research
> passes overturned v1's central diagnosis, its headline number, its wave order
> and two of its four proposed mechanisms. v1 claimed `ttm.pages_limit` was the
> lost kernel back-pressure; it never was any. v1 proposed a 21 GiB reserve; the
> correct direction is *smaller* than the 16 already shipped. v1 proposed cgroup
> enforcement; GTT is not charged to a cgroup at all. What survives is the
> observation that we have **eight uncoordinated memory budgets and no working
> enforcement layer other than our own code**. Errors are kept visible below
> rather than quietly deleted, because two of them are also committed in the
> repo and on the live box.

The box froze for seven hours on 2026-08-19 and needed a power cycle the owner
could not perform remotely. Three fixes have shipped since, each correct and
each treating a symptom. This plan stops that.

---

## 1. What is actually wrong

### D0 — eight uncoordinated budgets, and no single source of truth

**This is the root defect.** D1–D4 are its symptoms. Every one of these claims
to protect the same RAM, none of them agree, and nothing reconciles them:

| # | constant | value | location |
|---|---|---|---|
| 1 | `MIN_FREE_GTT_GB` | 6.0 | `gpu_guard.py:100` |
| 2 | `RUNAWAY_MULTIPLE` / `+2.0` | 1.75 | `gpu_guard.py:95` |
| 3 | `llm_local_free_ram_fraction` | 0.15 (live) | Settings / `config.py` |
| 4 | residency constructor default | **0.25** | `residency.py:159` |
| 5 | `LOAD_HEADROOM_GB` | 20.0 | `smoketest.py:85` |
| 6 | `RESERVE_GIB` | 16 | `strix-halo-host-setup.sh:57` |
| 6b | same, hardcoded | `16 * 1024 * 1024` | `update-inner.sh:627` |
| 7 | `HOST_RESERVE_GIB` | 16 | `host_settings.py:35` |
| 8 | `CACHE_RAM_GB` | 8.0 | `local_catalog.py:95` |

Row 4 disagreeing with row 3 is a latent bug on its own: any construction path
that does not pass the fraction explicitly reserves 30 GiB instead of 18.2.

Two live consequences already measurable today, neither of which anything
reports:

- `footprint_gb(qwen3-coder-next-q8, 262144)` = **103.55** against a residency
  ceiling of **103.02**. That model is permanently over budget at its own
  catalog default window — it evicts everything on every load, silently.
- Saved context-window overrides exist for **`qwen3-235b-a22b`** and
  **`qwen3.5-122b-a10b-mtp`**, neither of which is in `CATALOG` or
  `RETIRED_IDS`. `_footprint` returns **0.0** for a served name outside the
  catalog, so anything running under those names is invisible to the evictor.

### D1 — CORRECTED: `ttm.pages_limit` is not, and never was, back-pressure

**v1 of this plan was wrong here, and so are two comments already in the repo.**

v1 claimed: the limit sits above `MemTotal`, so the swapout loop is unreachable,
so we lost the kernel's allocation-time refusal. The first half is true. The
conclusion is false, because there was never a refusal to lose.

`ttm_tt_populate()`, read to the end this time:

```c
atomic_long_add(ttm->num_pages, &ttm_pages_allocated);   /* counted BEFORE the check */
while (atomic_long_read(&ttm_pages_allocated) > ttm_pages_limit || ...) {
        ret = ttm_global_swapout(ctx, GFP_KERNEL);
        if (ret == 0)
                break;        /* nothing swappable -> ALLOCATE ANYWAY, over the limit */
        if (ret < 0)
                goto error;
}
```

- The counter is incremented **before** the check: this is soft accounting, not
  an admission gate. Exceeding the limit never by itself refuses anything.
- `ret == 0` means "nothing could be swapped" and **breaks the loop, allowing
  the allocation**. Refusal happens only on a negative errno, which is a hard
  error (interrupted wait), never "over budget".
- **`-ENOSPC` cannot escape this path.** `ttm_bo_swapout_cb()` explicitly
  rewrites `-ENOSPC`/`-ENOMEM` → `-EBUSY`; `ttm_lru_walk_for_evict()` rewrites
  `-EBUSY` → 0. Pinned BOs (a serving model's weights) return `-EBUSY`, and the
  LRU walk is `trylock_only`, so contended BOs are skipped silently too.
- TTM "swapout" writes BO pages to **shmem — i.e. back into page cache**, not to
  a swap device. With little or no swap it copies RAM→RAM, frees the originals,
  and **transiently doubles the footprint during the copy**.
- The stock default is already `MemTotal / 2`, so "below MemTotal" is not even a
  tightening relative to stock.

> ✅ **RESOLVED 2026-08-19 — and it reverses the paragraph below.** A second
> mechanism exists and it is the real one. `amdgpu_ttm.c:2156` sets
> `gtt_size = ttm_tt_pages_limit() << PAGE_SHIFT` at probe and passes it to
> `amdgpu_gtt_mgr_init()`; `amdgpu_gtt_mgr.c:129-134` then refuses:
>
> ```c
> if (!(place->flags & TTM_PL_FLAG_TEMPORARY) &&
>     ttm_resource_manager_usage(man) > man->size) {
>         r = -ENOSPC;
> ```
>
> That IS a clean allocator refusal before pages are handed out. So:
>
> | mechanism | refuses? |
> |---|---|
> | `ttm_tt_populate` soft loop (analysed below) | **no** — breaks and allocates anyway |
> | GTT resource-manager size (`man->size`) | **YES** — `-ENOSPC` |
>
> **A BOOT-TIME `ttm.pages_limit` below MemTotal is a genuine hard cap.**
> **A RUNTIME write is not**: `man->size` is assigned once at
> `ttm_resource.c:541` and nothing re-reads `ttm_tt_pages_limit()` afterwards.
> The runtime write fails *silently and looks like success* — it returns 0 and
> reads back the new value while the cap is untouched. `mem_info_gtt_total`
> reports `man->size`, so it is the only honest readout of the enforced cap.
>
> Consequences: v1's conclusion ("inert, lower it") was RIGHT, for a mechanism
> neither v1 nor v2 identified. **W1b is dead. W1d (grub + reboot) is the only
> mechanism**, and is a terminal dependency that genuinely cannot be designed
> out. The Ops card's `ttm` row and its grub remedy are correct after all.
> Note the stock default is already `MemTotal/2`, so our 124 GiB cmdline
> *raises* the ceiling rather than setting one; and `TTM_PL_FLAG_TEMPORARY`
> allocations bypass the check.
>
> **Also a live defect:** #1160 (merged today) writes this parameter on every
> update and reports it produces "a clean `-ENOSPC` for the current boot". It
> does not. It has been reporting success for a no-op since 2026-08-19.

The soft-loop analysis below remains accurate for what it covers, and is kept
because it explains why the *other* mechanism is not a backstop:

> ⚠ **PENDING RE-VERIFICATION (2026-08-19).** Everything above concerns the
> `ttm_tt_populate` soft loop, and is confirmed. But a SECOND mechanism may
> exist and would change the conclusion: `amdgpu_ttm.c` derives the GTT
> **resource manager** size from `ttm_tt_pages_limit()` at probe
> (`gtt_size = ttm_tt_pages_limit() << PAGE_SHIFT`), and if `amdgpu_gtt_mgr_new()`
> refuses once that manager is full, then a BOOT-TIME `ttm.pages_limit` is a real
> hard cap after all — while a RUNTIME write would be the useless one, since the
> manager is already sized. That is the exact opposite of the paragraph below.
> Supporting observation: this box reports `gtt_total` = 124.0 GiB, matching its
> cmdline `ttm.pages_limit=32505856` exactly and EXCEEDING `MemTotal` (121.2), so
> the manager size does track the parameter. Do not act on D1 or W1 until this
> resolves.

**Therefore the soft loop is not a backstop**: in exactly the state we care
about (everything pinned) it forces a futile global LRU walk on every populate,
adding lock traffic and shmem churn. The enforcement comes entirely from
`man->size`, set at boot.

Two places assert the false version and must be corrected in the same change:
`scripts/strix-halo-host-setup.sh:47` and `deploy/update-inner.sh:175`, both
claiming this "turns a GTT over-commit into a clean `-ENOSPC`".

**And one is live and user-facing:** the Ops → Host settings card (shipped
#1160) tells the owner `ttm.pages_limit` is *"DISABLED — a GTT over-commit
cannot be refused"* and offers a grub-and-reboot remedy. A GTT over-commit
cannot be refused **at any value**. The card reports a false problem with a
remedy that would not work. Fixing that is W0.

### D2 — the failure path strands the whole model in page cache

`drop_weights_page_cache` is called on the success path only
(`local_gateway.py:374` and `:394`, both straight-line after the `await`,
neither in a `try`/`finally`). `guarded_load` raises `GpuBudgetError` at
`gpu_guard.py:379` from outside its own `try/finally`, so the drop is skipped.
No unload path drops either, and the abort routes through exactly that unload.

Measured: an aborted `qwen3.5-4b` left `Cached` +4.29 GiB (≈ its full 4.3 GB
weight file). A successful `qwen3.8-27b-q4` left +0.00 GiB. The drop works; it
just never runs when it matters most.

This is now a **ratchet**, because #1159 correctly changed `read_memory_gb` to
count page cache as used:

> abort strands cache → cache counts as used → less apparent headroom → next
> load likelier to abort → more stranded cache

and `residency.py:627` suppresses `GpuBudgetError` on the end-of-turn restore,
so it runs silently.

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

`ceiling = baseline + max(projected × 1.75, projected + 2.0)`. Crossover where
the two terms swap is exactly **8/3 GiB**. Measured against the real ceiling:

| model | projected | ceiling | observed | margin |
|---|---|---|---|---|
| qwen3.5-0.8b | 1.57 | 3.57 | 3.78 | +6% |
| qwen3.5-4b | 7.25 | 12.69 | 12.80 | **+0.9%** |
| qwen3.8-27b-q4 | 20.59 | 36.05 | 26.10 | −28% |

`qwen3.5-4b` **cannot load today**, aborted on a 0.9% overshoot by a guard whose
own docstring says it exists to catch "the ORDER-OF-MAGNITUDE balloon … not
ordinary overshoot". The projection is light by 5.55 GiB, most of it the
warm-up phase: `guarded_load` returns *before* `_warm()` runs, so KV allocation
and graph capture land after the guard's last sample. That is not a one-interval
race, it is an entire unwatched phase.

Note the direction is the opposite of the intuitive one: the effective allowed
multiple is `max(1.75, 1 + 2/p)`, so the guard is *looser* on small models
(2.27× for the 0.8b), not tighter.

---

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

### Candidates NOT yet evaluated, which belong here

- **Remove `--no-mmap`.** This is the big omission in v1. `--no-mmap` is the
  *cause* of the double residency; D2's page-cache drop is cleanup for a copy
  that should not exist. Dropping the flag makes weights page-cache-backed and
  evictable instead of duplicated, which removes the D2 ratchet at the root
  rather than sweeping after every load. It is currently justified as "a gfx1151
  stability flag" with no citation and no re-test date — though note the
  community toolbox does list it as standing guidance, and per §5.2 it is also
  what makes the cache drop possible at all. Needs a decision, not silence.
- **Swap configuration.** The incident narrative has every service running from
  swap for seven hours. TTM's own swapout writes to shmem, which needs somewhere
  to go. Whether this box should have swap at all is a first-order question and
  v1 never asked it.
- **BIOS/UMA carve-out.** A firmware-level split is a genuine hardware partition
  between GPU and host. Not mentioned in v1 even to be rejected.
- **`memory.high` rather than `memory.max`.** Throttle-and-reclaim rather than
  kill. Moot for GTT (§5.1) but relevant to the page-cache half.
- **Reduce demand instead of policing it.** KV quantisation (`--cache-type-k/v
  q8_0`) and narrower served windows cut the footprint directly; the roster
  currently runs several models at 262144.

---

## 3. The shape of the fix

Three layers, and the point is that **no layer is trusted alone**:

1. **Kernel back-pressure (D1).** `ttm.pages_limit` below `MemTotal` so TTM's own
   swapout loop re-arms ahead of generic reclaim. This is the only layer that
   can refuse an allocation the userspace guard never saw — ComfyUI, a stray
   process, anything not going through our gateway.
2. **Kernel-enforced per-process budget.** cgroup `MemoryMax=` on the
   llama-server units, so a bad estimate fails *cleanly* instead of taking the
   box down. **Gated on an open question — see W4.**
3. **Userspace admission (D3, D4).** Our estimate stays authoritative for
   *decisions* (which model, evict what, plan a load), but stops being the only
   thing between a bad guess and a power cycle.

Plus detection: **PSI** rather than 1 s GTT polling, because amdgpu allocates
with `gfp_retry_mayfail` — **the OOM killer is never invoked**, which is why
earlyoom never fired during the seven-hour livelock. Raising earlyoom's
thresholds (shipped) only helps once memory actually drops.

---

## 4. Waves

> ⚠ **W1–W3 below are v1 text and are NOT yet re-derived.** Three findings
> invalidate them and the rewrite is blocked on §5.6:
> 1. **The wave order is inverted.** W1's ceiling is an *output* of W2/W3, not an
>    input — W2/D4a raises every projection by ~5.5 GiB, and W1 picks a number
>    before that lands. Correct order is W0 → W5 → W3 → W2 → W1.
> 2. **W1's number is wrong in direction.** At the "~21 GiB reserve" figure,
>    `qwen3-coder-next-q8` at its catalog window needs 95.55 against 94.2
>    admissible — permanently unloadable. With slots=2 (a shipped PWA feature)
>    it needs 105.55, above *every* candidate ceiling. Keeping it loadable after
>    D4a needs a reserve **≤14.15 GiB**, i.e. SMALLER than the 16 already
>    shipped, not the 21 v1 proposed.
> 3. **W1 must become a consolidation of D0's eight budgets, not a ninth.**
>    "One derived budget, five constants deleted" is the fix; "one more constant,
>    asserted, shipped before W3 measures anything" is the fourth band-aid.
>
> Also unfixed in the v1 text below: **W0 claims "no dependencies" and then names
> a blocking research item four bullets later**; both cannot be true. And D4's
> "+5.5 GiB is the warm-up phase" causal claim is **falsified** by a measurement
> already in the runbook — gpt-oss-120b measures GTT 67.6 against a 68.55
> projection, i.e. 0.95 GiB *heavy*, so whatever the gap is, it is not a
> mechanism that applies to every model.
>
> **Update 2026-08-19:** seven measured loads are now recorded under W3, and they
> settle the shape of that gap — the error is **bidirectional** (light at the
> small end, heavy at the large-context end), so no single causal story and no
> multiplicative margin covers it. The gpt-oss reading above is also reconciled
> there: 67.6 is resting GTT and 69.26 is peak-across-warm, and the two are not
> comparable. Read W3 before rewriting W1–W3.



### W0 — the two unambiguous fixes ◻️
No dependencies, evidence already nailed down.
- **D3a**: smoke test projects the served window/slots, not the catalog default.
  Wire the loaders, or have it read the same resolved shape the gateway will use.
- **D2**: move `_drop_weights_cache` into a `try/finally` covering both branches.
  It is already idempotent and returns `None` on a missing directory.
- Tests: the drop fires when `guarded_load` raises (no such test exists today);
  the smoke test's projection equals the gateway's for a model with an override.
- **Blocked-on-research**: whether `posix_fadvise(DONTNEED)` is safe and
  non-no-op on the failure path while llama-server may still hold the file open.

### W1 — establish the reserve ◻️
Target: a hard ~21 GiB the GPU can never take (i.e. an effective GTT ceiling of
~104 GiB against `MemTotal` 121.2 GiB). Independently corroborated: another
maintainer reaches the same number empirically ("~15 GB needed for OS and
orchestrator").

An earlier draft of this plan asserted that this "needs grub + reboot and cannot
be designed out". **That was wrong, and rule 10 says to keep pushing before
accepting a terminal dependency.** There are four mechanisms, in descending
order of strength. Take the strongest that is available.

**W1a — userspace reserve (no reboot, ships today, do this regardless).**
We are the ones deciding whether to load. Stop trusting the reported
`gtt_total = 124`; compute admission against
`min(gtt_total, mem_total - RESERVE_GB)`. One change, fully PWA-operable.
*Limit: voluntary compliance.* It binds every load through our gateway, but not
ComfyUI (~58 GB, no guard) and not llama-server growing after admission. This is
the correct userspace half of a two-layer design, not a substitute for the
kernel half.

**W1b — runtime write to `/sys/module/ttm/parameters/pages_limit` (no reboot).**
The parameter is registered `module_param_named(pages_limit, ttm_pages_limit,
ulong, 0644)` — writable at runtime. Lowering it does not retroactively free
anything, but it re-arms `ttm_global_swapout()` for the *next* allocation, which
is the whole point. **#1160 already attempts this on every update**
(attempt-and-report). If it takes, W1 needs no grub and no reboot, and this is
full kernel enforcement covering processes we do not control.
*Status: unknown.* `mem_info_gtt_total` reflects the TTM manager size and does
not move with `pages_limit`, so it cannot confirm this; the Ops host-settings
card reads the live value and is the check.

**W1c — container cgroup limit (no reboot).** `mem_limit` on the `local-llm`
service: kernel-enforced, PWA-deployable. **Gated on §5.1** — if GTT pages are
not charged to the container's memcg this caps ordinary RSS and does nothing for
the GPU pool. Same gate as W4.

**W1d — grub `ttm.pages_limit=27262976` + reboot.** The fallback, only if W1b
proves the parameter is not runtime-writable. Also drop the deprecated
`amdgpu.gttsize` and revisit `amd_iommu=off`. This is the only variant that is a
genuine terminal dependency, and it is now the *last* resort rather than the
plan of record.

Rejected: MITM'ing what llama-server sees. RADV takes heap sizes from the
`AMDGPU_INFO_MEMORY` DRM ioctl and reports budget via `VK_EXT_memory_budget`
from the driver — a sysfs bind-mount cannot reach either, so it would need an
`LD_PRELOAD` shim or a fake ICD. Fragile, and still only voluntary compliance.
Moot anyway: llama.cpp only asks how much is free when `--fit` is on, and §2
says keep `-fit off` on this platform.

### W2 — correct the projection, then stop hand-maintaining it ◻️
- **D3b**: `_served_shape` must not silently fall back to the catalog window —
  fail loudly or surface the degradation.
- **D3c**: `router.py:813` gains the missing `slots_loader`.
- **D4a**: account for the warm-up phase, which is currently outside the guarded
  window entirely — either extend the guard across `_warm()` or add its cost to
  the projection.
- **D4b**: re-derive the ceiling from measurements once W3 lands, rather than
  tuning 1.75 and +2.0 by hand. A multiplicative margin on a base we know is
  light scales the error with model size and says nothing about why.

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

### W4 — ~~kernel-enforced per-model budget~~ ❌ DELETED
**Answered and dead.** GTT pages are allocated `GFP_USER | GFP_HIGHUSER` with no
`__GFP_ACCOUNT`, are never `mem_cgroup_charge()`d, and are mapped to userspace
as `VM_PFNMAP` so they appear in neither `memory.current` nor RSS. AMD's own
patch to add accounting was NAK'd upstream ("That's intentionally not done like
that"). The `dmem` controller (6.14, amdgpu 6.15) registers only a **vram**
region — `amdgpu_gtt_mgr.c` has no `cgroup_register_region` at all. Docker and
systemd are identical here; the gap is below both.

Consequence worth recording: a runaway llama-server can exhaust host RAM through
GTT while its cgroup shows `memory.current` far below `memory.max`, the cgroup
OOM killer never fires, and the global OOM killer's badness scores **also**
ignore GTT — so even if it had run during the 2026-08-19 freeze it would have
picked the wrong victim.

A cgroup limit is not useless, but it must be honestly labelled: it bounds
llama-server's anonymous memory and the `--no-mmap` page-cache copy, and
**nothing of the GTT**. A guard that fires on the wrong few percent of a
process's footprint is worse than no guard, because it reads as protection.

### W5 — unblock llama.cpp upgrades ◻️
Depends on W0's D3a. Once the smoke test stops failing spuriously, re-run the
upgrade and confirm whether the newest build carries `no_alloc`/`--fit`
(W3's prerequisite). Keep `-fit off` regardless, per §2.

---

## 5. Questions — answered

**5.1 — Are GTT allocations charged to a cgroup v2 memcg? → NO.** Confirmed
independently twice. See W4 above for the evidence and consequences.

**5.2 — Is `posix_fadvise(DONTNEED)` reliable on the failure path? → YES, with
conditions.** Confirmed twice. An open fd is irrelevant — page cache belongs to
the inode's `address_space`, not to any descriptor, so v1's stated fear was
unfounded. But:
- folios locked for in-flight I/O are **trylocked and skipped**, so dropping
  while the doomed llama-server is still streaming strands most of the file.
  The abort path must **terminate → wait for exit → drop**.
- **mapped folios are skipped wholesale**, so `--no-mmap` is what makes the drop
  work at all. If anyone ever flips it back for performance, the drop silently
  becomes a no-op with no code change to blame. Record it as load-bearing.
- `generic_fadvise()` returns **0 unconditionally**, and short-circuits to 0 for
  DAX and `noop_backing_dev_info` filesystems (tmpfs/ramfs).
- Walking an 85 GB file's page cache is ~21M folios — hundreds of ms to seconds
  of CPU, contending the reader's own lookups, and `lru_add_drain_all()` fires
  work on every CPU whenever anything fails to evict. Not free at roster scale.

**→ `local_weights.py` has a live bug.** It does
`os.posix_fadvise(...); dropped += os.fstat(fd).st_size`. Since the call always
returns 0, the reported "GiB dropped" is the total size of every `.gguf`
present, whether one page was evicted or none. It is not evidence of anything,
and a W0 test asserting on it would pass on a total no-op. Replace with a
`cachestat(2)` (syscall 451, Linux 6.5+) before/after delta.

**5.3 — Does PSI rise during this livelock? → YES.** v1's fear was unfounded.
`psi_memstall_enter()` sits in `__alloc_pages_direct_reclaim()`, so every
microsecond TTM spends in direct reclaim is charged; `gfp_retry_mayfail`
suppresses the OOM killer but is orthogonal to stall accounting. The refault
path in `filemap.c` measures exactly the evict-and-immediately-re-fault
treadmill. Caveats for implementation:
- **Attribution is broken because of 5.1** — PSI names the cgroup *suffering*,
  not the one *causing*; the GTT hog is invisible and the pressure may surface
  in Postgres. Never pick the victim from cgroup PSI. Use
  `/proc/<pid>/fdinfo/<drm-fd>` (`drm-memory-gtt`, `drm-resident-gtt`,
  `amd-requested-gtt`) — per-process GTT, which the residency table currently
  lacks entirely.
- Sub-second triggers need `CAP_SYS_RESOURCE` (not in Docker's default set), and
  per-cgroup `memory.pressure` needs writable cgroups (Docker mounts them ro).
  **Run the responder on the host, not in a container** — it must not live in the
  cgroup it is judging.
- May require `psi=1` at boot, i.e. the same grub dependency W1d has.
- Confirm with `pgscan`/`pgsteal` and `workingset_refault_file` deltas; PSI is a
  ratio, not a cause.

**5.4 — Is the RADV heap split?** `radv_enable_unified_heap_on_apu=true` belongs
in `drirc`; **there is none anywhere in this repo**. If the heap is split
(~80 GB device-local + ~40 GB host-visible) every number we compute is wrong.
Still unchecked, still cheap.

**5.5 — Was the 2026-08-19 freeze during load or during serving?** RADV has a
known slow-load path for >64 GB allocations that looks exactly like a hang.
Does not change D0–D4; changes how that incident is read.

**5.6 — NEW, and it gates D1 and all of W1.** Does the GTT **resource manager**
size (set at probe from `ttm_tt_pages_limit()`) refuse allocations independently
of the soft loop? If yes, a boot-time `ttm.pages_limit` is a real hard cap and a
runtime write is the useless one — the reverse of what D1 currently says. See
the caveat box in D1. **Under research.**

## 6. What "done" looks like

- `qwen3.5-4b` loads (it does not today).
- An aborted load leaves `Cached` unchanged, verified by measurement not by code
  reading.
- The reserve is enforced by the strongest mechanism available (W1a..W1d),
  and the Ops host-settings card says which one is actually in force.
- A llama.cpp upgrade passes its smoke test on merit.
- The catalog is cross-checked against a measured breakdown, and divergence
  raises rather than sits.
- No band-aid: every constant in `gpu_guard.py` is derived from a measurement or
  deleted.
