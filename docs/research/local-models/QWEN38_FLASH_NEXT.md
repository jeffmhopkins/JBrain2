# Qwen3.8-Flash-Next on Strix Halo — quant ladder, token speed, and whether it replaces gpt-oss-120b

> **Status:** Research · **Last verified:** 2026-09-15

> **What this is.** A decision dossier on one question the owner asked: *can
> `Qwen3.8-Flash-Next` become the box's default high-tier model in place of
> `gpt-oss-120b`?* It covers the quant ladder, the measured token speed on this exact
> silicon (Ryzen AI Max+ 395 / gfx1151 / 128 GB unified), the memory budget against
> this repo's own admission constants, and the serving-stack blockers.
>
> **Every claim is labelled.** `[REPO]` = read in this tree, with `path:line`.
> `[WEB]` = published elsewhere, with a URL. `[DERIVED]` = arithmetic over the two,
> shown so it can be checked. Line numbers rot; re-grep before citing.

---

## 0. Verdict first

**Do not change the default yet. Keep `gpt-oss-120b` as the high tier.**

The quality case is genuinely strong — this is not a marginal model — but on *this*
box the speed case inverts it, and the thing that fixes the speed case is not in
mainline llama.cpp.

Five reasons, each a number below:

1. **Without MTP it is roughly half the speed of what it would replace.** Measured on
   gfx1151: **15.3–21.9 tok/s** decode across 185 → 26k context depths `[WEB]`, against
   `gpt-oss-120b`'s **~31 tok/s** on this box `[REPO]`
   `backend/src/jbrain/llm/local_catalog.py:585`. That is a **0.49–0.71x** speed
   change `[DERIVED]` for the owner's interactive persona.
