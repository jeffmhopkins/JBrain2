# Flash-Next engine — a switchable second local-LLM stack (Qwen3.8-Flash-Next)

> **Status:** In progress · **Last verified:** 2026-10-01 · **Waves:** F1✅ F2◻️ F3◻️ F4◻️ F5◻️

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
| Slots | **5 role-pinned slots sharing one 524,288-cell (512k) `--kv-unified` pool**, each role capped by a router-enforced **reservation**: agent 256k, ingest/analysis 64k, agents/research 128k (jcode shares this slot — no reservation of its own), and two 32k slots for the jpanel pet and small-prompt tasks (§4a). Slots exist to **keep each workload's prefix warm**, not for concurrency. Decided 2026-10-01 from the F2 measurements (§3a): ~74.2 GiB, the size measured directly as the 2×262k row. |
| Checkpoints | **8 per slot** to start; 16 only once F2 has measured their real cost (§3). |
| Quant | Unsloth **UD-IQ4_XS** (93.7 GB on disk) + F16 vision projector (904 MB). |
| Engram (PLE) table | **Memory-mapped from disk**, pinned to CPU (`-ot per_layer_token_embd=CPU`). |
| Disk prefix cache | One primed prefix per slot role, restored into its own slot (§4b). |
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

**Consequence for §4a (owner decision 2026-10-01):** slots stay role-pinned but share one
`--kv-unified` pool of **512k cells** (agent 256k + ingest 64k + research/jcode 128k + 2 × 32k
for the pet and small prompts) — the same total cells as the measured 2×262k row, **74.2 GiB**
GTT, and slot count costs ~nothing. F3 re-measures the chosen pool before it ships.

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
from the measured `disk_gb`, and the page-cache drop skips mmapped tensors.

## 4. Shape

- **Image:** `deploy/Dockerfile.flash-next` — same kyuz0 Vulkan toolbox base as
  `Dockerfile.local-llm`, llama.cpp pinned to a commit at or after #27941 **and** past
  whatever closed #29028, llama-swap (pinned by commit, as `test_llama_swap_pin` requires)
  in front of the one model. Keeping llama-swap keeps the `local_gateway.py` contract
  (`/running`, `/api/models/unload`, `/upstream/…/health`) that `residency`, `warm_keeper`
  and the ledger read. The checkpoint-sidecar patch is a build arg, off until F4.
- **Own config:** Flash-Next renders its own `llama-swap.flash-next.yaml`. Every reader of
  `llama-swap.yaml` (`launch_line`, `served_shape_from_config`, `kv_prefix._resolve`, the
  smoketest) resolves the file for the **active engine**. The standard `render`, the
  providers list, `jcode_models` and residency filter catalog entries by a new
  **`engine`** field, so the standard gateway never sees Flash-Next and vice versa.
- **Network identity:** the service also carries the network alias `local-llm`. Safe only
  because both are never up at once (the API opens a fresh client per call, so there is no
  stale connection or DNS pinning); §4d is what guarantees it.
