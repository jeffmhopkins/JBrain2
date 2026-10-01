# Flash-Next engine — a switchable second local-LLM stack (Qwen3.8-Flash-Next)

> **Status:** Proposed · **Last verified:** 2026-10-01

Run **Qwen3.8-Flash-Next** (125B MoE, ~6B active, text + image) on the Strix Halo box as
the **only** local LLM, in its own container, behind an owner-operated switch:
**Standard** (today's `local-llm` gateway: gpt-oss-120b, Qwen3.8-27B, …) **or**
**Flash-Next** — never both. While Flash-Next is on, every `local:*` call is remapped to it.
Switching back restores the standard gateway and every per-task pick untouched.

Why: one multimodal model replaces the gpt-oss-120b + Qwen3.8-27B pair (~87 GB of weights
before KV) in ~84 GiB with four full-context, role-pinned slots, and the community has already done
most of the gfx1151 groundwork (§2). Nothing changes for a box that never flips the switch.

## 1. Decisions taken with the owner

| Decision | Choice |
|---|---|
| Container | New `flash-next` compose profile, own image. Never co-resident with `local-llm`. |
| Switching | PWA (Ops), no terminal. Drain → swap → smoke test → automatic rollback on failure. |
| Routing | **Remap all calls.** While active, every `local:*` spec resolves to Flash-Next. |
| Slots | **4 role-pinned slots, each its own 262,144-cell KV** (`-np 4 -c 1048576`, NOT `--kv-unified`). Slots exist to **keep each workload's prefix warm** (prefill reuse), not for concurrency — see §4a. |
| Quant | Unsloth **UD-IQ4_XS** (93.7 GB on disk) + F16 vision projector. |
| Engram (PLE) table | **Memory-mapped from disk**, pinned to CPU (`-ot per_layer_token_embd=CPU`). |
| Engine | Start on mainline llama.cpp (Vulkan); a **custom community engine** is an explicit later wave (W4), adopted only on evidence. |

## 2. Prior work this plan stands on

| Source | What it proves for us |
|---|---|
| llama.cpp [#27742](https://github.com/ggml-org/llama.cpp/pull/27742) (merged 2026-08-27) | Mainline support: GDN hybrid, 512-expert MoE, QSA sparse attention, PLE, **vision via the Qwen3-VL clip path**. |
| llama.cpp [#27941](https://github.com/ggml-org/llama.cpp/pull/27941) (merged 2026-09-01) | Fixes image tokens collapsing onto wrong pooled keys under M-RoPE, and stale indexer keys on copied sequences (multi-slot). Both are hard requirements for us (vision + 4 slots). Our pin (2026-08-25) predates both. |
| [Soot/Silicon, ROCm vs Vulkan](https://www.soothill.io/blog/2026/08/27/qwen38-flash-next-rocm-vulkan-strix-halo/) | Mainline on **Vulkan/RADV** is the safe path: 6.4× ROCm decode, far less GTT, 60-min soak with zero GPU faults. PLE on CPU + mmap; `--no-mmap` exhausted RAM. ~15.6 tok/s single, 24.3 aggregate at 2 slots. |
| [julianmb/haloq38flash](https://github.com/julianmb/haloq38flash) | PLE streamed from SSD works: ~2.5 GB working set, 262k context, 34–42 GB headroom. Warns the official converter's hyper-connection norms were **off by 1.0** — check our GGUF (W0). |
| [abliter8-ai repo](https://github.com/abliter8-ai/qwen-3.8-next-flash-amd-strix-halo) | Vision works on gfx1151 (needs thinking off + ≥1024 output tokens). PLE exceeds Vulkan's 4 GiB buffer limit unless split or kept on CPU. |
| [chm123 gist](https://gist.github.com/chm123/b0b3eec2b7f68e09e5855e23fef44dba) | IQ4_XS single slot: 70 GB GTT at 128k, 74 GB at 256k — consistent with §3. |
| [kyuz0/gufo](https://github.com/kyuz0/gufo) (MIT) | Custom HIP engine from the author of our base toolbox image: 59 tok/s single, **157 tok/s across 8 users**, images, OpenAI API. Early-stage. |
| [halogen-flash-server](https://github.com/peonist-ai/halogen-flash-server) | Custom kernels, 34–46 tok/s, vision tower, OpenAI + Anthropic APIs, quality gates. Proprietary weight format; wants most of the box. |
| [vLLM on Strix Halo](https://www.soothill.io/blog/2026/09/16/qwen38-vllm-disk-ple-strix-halo/) | Disk-backed PLE in vLLM; 3.8× faster long prefill, slower decode. Not a v1 candidate (eager-only, experimental build). |

## 3. Memory budget (derived — W0 replaces it with a measurement)

From `config.json`: 48 layers, 12 full-attention (2 KV heads × 256 head-dim), 36 Gated
DeltaNet; QSA indexer 1 KV head × 128, compress 4.

| Item | GiB |
|---|---|
| Weights without the PLE table (IQ4_XS) | ~60 |
| KV, q8_0, 4 × 262,144 cells, per-slot (12 × 2 × 256 × 2 × 1.0625 B/token) | ~12.8 |
| QSA indexer keys | ~0.8 |
| Recurrent state, 4 slots | ~0.4 |
| Context checkpoints, 16 per slot × 4 (~0.11 each derived, unmeasured) | ~7 |
| Compute buffers | ~1.5 |
| Vision projector (F16) + CLIP workspace (flash attention on) | ~1.4 |
| **Total resident** | **~84** |
| PLE table (file-backed page cache, reclaimable — not budgeted) | ~27 on disk |

Levers if W0 measures it heavier: 8 checkpoints per slot (~−3.5), or 131k per slot
(`-c 524288`, ~−6.8). Both are settings, not rebuilds. A unified pool would save the same
~6.8 but lets one long conversation evict the other slots' prefixes — the thing the slots
are for — so it is rejected (§4a).

## 4. Shape

- **Image:** `deploy/Dockerfile.flash-next` — same kyuz0 Vulkan toolbox base as
  `Dockerfile.local-llm`, llama.cpp pinned to a commit at or after #27941, **llama-swap in
  front** of the single model. Keeping llama-swap means `local_gateway.py`'s `/running`,
  `/api/models/unload`, `/upstream/…/health` contract — and so `residency.py`,
  `warm_keeper.py`, admission — work unchanged.
- **Network identity:** the service carries the network alias `local-llm` on the same
  networks. Only one of the two runs, so anything addressing `local-llm:8080` (the API,
  `jcode`) reaches the active engine with no client change. llama-swap `aliases` map every
  served name from the standard catalog (`gpt-oss-120b`, `qwen3-coder-next`, …) to the
  Flash-Next model, so a direct caller sending an old name is still served.
- **Serving flags:** `--load-mode mmap` (overrides our global `--no-mmap`),
  `-ot per_layer_token_embd=CPU`, `-np 4 -c 1048576`, `-ctk q8_0 -ctv q8_0`,
  `-fa 1`, `-cram 0`, `--ctx-checkpoints 16 --checkpoint-min-step 1024`, `--jinja`, the F16 mmproj with the existing
  `--image-min-tokens` floor. No MTP: four slots and speculation are mutually exclusive
  (`llama_swap_config.py`).
- **Switch:** a `local_engine` setting (`standard` | `flash-next`) in the existing settings
  store. The API orchestrates the swap through the supervisor's existing `/start` and
  `/stop` (the path the ComfyUI toggle uses), and `update-inner.sh` brings back whichever
  engine is selected after **Ops → Update**.
- **Routing:** the remap lives in the router, not the transport. A single-model server
  ignores the `model` name, but the call would carry the *old* model's sampling (gpt-oss:
  top_k 0), its reasoning-floor quirk, and its chat template. While Flash-Next is active,
  `_resolve_live` rewrites any `local:*` result to `local:qwen3.8-flash-next` and applies
  Flash-Next's own sampling and `reasoning_effort` mapping. Stored per-task picks are
  never rewritten.

### 4a. Slots are prefix caches, pinned by role

On a hybrid model a prefix is reused only if a context checkpoint covers the divergence
point; otherwise the whole prompt is re-prefilled (the 27B paid 232 s per turn for this
before checkpoints were raised). Today separate models give separate caches — the persona
lives on gpt-oss, ingest on the 27B. With one model, **slots take over that job**.

The gap: nothing pins a request to a slot today (`llama_swap_config.py`, the `-np` comment).
llama-server picks the longest matching prefix, else the least-recently-used slot — which is
usually the idle slot holding the primed persona. So W3 adds **slot affinity**: the router
sends `id_slot` by workload class, and a busy slot queues its own traffic instead of
stealing another's.

| Slot | Workload | Why it gets its own |
|---|---|---|
| 0 | Interactive persona (jerv, omnibox turns) | The prefill the owner waits on. Never evicted by background work. |
| 1 | Ingest + analysis | Long, stable system prompt reused on every note. |
| 2 | Agents, research, workflow tasks | Heterogeneous; isolates churn from 0 and 1. |
| 3 | jcode + overflow | Coding sessions have their own long prefix. |

Per-slot (non-unified) KV is what makes this hold: each slot owns its 262k, so no slot can
evict another's cache. The disk slot store (`jbrain.llm.kv_prefix`) stays the backstop for a
prefix that loses its slot anyway (restart, a switch, a fifth workload).

Not touched by the switch: `embed`, `tts-stt` (Whisper/Kokoro), `comfyui`.

## 5. Waves

### W0 — On-box spike (measurement, no product surface) ◻️
Build the image and serve the model through the debug API (`docs/runbooks/DEBUG_ACCESS.md`)
with the standard engine stopped. Record, into this doc:
- resident GTT + host RSS + PLE page-cache working set, cold and warm;
- decode tok/s at 1, 2 and 4 concurrent requests; prefill at 8k / 32k / 128k;
- per-checkpoint size (to replace the 0.11 derivation);
- **prefix reuse per slot**: a second turn on a warm slot re-processes only its delta
  (the 27B's measured bar: 33k-token history, 232 s cold → ~8 s warm), and a request
  pinned to slot 1 leaves slot 0's prefix intact;
- correctness: WikiText-2 perplexity vs the #27742 reference (catches the converter norm
  bug), an image-grounding check (catches pre-#27941 collapse), a tool-call round-trip,
  JSON-mode output;
- a 60-minute mixed soak with zero device-loss or GPU reset events.

**Exit gate:** fits in ≤ 90 GiB resident at 4 slots, warm-slot turns reuse their prefix, survives the soak, passes the
correctness checks. Fail → this plan parks with the numbers recorded.

### W1 — Container, profile, weights ◻️
- `deploy/Dockerfile.flash-next`, the `flash-next` compose profile (devices, groups,
  models volume, `local-llm` alias), a llama-swap config generator for the single model
  + aliases.
- A catalog entry `qwen3.8-flash-next` carrying a new **engine** field (which container
  serves it) and a **file-backed weights** figure (the PLE share excluded from
  `footprint_gb`). KV and checkpoints are charged per slot (the existing `slots`
  multiplier) at the measured W0 checkpoint size.
- PWA-driven weight install/uninstall through the existing on-box models path.
- Smoke test (text, image, tool call) in the update path when this engine is selected.
- Tests: config rendering, catalog footprint maths, supervisor/compose wiring.

### W2 — The switch ◻️
- `local_engine` setting + API endpoints: drain in-flight local calls (admission closed,
  wait for zero), stop `local-llm`, wait until host memory has actually settled, start
  `flash-next`, health + smoke, persist. On any failure: stop it, restart `local-llm`,
  surface the reason as a box event.
- Ops card in the PWA: current engine, switch control, the live memory/tok/s readout,
  last smoke result.
- `update-inner.sh` starts the selected engine; a box with the profile never provisioned
  behaves exactly as today.
- Tests: the orchestration against a fake supervisor (happy path, every rollback branch),
  the update-script branch, the frontend card.

### W3 — Remap all calls ◻️
- Router remap of every `local:*` spec while the engine is `flash-next`; per-call
  `spec_override` included; sampling and reasoning effort taken from Flash-Next.
- `context_window_for_spec` caps at 262,144 per request (the pool is larger than any one
  sequence may use).
- `residency.py` budgets one resident model; `warm_keeper` and `kv_prefix` verified
  against a hybrid with 4 slots (the slot-restore patch in `deploy/patches/` is
  re-validated against the new pin, or disabled for this engine).
- Settings screen shows each task's pick with an "→ Flash-Next (engine active)" marker.
- Tests: remap on/off for every task and tier, override precedence, cloud routes untouched.

### W4 — Custom engine track (evidence-gated) ◻️
The container's contract stays fixed — llama-swap in front, OpenAI API behind — so an
engine is a `cmd` swap in the llama-swap config plus an image change, not a re-plumb.
- Candidates, in order: **gufo** (MIT, multi-user throughput, our base image's author),
  **halogen** (fastest single-stream, but its own weight format), the **EngramHalo**
  llama.cpp fork (MTP + depth patches; one-slot only).
- Same W0 harness for each: quality vs the mainline reference (perplexity, token
  agreement, needle at 32k/128k, tool calls, image grounding), throughput at 4
  concurrent, 60-min soak.
- **Adopt only if** it matches mainline quality within noise, beats it on 4-slot
  throughput, and passes the soak. Images pinned by digest, sources by commit — same
  supply-chain rule as `Dockerfile.local-llm`.
- An `engine` sub-setting under Flash-Next (mainline | gufo | …) so the owner can fall
  back without a deploy.

## 6. Risks

- **Single-stream speed on mainline Vulkan** — ~15 tok/s, against gpt-oss's ~31 today.
  Slots are caches, not concurrency, so this is the number that matters; W4 is the answer
  if it bites. (When two slots do run at once they share the GPU: ~24 tok/s aggregate
  measured at 2.)
- **Quality vs gpt-oss-120b is unknown** — no shared public benchmark. W0's correctness
  checks are not an eval; run the existing ingest/analysis eval fixtures before retiring
  anything.
- **Page-cache pressure** — the PLE working set is reclaimable, so pressure shows up as
  slower cold prefill rather than an OOM. W0 measures it under a concurrent ingest load.
- **Upstream churn** — qwen4exp is a month old; follow-up fixes are still landing. The pin
  moves deliberately, each move re-running the W0 checks.
- **The standard engine must stay healthy** — nothing in W1–W3 changes `local-llm`'s
  image or flags; the switch only starts and stops it.

## 7. Open questions

1. Can the W0 on-box spike run entirely through the debug API, or does building the image
   need one host step? If the latter, it is a gap to design out before W1 lands.
2. Should the switch also be schedulable (e.g. Flash-Next overnight for batch ingest)?
   Out of scope for v1; the endpoint shape should not preclude it.