2. **MTP — the only thing that closes the gap — requires an out-of-tree fork.** Base
   `qwen4exp` support merged to mainline on 2026-08-27 (PR #27742), but **MTP is PR
   #28243, still a DRAFT** as of 2026-09-10, with the maintainer asking for a rework
   (reuse `ctx_other`, split the CUDA-graph changes out) `[WEB]`. Every good Strix
   Halo number published so far comes from a fork (`strix-halo-flash-next`,
   `EngramHalo.cpp`), not from a build this repo's gateway would ever produce.
3. **The good numbers are ROCm/HIP; this box serves Vulkan by design.** The runbook's
   Phase 3 picks Vulkan/RADV precisely because it "needs only `/dev/dri`; no ROCm
   setup" `[REPO]` `docs/runbooks/STRIX_HALO_SETUP.md`. PR #28243 reports **7.6%
   degradation** on Vulkan `[WEB]`. There is exactly one promising Vulkan MTP
   datapoint, single-source and unreplicated (§3).
4. **MTP's speedup is not monotonic — it goes negative at the depths this box works
   at.** Same rig, same build: +MTP is **38.5 tok/s at 3.4k** but **15.0 tok/s at
   26k**, *below* that rig's own 16.8 baseline `[WEB]`. "The 32K regression is real"
   is the author's own words.
5. **It cannot co-reside.** At the honest resident figure it leaves **17–22 GB**
   headroom on a 128 GB box `[WEB]`, and the repo's ledger would book its *disk* size
   (93.7 GB) and refuse every co-resident outright (§4). Today `gpt-oss-120b` (59 GB)
   shares the box with a vision model.

**What would flip this:** MTP merged to mainline `[WEB]` PR #28243, *plus* a Vulkan
run on this box reproducing ≥31 tok/s at ≥13k depth. §7 is the re-evaluation trigger.

**Worth doing now:** nothing in the serving path. §6 records the one cheap,
genuinely attractive option — an opt-in, non-`recommended` catalog entry at
`UD-IQ3_XXS` — and why even that waits.

---

## 1. What the model actually is

| fact | value | source |
|---|---|---|
| Released | 2026-08-26 | `[WEB]` |
| Total params | 125B, **6B activated** | `[WEB]` |
| Plus | **51B n-gram embedding**, 4B MTP head | `[WEB]` |
| Layers / hidden | 48 / 2560 | `[WEB]` |
| Attention | Hybrid **Gated DeltaNet** (48 V heads, 16 QK, dim 128) + **Qwen Sparse Attention** (24 Q, 2 KV, dim 256, **512-block / 2048-token budget**) | `[WEB]` |
| MoE | 512 experts, 10 routed + 1 shared | `[WEB]` |
| N-gram embedding | 20,000,000 bigrams/trigrams, injected at layer 2 | `[WEB]` |
| Context | **262,144** native, extensible to 1M | `[WEB]` |
| Modality | text + **image + video** in | `[WEB]` |
| License | `qwen-community-1.0` | `[WEB]` |
| llama.cpp arch id | **`qwen4exp`**, merged 2026-08-27 (PR #27742) | `[WEB]` |

Two architectural facts drive everything below.

**The 51B n-gram table is why no quant is small.** It is a lookup table, not compute —
20M bigram/trigram rows — and it dominates the file at every bit-width. This is why the
ladder in §2 bottoms out at **72.5 GB even at 1-bit**, above `gpt-oss-120b`'s 59 GB
MXFP4 `[REPO]` `local_catalog.py:584`. There is no quant of this model that is smaller
than the model it would replace.

**QSA's fixed block budget is why long context is nearly free.** With a 512-block /
2048-token attention budget the KV cache stops growing with the window: measured **~3.5
GB at max context** `[WEB]`, and one report describes extended contexts as "essentially
free compared to 32K." Against `gpt-oss-120b`'s `kv_gb_per_128k=5.01` `[REPO]`
`local_catalog.py:609` — which is itself the flattered figure, and the only
`kv_full_history` entry in the catalog — this is a real structural win, and it is the
single most attractive property of the model for this box.

**Sampling** `[WEB]`, and it is the same hybrid split the catalog already models for the
Qwen3.8 27B family (`local_catalog.py:663-666`):

| mode | temp | top_p | top_k | min_p | presence_penalty |
|---|---|---|---|---|---|
| thinking | 1.0 | 0.95 | 20 | 0.0 | 0.0 |
| instruct (non-thinking) | 0.7 | 0.80 | 20 | 0.0 | 1.5 |

Thinking is the `enable_thinking` chat-template toggle, with a `reasoning_effort`
kwarg (`low`/`medium`/`xhigh`) `[WEB]` — so it maps cleanly onto the catalog's existing
`hybrid_thinking` + `reasoning_format="deepseek"` fields. That part would be easy.

---

## 2. The quant ladder

Unsloth's GGUF repo `[WEB]`. Sizes as published; shard counts not stated.

| quant | size (GB) | verdict on this box |
|---|---|---|
| UD-IQ1_S | 72.5 | 1-bit; quality unproven for this family |
| UD-IQ1_M | 74.5 | 1-bit; unsloth claims 80% top-1 |
| UD-Q2_K_XL | 78.9 | 2-bit |
| **UD-IQ3_XXS** | **82** | **the only quant with real slack (§4)** |
| UD-Q3_K_XL | 90 | ~7 GB slack — too tight |
| **UD-IQ4_XS** | **93.7** | the community default; ~1.5 GB slack `[DERIVED]` |
| UD-Q4_K_XL | 111 | over ceiling |
| UD-Q5_K_XL | 158 | over |
| UD-Q6_K_XL | 169 | over |
| Q8_0 | 192 | over |
| BF16 | 354 | over |

MTP sidecars, which are separate downloads: `MTP Q4_K_M` **1.91 / 2.79 GB**, `MTP Q8_0`
**2.79 GB** `[WEB]`; EasiiX publishes a Strix-Halo-targeted `mtp-…-Q8_0.gguf` at
**4.1 GB**, noting Q8_0 measured *faster* than BF16 ("half the draft reads,
quant-matched errors → higher acceptance") `[WEB]`.

Unsloth's own RAM guidance `[WEB]`: 1-bit **75 GB**, 4-bit **96–114 GB**, 8-bit 200 GB,
BF16 355 GB, plus **1–2 GB** for MTP. Note that their 4-bit floor (96 GB) is already
above this box's admissible ceiling (§4).

**A third-party quant is worth knowing about.** AtomicChat's `AD-4.27bpw` is 92.9 GB
across 33 shards but reports only **54.5 GB in fast memory** — active weights 55–60 GB
in GTT, the n-gram table up to **38.4 GB mmap-paged from SSD**, KV ~3.5 GB `[WEB]`.
Quantization error is small: **1.026 PPL ratio** against the reference `[WEB]`. That
mmap split is the most interesting number in this dossier and §4 takes it seriously.

---

## 3. Token speed, measured on gfx1151

Four independent Strix Halo reports. **All four are ROCm/HIP or fork builds except
where marked.** `gpt-oss-120b` on this box is **~31 tok/s** `[REPO]`
`local_catalog.py:585`.

**A. drluoto** — UD-IQ4_XS, ROCm/HIP, branch `strix-halo-flash-next`, flags
`--spec-type draft-mtp,ngram-mod` `[WEB]`:

| depth | no spec | +MTP (file rewrite) | +MTP (new code) |
|---|---|---|---|
| 8k | 16.8 | **47.1** | 31.7 |
| 24k | ~15 | 28.6 | 25.4 |

**B. EngramHalo.cpp** — AD-4.27bpw, ROCm/HIP 7.14, fork branch
`strix-halo-qwen4exp` `[WEB]`. This is the most complete depth sweep published:

| depth | baseline | EngramHalo | +MTP |
|---|---|---|---|
| ~185 | 21.9 | 20.9 | 29.3 |
| ~1k | 20.4 | 21.2 | 28.2 |
| ~3.4k | 19.8 | 20.5 | **38.5** |
| ~6.6k | 19.0 | 19.4 | 23.5 |
| ~13k | 17.6 | 18.4 | 31.7 |
| ~26k | 15.5 | 16.8 | **15.0** |

Prefill 291–450 tok/s. The +MTP column is not monotonic and **ends below its own
baseline** — the author calls the 32K regression real, and notes MTP's "overhead mostly
cancels out" on prose while paying off on code and structured text.

**C. KYmidnight** — ROCmFP4-FAST (~90 GiB, 5 shards) + 2.4 GiB MTP head, **Vulkan**
`[WEB]`. The only Vulkan datapoint: decode **31.9 / 29.8 / 46.9** tok/s at small /
medium / ~24k depth, prefill 56 / 55 / 192, draft acceptance 0.86–1.00 with mean draft
length ~4. Encouraging, unreplicated, and the depth-ordering is odd enough (fastest at
the deepest point, contradicting A and B) to want a second run before it is trusted.

**D. olliehm** — Windows 11 / WDDM, UD-IQ4_XS + MTP Q8_0 sidecar `[WEB]`: **38 tok/s at
262k context**, 85–100% draft acceptance, **74 GB measured in a 96 GB allocation**.
Different OS and memory manager, so it transfers poorly, but it is the one report at
full native context.

### What this adds up to

- **Baseline (no MTP), the only configuration mainline llama.cpp can serve today:
  15.3–21.9 tok/s** — i.e. **0.49–0.71x** of `gpt-oss-120b` `[DERIVED]`.
- **With MTP on a fork: 15.0–47.1 tok/s**, workload- and depth-dependent, best on
  code/structured output, worst on prose and at ≥26k depth.
- Prefill is healthy (291–450 tok/s) and is *not* the problem.

The dossier's own warning, from the llama.cpp discussion `[WEB]`: earlier community MTP
ports "produced genuine-looking tok/s while emitting multilingual noise above ~1k prompt
tokens," and "same-build noise is huge — don't trust single-run posts." Any spike here
must validate output, not just throughput.

---

## 4. The memory budget, against this repo's own constants

This box's ceiling `[REPO]`:

- `scripts/strix-halo-host-setup.sh:76` — `RESERVE_GIB=16`; `ttm.pages_limit` is
  MemTotal less 16 GiB. At MemTotal ~125 GB that is a **GTT ceiling ≈ 109 GiB**.
- `backend/src/jbrain/llm/gpu_guard.py:100` — `MIN_FREE_GTT_GB = 6.0`, a hard floor
  held even when a load's own prediction says it fits (the failure mode is a reclaim
  livelock, not a clean OOM).
- **Admissible ≈ 103 GiB** `[DERIVED]`.

Per-load overheads the catalog already charges `[REPO]`: `RUNTIME_OVERHEAD_GB = 0.55`
(`local_catalog.py:78`), `MTP_OVERHEAD_GB = 1.0` (`:88`), `CACHE_RAM_GB = 0.0` (`:118`,
since the gateway serves `-cram 0`).

**Naive budget** — what the ledger would actually compute, booking `size_gb` as
resident, with KV at the measured ~3.5 GB and an MTP sidecar `[DERIVED]`:

| quant | weights | KV | runtime | MTP | sidecar | total | slack under 103 |
|---|---|---|---|---|---|---|---|
| UD-IQ3_XXS | 82.0 | 3.5 | 0.55 | 1.0 | 2.79 | **89.8** | 13.2 |
| UD-Q3_K_XL | 90.0 | 3.5 | 0.55 | 1.0 | 2.79 | **97.8** | 5.2 |
| UD-IQ4_XS | 93.7 | 3.5 | 0.55 | 1.0 | 2.79 | **101.5** | **1.5** |

So the community's default quant (UD-IQ4_XS) is *arithmetically* admissible and
*practically* not — 1.5 GB of slack on a box whose documented failure mode is a
freeze. **UD-IQ3_XXS is the only rung with honest room.**

**mmap-aware budget.** The AD-4.27bpw report changes the shape: if the n-gram table
pages from SSD rather than sitting in GTT, resident falls to **55–60 GB active + 3.5 KV
+ 1.55 overhead ≈ 60–65 GB** `[DERIVED]`, which is *comparable to `gpt-oss-120b`'s 59
GB* — with 38.4 GB paging off NVMe and 17–22 GB headroom `[WEB]`.

That is the optimistic reading, and it carries two caveats this repo must not gloss:

1. **The admission ledger cannot see it.** `LocalModel.size_gb` is a single number
   booked as resident (`local_catalog.py:246`). Modelling a weights/paged-table split
   means a new field and a change to the cost model — not a catalog line. Without it
   the ledger books 93.7 GB, and every co-resident is refused.
2. **Paging 38 GB off SSD per-token-ish is a latency profile nobody here has measured.**
   The published decode figures already include it, so it is priced into §3's numbers —
   but it is priced in *on that rig's NVMe*, not this one.

**Co-residency dies either way.** Today `gpt-oss-120b` (59 GB) sits beside
`qwen3-vl-30b` (32 GB Q8, or 18.3 GB Q4 — `local_catalog.py:477`, `:513`), a pair the
catalog explicitly designs for ("co-resides beside gpt-oss-120b with room"). At 82–94 GB
booked, Flash-Next is a **sole tenant**.

**The honest counter-argument, and it is a good one:** Flash-Next is *multimodal*. It
could retire `gpt-oss-120b` **and** `qwen3-vl-30b` at once — 59 + 32 = **91 GB** of
catalog weight today `[DERIVED]` versus 82–94 GB for one model that does both, at 262k
context instead of 131k, with a smaller KV term. On footprint alone that trade is
roughly neutral-to-favourable. It is the *speed* (§3) and the *fork dependency* (§5)
that sink it, not the memory.

---

## 5. Serving-stack blockers

1. **MTP is not in mainline.** PR #28243 is a draft `[WEB]`; base `qwen4exp` (PR #27742)
   is merged and serves the model *without* speculation — i.e. at §3's 15–22 tok/s. The
   gateway auto-tracks newest mainline with a smoke test and rollback
   (`settings_store.py:296-297` `[REPO]`), so base support will arrive here for free.
   MTP will not.
2. **This repo already carries exactly one llama.cpp patch, deliberately.**
   `LOCAL_LLM_PATCH_RESTORE_CHECKPOINT` `[REPO]` `settings_store.py:308` — a
   checkpoint-restore fix, default **OFF**, because it "compiles llama.cpp from source
   … a ~20-30 min rebuild." That is the cost of one narrow patch. `EngramHalo.cpp` /
   `strix-halo-qwen4exp` are *forks* with their own kernels — a different maintenance
   class, and one that would sit directly across the auto-update path.
3. **Vulkan vs ROCm.** The runbook picks Vulkan/RADV on purpose. Adopting Flash-Next at
   speed currently means adopting ROCm, which is a change to Phase 3 of the owner's
   setup — and the owner has no terminal (CLAUDE.md #10), so it must be an update-script
   path, not a host step.
4. **MTP is a serving *mode*, not a model — this repo learned that already.**
   `qwen3.8-27b-mtp` is in `RETIRED_IDS` precisely because "MTP turned out to be a
   serving MODE of the Q4 entry rather than a model" `[REPO]` `local_catalog.py:1118-1124`.
   Whatever lands here must be `extra_server_args` on one entry, not a second entry.
   The good news: the plumbing exists — `--spec-type draft-mtp` is already served for
   the qwen35 family (`local_catalog.py:687-691`) and `_is_mtp_speculative` already
   gates KV-slot behaviour (`llama_swap_config.py:202`).
5. **Unbounded thinking.** Thinking mode "can consume entire output budget without
   explicit reasoning budget caps" `[WEB]`; Artificial Analysis measures **41.18 s to
   first *answer* token** versus 9.55 s for `gpt-oss-120b` `[WEB]`. On a box decoding at
   ~20 tok/s that is the owner waiting, and it would need a `reasoning_effort` default
   pinned low before this model ever fronted the interactive persona.

---

## 6. What we would actually gain — the quality case

This is the part that makes the model worth re-checking rather than dismissing.
Artificial Analysis, Flash-Next vs `gpt-oss-120b` `[WEB]`:

| metric | Qwen3.8-Flash-Next | gpt-oss-120b |
|---|---|---|
| AA Intelligence Index | **40** | 12 |
| AutomationBench-AA | **56%** | 0% |
| SciCode | **51%** | 34% |
| Humanity's Last Exam | **38%** | 20% |
| Context | 256k | 131k |
| Output speed (cloud) | 52 tok/s | 230 tok/s |
| Time to first answer token | 41.18 s | 9.55 s |

An Intelligence Index of 40 against 12 is not a rounding difference, and
`AutomationBench-AA` at 56% versus **0%** is the agentic-competence axis this repo cares
about most — §0 of `docs/research/agent-ingest/L1-LOCAL-TOOLCALLING.md` records the
current model's streamed tool-call path as **~44% reliable** `[REPO]`. A model that is
actually competent at multi-step tool use would reopen design questions that were closed
on `gpt-oss-120b`'s evidence.

**The cheap option, when the time comes:** add one non-`recommended` catalog entry at
`UD-IQ3_XXS` — `hybrid_thinking=True`, `reasoning_format="deepseek"`, `recurrent=True`,
`supports_vision=True`, the §1 sampling split, `kv_gb_per_128k≈3.5`,
`native_context_window=262144`. Nothing routes to it by default, so the first load is
the measurement — exactly the pattern the catalog already uses for the unmeasured Q8
sibling (`local_catalog.py:684-686`). **Even this waits**, because at 82 GB it evicts
every co-resident the moment it is loaded, and until MTP is real the thing it evicts
them for is slower than what it displaced.

---

## 7. Re-evaluation trigger

Revisit when **both** hold:

1. **MTP for `qwen4exp` is merged to mainline llama.cpp** — watch PR #28243 (draft as of
   2026-09-10; maintainer asked for `ctx_other` reuse and a CUDA-graph split, so a
   rework is expected, not a quick merge).
2. **A Vulkan run reproduces ≥31 tok/s at ≥13k depth** — either published by someone
   else on gfx1151, or spiked here. KYmidnight's Vulkan numbers (§3C) are the only
   evidence today and want replication.

Then spike, in this order: `UD-IQ3_XXS` + MTP sidecar, sole tenant, **output validated
against a known-good transcript above 1k prompt tokens** (§3's silent-garbage failure
mode), measured at 1k / 13k / 32k depth, with `reasoning_effort` pinned low. Compare
against `gpt-oss-120b`'s ~31 tok/s on the same box, same day.

If it clears, the follow-on question is the interesting one and should be planned
deliberately rather than fallen into: **retire `gpt-oss-120b` and `qwen3-vl-30b`
together** for one multimodal sole tenant, which needs the mmap/paged-table split
modelled in the admission ledger (§4) and both ids added to `RETIRED_IDS` so the weights
are actually reclaimed on a box that cannot `rm` them (CLAUDE.md #10).

---

## Sources

`[WEB]`, all retrieved 2026-09-15:

- [Qwen/Qwen3.8-Flash-Next — Hugging Face](https://huggingface.co/Qwen/Qwen3.8-Flash-Next) — architecture, params, sampling, context
- [Qwen3.8-Flash-Next — qwen.ai blog](https://qwen.ai/blog?id=qwen3.8-flash-next) — release
- [unsloth/Qwen3.8-Flash-Next-GGUF](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) — quant ladder and sizes
- [Qwen3.8-Flash-Next: How to Run Locally — Unsloth docs](https://unsloth.ai/docs/models/qwen3.8-next) — RAM guidance, MTP branch, reasoning_effort
- [llama.cpp discussion #27950 — Flash-Next on Strix Halo, 17 → 47 tok/s](https://github.com/ggml-org/llama.cpp/discussions/27950) — measurements A and C, validation caveats
- [llama.cpp PR #27742 — model: add Qwen3.8-Flash-Next (qwen4exp)](https://github.com/ggml-org/llama.cpp/pull/27742) — mainline base support
- [llama.cpp PR #28243 — Qwen3.8-Flash-Next MTP](https://github.com/ggml-org/llama.cpp/pull/28243) — draft status, Vulkan degradation
- [EasiiX/Qwen3.8-Flash-Next-MTP-Strix-Halo-GGUF](https://huggingface.co/EasiiX/Qwen3.8-Flash-Next-MTP-Strix-Halo-GGUF) — MTP sidecar, flags
- [EngramHalo.cpp: Qwen 3.8 Flash Next at 38 t/s on Strix Halo](https://sleepingrobots.com/dreams/engramhalo-qwen38-flash-next-strix-halo/) — measurement B
- [Running Qwen 3.8 Flash Next on Strix Halo: 125B at 20 t/s](https://sleepingrobots.com/dreams/qwen38-flash-next-strix-halo/) — mmap/n-gram split, PPL ratio
- [Artificial Analysis — Qwen3.8-Flash-Next vs gpt-oss-120b](https://artificialanalysis.ai/models/comparisons/qwen3-8-flash-next-vs-gpt-oss-120b) — quality benchmarks
