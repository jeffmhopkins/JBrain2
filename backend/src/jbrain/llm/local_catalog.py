"""Curated catalog of self-hostable local models.

The single source of truth for the OPT-IN local-hosting feature: it names the
models we support running on-box (through the `local-llm` compose profile's
llama-swap + llama.cpp Vulkan gateway), maps each to a `local:<model>` router
spec, and records the provisioning facts the setup script needs to download the
right GGUF weights. Tuned for an AMD Strix Halo class box (large unified memory,
~256 GB/s bandwidth) where MoE / small-dense models with a small active-param
set are the only ones that run at interactive speed.

Two consumers read this:
  - the app (jbrain.llm.providers) surfaces enabled models as settings choices;
  - scripts/local-llm-setup.sh reads `python -m jbrain.llm.local_catalog <ids>`
    for the JSON download manifest.

Nothing here changes default routing — every default stays on the cloud
providers (jbrain.llm.router.TASK_DEFAULTS). A model is reachable only after an
operator enables local hosting and selects it.
"""

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from jbrain.llm.types import Sampling

# The router spec for a local model is always "local:<served_model>": the local
# provider client posts <served_model> as the OpenAI `model`, and llama-swap
# routes/loads on that name. Keep served names matching the llama-swap config the
# setup script generates.
LOCAL_PROVIDER = "local"


@dataclass(frozen=True)
class LocalModel:
    """One self-hostable model and how to provision it."""

    id: str  # stable settings-choice id (also the UI provider id)
    label: str  # human label for the settings screen
    served_model: str  # name the local gateway serves it under
    tiers: tuple[str, ...]  # capability tiers it can credibly serve
    supports_vision: bool
    supports_tools: bool
    # In the recommended default-enabled set the install prompt offers first.
    recommended: bool
    # Provisioning hints for scripts/local-llm-setup.sh.
    hf_repo: str
    gguf_include: str  # huggingface-cli --include glob for the weights
    mmproj_include: str | None  # vision projector glob, or None for text-only
    quant: str
    size_gb: float
    note: str = ""
    # Emits a `reasoning_content` channel and honors a reasoning setting (gpt-oss
    # harmony effort / GLM thinking / a Qwen hybrid think toggle). Drives the settings
    # effort control and lets the router send a level to this model; default False
    # (plain Instruct variants and Llama here are non-thinking).
    supports_reasoning: bool = False
    # llama-server `--reasoning-format` for a model that emits its thinking inline as
    # `<think>…</think>` (DeepSeek-R1 / Qwen3-Thinking / a Qwen hybrid with thinking on):
    # "deepseek" makes llama.cpp parse those tags OUT of `content` into a separate
    # `reasoning_content` channel, which OpenAI-compatible clients (grok build) read as
    # the reasoning trace. Empty = leave llama.cpp's default (`auto`) — correct for
    # harmony/GLM reasoners, whose template `auto` handles.
    reasoning_format: str = ""
    # A Qwen-style HYBRID reasoner: thinking is a chat-template toggle
    # (`enable_thinking`), not a `reasoning_effort` level. The adapter maps the routed
    # level onto that toggle instead of sending `reasoning_effort` (which the Qwen
    # template ignores): "none" → `enable_thinking=false` (a real "reasoning off"),
    # any other level → thinking on. False for harmony/grok/GLM (they take the effort
    # verbatim) and for always-on `<think>` checkpoints (which have no off switch).
    hybrid_thinking: bool = False
    # Extra `llama-server` flags appended verbatim to the gateway command
    # (jbrain.llm.llama_swap_config). Carries the MTP self-speculative-decoding flags
    # (`--spec-type draft-mtp …`) for the MTP variant; empty for every model whose
    # serving is fully described by the fields above.
    extra_server_args: tuple[str, ...] = ()
    # The context window the gateway serves this model with (llama-server's `-c`)
    # ABSENT an operator override. The single source of truth: scripts/local-llm-setup.sh
    # stamps this into the llama-swap config, and the router reports it to the PWA's
    # context-usage meter. Kept conservative on this memory-bound box — the operator
    # raises it per-model up to `native_context_window` when the KV cache fits.
    context_window: int = 32768
    # The model's native (architectural) maximum context — the CEILING the operator
    # may raise the served window to from the settings drawer. 0 means "no headroom
    # above context_window" (the served default is already the max we expose). The
    # served default stays small for memory; this opens the door to the full window
    # the weights support, with the drawer's KV-cache estimate as the guardrail.
    native_context_window: int = 0
    # Rough KV-cache size (GB) at the model's full 131072-token window — an ESTIMATE
    # (not a measurement) the settings drawer's memory bar uses to size the context
    # portion of each model's segment, scaled linearly by the configured window.
    # gpt-oss is low (alternating sliding-window attention); dense models are higher.
    kv_gb_per_128k: float = 0.0
    # The vendor's RECOMMENDED sampling for this model (docs/reference/MODEL_PROMPTING.md).
    # The router applies it to every call so the model runs at its card's values instead of
    # llama.cpp's engine defaults (temp 0.8 / top_p 0.95 / top_k 40 / min_p 0.1 — which match
    # no card and, for gpt-oss, actively prune tokens the model wants). Empty for a model
    # whose vendor publishes none. A per-task `.prompt` `sampling:` override merges on top.
    sampling: Sampling = field(default_factory=Sampling)
    # A hybrid reasoner whose card gives DIFFERENT sampling for thinking vs non-thinking mode
    # (the Qwen hybrids) sets this to the thinking-mode values; the router picks it when the
    # model is generating a thinking trace, else `sampling`. None when the card is unified
    # (Nemotron/GLM) or the model has no thinking mode — `sampling` then applies always.
    sampling_thinking: Sampling | None = None

    @property
    def spec(self) -> str:
        return f"{LOCAL_PROVIDER}:{self.served_model}"

    @property
    def max_context_window(self) -> int:
        """The largest `-c` the operator may select for this model: its native
        window when recorded, else the served default (no headroom above it)."""
        return self.native_context_window or self.context_window


