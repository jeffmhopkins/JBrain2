# Flash-Next engine — a switchable second local-LLM stack (Qwen3.8-Flash-Next)

> **Status:** In progress · **Last verified:** 2026-10-05 · **Waves:** F1✅ F2◻️ F3a✅ F3b◻️ F4🟡 F5◻️

Run **Qwen3.8-Flash-Next** (text + image; 125B MoE with ~6B active, plus a 51B n-gram
"engram" table) on the Strix Halo box as the **only** local LLM, in its own container,
behind an owner-operated switch: **Standard** (today's `local-llm` gateway: gpt-oss-120b,
Qwen3.8-27B, …) **or** **Flash-Next** — never both. While Flash-Next is on, every local call
is remapped to it. Switching back restores the standard gateway and every per-task pick
untouched.

Why: one multimodal model replaces the gpt-oss-120b + Qwen3.8-27B pair (~87 GB of weights
before KV) in ~83 GiB with four full-context, role-pinned slots, and the community has
already done most of the gfx1151 groundwork (§2). Nothing changes for a box that never
provisions it.

Revised 2026-10-01 after two independent reviews (one against this codebase, one against
upstream source and published measurements). §10 records what they changed.

## 1. Decisions taken with the owner

| Decision | Choice |
|---|---|
| Container | New `flash-next` compose profile, own image, own llama-swap config. Never co-resident with `local-llm`. |
| Switching | PWA (Ops), no terminal. Drain → swap → smoke test → automatic rollback on failure. |
| Routing | **Remap all calls**, inside the API — not by model-name aliases at the gateway (§4c). |
| Slots | **9 role-pinned slots sharing one 524,288-cell (512k) `--kv-unified` pool**, each with a router-enforced per-slot **cap** (jerv 256k, ingest 128k, scheduled 256k, research 256k, jcode 256k, wiki/notes/intake 128k, pet 32k, small prompts 64k, browser agent 128k — §4a). The ninth, browse, was added 2026-10-05 (owner: research agents will browse a lot, so browsing gets its own prefix cache rather than sharing theirs). Caps oversubscribe the pool; a router **pool guard** frees idle slots in a fixed eviction order before the pool could overrun. Slots exist to **keep each hot prompt's prefix warm**, not for concurrency. Decided 2026-10-03 (superseding the 2026-10-01 512k/5-slot layout): the same cells as the 4×262k layout run live, with one slot per frequently-used prefix. |
| Checkpoints | **8 per slot** to start; 16 only once F2 has measured their real cost (§3). |
| Quant | Unsloth **UD-IQ4_XS** (93.7 GB on disk) + F16 vision projector (904 MB). |
| Engram (PLE) table | **Memory-mapped from disk**, pinned to CPU (`-ot per_layer_token_embd=CPU`). |
| Disk prefix cache | One primed prefix per slot role, restored into its own slot (§4b) — F4, not F3b: F3b primes only jerv's slot. Extended 2026-10-04 (owner): chat **conversations** on disk too, inside an owner-set budget (default 40 GB, Ops), toggle default on (F4c). |
| Engine | Mainline llama.cpp on Vulkan first; a custom community engine is F5, adopted only on evidence. **halogen is excluded** (closed-source server; conflicts with pin-sources-by-commit). |

## 2. Prior work, and what each piece actually measured

Most published numbers were taken under conditions different from ours; the column says
which, so none is mistaken for a prediction of this configuration.

