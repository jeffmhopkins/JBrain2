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
from dataclasses import asdict, dataclass

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
    # Emits a `reasoning_content` channel and honors `reasoning_effort` (gpt-oss
    # harmony reasoning / GLM thinking). Drives the settings effort control and lets
    # the router send an effort to this model; default False (the Qwen Instruct
    # variants and Llama here are non-thinking).
    supports_reasoning: bool = False
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
        id="gpt-oss-120b",
        label="GPT-OSS 120B · reasoning",
        served_model="gpt-oss-120b",
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
        id="qwen3-235b-a22b",
        label="Qwen3-235B-A22B · reasoning (alt, 3-bit)",
        served_model="qwen3-235b-a22b",
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF",
        gguf_include="*UD-Q3_K_XL*.gguf",
        mmproj_include=None,
        quant="UD-Q3_K_XL",
        # Measured on-box footprint of the three UD-Q3_K_XL shards (Unsloth nests
        # them in a UD-Q3_K_XL/ subdir); the HF web estimate of ~104 was high.
        size_gb=97.0,
        note="235B MoE, 22B active — the strongest open model that fits this "
        "128 GB box, at Unsloth's 3-bit dynamic quant (~104 GB weights). "
        "Standalone only: too large to co-reside, so expect a cold load on every "
        "switch and a tight context budget beside the weights. Instruct-2507 "
        "(non-thinking).",
        # Native window is 262144, but ~104 GB of weights leaves little headroom on
        # the box — its 94 dense-attention layers make the KV cache the binding
        # constraint, so it serves the gateway default. The native ceiling is exposed
        # for selection, but the drawer's KV estimate (46 GB/128k here) is the warning.
        native_context_window=262144,
        kv_gb_per_128k=46.0,
    ),
    LocalModel(
        id="qwen3-next-80b-a3b",
        label="Qwen3-Next 80B · reasoning (alt)",
        served_model="qwen3-next-80b-a3b",
        tiers=("high",),
        supports_vision=False,
        supports_tools=True,
        recommended=False,
        hf_repo="unsloth/Qwen3-Next-80B-A3B-Instruct-GGUF",
        gguf_include="*UD-Q4_K_XL*.gguf",
        mmproj_include=None,
        quant="UD-Q4_K_XL",
        size_gb=46.1,
        note="80B MoE, 3B active — ~59 t/s, fits resident beside gpt-oss-120b. "
        "Hybrid-attention arch: confirm the gateway's llama.cpp build supports it.",
        # Native 256k; serves the gateway default — its light KV makes a big -c cheap.
        native_context_window=262144,
        kv_gb_per_128k=5.0,
    ),
    LocalModel(
        id="qwen3-coder-next",
        label="Qwen3-Coder-Next 80B · coding agent (Q4)",
        served_model="qwen3-coder-next",
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
        "behind code mode (jcode). Co-resides beside another large model. Same "
        "hybrid-attention arch as qwen3-next-80b — confirm the gateway's llama.cpp "
        "build supports it (a recent build fixed a Qwen looping bug). Native 256k "
        "window; serves the gateway default — raise -c toward native when it fits.",
        native_context_window=262144,
        kv_gb_per_128k=5.0,
    ),
    LocalModel(
        id="qwen3-coder-next-q8",
        label="Qwen3-Coder-Next 80B · coding agent (Q8)",
        served_model="qwen3-coder-next-q8",
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
        "Same hybrid-attention arch — confirm the llama.cpp build serves Q8 on gfx1151.",
        # Native 256k; standalone here, so the full window has the most room to grow.
        native_context_window=262144,
        kv_gb_per_128k=5.0,
    ),
    LocalModel(
        id="glm-4.5-air",
        label="GLM-4.5 Air · reasoning (alt)",
        served_model="glm-4.5-air",
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
        id="llama-3.3-70b",
        label="Llama 3.3 70B · batch (slow)",
        served_model="llama-3.3-70b",
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


def recommended_ids() -> tuple[str, ...]:
    """The default-enabled set the install prompt offers first."""
    return tuple(m.id for m in CATALOG if m.recommended)


def selected(ids: Sequence[str]) -> tuple[LocalModel, ...]:
    """Catalog entries for the given ids, in catalog order; unknown ids dropped."""
    wanted = set(ids)
    return tuple(m for m in CATALOG if m.id in wanted)


def _manifest(ids: Sequence[str]) -> str:
    """JSON download manifest for the setup script (one object per model)."""
    models = selected(ids) if ids else CATALOG
    return json.dumps([asdict(m) for m in models], indent=2)


if __name__ == "__main__":  # scripts/local-llm-setup.sh reads this
    print(_manifest(sys.argv[1:]))