- **Serving flags:** `-ngl 999` (explicit — #29028 is the full-offload crash),
  `--load-mode mmap` (with the global `--no-mmap` **removed** from the base command for
  this engine — catalog `extra_server_args` do not supersede base flags today; only
  operator args do), `-ot per_layer_token_embd=CPU` (the 26.8 GiB tensor exceeds Vulkan's
  4 GiB binding limit; GPU placement aborted for Soot/Silicon), `--lazy-mode on`,
  `-np 5 --kv-unified -c 524288` (one shared pool; no single sequence may exceed
  `n_ctx_train` = 262,144, which the agent's reservation equals), `-ctk q8_0 -ctv q8_0`, `-fa 1`, `-cram 0`,
  `--ctx-checkpoints 8 --checkpoint-min-step 1024`, `--jinja`, the F16 mmproj with the
  existing `--image-min-tokens` floor.
- **MTP: not adopted in v1, pending measurement.** Upstream now has a qwen4exp MTP draft
  context; our one-slot rule for speculation is our own (`llama_swap_config.py`), and the
  evidence on multi-slot MTP conflicts (#27836 reported cross-slot contamination and a net
  loss on Vulkan; a fork reported 40→47 tok/s). A later wave can measure it.
- **Slots:** a catalog `default_slots` (4 here) threaded through `render`, `footprint_gb`,
  `residency._slots` and the served shape. The settings cap `PARALLEL_SLOTS_MAX = 2`
  (`api/llm_settings.py`) becomes per-model.

### 4a. Slots are prefix caches, pinned by role

On a hybrid model a prefix is reused only if a context checkpoint covers the divergence
point; otherwise the whole prompt is re-prefilled (the 27B paid 232 s per turn before
checkpoints were raised). Today separate models give separate caches — the persona lives
on gpt-oss, ingest on the 27B. With one model, **slots take over that job**.

Nothing pins a request to a slot today (`llama_swap_config.py`, the `-np` comment):
llama-server picks a slot sharing ≥10% of the prefix, else the least-recently-used one —
usually the idle slot holding the primed persona. F3 adds **slot affinity**: an `id_slot`
parameter through the provider protocol and `openai_compat` (rule 1), chosen by **task
name** (agent turns pass `SYSTEM_STRENGTH`, so strength cannot carry the class). A busy
pinned slot queues its own traffic; llama-server defers the task until the slot frees.

Because the pool is shared, each role also carries a **reservation**: the router refuses (or
trims, where the caller allows it) a request whose prompt plus `max_tokens` exceeds its role's
cap, so no role can grow into another's cells and evict its cached prefix. The caps sum to the
pool exactly, so the pool never runs out of cells while every role stays inside its own.
F2/F3 also verify what llama-server does if a pool fills anyway (it should never happen with
the caps enforced; the test is that it fails loudly rather than silently evicting).

| Slot | Workload | Reservation | Pinned by |
|---|---|---|---|
| 0 | Interactive persona (jerv, omnibox turns) | 256k (262,144) | router, by task |
| 1 | Ingest + analysis | 64k (65,536) | router, by task |
| 2 | Agents, research, workflow tasks — **and jcode**, which gets no reservation of its own (owner, 2026-10-01) and shares this slot's cap | 128k (131,072) | router by task; the jcode proxy (`api/jcode_llm.py`) for jcode |
| 3 | jpanel kid pet (`pet.turn`, `pet.thought`, `pet.statue`) | 32k (32,768) | router, by task |
| 4 | Small-prompt tasks: titles (`research.title`), `triage.classify`, and other short-prefill calls; overflow for the pet | 32k (32,768) | router, by task |
| | **Pool** | **512k (524,288)** | `--kv-unified -c 524288` |

jcode is pinned (to slot 2) rather than left unpinned on purpose: an unpinned request lands on
the least-recently-used idle slot, which would evict whichever role's cached prefix lives there.
The pet and small-prompt slots are short by design — their prompts are small, so a 32k cap
costs them nothing and keeps their churn away from the long agent and ingest prefixes.

### 4b. Disk prefix cache, one prefix per slot role

Today `jbrain.llm.kv_prefix` saves ONE prefix — the interactive model's persona + tools
(~29k tokens) — after the warm keeper primes it, and restores it in ~100 ms page-cache-warm
instead of a cold prefill of ~60–100 s. Its fingerprint (launch line + system text + tool
schema + reasoning effort) names the file; an owner-set byte budget evicts by least-recent
use.

On Flash-Next a 29k-token prefix file is ~0.55 GiB (attention KV + indexer cache + recurrent
state), a quarter of gpt-oss's ~2 GiB — so **one primed prefix per slot role** is
affordable: persona (0), ingest/analysis system prompt (1), agent/research base prompt (2),
the pet's persona prompt (3). jcode shares slot 2 and the small-prompt slot (4) is too short
to be worth priming, so neither gets a primed prefix. Each is primed once, saved, and restored **into its own slot**
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
- the jcode proxy (`jcode_llm.py`), which also injects `id_slot: 3`.

Stored per-task picks are never rewritten; the settings screen marks each local pick
"→ Flash-Next (engine active)".

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
`jbrain.cli local-engine`; only the debug engine route — and F3's switch — writes it), and
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
| Start / stop Flash-Next **before** the switch exists (F2) | A debug-only engine route, `POST /api/debug/llm/engine {standard\|flash-next}`, applying the same §4d one-engine guard; on success it records the target as both the **desired** and the **effective** engine (§4d), and `GET` shows both, so a deploy fallback is visible. The supervisor refuses any engine `/start` or `/restart` that would make two (a stopped engine is never restarted; Ops "Restart all" skips it). Removed or folded into the F3 switch once that lands | Claude with a token | F1 |
| On-box measurements (F2) | Debug routes: `/complete`, `/vision`, `/grounding`, `/tool-probe`, `/host/metrics`, `/llm/upstream-logs`, `/llm/gateway-logs`, the extra-args launch-flag route — all made **engine-aware** in F1. Two new ones: a **slot save/restore probe** (usable from F4, whose first check it is — §6) and an **allowlisted perplexity one-shot** run by the supervisor inside the `flash-next` image on a bundled WikiText-2 sample (check 6) — a fixed job, never free-form exec | Claude with a token | F1 |
| Iterate on the image (pin bump, flags baked into it) | Debug `POST /refresh` (or `/rebuild`) with `flash-next` — rebuilds that one service from `main` without the ~10-minute full update. **For an engine service it is a quiesced build**: whichever engine is up is released (models unloaded, stopped, memory settled), the image builds under the update's bounded runner with **no engine running** (a llama.cpp compile beside a ~90 GiB engine is the update's own freeze), the container is recreated **stopped**, and exactly the engine that was up before comes back — the refreshed one or the other. So it works whichever engine serves, at the cost of local inference being down for the build; to try the new build, switch with the debug engine route afterwards | Claude with a token | exists; engine-safe in F1 |
| Reclaim weight page cache | Debug `POST /llm/drop-page-cache` — made to skip the mmapped engram table, which it would otherwise evict (§3) | Claude with a token | exists; F1 |
| Tune launch flags | Debug extra-args route (`-ngl`, `-ub`, `--ctx-checkpoints`, `-lv`, `--load-mode`, …), engine-aware; `-ot` is added to `EXTRA_ARG_FLAGS` | Claude with a token | F1 |
| Switch engines | PWA **Ops → Local engine** (drain → swap → smoke → auto-rollback) | owner | F3 |
| See what the engine is doing | PWA Ops card (engine, memory, tok/s, last smoke); logs via PWA and debug | owner | F3 |
| Clear or inspect disk prefix caches | The existing kv-prefix clear/snapshot surfaces, per role | owner | F4 |
| Try a custom engine | PWA engine sub-setting (mainline \| gufo \| …); images arrive by Ops → Update | owner | F5 |
| Back out completely | Switch to Standard, Uninstall the weights in the PWA; the next update removes the stopped container and its image | owner | F1 + F3 |
| Recover from a bad build | Ops → Update's existing rollback; the switch's auto-rollback keeps a local engine serving; Standard is never rebuilt by this plan | automatic | exists + F3 |

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
  figure, the §3 KV/indexer/checkpoint terms. **Hidden from the settings picker** until F3
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

### F3 — The switch and the remap (one wave) ◻️
They land together: a live switch without the remap would send every call to Flash-Next
carrying gpt-oss's sampling and reasoning quirks.
- `local_engine` setting (`standard` | `flash-next`) in the existing settings store — no
  new table, so no new RLS test.
- **Drain** — new code, not an existing gate: `admission.py` is load arithmetic and the
  ledger's `_in_flight` tracks per-process load charges. Close local admission across api
  and worker, wait for in-flight local calls to finish (bounded), then stop the active
  engine, wait for host memory to settle, start the other, health + smoke, persist.
  Discharge ledger rows for the stopped engine's instances. Any failure: stop it, restart
  the previous engine, surface the reason as a box event.
- §4c remap at every entry point; §4a slot affinity (`id_slot` through the provider
  protocol, chosen by task name; slot 2 injected by the jcode proxy).
- §4a reservations: the 512k `--kv-unified` pool (`-np 5 --kv-unified -c 524288`), a
  per-role cap table in the catalog entry, enforced at the router and the jcode proxy (refuse
  or trim over-cap requests, with a clear error), the budget charging the pool once; re-measure
  the pool on the box against the ~74.2 GiB prediction. Tests: caps per role, over-cap refusal,
  caps sum to the pool, pool flags rendered.
- PWA: Ops card (current engine, switch, memory + tok/s readout, last smoke) and the
  per-task "→ Flash-Next" marker — **three mocks each** before code (`PROCESS.md`).
- Tests: orchestration against a fake supervisor (happy path, every rollback branch),
  drain across processes, remap on/off for every task, tier and entry point in §4c, slot
  selection per task, engine-aware jcode power-on, residency never evicting the active
  Flash-Next for an old name, frontend card and marker.

### F4 — Per-role disk prefix cache ◻️
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
  not ship without the sidecar patch validated on the pin.
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
  PWA steps only; the operator-facing switch is documented in F3.

## 9. Open questions

1. Turn llama.cpp's in-RAM prompt cache back on for this engine? We serve `-cram 0`
   because it cost 8 GiB per model; with ~0.55 GiB prefixes, 3–4 GiB holds ~6 recent ones
   (Soot/Silicon measured 27 s → 0.73 s on a hit on qwen4exp), and `--cache-idle-slots`
   needs it. It is host memory the budget must count. Decide after F2.
2. Should the switch be schedulable (e.g. Flash-Next overnight for batch ingest)? Out of
   scope for v1; the endpoint shape should not preclude it.
3. MTP with multiple slots — measure after F3, or leave to F5's engines?

## 10. What the reviews changed

- Wave order: container + provisioning (F1) now precede the spike (F2), which cannot run
  through the debug API without them; the switch and remap merged into one wave (F3).
- Remap moved from llama-swap aliases into the API (aliases cause residency thrash);
  jcode found to reach the model through the API proxy, which pins slot 3.
- Added §4d: four unconditional `local-llm` start paths and the update's pre-build release.
- Memory: QSA indexer corrected from ~0.8 to 3.2 GiB; checkpoints cut to 8 per slot;
  page-cache accounting and the page-cache drop identified as conflicts.
- Evidence relabelled with its measurement conditions; #29028 and mainline-Vulkan vision
  added as explicit F2 checks; speed restated by context depth.
- Slot save/restore found to serialize all model state but clear checkpoints — the sidecar
  patch is mandatory; restore comparison switched to logits within tolerance.
- MTP restated as deferred, not impossible; halogen dropped; slot cap, `--no-mmap`
  supersede and `-ot` allowlist gaps recorded.