| Source | Conditions | What it shows for us |
|---|---|---|
| llama.cpp [#27742](https://github.com/ggml-org/llama.cpp/pull/27742), merged 2026-08-27 | upstream | Mainline support: GDN hybrid, 512-expert MoE, QSA sparse attention, PLE, vision via the Qwen3-VL clip path. |
| llama.cpp [#27941](https://github.com/ggml-org/llama.cpp/pull/27941), merged 2026-09-01 | upstream | Fixes image tokens collapsing onto wrong pooled keys (M-RoPE) and stale indexer keys on copied sequences. Required for vision + multi-slot. Our current pin (2026-08-25) predates both. |
| llama.cpp [#29028](https://github.com/ggml-org/llama.cpp/issues/29028), 2026-09-17 | Vulkan/RADV, gfx1151, `-ngl 999` | qwen4exp **aborts at first decode** ("tensor read out of bounds"); works on ROCm. Now closed, fix commit unconfirmed. **F2's first check.** |
| llama.cpp [#29149](https://github.com/ggml-org/llama.cpp/issues/29149) | HIP | Livelock at load on HIP; Vulkan unaffected. Another reason to start on Vulkan. |
| [Soot/Silicon, ROCm vs Vulkan](https://www.soothill.io/blog/2026/08/27/qwen38-flash-next-rocm-vulkan-strix-halo/) | UD-Q4_K_XL, **f16 KV, 40/48 layers offloaded**, 2 slots | Vulkan 6.4× ROCm decode, far less GTT, 60-min soak clean; PLE on CPU + mmap (`--no-mmap` exhausted RAM). Full offload estimated at ~14 GiB more GTT. |
| [Soot/Silicon, vLLM](https://www.soothill.io/blog/2026/09/16/qwen38-vllm-disk-ple-strix-halo/) | same, mainline Vulkan baseline | Mainline decode **17.3 tok/s @512, 14.8 @8k, 12.2 @32k**. 2-slot aggregate 17.4 uncached (24.3 only on a prompt-cache hit). |
| [julianmb/haloq38flash](https://github.com/julianmb/haloq38flash) | Vulkan, fork, MTP | PLE streamed from SSD: ~2.5 GB working set, 262k context. Warns the official converter's hyper-connection norms were off by 1.0 — F2's perplexity check covers it. |
| [abliter8-ai repo](https://github.com/abliter8-ai/qwen-3.8-next-flash-amd-strix-halo) | EngramHalo fork, **HIP** | Vision works on gfx1151 (thinking off, ≥1024 output tokens). **Not** evidence for mainline Vulkan vision — F2 checks that. |
| [chm123 gist](https://gist.github.com/chm123/b0b3eec2b7f68e09e5855e23fef44dba) | ROCm, 1 slot, MTP | 70/74 GB GTT at 128k/256k. |
| [kyuz0/gufo](https://github.com/kyuz0/gufo) | HIP, MIT, MTP | 59 tok/s single, 157 tok/s at 8 users, OpenAI API with images. From the author of our base toolbox image. Early-stage. |
| [EngramHalo.cpp](https://github.com/Aristo94/EngramHalo.cpp) | llama.cpp fork, MIT, HIP-first | 30–40 tok/s reported on Vulkan with MTP. Multi-slot support unverified. |
| [unsloth GGUF discussions #52](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/discussions/52) | ROCm 7.x | Garbage output on gfx1151 before #27941; fixed by building from it. |

## 3a. Measured on the box (F2 sitting 2026-10-01)

UD-IQ4_XS, q8_0 KV, every layer offloaded on mainline Vulkan (pin `869034b`), engram table
`-ot …=CPU` + `--load-mode mmap`. Each row is a cold load, read as GTT used from host metrics
with nothing else resident (standard engine stopped; baseline GTT 0.06 GiB).

| Layout (slots × cells per slot) | Total cells | GTT used |
|---|---|---|
| 2 × 65,536 | 131,072 | 63.62 GiB |
| 1 × 131,072 | 131,072 | 63.76 GiB |
| 1 × 262,144 | 262,144 | 67.45 GiB |
| 2 × 262,144 | 524,288 | 74.21 GiB |
| 4 × 262,144 (sitting 1, before the override fix) | 1,048,576 | 96.9 GiB — see below |

**Fit: GTT ≈ 60.2 GiB + 7.0 GiB per 262,144 cells; slot count costs ~nothing** (2×64k equals
1×131k within noise). The per-cell cost is ~1.75× the §3 derivation (KV + QSA indexer), and the
fixed cost (weights without the engram table, compute, vision) is ~60 GiB as derived.

- The 4×262k point sits ~8.7 GiB above the fit (88.2 predicted). It was taken in sitting 1 under
  a different load path and is unexplained; it is not re-measured because at that size host
  free memory reached the gateway guard's floor (a reload was aborted at 5.7 GB free).
- Host side: anonymous memory stays ~1–2 GiB; page cache from the mmapped weights/engram ran
  25–45 GiB and is reclaimable (the engram really is paged from disk).
- Speed (sitting 1, 4×262k): load 50 s; decode 23.5 tok/s at short context; prefill 580 → 373
  tok/s over a 37,916-token prompt. First decode with full offload is clean (#29028 fixed in the pin).

Correctness and stability (sitting 2, 2026-10-02; vision 2026-10-03):

| Check | Result |
|---|---|
| WikiText-2 perplexity (100 chunks) | 3.42 ± 0.05 — no converter norm bug |
| Tool-call round-trip | ✅ well-formed `web_search` call |
| JSON-mode output against a schema | ✅ parses, correct |
| Prefix reuse, 31k-token prompt | 84 s cold → 2 s warm (22 new tokens re-processed) |
| Pet-sized prompts | first token 0.6 s warm / 1.7 s cold, 24 tok/s |
| Mainline-Vulkan vision | ✅ owner photo through the PWA, read correctly, Flash-Next serving (after F3a) |
| Soak | 30 of 60 min: 214 mixed calls, all succeeded, GTT flat at 74.23 GiB, no device-loss or engine restart. Stopped early (it overlapped the nightly window); the remainder is the last open F2 check |

**Consequence for §4a (owner decision 2026-10-01):** slots stay role-pinned but share one
`--kv-unified` pool of **512k cells** (agent 256k + ingest 64k + research/jcode 128k + 2 × 32k
for the pet and small prompts) — the same total cells as the measured 2×262k row, **74.2 GiB**
GTT, and slot count costs ~nothing. F3b re-measures the chosen pool before it ships.

**Superseded (owner decision 2026-10-03):** a **1M pool across 8 slots** instead — the same cells
as the 4×262k layout running live (78.6 GiB GTT read on 2026-10-03, partly filled; the fit
predicts ~88.2 GiB full, and sitting 1 read 96.9). The point is more warm prefixes, not longer
ones: slot count costs ~nothing, so each frequently-used prompt gets its own slot. F3b's
re-measure fills every slot to its cap to settle the true worst case before it ships.

**Resized to 512k on deploy (2026-10-03):** the 1M pool never loaded on the box. A
`--kv-unified` pool allocates all its cells at load (the 4×262k layout grew into its cells as
they filled), and with the GGUF streaming through page cache at the same time host free memory
fell to 5.3 GB, under the load guard's 6 GB floor, so the guard aborted every load. 512k cells is
the 2×262k size measured loading cleanly at 74.2 GiB; the 8 slots and their caps are unchanged —
the caps now oversubscribe the pool further, which the pool guard is built for.

**Why the 1M load failed, and the fix (2026-10-03).** Per llama.cpp `869034b`, `--load-mode
mmap` MAP_POPULATEs/WILLNEEDs the GPU-weight shards (~59.5 GiB of page cache) before uploading
them to Vulkan, and our gateway never dropped any Flash-Next page cache (it assumed all of it was
engram); the unified 8-slot compute reserve is also ~9–10 GiB larger than non-unified. Measured
with `--load-mode none` at 512k (operator extra-arg first, now the catalog flag):

| | mmap (steady) | none (during load) | none (steady) |
|---|---|---|---|
| Load time | 41 s | — | 27 s |
| Host free | 16.5 GB | min ~11.5 GB | 25.8 GB |
| Page cache | 25.9 GiB | peak 52 GiB (the buffered read) | ~17.8 GiB |
| GTT | 82.3 GiB | 63.5 GiB early | 82.9 GiB |

In `none` the lazy engram tensor is still mmapped (prefetch 0) and served fine; the GPU tensors
go through a buffered read into pinned staging, which leaves unmapped, droppable page cache.
(`dio` falls back to buffered on Vulkan's pinned staging, so it buys nothing.) So the gateway now
drops Flash-Next's cache **by byte range**: each shard's GGUF header gives the tensor offsets,
the catalog's `-ot per_layer_token_embd=CPU` rule names the ranges to keep, and every other range
is `POSIX_FADV_DONTNEED`ed — in the in-load sweep, after the load, from the debug drop route, and
by the load guard proactively when host free memory is within 4 GB of its 6 GB floor (once per
10 s, bounded to 2 s, then host and GTT re-read; any sample under the floor still aborts). The
cache is never counted as free (the 2026-08-19 livelock was ~39 GiB of clean cache that
`MemAvailable` did). The pool size is selectable without a release — 524,288 (default) or
1,048,576, saved as the model's context-window override (§4a) — so the 1M re-measure is a debug
console call, not a deploy.

## 3. Memory budget (derived — F2 replaces it with a measurement)

Read from the UD-IQ4_XS GGUF headers and llama.cpp master: 48 layers, 12 full-attention
(2 KV heads × 256), 36 Gated DeltaNet; the QSA indexer caches one 128-wide key head per
token as raw + pooled (`indexer_kpool_row = 2`) at the KV type, for every token —
compression does not shrink the cache.

| Item | GiB |
|---|---|
| Weights without the PLE table | 60.4 |
| Attention KV, q8_0, 4 slots × 262,144 (12 × 2 × 256 × 2 × 1.0625 B/token) | 12.75 |
| QSA indexer cache, 4 slots (12 × 256 × 1.0625 B/token) | 3.2 |
| Recurrent state (GDN + conv + PLE conv row), 4 slots | 0.44 |
| Context checkpoints, 8 per slot × 4 × ~0.11 (recurrent state only) | 3.5 |
| Compute buffers (unverified for QSA at 262k on Vulkan) | ~1.5 |
| Vision projector (F16) + CLIP workspace (flash attention on) | 1.4 |
| **Total resident** | **~83** |
| PLE table, IQ4_NL, file-backed (see below) | 26.8 on disk |

**Margin is thin and the history says derivations run light.** The catalog's Qwen3.8 entries
measured 0.28 GiB per checkpoint against 0.15 derived, and KV 1.53 GiB per 128k above the
derived cache (`local_catalog.py`, `_QWEN38_KV_GB_PER_128K`). So F2's exit gate is set on
the **measurement**, and the levers are settings, not rebuilds: 16 checkpoints per slot
(+3.5), 131k per slot (`-c 524288`, −8). A unified pool would save the same but lets one
long conversation evict the other slots' prefixes — which the router-enforced reservations of
§4a prevent, so the pool is what was chosen after F2 measured that slots cost ~nothing (§3a).

**The engram page cache is not free in our accounting today.** `host_metrics` counts page
cache as used, so admission would charge the PLE working set; and the load path drops the
weights' page cache during and after load (`local_weights.drop_weights_page_cache`, called
from `local_gateway`), which was built for `--no-mmap` and would evict the engram working set
here. F1 makes both engine-aware: `footprint_gb` subtracts a catalog **file-backed** figure
from the measured `disk_gb`, and the page-cache drop skips mmapped tensors — first by skipping
the whole model, since 2026-10-03 by byte range (`local_weights
.drop_weights_page_cache_except_mapped`: the GGUF headers locate the `-ot …=CPU` tensors, and
every other range is dropped; §3a).

## 4. Shape

- **Image:** `deploy/Dockerfile.flash-next` — same kyuz0 Vulkan toolbox base as
  `Dockerfile.local-llm`, llama.cpp pinned to a commit at or after #27941 **and** past
  whatever closed #29028, llama-swap (pinned by commit, as `test_llama_swap_pin` requires)
  in front of the one model. Keeping llama-swap keeps the `local_gateway.py` contract
  (`/running`, `/api/models/unload`, `/upstream/…/health`) that `residency`, `warm_keeper`
  and the ledger read. The checkpoint-sidecar patch is a build arg, on since F4.
- **Own config:** Flash-Next renders its own `llama-swap.flash-next.yaml`. Every reader of
  `llama-swap.yaml` (`launch_line`, `served_shape_from_config`, `kv_prefix._resolve`, the
  smoketest) resolves the file for the **active engine**. The standard `render`, the
  providers list, `jcode_models` and residency filter catalog entries by a new
  **`engine`** field, so the standard gateway never sees Flash-Next and vice versa.
- **Network identity:** the service also carries the network alias `local-llm`. Safe only
  because both are never up at once (the API opens a fresh client per call, so there is no
  stale connection or DNS pinning); §4d is what guarantees it.
- **Serving flags:** `-ngl 999` (explicit — #29028 is the full-offload crash),
  `--load-mode none` (with the global `--no-mmap` **removed** from the base command for
  this engine; `mmap` until 2026-10-03, see §3a — `none` reads the GPU weights buffered and
  still maps the lazy engram tensor), `-ot per_layer_token_embd=CPU` (the 26.8 GiB tensor exceeds Vulkan's
  4 GiB binding limit; GPU placement aborted for Soot/Silicon), `--lazy-mode on`,
  `-np 9 --kv-unified -c 524288 --slot-save-path …` (one shared pool; no single sequence may exceed
  `n_ctx_train` = 262,144, which the agent's reservation equals), `-ctk q8_0 -ctv q8_0`, `-fa 1`, `-cram 0`,
  `--ctx-checkpoints 8 --checkpoint-min-step 1024`, `--jinja`, the F16 mmproj with the
  existing `--image-min-tokens` floor.
- **MTP: not adopted in v1, pending measurement.** Upstream now has a qwen4exp MTP draft
  context; our one-slot rule for speculation is our own (`llama_swap_config.py`), and the
  evidence on multi-slot MTP conflicts (#27836 reported cross-slot contamination and a net
  loss on Vulkan; a fork reported 40→47 tok/s). A later wave can measure it.
- **Slots:** a catalog `kv_pool` (9 slots since 2026-10-05, 512k cells) with `default_slots` equal to its slot count, threaded through `render`, `footprint_gb`,
  `residency._slots` and the served shape. The settings cap `PARALLEL_SLOTS_MAX = 2`
  (`api/llm_settings.py`) becomes per-model.

### 4a. Slots are prefix caches, pinned by role

On a hybrid model a prefix is reused only if a context checkpoint covers the divergence
point; otherwise the whole prompt is re-prefilled (the 27B paid 232 s per turn before
checkpoints were raised). Today separate models give separate caches — the persona lives
on gpt-oss, ingest on the 27B. With one model, **slots take over that job**.

Nothing pins a request to a slot today (`llama_swap_config.py`, the `-np` comment):
llama-server picks a slot sharing ≥10% of the prefix, else the least-recently-used one —
usually the idle slot holding the primed persona. F3b adds **slot affinity**: an `id_slot`
parameter through the provider protocol and `openai_compat` (rule 1), chosen by **task
name** (agent turns pass `SYSTEM_STRENGTH`, so strength cannot carry the class). A busy
pinned slot queues its own traffic; llama-server defers the task until the slot frees.

Because the pool is shared, each role carries a **cap** (prompt + `max_tokens`): the router
clamps a call's output or refuses it (`SlotCapError`, shown as a context overflow) when it would
exceed its slot's cap. Caps are per slot, not reservations — together they add up to more than
the pool, because most slots hold small prompts most of the time.

What keeps the pool from overrunning is the router's **pool guard**. Verified in the pinned
llama.cpp source (2026-10-03): idle slots keep their cells (last prompt + output) until reused,
erased or purged; when the pool fills, llama-server purges idle slots one at a time **in its
own order**, and if that is not enough it fails **every** busy request with a 500 and clears
their KV. So before a pinned call the guard projects occupancy from `GET /slots` (idle:
`n_prompt_tokens`; busy: plus `n_remain`) and, if the call would overrun the pool, erases idle
slots (`POST /slots/{id}?action=erase`, which needs `--slot-save-path` even though it writes
nothing) in **our** eviction order — small prompts first, jerv's persona last — and waits,
bounded, when only busy slots hold the cells (two minutes for background work, ~15 s for the
owner's chat, which then ends as "the model's memory is busy"). Each erase is preceded by a
fresh `/slots` read that must still show the slot idle, so jerv's slot is only ever erased off a
read taken just before; an erase that still reaches a busy slot is deferred by the server and
may land after that slot's turn. An `id_slot` beyond `-np` wraps silently, so the router checks
the live slot count and sends unpinned (still capped) on a mismatch.

| Slot | Workload | Cap | Evicted |
|---|---|---|---|
| 0 | jerv — chat and omnibox turns, the warm prime | 256k (262,144) | last |
| 1 | Ingest + analysis (`entity.disambiguate`, `fact.adjudicate`, OCR, captions, EMR) | 128k (131,072) | 8th |
| 2 | Scheduled tasks, plan continuations, jmolt night, daily briefing | 256k (262,144) | 7th |
| 3 | Research and sub-agents | 256k (262,144) | 5th |
| 4 | jcode (the proxy pins it) | 256k (262,144) | 6th |
| 5 | Wiki, note conversations, guided intake, video summaries, unknown tasks | 128k (131,072) | 4th |
| 6 | jpanel kid pet (`pet.*`); overflows to slot 7 when busy | 32k (32,768) | 3rd |
| 7 | Small prompts: titles, `triage.classify`, one-shot vision reads, probes | 64k (65,536) | first |
| 8 | The browser agent (`browse.step`, one run at a time) | 128k (131,072) | 2nd |
| | **Pool** | **512k (524,288)** by default; 1M (1,048,576) selectable | `--kv-unified -c 524288` |

**The ninth slot (browse, 2026-10-05).** Owner decision: research agents will search and
browse a lot, so the browse sub-agent gets its own slot rather than sharing theirs — shared, a
research turn between two browse steps evicts the run's cache and the next step re-prefills
it whole. Appended as slot 8 so slots 0-7 keep their ids and their relative eviction order. Freed second (after small prompts): its prefix is worth something only
while a run is going. **Memory: nothing up front.** A slot reserves no cells — the pool is
`-c 524288` whatever `-np` says, and caps are router-side limits, not allocations; F2 measured
slot count moving GTT by noise. One more slot adds its recurrent state (~0.11 GiB, derived,
inside that noise) and up to 8 more context checkpoints (8 × 0.11 = ~0.9 GiB, host-only, made
lazily as the slot fills), which the footprint books: ~82.1 GiB for eviction and the meter, up
from ~81.2; the load is still admitted on the device figure (~74.2 GiB). Rollout needs no
terminal: Ops → Update (and every model load) re-stamps the config with `-np 9`; llama-swap's
config watch stops the running server and the next load serves nine slots. Until then the
live `/slots` count (8) does not match the pool (9), so the router sends every call unpinned
(still capped) rather than wrap an `id_slot` onto another role's slot — one cold prefix per
role, then normal. **The F4 disk cache starts over:** `-np` is part of the launch line that the
prefix fingerprint and the restore gate's key hash (deliberately — a different slot count is a
different server), so every saved role prefix and conversation file is orphaned (they age out
of the byte budget) and the gate is back to `awaiting_probe`. Role prefixes re-save on the next
prime; nothing is restored until `POST /llm/slot-probe` passes again against the new launch line
(its default pair is now slots 7 and 8 — overwriting the browse slot is harmless outside a run).

`agent.turn` is shared by the chat and every background agent, so the task name alone cannot
pick the slot: background callers name their role (`slot_role`), and an unnamed `agent.turn`
is jerv's. Every request to a pool model is pinned — an unpinned one lands on the
least-recently-used idle slot and evicts whichever prefix lives there — with two exceptions:
a live slot count that does not match the pool (a pre-pool config) sends unpinned, and so does an
unreadable `/slots` unless that model's layout matched within the last minute, in which case the
call is pinned and only the eviction projection is skipped.

### 4b. Disk prefix cache, one prefix per slot role

F4's design, not F3b's: F3b primes only jerv's slot (see its wave entry).

Today `jbrain.llm.kv_prefix` saves ONE prefix — the interactive model's persona + tools
(~29k tokens) — after the warm keeper primes it, and restores it in ~100 ms page-cache-warm
instead of a cold prefill of ~60–100 s. Its fingerprint (launch line + system text + tool
schema + reasoning effort) names the file; an owner-set byte budget evicts by least-recent
use.

On Flash-Next a 29k-token prefix file is ~0.55 GiB (attention KV + indexer cache + recurrent
state), a quarter of gpt-oss's ~2 GiB — so **one primed prefix per slot role** is
affordable: jerv's persona (0), the ingest/analysis system prompt (1), the scheduled-task
prefix (2, which deliberately matches jerv's), the research base prompt (3) and the pet's
persona prompt (6). jcode (4) brings its own prompt, and the workshop (5) and small-prompt (7)
slots have no stable prefix worth priming. Each is primed once, saved, and restored **into its own slot**
when lost (restart, engine switch, an overflow request). The launch line is in the
fingerprint, so switching back finds gpt-oss's file still in the budget.

What upstream gives and takes: `llama_state_seq_save_file` serializes attention KV,
recurrent state (including the PLE conv row) **and** the QSA indexer cache — so nothing the
model needs is left out. But restore clears the slot's checkpoints, after which a hybrid
re-prefills from zero even on a perfect match (confirmed in Discussion #27950). **The
checkpoint-sidecar patch (`deploy/patches/0001`) is therefore mandatory** for F4.

What the store must change (beyond "a catalog flag"):
- eligibility: a recurrent non-MTP model qualifies only via `kv_slot_restorable`, which
  bypasses the `_patch_active` gate — Flash-Next must require the patch like the MTP
  hybrids do, and `--slot-save-path` follows the same rule;
- per-**role** state: `_restored_unused` and `_last_identity` are keyed per served model,
  so `identity_drift` would fire on every role switch;
- restore targets a slot id, and the guard `_prefix_sized(any slot) and not _empty_idle`
  (which always skips once four slots are primed) becomes per-slot;
- each role carries its own reasoning effort into the fingerprint.

### 4c. The remap lives in the API

Model-name aliases at llama-swap were considered and rejected: `/running` reports the real
model, and residency sizes by `get_by_served`, so a request naming `gpt-oss-120b` would
evict Flash-Next — the only model loaded — and llama-swap would reload it. Instead, while
the engine is `flash-next`, every API entry point that turns a spec or served name into a
load resolves to `qwen3.8-flash-next`, with Flash-Next's own sampling and
`reasoning_effort` mapping:
- `_resolve_live` (including per-call `spec_override`);
- `primary_local_served_model` / `_followed_primary_model` (warm keeper) and
  `warm_reasoning_effort`;
- `context_window_for_spec` and `providers.supports_vision_for_spec` — module-level today,
  so they take the active engine explicitly;
- `residency.ensure_room` / load, and `_displaced` auto-restore;
- the jcode proxy (`jcode_llm.py`); pinning it to its own slot (4) with `id_slot` is F3b.

And the mirror: while Standard serves, a stored pick of Flash-Next runs on the task's static
route (env pin, tier or default) — never refused. Stored per-task picks are never rewritten;
the settings snapshot reports each task's `effective_spec` with a `remap_note`
("→ Flash-Next (engine active)") for the screen to mark.

### 4d. Exactly one engine, on every path

Both engines up at once is ~170 GiB on a 128 GB box: a freeze. These paths start
`local-llm` today with no engine check, and each must consult the selected engine:
- `deploy/local-models-sync.sh` (`--profile local-llm up -d`);
- `deploy/update-inner.sh` — auto-update rebuild, rollback, and final restart;
- `api/jcode.py` `_POWER_ON_SERVICES`.

The update's pre-build release (`update-inner.sh`: unload, then `rm -sf local-llm`) must
release **whichever engine is running**, or a live ~83 GiB Flash-Next stays up through the
build — the documented freeze mechanism.

**Provisioning.** The supervisor's `/start` only starts an existing container (404
otherwise), and every update removes `local-llm`. So provisioning creates **both**
containers stopped (`up --no-start` for an allowlisted profile service, run by the
supervisor or by `update-inner.sh`), and the switch is then a plain stop/start. Without
this, a switch back after an update taken on Flash-Next 404s — and no on-box work,
including F2, can run through the debug API before F1 lands.

**Desired vs effective engine.** Two settings-store keys, never an `.env` flag:
`llm_local_engine` is what the owner **wants** (read by every deploy path through
`jbrain.cli local-engine`; only the engine switch — the F3a owner route and the debug route, one orchestration — writes it), and
`llm_local_engine_effective` is what **actually started**, written by whatever starts an engine
(`deploy/local-engine.sh` via `jbrain.cli set-local-engine-effective`, the debug engine
route). Every api actor that gates a load, lists models or re-stamps a config — residency's
off-engine refusal, `kv_prefix`, the jcode proxy's model list, the settings picker and
re-stamp, `engine.ActiveEngine` — follows the **effective** engine. So when Flash-Next is
wanted but cannot start (no image, weights incomplete, a failed start) the deploy falls back to
standard, logs `FALLBACK` in the update log, records standard as effective, and the api keeps
serving standard instead of refusing every load; the desire is untouched, so the next update
tries Flash-Next again. `GET /api/debug/llm/engine` shows both.

**One "up" predicate.** An engine is up — holding, or about to re-take, its memory — when its
container is `running`, `paused`, `restarting` or `removing`. The supervisor's guard, the debug
route, `local-engine.sh` and the perplexity job all use that one definition; a crash-looping
(`restarting`) engine is **stopped** before the other starts, never skipped as down.

**Every route that can start a container is guarded** in the supervisor, under one lock (guard
and start are atomic): `/start` of an engine refuses while the other is up or while **any**
one-shot runs, and `/restart` — `docker restart` of a stopped container starts it — refuses a
stopped engine outright (Ops "Restart all" skips it) and puts a running one through the same
guard.

Not touched by the switch: `embed`, `tts-stt` (Whisper/Kokoro), `comfyui`.

## 5. No terminal, anywhere

The owner runs this box remotely with **no shell** (`CLAUDE.md` rule 10). Every step of this
plan — building it, provisioning it, testing it on the box, operating it, upgrading it,
backing it out — must be doable through **GitHub** (code lands as merged PRs) and the
**PWA** (or the owner-minted debug token, `docs/runbooks/DEBUG_ACCESS.md`, which reaches the
same surfaces). A step that needs `ssh`, `sudo jbrain …`, `docker compose …` or an `.env`
edit is a **defect in the plan**, not an instruction to the owner. The table is the
contract; each wave's exit criteria include "every row this wave touches works from the PWA
on the real box".

| Step | How it happens | Who | Built in |
|---|---|---|---|
| Land code, bump the llama.cpp / llama-swap pins | PR on GitHub, CI green, merge to `main` | Claude + owner review | every wave |
| Deploy | **Ops → Update** (or debug `POST /update`): pulls `main`, builds images — including `flash-next` — and recreates containers. A token can only deploy what `main` already holds | owner, or Claude with a token | exists |
| Provision the Flash-Next container | The update creates it **stopped** whenever Flash-Next is installed — keyed off a **settings-store value** read through the api CLI (the pattern `update-inner.sh` already uses for `local-llm-unload`), never an `.env` flag | automatic | F1 |
| Download / remove weights (~94 GB) | PWA on-box models **Install / Uninstall** queue; the next update one-shot downloads or prunes. Progress and failure reasons: the PWA, or debug `GET /provision/status` | owner | exists; F1 adds the entry |
| Check disk and host limits first | Install is refused in the PWA when free disk is short; GTT/TTM limits are read by `host_settings.check_host_settings` and shown in the PWA. The current box already serves ~94 GiB of GTT (gpt-oss + 27B), above Flash-Next's ~83 | automatic | F1 (disk guard) |
| Start / stop Flash-Next **before** the switch exists (F2) | A debug-only engine route, `POST /api/debug/llm/engine {standard\|flash-next}`, applying the same §4d one-engine guard; on success it records the target as both the **desired** and the **effective** engine (§4d), and `GET` shows both, so a deploy fallback is visible. The supervisor refuses any engine `/start` or `/restart` that would make two (a stopped engine is never restarted; Ops "Restart all" skips it). Since F3a it is a thin wrapper over the owner switch — one orchestration (drain, smoke, rollback), reached with the token | Claude with a token | F1; folded in F3a |
| On-box measurements (F2) | Debug routes: `/complete`, `/vision`, `/grounding`, `/tool-probe`, `/host/metrics`, `/llm/upstream-logs`, `/llm/gateway-logs`, the extra-args launch-flag route — all made **engine-aware** in F1. Two new ones: a **slot save/restore probe** (usable from F4, whose first check it is — §6) and an **allowlisted perplexity one-shot** run by the supervisor inside the `flash-next` image on a bundled WikiText-2 sample (check 6) — a fixed job, never free-form exec | Claude with a token | F1 |
| Iterate on the image (pin bump, flags baked into it) | Debug `POST /refresh` (or `/rebuild`) with `flash-next` — rebuilds that one service from `main` without the ~10-minute full update. **For an engine service it is a quiesced build**: whichever engine is up is released (models unloaded, stopped, memory settled), the image builds under the update's bounded runner with **no engine running** (a llama.cpp compile beside a ~90 GiB engine is the update's own freeze), the container is recreated **stopped**, and exactly the engine that was up before comes back — the refreshed one or the other. So it works whichever engine serves, at the cost of local inference being down for the build; to try the new build, switch with the debug engine route afterwards | Claude with a token | exists; engine-safe in F1 |
| Reclaim weight page cache | Debug `POST /llm/drop-page-cache` — range-aware on a resident Flash-Next: keeps the mmapped engram tensor's bytes, drops the rest (§3a) | Claude with a token | exists; F1, range-aware F3b |
| Tune launch flags | Debug extra-args route (`-ngl`, `-ub`, `--ctx-checkpoints`, `-lv`, `--load-mode`, …), engine-aware; `-ot` is added to `EXTRA_ARG_FLAGS` | Claude with a token | F1 |
| Switch engines | PWA **Ops → Local engine** (drain → swap → smoke → auto-rollback) over the owner API `POST /api/settings/llm/engine` | owner | F3a (API; the card after its mock is chosen) |
| See what the engine is doing | PWA Ops card over `GET /api/settings/llm/engine` (engine, memory, last switch + smoke, history as `engine_switch` box events); logs via PWA and debug | owner | F3a (API) |
| Clear or inspect disk prefix caches | The existing kv-prefix clear/snapshot surfaces, per role and per conversation; Ops → *Keep chats on disk* and *Prompt cache disk* | owner | F4 |
| Try a custom engine | PWA engine sub-setting (mainline \| gufo \| …); images arrive by Ops → Update | owner | F5 |
| Back out completely | Switch to Standard, Uninstall the weights in the PWA; the next update removes the stopped container and its image | owner | F1 + F3a |
| Recover from a bad build | Ops → Update's existing rollback; the switch's auto-rollback keeps a local engine serving; Standard is never rebuilt by this plan | automatic | exists + F3a |

**Precondition, not introduced here:** the box already has local hosting enabled
(`LOCAL_LLM_ENABLED=true`). Turning local hosting on for the *first* time is still a
shell step (`jbrain enable-local-models`) — a pre-existing gap this plan does not widen
and does not depend on; it is recorded for its own fix.

**If F2 finds a host limit too low** (GTT/TTM ceiling, swap, a kernel parameter), the plan
stops there: it does not ship a runbook step asking the owner to edit the host. The fix is
designed as a PWA-applied host setting first, the same way `host_settings` surfaces limits
today.

## 6. Waves

### F1 — Engine-aware container and provisioning ✅
- `deploy/Dockerfile.flash-next`; the `flash-next` compose profile (devices, groups,
  models volume, rw `.kvslots` mount, logbound logging, `local-llm` alias); its own
  llama-swap config renderer.
- Catalog: `qwen3.8-flash-next` with `engine`, `default_slots`, a file-backed weights
  figure, the §3 KV/indexer/checkpoint terms. **Hidden from the settings picker** unless it is the effective engine
  (an `engine` other than `standard` is not offered while the standard engine is active).
- Engine filtering in `render`, providers, `jcode_models`, residency; engine-aware
  resolution for every `llama-swap.yaml` reader; base-command `--no-mmap` removed for this
  engine; page-cache drop and host-metrics handling of mmapped tensors (§3).
- §4d: every `local-llm` start path and the update's pre-build release made engine-aware;
  provisioning creates both containers stopped.
- PWA weight install/uninstall through the on-box models path, with a free-disk guard;
  provisioning keyed off the settings store, not `.env` (§5).
- No-terminal tooling for F2 (§5): the debug-only engine route, engine-aware debug probes,
  the slot save/restore probe, the allowlisted perplexity one-shot, `-ot` on
  `EXTRA_ARG_FLAGS`.
- Engine-aware debug logs and upstream routes (`debug.py`'s
  `_JCODE_LOG_SERVICES` and the upstream-log routes are tied to `local-llm` today).
- Tests: config rendering for both engines, catalog footprint maths (file-backed
  subtraction, default slots), engine filtering, `supervisor/tests/test_deploy_scripts.py`
  for each update-inner branch, the existing compose tests (`test_compose_logging`,
  `test_llama_swap_pin`, `test_kvslot_compose`, `test_jcode_compose`) passing for the new
  service.

### F2 — On-box spike (measurement) ◻️
Run entirely through the debug token (§5): switch to `flash-next` with the debug engine
route, measure, switch back. The owner touches nothing but Ops → Update and the token.
Record into this doc:
1. **Full offload survives the first decode** on the pinned commit (#29028). Fail → stop.
2. Resident GTT, host RSS, PLE page-cache working set — cold, warm, and under a concurrent
   ingest load.
3. Decode tok/s at 512 / 8k / 32k / 128k depth; prefill at 8k / 32k / 128k; 1, 2 and 4
   concurrent.
4. Per-checkpoint size (replacing ~0.11) and the compute buffer at 262k with q8_0.
5. Prefix reuse per slot: a warm-slot second turn re-processes only its delta; a request
   pinned to slot 1 leaves slot 0's prefix intact.
6. Correctness: WikiText-2 perplexity vs the #27742 reference (catches the converter norm
   bug); **mainline-Vulkan vision** grounding (catches pre-#27941 collapse; thinking off,
   ≥1024 output tokens); a tool-call round-trip; JSON-mode output.
7. A 60-minute mixed soak with zero device-loss or GPU reset events.

Slot save/restore is **not** an F2 check: it needs `--slot-save-path` and the checkpoint
sidecar patch, which the flash-next image only gets in F4, so it is F4's first check (below).

**Exit gate:** measured resident ≤ 90 GiB with 8 checkpoints per slot, every check above
passes, and no check needed a host shell. Fail → the plan parks with the numbers recorded.

### F3a — The owner switch and the remap ✅
Split from F3 on 2026-10-02, after F1 ran live: while Flash-Next was effective, every task
still routed to a standard model (gpt-oss-120b, qwen3.8-27b-q4, …) was refused by residency's
off-engine gate, so the nightly workflows did nothing; and the PWA's Load button for
Flash-Next got a silent 409 while Standard served. The switch and the remap land together —
a live switch without the remap would send every call to Flash-Next carrying gpt-oss's
sampling and reasoning quirks, or refuse it outright. Backend built; the PWA card and the
per-task marker follow the owner's choice of mock (the GUI gate).
- **Owner API** (owner session, not the debug token): `GET /api/settings/llm/engine` —
  desired, effective, per-service state, installed per engine, a busy one-shot, local
  admission, device memory, the nightly guard and the in-flight or last switch — and
  `POST /api/settings/llm/engine {engine, force}`. The debug `GET`/`POST /llm/engine` are thin
  wrappers over the same two functions: **one orchestration** (`jbrain.llm.engine_switch`).
- **Orchestration**, as a background job (a Flash-Next load outlives a request and the 100 s
  Cloudflare limit; the POST answers 202 and the status is polled), stages
  `draining → stopping → starting → loading → smoke → done | rolled_back | failed` + reason,
  persisted in the settings store at each stage and ended as an `engine_switch` box event.
  Refused (nothing touched) while a switch or any supervisor one-shot runs, when the target is
  not provisioned or its weights are not installed, and — unless `force` — while a workflow
  run is executing or inside the nightly window (30 min before a daily schedule fires to 60
  min after, read from the scheduler's own `app.schedules`). Any failure after the target was
  touched: stop it, confirm it down, restart the previous engine (`rolled_back`); unconfirmed
  → restore nothing (`failed`).
- **Drain** — new code, not an existing gate (`admission.py` is load arithmetic, the ledger
  tracks load charges): a settings-store row with a wall-clock deadline that the api's and the
  worker's routers and residency coordinators read (`jbrain.llm.drain`, 1 s cache). A local
  call waits up to 30 s for it to reopen, then is refused with a `ResidencyError` (a worker job
  is deferred, no attempt burned). The switch then waits up to 60 s for in-flight calls,
  read off the gateway (a loading model or a slot `is_processing` — every process's calls),
  and proceeds. Admission reopens on every exit, after the processes' cached effective
  engine has expired; an api restart mid-switch reopens it on boot.
- **Exclusivity and honest endings** (review round): the switch holds a supervisor-side
  switch hold for its whole run — a short deadline (2 min) renewed at every stage and by a
  heartbeat, and released on api boot after a crash, so a dead switch cannot block the box
  for long — so no engine start, restart or one-shot **started via the supervisor** can begin
  under it from any caller (a host-shell `jbrain update` is outside it). The api refuses its
  own engine-affecting routes (Ops update/rebuild/provision/restart/start, restarting the api
  itself, jcode power-on, debug update/refresh/perplexity) while it runs; one-shots are
  re-checked before every start. Whatever cannot be
  confirmed, the switch ends on what is actually up and records that as effective — "NO local
  engine is up" when nothing is. It can be cancelled while still draining. The engine read
  carries `effective_since`, the recorded `fallback_reason` (also from deploy/local-engine.sh)
  and llama-server's own `decode_tps` gauge.
- **Smoke**: a thinking-off text completion, a tool-carrying probe, and an image probe on a
  vision model — a solid-colour PNG synthesized in memory, so no attachment or DB lookup.
- **Remap** (§4c) at every entry point: the router's `_resolve_live` (stored pick, env pin,
  tier and per-call `spec_override`, then the effort re-gated on the model that runs), the warm
  keeper's target (`primary_local_served_model`), `context_window_for_spec` and
  `providers.supports_vision_for_spec` (engine passed explicitly), residency's `ensure_room`
  (never evicts Flash-Next for an old name) and `_displaced` restore, and the jcode proxy
  (rewrites a stale model name). The mirror while Standard serves: a Flash-Next pick runs on
  the task's static route. Stored picks are never rewritten; the snapshot reports each task's
  `effective_spec` + `remap_note`.
- **Load button**: each local model carries `loadable_now` + `blocked_reason` ("Runs on the
  Flash-Next engine — switch engines to load it"); the load route's 409 carries the same text.
- No new table (two settings-store keys), so no new RLS test.
- Tests: owner-route auth, orchestration against a fake supervisor (happy path, every rollback
  branch, crash, refusals, concurrency), drain across processes, remap per task/tier/override
  and entry point in both directions, warm-keeper target, jcode proxy, residency never evicting
  the active engine's model for an old name, `loadable_now`, the nightly guard (pure + real
  Postgres), the box event.

### F3b — Slot affinity, the shared 512k pool ◻️ (shipped #1550; resized to 512k on deploy; on-box re-measure pending)
Scope changed 2026-10-03 (owner): the 512k/5-slot reservations became a 1M pool over 8 slots (resized to 512k on deploy, §3a)
with per-slot caps and a pool guard (§4a).
- Slot affinity: `id_slot` through the provider protocol and `openai_compat` (rule 1), chosen
  by task name or the caller's `slot_role`; the jcode proxy pins slot 4; direct gateway calls
  (load prime, probes) pinned too.
- The pool: `-np 8 --kv-unified -c 524288 --slot-save-path …` (1M as first shipped; see §3a), saved window/slot overrides
  ignored for it except a saved pool size from `{524288, 1048576}` (the context-window route,
  owner or debug; render, budget, admission, the pool guard and the drawer's `kv_pool` follow it), the budget charging the pool once; the settings API refuses slot/window
  changes for it and reports the slot table.
- Caps at the router and the jcode proxy (clamp or refuse, `SlotCapError`), the live slot-count
  check, and the pool guard (evict idle slots in our order, bounded wait). A refusal the proxies
  can only reach after their 200 headers (a busy pool, a cap re-checked after a remap) is
  written into the body as an OpenAI error, never a cut stream.
- The external remote-coder proxy (`external_llm`) is held to the jcode slot too, but its
  `jcode_model` gets no engine remap — pre-existing: while Flash-Next serves it answers "not
  loaded" for a standard coder rather than running on Flash-Next.
- ~~Per-role prefix priming~~ — **dropped 2026-10-03** after review: the ingest and pet prefixes
  are ~400–500 tokens (under a second of prefill, and the slot keeps the last real call's prefix
  anyway), and the scheduled-task prime is a ~60 s jerv prefill nobody waits on that competed with
  the owner right after a load. Only the interactive (slot 0) prime remains, now pinned; F4's disk
  layer is where per-role prefixes would come back if a measured one earns it.
- Load admission: booked at the F2 fit (~74 GiB on the GPU at 512k); context checkpoints (host-only,
  lazily filled, up to ~7 GiB) stay in the footprint but out of the load charge, so a switch
  needs ~80 GiB free (it was ~94 at 1M).
- PWA: a read-only pool view replacing the window/slot pickers for a pool model — **three
  mocks** before code (`PROCESS.md`).
- Re-measure on the box with every slot filled to its cap (the worst case, ~74 GiB predicted).
- Page cache and pool size (2026-10-03, §3a): `--load-mode none`, the range-aware page-cache
  drop (in-load sweep, post-load, debug route, and the guard's proactive drop in the 4 GB band
  above its host floor), and the pool size selectable between 512k and 1M without a release. Pending on the
  box: a 1M load under `none` with the range drop, read off `GET /api/debug/host` while it runs.
- Tests: caps per role, clamp and refusal, eviction order, layout mismatch, pool flags rendered,
  slot selection per task and caller, every pool-model request pinned, engine-aware jcode
  power-on.

#### F3b follow-on — Flash-Next's own reasoning levels (backend built 2026-10-03; PWA after the mock)
Owner request 2026-10-03. The §4c remap re-gates each task's Standard effort onto Flash-Next,
so a level chosen for gpt-oss or Grok was what Flash-Next ran at. Now the owner sets the level
used ON Flash-Next per tier (the screen's role groups) and per task, which inherits its tier
unless it has its own:
- A new table, `app.llm_engine_effort` (engine, scope `task`|`tier`, key, effort; unique per
  engine/scope/key; owner-only RLS plus the jmolt deny, no principal column — the router reads
  under the system context, as with `app.settings`), kept apart from `llm_task_overrides` so
  switching back to Standard restores everything untouched. `engine` is a column, CHECKed to
  `flash-next` today (the only engine with levels: its catalog `thinking_effort_map` keys plus
  the hybrid's `none`); another engine widens that CHECK.
- The router, after the remap and before the capability gate: a call whose model runs on a
  non-Standard engine takes the task row, else the tier row, else today's effort; a per-call
  `effort_override` still wins. Standard calls never read the table. A 5 s cache, invalidated
  in-process on a write (a read already in flight when the write lands is not kept as fresh).
  The load-time warm-up folds in the same level.
- Owner API: the snapshot's `engine_efforts["flash-next"]` (levels; `model_default`, what an
  unset task really runs at; each tier's level and default; each task's level, fallback and
  its source, effective level, and whether it applies now); owner-only (`OwnerDep`, a non-owner
  session is a 403) `PUT`/`DELETE /api/settings/llm/engine-effort/{engine}/{scope}/{key}` and a
  batch `PUT /api/settings/llm/engine-effort/{engine}`; the debug twin is the batch route.
- **Open question (owner):** unset on Flash-Next runs at the template default (high/xhigh),
  including vision/OCR — owner to decide whether unset should send an explicit level. Routing
  is unchanged until then; the snapshot's `model_default` says so truthfully.
- PWA: per-tier and per-task controls on the LLM settings screen — mocks first (`PROCESS.md`).
- Tests: resolution precedence, Standard unchanged, the override still winning, the cache,
  the warm-up, API validation and snapshot shape; the table's RLS isolation on real Postgres.
- **Code mode on Flash-Next** (owner, 2026-10-04: "code mode needs to also be auto routed and
  just be added to the flash choices"). While Flash-Next serves, both of jcode's roles already
  ran on it — the proxy (`api/jcode_llm.py`, `_served_on_engine`) remaps any Standard name the
  sandbox sends. Now they take Flash-Next levels too:
  - A `code` tier (*Code mode*) with two tasks, `jcode.executor` and `jcode.planner`
    (`router.CODE_TASKS`; not router tasks — nothing routes them). The engine-effort routes,
    validation and the debug twin accept them; the table's CHECKs never constrained keys. The
    snapshot lists them last, with a `label` (*Code mode — executor* / *— planner*), no Standard
    fallback, and `applies` while Flash-Next serves and code mode is on.
  - **Role at the proxy.** grok sends only the model of the block it picked: its default (the
    executor) or the one pinned to its `plan` subagent (the planner). A request naming the
    owner's planner pick, when that differs from the executor pick, is the planner; anything
    else (including single-model, "same") is the executor. Judged on the name grok sent,
    before the remap.
  - So a shell opened while Flash-Next serves keeps those names, the proxy's `?format=lines`
    list adds a block per installed Standard coder, each naming Flash-Next and its jcode slot
    window. Without them grok's default had no block and its `plan` pin was dropped, leaving
    one name for both roles.
  - **Precedence: the owner's level wins.** The role's row, else the `code` tier row, replaces
    every reasoning field grok sent (`reasoning_effort`, `reasoning`, the template kwargs'
    `enable_thinking` / `reasoning_effort`) and is encoded as the adapter encodes any local call
    (`apply_local_reasoning`). grok sends one level to every model it talks to, while the
    owner's was set for this one. With no row the request goes as grok sent it. A Standard model
    never reads the table: its body is forwarded unchanged.
  - PWA: the Flash-Next reasoning card gains the *Code mode* row. While Flash-Next serves, the
    Code mode card drops its two model selects for one line naming the model, with a link that
    opens the reasoning card at that row.

#### F3b follow-on — preserved thinking within a turn (backend built 2026-10-04)
The Qwen3.8-Flash-Next card recommends keeping the model's own thinking across an agent's
steps (decision consistency, less re-reasoning, better KV reuse), and the served template reads
`reasoning_content` on assistant messages (llama-server: "chat template supports preserving
reasoning"). The adapter never sent it back. Owner decision: the card's lighter
`preserve_thinking: false` mode — replay only the turn in flight's tool steps.
- `AssistantMessage.reasoning` + `reasoning_model`; the agent loop sets both on each tool step it
  appends (`run`, `run_stream`, the buffered reflexion path). The router stamps the served model
  on every `LlmTurn`, so a step names its thinker and only that same model is ever replayed to
  (a mid-turn engine switch drops the other model's thinking). History rebuilt from earlier turns
  stays text-only. A sub-agent's hidden tool-round text, folded onto the persisted trace, is not
  replayed as reasoning (it already goes back as the step's content).
- `openai_compat._openai_messages` emits `reasoning_content` only for the steps
  `types.replayed_steps` picks (after the last user message, produced by this model), and every
  call to a preserving model carries `chat_template_kwargs.preserve_thinking=false` so the
  template draws the same line. An injected directive (budget warning, forced final answer) is a
  user message, so it starts a new boundary for both alike.
- Gate: the catalog flag `LocalModel.preserves_reasoning`, set on Flash-Next only, read through
  `local_catalog.replays_reasoning(provider, served)`, which is never true for a cloud provider,
  an unknown served name or a Standard model. The Qwen3.8-27B entries stay off until their served
  template is shown to read both fields. The ROUTER decides per call and passes
  `replay_reasoning=True` to the client explicitly; the client re-checks the catalog gate.
- **Degrade on overflow:** `slot_roles.prompt_chars(..., replay_model=…)` counts the replayed
  trace, so the cap check and pool guard book what is sent. If the replay is what pushes a prompt
  past its role's slot cap, the router runs that round WITHOUT it (`llm.reasoning_replay_dropped`)
  instead of failing; a prompt too big even bare is still refused. The raw-body proxies'
  `openai_slot_fit.prompt_chars` counts a client's own `reasoning_content`.
- Cost: the router stamps an estimate of the replayed tokens on the turn (`replayed_tokens`), and
  the loop's cost guardrail and tree budget leave them out — the model re-reads its own earlier
  output each round, which would otherwise end a long Flash-Next loop on `budget` early. The
  prompt capture shows a step's trace size (`[reasoning: N chars]`).
- Known gap (comment in `STEPS_BY_EFFORT`): an unset effort on Flash-Next runs at the template's
  `xhigh` but sizes the step cap as the default, since the guardrail sees the routed effort.
- **Expect a one-time re-prefill per conversation after deploy:** if the template's default
  rendered an (empty) think block on earlier turns' assistant messages, `preserve_thinking=false`
  drops it, so each conversation's history prefix changes once and its first turn re-reads the
  history cold. Not yet observed on-box.

### F4 — Per-role disk prefix cache 🟡 (code built 2026-10-04; the on-box check is pending)
Begins with the check moved out of F2, and gated on it:
- Re-validate the sidecar patch against the new pin (anchors fail hard on drift, by
  design), turn its build arg on for this image and render `--slot-save-path` for it.
- **Slot save/restore** (debug `POST /llm/slot-probe`, built in F1): save a primed slot,
  restore, and compare the next-token **logits within a tolerance** against a cold prefill
  with identical ubatch boundaries — greedy token equality can differ legitimately. Fail →
  F4 stops there.
- §4b store changes: patch-gated eligibility, per-role state, slot-targeted restore,
  per-slot guard, per-role fingerprint inputs; budget sized for four files per engine.
- Warm keeper primes each role's prefix into its slot after a load or switch, saves once,
  restores on loss.
- Tests: per-role fingerprints, restore targets the right slot and never overwrites an
  occupied one, a role switch does not raise `identity_drift`, an engine switch leaves the
  other engine's files intact, budget eviction across roles.

**Built (2026-10-04), ready to deploy:**
- **F4a prep.** The sidecar patch re-validated against `869034b`: both anchors match exactly
  once (`res->is_save  = true;`, `slot->prompt.tokens = std::move(restored);`), a second run
  skips both, and the patched `server-context.cpp` compiles (`-fsyntax-only`); the
  checkpoint struct still carries the fields the blocks serialize. `PATCH_RESTORE_CHECKPOINT`
  defaults to 1 for the flash-next image (Dockerfile and compose). `--slot-save-path` was
  already rendered for the pool (slot erase needs it). The slot probe takes any slot pair and
  now returns `tolerance` (default 0.01 in probability — 0.05 nats until the first on-box run
  failed on deep-tail noise), `within_tolerance` (top token agrees, half the top-n
  shared, every shared candidate's probability within tolerance — against both the cold and the warm read),
  `passed` (that plus `restore_effective`) and `sidecar` (the save wrote its `.ckpt` — the
  patched build is the one running).
- **§4b store.** Flash-Next is eligible through a catalog `kv_restore_needs_patch` gate, and
  each save PROVES the patch: no sidecar → the file is removed, the model leaves the disk layer
  until the next api start (`patch_absent`), and a file without a sidecar is never restored.
  Memos and identity drift are per (model, role); saves read only the role's own slot; a
  restore targets the role's slot, only while it is idle and empty, only when the restored
  tokens fit the pool (through the pool guard since the review, below). A restored-unused
  slot is not restored again until a request uses it, the model reloads or the guard erases it.
  Each role's effort is in its fingerprint as before; the standard engine keeps its single-memo
  behaviour.
- **Roles primed.** Deviation from the list above, deliberate: jerv's prime is saved from slot
  0, and the keeper RESTORES that same file into the scheduled slot (2) when it is empty — the
  scheduled prefix is jerv's — but primes nothing else. Ingest and pet prefixes are ~400–500
  tokens (under the store's 4,096-token floor and under a second of prefill) and research has
  no stable prefix, which is why F3a dropped role priming; a role's turn still restores into its
  own slot on demand if a file of its identity exists.
- **F4c — conversations (owner extension).** The chat names its conversation to the router
  (`conversation_key`, the session id). On a pooled model, before an interactive request that
  is not the conversation slot 0 holds, the store saves the holder (only if `/slots` still
  reads as that conversation's cache and `n_saved` matches) and then restores the request's own
  conversation file when its key and base identity match (see the review entry below).
  The keeper saves a conversation idle for 10 min. Conversation files live beside the role
  prefixes (`c-<hash>.kvslot` + a `.meta` claim: key hash, base, counts), share the budget, and
  are all evicted before any role prefix. Toggle *Keep chats on disk* (default on) and budget
  *Prompt cache disk* (default 40 GiB, was 25) in Ops, both live; debug twins
  `PUT /llm/kv-prefix/conversations` and `…/budget`. `GET /llm/kv-prefix` reports per-role
  rows, the held conversation, conversation files and hit/miss counters.
- **Restore gate (review, 2026-10-04).** No restore of any kind on Flash-Next until the slot
  probe has passed — sidecar included — against the running launch line and llama.cpp build;
  the probe records its verdict in `restore-gate.json` beside the slot files, so a new image or
  launch line needs a new run. Saves continue. `restore_gate` is in the state read and the
  settings read (Ops hints say when restores wait or are off).
- **Privacy scope (review, 2026-10-04; CLAUDE.md #3).** A conversation file holds that chat's
  tokens outside Postgres's domain firewalls, so only chats that cannot hold firewalled data get
  one: a persona with `reads_knowledge_base=False`, a session scoped to `general` only (or
  nothing) and no subject. Brain/curator chats never do. Role prefixes (system + tools only) are
  unaffected.
- **Independent review (2026-10-04).** Conversation restores are decided on the conversation key
  and base identity alone — a chat's message list is never stable between turns (volatile
  blocks, the turn's own tool steps) — and judged by the `cached_tokens` the first request
  reports (hit / partial / miss; three misses drop the file). Saves stream outside the store's
  main lock, one at a time, each deleting the old sidecar first, and never leave the volume
  under 20 GiB free. Restore fits go through the router's pool guard (its lock, its pending
  calls, a charge for never-used restored slots, an erase notice back to the store). Privacy
  also excludes mail-holding personas and any chat in which a location, mail or records tool
  ran (read off the transcript); deleting or re-scoping a chat deletes its files, and the
  toggle off deletes them all. A turn superseded by another interactive request never claims
  the slot; a reload forgets the restore gate.
- **Re-review (2026-10-04).** A forget or a clear can no longer be undone by a save parked
  between its snapshot and its write (forgotten-set + clear epoch, checked under the save lock;
  a turn streaming at the delete claims nothing). A restore reserves its cells in the pool
  guard's locked decision and holds them while it streams. A session ever scoped to a firewalled
  domain or a subject before a re-scope is recorded (owner setting, no new table) and stays off
  disk for good. APRS tools join the excluded list; the toggle reads malformed values as off.
- **Preserved thinking (#1560).** Restores compare no messages at all; the next turn's text-only,
  `preserve_thinking=false` render diverges at the previous turn's first tool step, where reuse
  stops at the nearest checkpoint before it — which the hit/partial/miss judging measures.

**Pending on the box (in order; each needs only the debug token):**
1. Ops → Update (rebuilds the flash-next image with the patch), switch to Flash-Next.
2. `POST /llm/slot-probe {"synth_tokens": 29000, "slot_a": 6, "slot_b": 7}` → `sidecar: true`,
   `passed: true` and `restore_gate: passed` — that verdict is what opens restores. **Fail →
   F4 stops there**: the gate stays `failed`, so nothing is restored; turn *Keep chats on disk*
   off and `DELETE /llm/kv-prefix?model=qwen3.8-flash-next` to stop the saves too.
3. After a load: `GET /llm/kv-prefix` shows `saved` for the interactive role, a file with
   `sidecar: true`, and after an engine round-trip `restored` into slot 0 with the prime's
   `reuse_rate` near 1.0 (`POST …/prime`).
4. Conversations: two chats alternated; `conversation_saved` / `conversation_restored` move,
   and the restored turn's `prefill` is a fraction of the transcript. If the previous answer's
   re-render diverges before the last checkpoint so that nothing is reused, turn the toggle
   off and record it here.

### F5 — Custom engine track (evidence-gated) ◻️
The container's contract stays fixed — llama-swap in front, OpenAI API behind — so an
engine is a `cmd` swap in its llama-swap config plus an image change.
- Candidates: **gufo** (MIT, HIP, best published multi-user throughput), **EngramHalo.cpp**
  (MIT llama.cpp fork; multi-slot unverified). halogen excluded (§1).
- Same F2 harness for each, plus needle-in-haystack at 32k/128k and token agreement vs the
  mainline reference. **Adopt only if** quality matches mainline within noise, single-stream
  decode beats it at 8k and 32k depth, prefix reuse per slot still holds, and the soak
  passes. Images pinned by digest, sources by commit.
- An engine sub-setting under Flash-Next (mainline | gufo | …) so the owner can fall back
  without a deploy.

## 7. Risks

- **Speed** — mainline Vulkan single-stream is ~17 tok/s at short context and ~12 at 32k
  (Soot/Silicon), against gpt-oss's ~31 today. Slots are caches, not concurrency, so this
  is the number that matters; F5 is the answer if it bites.
- **Full-offload crash (#29028)** — closed upstream but unconfirmed in our pin; F2 check 1.
- **Memory margin** — ~83 GiB derived against a 90 GiB gate, with a history of
  derivations running light. F2 measures before anything depends on it.
- **Quality vs gpt-oss-120b is unknown** — no shared public benchmark. F2's checks are not
  an eval; run the existing ingest/analysis eval fixtures before retiring anything.
- **Restore without the patch is useless** — checkpoints are cleared on restore; F4 does
  not ship without the sidecar patch validated on the pin (anchors and compile: done
  2026-10-04; the live restore: the slot probe, on the box). Each save also proves it.
- **Conversation files hold conversation tokens on disk** — like the KV in RAM, outside the
  database's RLS and its backups, until the budget or a clear removes them. The owner chose
  this (2026-10-04); the toggle turns it off.
- **Upstream churn** — qwen4exp is a month old; each pin move re-runs F2.
- **The standard engine must stay healthy** — its image and flags do not change; F1's
  engine-awareness must leave a box that never provisions Flash-Next byte-identical in
  behaviour (covered by the existing test suite plus the new update-inner branch tests).

## 8. Obligations

- `scripts/dev-setup.sh`: unaffected (no new dev dependency; the image builds on-box).
- No new table → no new RLS isolation test.
- No-terminal acceptance (§5): each wave is done only when every §5 row it touches has
  been exercised from the PWA or the debug token on the real box.
- `docs/runbooks/DEBUG_ACCESS.md` lists the new debug routes (F1).
- Docs: `docs/runbooks/STRIX_HALO_SETUP.md` gains a Flash-Next section in F1, written as
  PWA steps only; the operator-facing switch is documented there in F3a.

## 9. Open questions

1. Turn llama.cpp's in-RAM prompt cache back on for this engine? We serve `-cram 0`
   because it cost 8 GiB per model; with ~0.55 GiB prefixes, 3–4 GiB holds ~6 recent ones
   (Soot/Silicon measured 27 s → 0.73 s on a hit on qwen4exp), and `--cache-idle-slots`
   needs it. It is host memory the budget must count. Decide after F2.
2. Should the switch be schedulable (e.g. Flash-Next overnight for batch ingest)? Out of
   scope for v1; the endpoint shape should not preclude it.
3. MTP with multiple slots — measure after F3b, or leave to F5's engines?
4. Canvas grounding was admitted **unprobed** (owner decision 2026-10-04): Flash-Next is in
   `CANVAS_MODELS` at the Qwen3.8 `norm_1000` base so jerv can mark up photos at all.
   A `/grounding` run during the F2 soak should confirm it; a disagreement is one line.

## 10. What the reviews changed

- Wave order: container + provisioning (F1) now precede the spike (F2), which cannot run
  through the debug API without them; the switch and remap merged into one wave (F3).
- Remap moved from llama-swap aliases into the API (aliases cause residency thrash);
  jcode found to reach the model through the API proxy, which pins its slot.
- Added §4d: four unconditional `local-llm` start paths and the update's pre-build release.
- Memory: QSA indexer corrected from ~0.8 to 3.2 GiB; checkpoints cut to 8 per slot;
  page-cache accounting and the page-cache drop identified as conflicts.
- Evidence relabelled with its measurement conditions; #29028 and mainline-Vulkan vision
  added as explicit F2 checks; speed restated by context depth.
- Slot save/restore found to serialize all model state but clear checkpoints — the sidecar
  patch is mandatory; restore comparison switched to logits within tolerance.
- MTP restated as deferred, not impossible; halogen dropped; slot cap, `--no-mmap`
  supersede and `-ot` allowlist gaps recorded.