# Order is the order the settings screen and install prompt present them.
CATALOG: tuple[LocalModel, ...] = (
    LocalModel(
        id="qwen3-vl-30b",
        label="Qwen3-VL 30B · vision",
        served_model="qwen3-vl-30b-a3b",
        # Qwen VL Instruct card values; presence_penalty 1.5 is Qwen's headline knob for
        # the repetition/endless-loop failure VL models hit on dense images and long OCR.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        tiers=("vision", "low"),
        supports_vision=True,
        supports_tools=True,
        recommended=True,
        hf_repo="Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF",
        gguf_include="*Q8_0*.gguf",
        mmproj_include="mmproj*.gguf",
        quant="Q8_0",
        size_gb=32.0,
        note="Vision + a capable cheap text model; Q8 preserves OCR fidelity.",
        # Native 256k (expandable to 1M upstream); serves the gateway default.
        native_context_window=262144,
        kv_gb_per_128k=6.0,
    ),
    LocalModel(
        id="qwen3-vl-30b-q4",
        label="Qwen3-VL 30B · vision (Q4, memory-saver)",
        served_model="qwen3-vl-30b-a3b-q4",
        # Same model as the Q8 twin — same VL card sampling.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        tiers=("vision", "low"),
        supports_vision=True,
        supports_tools=True,
        # Opt-in memory-saver twin of the recommended Q8 entry — same model and repo, half
        # the weights, so it co-resides beside gpt-oss-120b with real headroom under the
        # free-RAM floor instead of evicting it. A plain local-hosting enable never pulls it
        # (Q8 is the recommended default); the operator installs it when co-residence matters
        # more than the last bit of OCR fidelity.
        recommended=False,
        hf_repo="Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        # Keep the F16 projector so the vision tower stays full precision even at Q4 weights
        # (fine text degrades first under quantization, docs/reference/MODEL_PROMPTING.md). This
        # repo names it `mmproj-Qwen3VL-30B-A3B-Instruct-F16.gguf`, so a glob (not the bare
        # `mmproj-F16.gguf` name Unsloth repos use) is required — it matches the F16 projector
        # while skipping the redundant `...-Q8_0.gguf` one. The include feeds BOTH `hf download
        # --include` and resolve_weight's glob, so a mismatch pulls no projector and the install
        # sticks partway (bar caps < 100%, resolve fails "download incomplete").
        mmproj_include="mmproj*F16.gguf",
        quant="Q4_K_M",
        # GiB on disk (the catalog's unit): HF lists Q4_K_M at ~18.6 decimal GB (~17.3 GiB)
        # plus the ~1.0 GiB F16 projector = ~18.3 GiB — an ESTIMATE until measured on-box,
        # kept at the GiB (not decimal-GB) sum so the install bar doesn't cap early (the
        # Nemotron note). ~14 GiB lighter than the Q8 entry: that gap is the co-residence win.
        size_gb=18.3,
        note="Vision + a capable cheap text model at Q4_K_M — the memory-saver twin of the "
        "recommended Q8 entry. ~18 GiB vs ~32, so it co-resides beside gpt-oss-120b with room "
        "to spare under the free-RAM floor rather than evicting it. The projector stays F16, "
        "but the Q4 weights trade some OCR fidelity on dense/small text — prefer the Q8 entry "
        "when transcription accuracy matters, this one when co-residence headroom does.",
        # Same architecture as the Q8 entry: native 256k, served at the gateway default, and
        # the same KV estimate (quantizing the weights doesn't shrink the KV cache).
        native_context_window=262144,
        kv_gb_per_128k=6.0,
    ),
    LocalModel(
        id="llama-4-scout-int4",
        label="Llama 4 Scout · vision (int4)",
        served_model="llama-4-scout-int4",
        # Meta ships temp 0.6 / top_p 0.9 (generation_config.json); min_p 0.01 is Unsloth's
        # community default for Scout. Meta gives no top_k, so disable it (0) and sample from
        # pure temperature/top_p — the way the card was validated — not llama.cpp's top_k 40.
        sampling=Sampling(temperature=0.6, top_p=0.9, top_k=0, min_p=0.01),
        tiers=("vision", "low"),
        supports_vision=True,
        supports_tools=True,
        # Opt-in alternate to qwen3-vl-30b — a plain local-hosting enable never pulls
        # its ~59 GB, and adding it leaves the already-selected models untouched.
        recommended=False,
        hf_repo="unsloth/Llama-4-Scout-17B-16E-Instruct-GGUF",
        # Unsloth nests the int4 dynamic quant's shards in a UD-Q4_K_XL/ subdir (two
        # shards); the recursive glob matches each shard path and resolve_weight follows
        # the -00001-of-00002 head to the rest.
        gguf_include="*UD-Q4_K_XL*.gguf",
        # The F16 vision projector lives at the repo root. Name it exactly (not mmproj*)
        # so the pull doesn't also grab the redundant BF16 projector beside it.
        mmproj_include="mmproj-F16.gguf",
        quant="UD-Q4_K_XL",
        # GiB on disk (the catalog's unit): the two UD-Q4_K_XL shards (~57.8 GiB from
        # HF's 49.6 + 12.4 decimal-GB listing) plus the ~1.6 GiB F16 projector. An
        # ESTIMATE until measured on-box; kept at the GiB (not the decimal-GB) sum so the
        # install bar doesn't cap early and read as a stall (see the Nemotron note).
        size_gb=59.4,
        note="109B MoE, 17B active over 16 experts — Meta's multimodal (text + vision) "
        "Scout at Unsloth's int4 dynamic quant. A fast vision alternate to qwen3-vl-30b "
        "(more total params, similar active cost); co-resides beside a small model on a "
        "128 GB box. Non-thinking (no reasoning channel). Vision needs a recent llama.cpp "
        "build with Llama 4 mmproj support (in the multimodal set upstream).",
        # Scout's native window is architecturally huge (10M via iRoPE), far beyond what
        # this box can hold. Expose a 1M ceiling (picker steps at 500k and 1M): ~59 GB of
        # weights plus this model's KV estimate keeps 1M inside a 128 GB box (~105 GB),
        # while the steps above that would exceed it — so 1M is the largest window an
        # operator can realistically serve here. The drawer's KV bar is the guardrail;
        # serves the conservative gateway default until raised.
        native_context_window=1_000_000,
        # Interleaved local/global attention keeps the KV cache moderate for a model this
        # size; matches the other vision MoE's conservative guardrail estimate.
        kv_gb_per_128k=6.0,
    ),
    LocalModel(
        id="gpt-oss-120b",
        label="GPT-OSS 120B · reasoning",
        served_model="gpt-oss-120b",
        # OpenAI's guidance: sample from the model's own distribution — temp 1.0, top_p 1.0,
        # top_k 0 (off). min_p 0.0 is CRITICAL here: llama.cpp's default min_p 0.1 would prune
        # low-probability tokens gpt-oss wants, fighting the model. Penalties stay off.
        sampling=Sampling(temperature=1.0, top_p=1.0, top_k=0, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=True,
        hf_repo="ggml-org/gpt-oss-120b-GGUF",
        gguf_include="*mxfp4*.gguf",
        mmproj_include=None,
        quant="MXFP4",
        size_gb=59.0,
        note="Strongest open reasoning that still runs fast here (~31 t/s).",
        supports_reasoning=True,
        # The model's full native window. Its alternating sliding-window attention
        # keeps the f16 KV cache modest (~half the layers grow with context), so
        # 128k fits the box's unified memory beside the MXFP4 weights.
        context_window=131072,
        kv_gb_per_128k=4.5,
    ),
    LocalModel(
        id="nemotron-3-super-120b",
        label="Nemotron 3 Super 120B · reasoning (alt)",
        served_model="nemotron-3-super-120b",
        # NVIDIA's card prescribes temp 1.0 / top_p 0.95 across ALL modes (reasoning, tools,
        # chat) — no thinking/non-thinking split, so no sampling_thinking. top_k/min_p disabled
        # to honour the pure temp/top_p sampling the card documents.
        sampling=Sampling(temperature=1.0, top_p=0.95, top_k=0, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/NVIDIA-Nemotron-3-Super-120B-A12B-GGUF",
        # Sharded into a UD-Q4_K_XL/ subdir (same shape as the 235B's UD-Q3_K_XL/ and
        # the coder's Q8_0/ subdirs); the glob matches each shard path and the config
        # generator resolves them.
        gguf_include="*UD-Q4_K_XL*.gguf",
        mmproj_include=None,
        quant="UD-Q4_K_XL",
        # GiB on disk (the catalog's size unit — local_weights measures in GiB), summed
        # from the three real UD-Q4_K_XL shards (0.01 + 49.91 + 33.86 GB = 78.0 GiB). NOT
        # HuggingFace's 83.8 decimal-GB listing: that overshoots by ~7%, so the install
        # bar (download_gb_GiB / size_gb) would cap near 93% and read as a stall.
        size_gb=78.0,
        note="120B MoE, 12B active — NVIDIA's US-made agentic (tool-use + RAG) reasoner, "
        "an alternate to gpt-oss-120b in the high tier. Hybrid LatentMoE (interleaved "
        "Mamba-2 + MoE + select attention): the constant Mamba state keeps the KV cache "
        "small, so it holds long context far better than a dense 120B. ~78 GiB at "
        "UD-Q4_K_XL — co-resides only with a small low-tier model on a 128 GB box, else "
        "standalone with a cold load on every switch. A HYBRID reasoner: thinking is the "
        "enable_thinking chat-template toggle, set per task in LLM Settings ('none' runs "
        "it as a snappy Instruct model). Emits <think> traces, so it needs a recent "
        "llama.cpp build that serves the LatentMoE arch and supports --reasoning-format.",
        supports_reasoning=True,
        # Emits <think>…</think> inline (token ids 12/13); deepseek format splits it onto
        # the reasoning channel like the Qwen hybrids and the Next-Thinking checkpoint.
        reasoning_format="deepseek",
        # enable_thinking chat-template toggle (not a reasoning_effort level), so the
        # adapter maps the routed level onto it — same path as the Qwen hybrids.
        hybrid_thinking=True,
        # Native 1M context; serves the conservative gateway default. The Mamba-2 hybrid's
        # constant state makes the KV term small, so raising the window is cheap here — the
        # drawer's linear KV estimate overcounts the non-growing Mamba layers, so it is a
        # conservative guardrail rather than a true measure.
        native_context_window=1048576,
        kv_gb_per_128k=3.0,
    ),
    LocalModel(
        id="nemotron-3.5-lightning-30b",
        label="Nemotron 3.5 Lightning 30B · reasoning (alt)",
        served_model="nemotron-3.5-lightning-30b",
        # Same NVIDIA guidance as the Super 120B: temp 1.0 / top_p 0.95, unified across modes.
        sampling=Sampling(temperature=1.0, top_p=0.95, top_k=0, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="ggml-org/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-GGUF",
        gguf_include="*Q8_0*.gguf",
        mmproj_include=None,
        quant="Q8_0",
        # GiB on disk (the catalog's unit): the single Q8_0 weight, ~32.6 GiB from HF's 35
        # decimal-GB listing. ESTIMATE until measured on-box; kept at the GiB (not the
        # decimal-GB) figure so the install bar doesn't cap early.
        size_gb=32.6,
        note="30B MoE, 3B active — NVIDIA's Nemotron 3.5 Lightning at 8-bit (near-lossless), a "
        "fast high-tier alt. Hybrid Mamba-2 + MoE + attention arch: the constant Mamba state "
        "keeps the KV cache tiny, so it holds long context far better than a dense model and "
        "stays fast even at Q8 (only ~3B active is read per token, so quant barely moves speed "
        "here — Q8 for the quality). Co-resides beside gpt-oss-120b. A HYBRID reasoner: "
        "thinking is the enable_thinking chat-template toggle, set per task in LLM Settings "
        "('none' runs it as a snappy Instruct model). Emits <think> traces, so it needs a "
        "recent llama.cpp build that serves the hybrid Mamba arch and supports "
        "--reasoning-format.",
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        # Native 1M context; serves the conservative gateway default. The Mamba-2 hybrid's
        # constant state makes the KV term small, so raising the window is cheap here — the
        # drawer's linear KV estimate overcounts the non-growing Mamba layers (a conservative
        # guardrail, not a true measure).
        native_context_window=1048576,
        kv_gb_per_128k=3.0,
    ),
    LocalModel(
        id="qwen3.8-27b",
        label="Qwen3.8 27B · vision + reasoning (Q8)",
        served_model="qwen3.8-27b",
        # The Qwen hybrid sampling split: non-thinking (Instruct) temp 0.7 / top_p 0.8 /
        # presence_penalty 1.5; thinking temp 1.0 / top_p 0.95, no presence penalty. The router
        # picks by whether thinking is on for the call.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        sampling_thinking=Sampling(temperature=1.0, top_p=0.95, top_k=20, min_p=0.0),
        tiers=("vision", "high"),
        supports_vision=True,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3.8-27B-GGUF",
        gguf_include="*Q8_0*.gguf",
        # Keep the F16 projector (fine text degrades first under quantization). Name it EXACTLY:
        # a `mmproj*F16.gguf` glob would also match this repo's `mmproj-BF16.gguf` (it ends in
        # `F16.gguf` too), so the exact name pulls only the F16 one and skips the BF16 beside it.
        mmproj_include="mmproj-F16.gguf",
        # Grounding (box-a-thing-in-my-photo) is unreliable when the projector is fed
        # too few visual tokens: llama.cpp sizes the image budget from the model
        # metadata and a modest photo can land well under what Qwen-VL needs to
        # localize. Floor it. Without this the model still answers confidently — it
        # just answers with the wrong box (AGENT_CANVAS_PLAN §5.4).
        extra_server_args=("--image-min-tokens", "1024"),
        quant="Q8_0",
        # GiB on disk (the catalog's unit): the single Q8_0 weight (~27.0 GiB from HF's 29
        # decimal-GB listing) plus the ~0.86 GiB F16 projector. ESTIMATE until measured on-box;
        # kept at the GiB (not decimal-GB) sum so the install bar doesn't cap early.
        size_gb=27.9,
        note="Dense 27B (text + vision, image & video) — Qwen3.8's hybrid reasoner at 8-bit "
        "(near-lossless), the compact vision + high-tier entry. "
        "A DENSE 27B is memory-bandwidth-bound on this box, so Q8 runs ~7 t/s "
        "(quality-first / batch) — prefer the Q4 twin for interactive use. Thinking is the "
        "enable_thinking chat-template toggle, set per task in LLM Settings ('none' runs it as a "
        "snappy Instruct model). ~28 GiB, co-resides beside gpt-oss-120b. Needs a llama.cpp "
        "build with --reasoning-format and Qwen3.8 mmproj support.",
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        # Native 262k (YaRN-extensible to ~1M upstream); serves the conservative gateway
        # default with the native window as the picker's ceiling.
        native_context_window=262144,
        kv_gb_per_128k=6.0,
    ),
    LocalModel(
        id="qwen3.8-27b-q4",
        label="Qwen3.8 27B · vision + reasoning (Q4, interactive)",
        served_model="qwen3.8-27b-q4",
        # Same model as the Q8 twin — same hybrid thinking/non-thinking sampling split.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        sampling_thinking=Sampling(temperature=1.0, top_p=0.95, top_k=20, min_p=0.0),
        tiers=("vision", "high"),
        supports_vision=True,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3.8-27B-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        # Same F16 projector as the Q8 twin (kept full precision even at Q4 weights). Exact
        # name, not `mmproj*F16.gguf`, so it doesn't also pull the `mmproj-BF16.gguf` beside it.
        mmproj_include="mmproj-F16.gguf",
        # Same grounding floor as the Q8 twin — see the note there.
        extra_server_args=("--image-min-tokens", "1024"),
        quant="Q4_K_M",
        # GiB on disk: the Q4_K_M weight (~15.9 GiB from HF's 17.1 decimal-GB listing) plus the
        # ~0.86 GiB F16 projector. ESTIMATE until measured on-box.
        size_gb=16.8,
        note="Dense 27B (text + vision) hybrid reasoner at Q4_K_M — the INTERACTIVE twin of the "
        "Q8 entry, same model + repo. A dense 27B is bandwidth-bound here, so Q4 roughly doubles "
        "Q8's throughput: the better daily driver, at some quality cost the Q8 twin keeps. ~17 "
        "GiB, so it co-resides beside gpt-oss-120b with wide headroom. Projector stays F16 "
        "(fine-text OCR degrades first under weight quantization). Thinking is the "
        "enable_thinking toggle, set per task in LLM Settings.",
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        native_context_window=262144,
        kv_gb_per_128k=6.0,
    ),
    LocalModel(
        id="qwen3.8-27b-mtp",
        label="Qwen3.8 27B · MTP (faster text, no vision)",
        served_model="qwen3.8-27b-mtp",
        # Same model + sampling as the Q4 twin — MTP changes only HOW it's served, not the model.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        sampling_thinking=Sampling(temperature=1.0, top_p=0.95, top_k=20, min_p=0.0),
        # TEXT-ONLY: llama.cpp's MTP path can't run alongside the vision projector (`--mmproj`),
        # so this variant drops the "vision" tier and serves text/reasoning only.
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        # Same Q4_K_M weights as qwen3.8-27b-q4 — unsloth's GGUF already ships the MTP head
        # (the `blk.*.nextn.*` / `qwen35.nextn_predict_layers` tensors), which llama.cpp IGNORES
        # without the flag and USES with `--spec-type draft-mtp`. So no separate MTP repo and no
        # `--model-draft` file — the speedup is the same weights served differently. (A distinct
        # served name means a separate ~16 GiB copy on disk from the q4 twin; install whichever
        # serving mode you want.)
        hf_repo="unsloth/Qwen3.8-27B-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        # No projector — MTP is text-only (see the tiers note), and skipping `--mmproj` also
        # dodges the RADV mmproj instability. So NO mmproj_include.
        mmproj_include=None,
        quant="Q4_K_M",
        # Just the Q4_K_M weight (~15.9 GiB), no projector — a hair lighter than the q4 twin.
        size_gb=15.9,
        note="Dense 27B hybrid reasoner served with MTP (multi-token prediction / self-"
        "speculative decoding) for ~1.4-2x faster TEXT generation — the same unsloth Q4_K_M "
        "weights as qwen3.8-27b-q4, which already carry the MTP head; the --spec-type draft-mtp "
        "flags turn it on. TEXT-ONLY: llama.cpp MTP can't serve the vision projector or a second "
        "parallel slot, so this variant drops vision and must run single-slot (-np 1). It only "
        "helps DECODE (prompt processing is a touch slower). Opt-in for a fast text/reasoning "
        "driver when you don't need Qwen3.8's vision. CAVEAT: MTP on the Vulkan/RADV gateway is "
        "build-fragile (it crashed on gfx1151 at llama.cpp's MTP launch and still has open Vulkan "
        "MTP bugs) — since the gateway tracks llama.cpp master, verify it loads + generates after "
        "an update; if a build regresses, uninstall this entry and use qwen3.8-27b-q4.",
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        # Self-speculation off the model's own MTP head — no separate draft model. n-max 2 is the
        # AMD/bandwidth-bound sweet spot (acceptance falls as it rises; llama.cpp's own default
        # is 3). --spec-type/-n-max ride extra_server_args, appended verbatim to the gateway cmd.
        extra_server_args=("--spec-type", "draft-mtp", "--spec-draft-n-max", "2"),
        native_context_window=262144,
        kv_gb_per_128k=6.0,
    ),
    LocalModel(
        id="qwen3-coder-next",
        label="Qwen3-Coder-Next 80B · coding agent (Q4)",
        served_model="qwen3-coder-next",
        # Qwen3-Coder-Next card: temp 1.0 / top_p 0.95 / top_k 40 (the Coder line uses 40, not
        # the 20 the rest of the Qwen family uses). Agentic coder, no separate thinking mode.
        sampling=Sampling(temperature=1.0, top_p=0.95, top_k=40, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        # Opt-in: code mode (jcode) provisions this via scripts/jcode-setup.sh; it is
        # NOT recommended, so a plain local-hosting enable never pulls its ~50 GB.
        recommended=False,
        hf_repo="unsloth/Qwen3-Coder-Next-GGUF",
        gguf_include="*UD-Q4_K_XL*.gguf",
        mmproj_include=None,
        quant="UD-Q4_K_XL",
        size_gb=49.6,
        note="80B MoE, 3B active — agentic coder (~70% SWE-Bench Verified); the model "
        "behind code mode (jcode). Co-resides beside another large model. Uses the "
        "Qwen3-Next hybrid-attention arch — confirm the gateway's llama.cpp "
        "build supports it (a recent build fixed a Qwen looping bug). Served at its full "
        "native 256k window: jcode's terminal `claude` wants the whole context, and the "
        "light hybrid-attention KV (~10 GB at 256k) fits beside the weights here.",
        # Code mode wants the whole window — serve the full native 256k (not the small
        # memory-bound default) so the coder gets full context.
        context_window=262144,
        native_context_window=262144,
        kv_gb_per_128k=5.0,
    ),
    LocalModel(
        id="qwen3-coder-next-q8",
        label="Qwen3-Coder-Next 80B · coding agent (Q8)",
        served_model="qwen3-coder-next-q8",
        # Same model as the Q4 twin — same Coder card sampling.
        sampling=Sampling(temperature=1.0, top_p=0.95, top_k=40, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        # Opt-in, standalone high-fidelity coder for a box that PINS one jcode model.
        # Not recommended (a plain local-hosting enable never pulls its ~85 GB).
        recommended=False,
        hf_repo="unsloth/Qwen3-Coder-Next-GGUF",
        # Sharded into a Q8_0/ subdir; the glob matches each shard's path (same shape
        # as the 235B's UD-Q3_K_XL/ subdir). The config generator resolves the shards.
        gguf_include="*Q8_0*.gguf",
        mmproj_include=None,
        quant="Q8_0",
        # ~85 GB (8-bit of 80B) — an ESTIMATE until measured on disk; the install bar
        # tolerates it. Runs STANDALONE on a 128 GB box: it will NOT co-reside with
        # gpt-oss-120b, so expect a cold load on every switch and a tight context
        # budget beside the weights. If the gateway's llama.cpp build won't load Q8 on
        # gfx1151, fall back to the Q4 entry above.
        size_gb=85.0,
        note="80B MoE, 3B active — agentic coder at 8-bit (near-lossless) for jcode "
        "pinned to one model. Standalone only on a 128 GB box; cold-loads on switch. "
        "Same hybrid-attention arch — confirm the llama.cpp build serves Q8 on gfx1151. "
        "Served at full native 256k (standalone, so the window has the most room).",
        # Code mode wants the whole window — serve the full native 256k.
        context_window=262144,
        native_context_window=262144,
        kv_gb_per_128k=5.0,
    ),
    LocalModel(
        id="glm-4.5-air",
        label="GLM-4.5 Air · reasoning (alt)",
        served_model="glm-4.5-air",
        # Z.ai's GLM-4.5-series API default: temp 0.6 / top_p 0.95 (the 4.5 series defaults to
        # 0.6, distinct from 4.6+). No official top_k/min_p, so disable them for pure temp/top_p.
        sampling=Sampling(temperature=0.6, top_p=0.95, top_k=0, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/GLM-4.5-Air-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        mmproj_include=None,
        quant="Q4_K_M",
        size_gb=70.0,
        note="70B-class quality, MoE-fast; alternate high tier.",
        supports_reasoning=True,
        # Native 128k; serves the gateway default.
        native_context_window=131072,
        kv_gb_per_128k=5.0,
    ),
    LocalModel(
        id="qwen3-30b-a3b",
        label="Qwen3 30B · lightweight",
        served_model="qwen3-30b-a3b",
        # The Instruct-2507 (non-thinking) checkpoint — Qwen's non-thinking values:
        # temp 0.7 / top_p 0.8 / top_k 20. presence_penalty is optional here, left off.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0),
        tiers=("low",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="Qwen/Qwen3-30B-A3B-Instruct-2507-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        mmproj_include=None,
        quant="Q4_K_M",
        size_gb=18.0,
        note="Snappy text-only one-shots; swap-in for the low tier.",
        # Native 256k (Instruct-2507); serves the gateway default.
        native_context_window=262144,
        kv_gb_per_128k=3.2,
    ),
    LocalModel(
        id="qwen3.5-0.8b",
        label="Qwen3.5 0.8B · tiny",
        served_model="qwen3.5-0.8b",
        # Hybrid, and the card warns this tiny one is especially loop-prone — hence the high
        # presence_penalty (2.0 non-thinking, 1.5 thinking) as the mitigation.
        sampling=Sampling(temperature=1.0, top_p=1.0, top_k=20, min_p=0.0, presence_penalty=2.0),
        sampling_thinking=Sampling(
            temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=1.5
        ),
        tiers=("low",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3.5-0.8B-GGUF",
        gguf_include="*Q8_0*.gguf",
        mmproj_include=None,
        quant="Q8_0",
        # 8-bit of a 0.8B dense model — near-lossless at trivial cost. On this
        # memory-bandwidth-bound box the Q4 savings (~0.3 GB) buy nothing, so the
        # tiny model keeps its quality rather than shaving already-thin headroom.
        size_gb=0.9,
        note="Tiniest catalog model — a fast, cheap worker for side projects that "
        "don't need to be smart (classification, extraction, short one-shots). "
        "Newer generation than qwen3-30b: a hybrid reasoner whose thinking is a "
        "chat-template toggle. Its level is set per task in LLM Settings (pick "
        "'none' to run it as a snappy Instruct model, or a thinking level for the "
        "extra depth). Loads instantly and co-resides beside anything.",
        # A hybrid Qwen: emits <think> when thinking is on, so parse it onto the
        # reasoning channel (deepseek) and drive the on/off via the hybrid toggle.
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        # Native 256k; serves the conservative gateway default like the other low-tier
        # entries. Its KV cache is negligible at this size, so a big -c is cheap here.
        native_context_window=262144,
        kv_gb_per_128k=0.5,
    ),
    LocalModel(
        id="qwen3.5-4b",
        label="Qwen3.5 4B · small",
        served_model="qwen3.5-4b",
        # Hybrid: non-thinking temp 0.7 / top_p 0.8, thinking temp 1.0 / top_p 0.95; both keep
        # presence_penalty 1.5 (Qwen3.5's anti-loop default), top_k 20.
        sampling=Sampling(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5),
        sampling_thinking=Sampling(
            temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=1.5
        ),
        tiers=("low",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3.5-4B-GGUF",
        gguf_include="*Q8_0*.gguf",
        mmproj_include=None,
        quant="Q8_0",
        # 8-bit of a 4B dense model (~4.3 GB) — the step up from 0.8b when the tiny
        # model is too weak but you still want an instant, low-footprint local worker.
        size_gb=4.3,
        note="Small dense model — noticeably smarter than qwen3.5-0.8b while still "
        "loading instantly and co-residing beside anything. A solid low-tier daily "
        "driver for local one-shots. A hybrid reasoner: set its thinking level per "
        "task in LLM Settings ('none' runs it as a snappy Instruct model); tools on.",
        # A hybrid Qwen: emits <think> when thinking is on, so parse it onto the
        # reasoning channel (deepseek) and drive the on/off via the hybrid toggle.
        supports_reasoning=True,
        reasoning_format="deepseek",
        hybrid_thinking=True,
        # Native 256k; serves the conservative gateway default like the other low-tier
        # entries. A dense 4B KV stays cheap here, so a big -c is affordable.
        native_context_window=262144,
        kv_gb_per_128k=1.2,
    ),
    LocalModel(
        id="llama-3.3-70b",
        label="Llama 3.3 70B · batch (slow)",
        served_model="llama-3.3-70b",
        # Meta's shipped default (generation_config.json): temp 0.6 / top_p 0.9. No Meta
        # top_k/min_p, so disable them for pure temp/top_p sampling.
        sampling=Sampling(temperature=0.6, top_p=0.9, top_k=0, min_p=0.0),
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="bartowski/Llama-3.3-70B-Instruct-GGUF",
        gguf_include="*Q4_K_M*.gguf",
        mmproj_include=None,
        quant="Q4_K_M",
        size_gb=40.0,
        note="Dense 70B — high quality but only ~5 t/s here; batch use only.",
        # Native 128k; serves the gateway default. Dense KV — a big -c costs the most here.
        native_context_window=131072,
        kv_gb_per_128k=8.0,
    ),
)

_BY_ID = {m.id: m for m in CATALOG}

# DECOMMISSIONED model ids — entries removed from the catalog above that must be
# actively UNINSTALLED from any box still holding them, not merely dropped from the
# picker. deploy/local-models-sync.sh reads this (via `retired_ids()`) and, for any
# retired id actually present on a box, force-subtracts it from the served roster and
# prunes its weights on the next update — the no-shell path to retiring a model the
# operator can't `rm` themselves (CLAUDE.md #10). A box that never installed one is
# unaffected (it stays off the fast-path only while a retired weight lingers).
# Superseded by qwen3.8-27b / -q4 (its newer-generation twins), so nothing routes here.
RETIRED_IDS: tuple[str, ...] = ("qwen3.6-27b", "qwen3.6-27b-q4")

# Served-model names that emit reasoning + honor `reasoning_effort`. The router
# consults this to decide whether a `local:<served_model>` call may carry an effort
# (and the loop/UI surface the thinking trace only for these).
REASONING_SERVED_MODELS: frozenset[str] = frozenset(
    m.served_model for m in CATALOG if m.supports_reasoning
)


_BY_SERVED = {m.served_model: m for m in CATALOG}

# Fallback window for a `local:<served_model>` spec we don't recognize (an operator
# serving a model outside the catalog): the gateway's default `-c` for the set.
DEFAULT_LOCAL_CONTEXT_WINDOW = 32768


def get(model_id: str) -> LocalModel | None:
    return _BY_ID.get(model_id)


def context_window(served_model: str) -> int:
    """The context window a `local:<served_model>` runs with — the catalog value
    when known, else the gateway's default. Drives the PWA's context-usage meter."""
    model = _BY_SERVED.get(served_model)
    return model.context_window if model else DEFAULT_LOCAL_CONTEXT_WINDOW


def supports_vision(served_model: str) -> bool:
    """Whether a `local:<served_model>` can accept image content. False for a served
    name outside the catalog — the safe default that drops image bytes rather than
    sending them to a model with no vision projector (which errors at the gateway)."""
    model = _BY_SERVED.get(served_model)
    return model.supports_vision if model else False


def id_for_served(served_model: str) -> str | None:
    """Catalog id for a served-model name (the gateway loads/reports served names,
    but per-model settings — overrides, staging — key off the catalog id), or None
    for a served name outside the catalog."""
    model = _BY_SERVED.get(served_model)
    return model.id if model else None


def get_by_served(served_model: str) -> LocalModel | None:
    """The catalog entry a gateway `served_model` name maps to, or None for a served
    name outside the catalog (an operator serving something unlisted)."""
    return _BY_SERVED.get(served_model)


# The context length KV estimates are normalized to: kv_gb_per_128k is the KV cache at
# 131072 tokens, and KV scales linearly with the served window.
_KV_REFERENCE_TOKENS = 131072


def footprint_gb(
    model: LocalModel, window: int, *, disk_gb: float | None = None, slots: int = 1
) -> float:
    """Total unified-memory footprint (GiB) of `model` held resident at `window`
    tokens: weights + KV cache. Weights = the measured on-disk size when known
    (`disk_gb`), else the catalog's nominal `size_gb`; KV scales linearly off the 128k
    reference (`kv_gb_per_128k * window / 131072`) — the same figures the settings
    memory meter shows. `slots` (llama-server `-np`) multiplies the KV: each parallel
    slot holds its own `window`-sized cache, so a second slot (the interactive
    keep-warm slot) doubles the KV cost while the weights are shared. On a Strix Halo
    box the iGPU draws from unified system RAM, so this one number is the whole cost of
    keeping the model loaded. The residency budget compares it against live free RAM."""
    weights = disk_gb if disk_gb is not None else model.size_gb
    kv = model.kv_gb_per_128k * window / _KV_REFERENCE_TOKENS * max(1, slots)
    return round(weights + kv, 2)


def recommended_ids() -> tuple[str, ...]:
    """The default-enabled set the install prompt offers first."""
    return tuple(m.id for m in CATALOG if m.recommended)


def retired_ids() -> tuple[str, ...]:
    """Decommissioned model ids the sync must force-uninstall + prune on the next update
    (RETIRED_IDS). These are no longer in CATALOG, so they can never be re-selected — the
    list only drives the removal of weights left on a box from before they were retired."""
    return RETIRED_IDS


def selected(ids: Sequence[str]) -> tuple[LocalModel, ...]:
    """Catalog entries for the given ids, in catalog order; unknown ids dropped."""
    wanted = set(ids)
    return tuple(m for m in CATALOG if m.id in wanted)


def jcode_models(local_llm_enabled: bool, local_models: Sequence[str]) -> tuple[LocalModel, ...]:
    """Installed, tool-capable local models — the set code mode (jcode) can run, in catalog
    order. The single source of truth for three consumers that must agree: the jcode model
    dropdown (llm_settings), the sandbox's grok `/model` list, and the residency-aware jcode
    proxy's allow-list (api.jcode_llm). jcode is a tool-using agent, so non-tool models are
    excluded; empty when local hosting is off (nothing installed to serve)."""
    installed = set(local_models)
    return tuple(m for m in CATALOG if local_llm_enabled and m.id in installed and m.supports_tools)


def _manifest(ids: Sequence[str]) -> str:
    """JSON download manifest for the setup script (one object per model)."""
    models = selected(ids) if ids else CATALOG
    return json.dumps([asdict(m) for m in models], indent=2)


if __name__ == "__main__":  # scripts/local-llm-setup.sh reads this
    print(_manifest(sys.argv[1:]))
