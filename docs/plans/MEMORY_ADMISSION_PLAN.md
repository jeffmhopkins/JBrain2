# Memory Admission on Unified Memory — Design Spec

> **Status:** Draft · **Last verified:** 2026-08-19 · **Waves:** W0◻️ W1◻️ W2◻️ W3◻️ W4◻️ W5◻️

> Reconciled with the root `CLAUDE.md` non-negotiables — no LLM or storage
> surface changes (rules 1–2); the one new table is RLS-scoped with an isolation
> test (rule 3); tests land with the code (rule 5); **rule 10 is load-bearing
> here**, because every step below is either PWA/debug-operable or is explicitly
> named as a host step with a date and an owner.

The box froze for seven hours on 2026-08-19 and needed a power cycle the owner
could not perform remotely. Three separate fixes have shipped since, each
correct and each treating a symptom. This plan stops that: it names the actual
mechanism, states what we are **not** going to do, and sequences the work so the
guard stops being a guess.

---

## 1. What is actually wrong

Four defects, in descending order of how much damage they can still do. Every
one is measured or read from source, not inferred.

### D1 — `ttm.pages_limit` is inert, so there is no back-pressure at all

`ttm.pages_limit` is **not a hard cap**. From `ttm_tt_populate()`:

```c
atomic_long_add(ttm->num_pages, &ttm_pages_allocated);
while (atomic_long_read(&ttm_pages_allocated) > ttm_pages_limit || ...) {
        ret = ttm_global_swapout(ctx, GFP_KERNEL);
```

`ttm_pages_allocated` counts pages TTM has actually obtained, which by
construction cannot exceed physical RAM. So when the limit is at or above
`MemTotal`, the `while` condition is unreachable, `ttm_global_swapout()` never
runs, and **TTM applies zero back-pressure at allocation time**. All pressure
then falls to generic MM reclaim, which cannot reclaim GTT pages (not on the
LRU), leaving only page cache — which is re-faulted immediately. That is the
livelock, mechanically.

Our box: `gtt_total` **124.0 GiB** against `MemTotal` **121.2 GiB**. Inert.

We did not misconfigure this. `ttm.pages_limit=32505856` is the most-copied
line in the Strix Halo community (kyuz0's reference cmdline), and it is
**correct for that audience**: a benchmarking distribution wants the largest
possible model, the kernel default is `MemTotal/2` (~60 GiB) which would refuse
a 120B, so the guidance is "raise it". Nobody notices the inertness because a
freeze on a desk is a power button, not an outage. We are an unattended server
administered with no terminal. Same setting, opposite conclusion.

Also inherited from that line and worth correcting: `amdgpu.gttsize=126976` is
**deprecated** (the kernel prints a warning pointing at `ttm.pages_limit`), and
`amd_iommu=off` is a contested benchmark tweak — at least one report has IOMMU
*on* helping dual-model loading.

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

And the thing worth remembering when we are tempted again: `gpu_guard.py`
already computes `min(gtt_total - gtt_used, host_free - reserve)` and documents
why the alternative double-reserves. **That is precisely the bug in LM Studio
#1471, Ollama #16719, and llama.cpp #22592.** We are ahead of all three on the
one axis that matters here. Adopting any of them imports a bug we already fixed.

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
Adopt llama.cpp's `no_alloc` dry-run as an **install-time cross-check**, not a
replacement (see §2 for why not a replacement). Store the measured
model/context/compute breakdown per model, alert on divergence from the catalog.
`llama_memory_breakdown_print`'s `unaccounted` column is `fixed_overhead`,
measured. Cache it the way vLLM's `startup_plan.py` does: fingerprint the
config, store `{projected, free_memory_baseline}`, and **refuse to reuse a
cached number when current free memory is below the recorded baseline** — that
gate is what makes a stale cache safe.
- Prerequisite: confirm our pinned image has `no_alloc` (it may not; see W5).

### W4 — kernel-enforced per-model budget ◻️ **[gated]**
Per-model cgroup `MemoryMax=`. **Do not start until the open question in §5.1 is
answered** — if GTT pages are not charged to the allocating process's memcg,
this wave is void and must be deleted rather than attempted.

### W5 — unblock llama.cpp upgrades ◻️
Depends on W0's D3a. Once the smoke test stops failing spuriously, re-run the
upgrade and confirm whether the newest build carries `no_alloc`/`--fit`
(W3's prerequisite). Keep `-fit off` regardless, per §2.

---

## 5. Open questions

**5.1 — Are GTT allocations charged to a cgroup v2 memcg?** *(gates W4
entirely.)* TTM allocates `GFP_HIGHUSER` through the driver. If those pages are
not charged to the calling process's memcg, `MemoryMax=` gives no protection
against a model pinning GTT. DRM cgroup support (`drm.memory.stat`) has been an
in-flight patch series for years. **Under research.**

**5.2 — Is `posix_fadvise(DONTNEED)` reliable on the failure path?** *(gates
W0's D2.)* It is best-effort and silently no-ops on pages another process still
references — precisely the situation on an aborted load where llama-server may
still hold the file. A fix that looks right and does nothing is the exact shape
of the `timeout`-on-a-shell-function bug from earlier today. **Under research.**

**5.3 — Does PSI actually rise during this livelock?** During the event, reclaim
keeps *nominally succeeding* — it evicts page cache and the pages come straight
back. If `full` only rises when reclaim genuinely stalls, PSI could read
deceptively low during the exact event we want it to catch. **Under research.**

**5.4 — Is the RADV heap split?** RADV can present unified memory as ~80 GB
device-local + ~40 GB host-visible instead of one pool;
`radv_enable_unified_heap_on_apu=true` in `drirc` fixes it. **There is no
`drirc` anywhere in this repo.** If our heap is split, every number we compute
is subtly wrong. Cheap to check, not yet checked.

**5.5 — Was the 2026-08-19 freeze during load or during serving?** RADV has a
known slow-load path for >64 GB allocations that looks exactly like a hang and
is not one. Does not change D1–D4, but changes how we read that incident.

---

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
