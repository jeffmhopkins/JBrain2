"""Curated catalog of self-hostable image-generation models.

Single source of truth for the OPT-IN ComfyUI image feature
(docs/IMAGE_GEN_SERVICE_PLAN.md): it names the diffusion models JBrain can run
on-box through the `comfyui` compose profile (ROCm ComfyUI), records the weight
files scripts/comfyui-setup.sh must download and where ComfyUI expects each, and
maps each model to the workflow template the driver (jbrain.image_gen.comfyui)
fills.

Tuned for an AMD Strix Halo box (gfx1151, large unified memory): Qwen-Image runs
on the iGPU and the renders time-share the unified memory with the local LLMs (they
are unloaded before a render), so the models carry their native bf16 weights rather
than an fp8 quant — gfx1151 has no fp8 compute and upcast fp8 to bf16 at load anyway,
so bf16 is the same RAM without the quantization loss.

Two consumers read this:
  - the app surfaces enabled models in settings (Wave G5/G6);
  - scripts/comfyui-setup.sh reads `python -m jbrain.image_gen.catalog <ids>`
    for the JSON download manifest.

Validated on-box: the `qwen-image` generate model and its workflow (native bf16 weights).
The `*-lightning` entries add the 4-step Lightning LoRA (lightx2v) to the generate and edit
base models for the interactive `fast` path — the form kyuz0's validated Strix Halo 4-step
workflows ship (LoRA at CFG 1). Generate, edit, and both Lightning variants are recommended,
so a default provision downloads everything the `fast` and `quality` paths of both tools need
(the Lightning LoRA is shared, ~0.85 GB on top of the base weights). The `dreamshaper` entry
is a tiny standalone SDXL checkpoint kept as a lightweight option; it ships non-recommended
and is no longer wired to the `speed` knob.

The `ace-step-xl` entry (kind="music") extends the same catalog/setup seam to music generation
(ACE-Step 1.5 XL Turbo) on the SAME comfyui service — validated on-box in the M0 spike
(docs/proposed/MUSIC_GEN_PLAN.md). It ships non-recommended (opt-in: `comfyui-setup.sh
ace-step-xl`) while that plan is still in docs/proposed/.
"""

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

# ComfyUI models subdirs each weight file must land in (relative to the models
# mount). The setup script places files by these names; the catalog is validated
# against this set so a typo can't write to a directory ComfyUI never reads.
# `checkpoints` holds all-in-one SDXL-style checkpoints (model+CLIP+VAE in one file,
# loaded by CheckpointLoaderSimple) — the form DreamShaper XL ships in, distinct from
# the split diffusion_models/text_encoders/vae layout Qwen uses.
MODEL_SUBDIRS: frozenset[str] = frozenset(
    {"diffusion_models", "text_encoders", "vae", "loras", "checkpoints"}
)


@dataclass(frozen=True)
class ImageFile:
    """One weight file and the ComfyUI models subdir it belongs in.

    `repo_path` is the file's path inside the HF repo; the setup script downloads
    it and places its basename under `dest_subdir`."""

    hf_repo: str
    repo_path: str
    dest_subdir: str


@dataclass(frozen=True)
class ImageModel:
    """One self-hostable image model and how to provision + drive it."""

    id: str  # stable settings-choice id
    label: str  # human label for the settings screen
    kind: str  # "generate" (text->image), "edit" (image->image), or "music" (text+lyrics->song)
    workflow: str  # the workflows/ template filename the driver fills for it
    files: tuple[ImageFile, ...]  # everything ComfyUI needs to run it
    size_gb: float  # total on-disk download
    # Resident unified-memory footprint ESTIMATE (not a measurement) the RAM meter
    # reserves while this model is loaded; on Strix Halo generation itself barely
    # moves RAM beyond the loaded weights (host-observed), so this is ~the load cost.
    vram_gb: float
    # Step presets: `fast` is the interactive target (needs a step-distill LoRA —
    # see note), `quality` is the on-box-validated default.
    fast_steps: int
    quality_steps: int
    # In the recommended set the setup script provisions by default.
    recommended: bool
    note: str = ""


# Qwen-Image's published ComfyUI weights (the repo scripts/comfyui-setup.sh and
# the spike both pull from). The text encoder + VAE are shared by both graphs.
_QWEN_REPO = "Comfy-Org/Qwen-Image_ComfyUI"
# Qwen-Image-Edit's weights — repo/path UNCONFIRMED on-box (the edit model has not
# been downloaded yet); the entry ships non-recommended until an operator validates.
_QWEN_EDIT_REPO = "Comfy-Org/Qwen-Image-Edit_ComfyUI"

_TEXT_ENCODER = ImageFile(
    hf_repo=_QWEN_REPO,
    # Native bf16 (16.6 GB), not the fp8_scaled quant — shared by BOTH graphs, so it lifts
    # prompt adherence for generate and edit alike. The LLMs are offloaded during a render,
    # so the extra ~8 GB resident is free headroom on the box.
    repo_path="split_files/text_encoders/qwen_2.5_vl_7b.safetensors",
    dest_subdir="text_encoders",
)
_VAE = ImageFile(
    hf_repo=_QWEN_REPO,
    repo_path="split_files/vae/qwen_image_vae.safetensors",
    dest_subdir="vae",
)
_GEN_DIFFUSION = ImageFile(
    hf_repo=_QWEN_REPO,
    # Native bf16 of the 2512 checkpoint (40.9 GB) — matches the model name the handler
    # records (qwen-image-2512). gfx1151 has no fp8 compute so fp8 was upcast to bf16 at
    # load anyway: same RAM, but bf16 weights skip the fp8 quantization quality loss.
    repo_path="split_files/diffusion_models/qwen_image_2512_bf16.safetensors",
    dest_subdir="diffusion_models",
)
_EDIT_DIFFUSION = ImageFile(
    hf_repo=_QWEN_EDIT_REPO,
    repo_path="split_files/diffusion_models/qwen_image_edit_2511_bf16.safetensors",
    dest_subdir="diffusion_models",
)
# The Lightning step-distill LoRA (lightx2v) that makes the 4-step `fast` path work: it
# collapses the ~20-40 step schedule to 4 at CFG 1. The same bf16 LoRA (~0.85 GB) drives
# BOTH the base-generate and the edit Lightning graphs (the form kyuz0's validated Strix
# Halo 4-step workflows ship), so a box that runs either fast path downloads it once.
_LIGHTNING_LORA = ImageFile(
    hf_repo="lightx2v/Qwen-Image-Edit-2511-Lightning",
    repo_path="Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
    dest_subdir="loras",
)

# DreamShaper XL Lightning (Lykon): a single all-in-one SDXL checkpoint — model + CLIP +
# baked VAE in one file — so it needs no separate encoder/VAE, unlike the Qwen split. It is
# step-distilled (Lightning), so a few steps at low CFG produce a usable image in seconds on
# the iGPU; that speed is the whole point of the `fast` generate path.
_DREAMSHAPER_CHECKPOINT = ImageFile(
    hf_repo="Lykon/dreamshaper-xl-lightning",
    repo_path="DreamShaperXL_Lightning.safetensors",
    dest_subdir="checkpoints",
)

# ACE-Step 1.5 XL Turbo — the OPT-IN music capability on the SAME comfyui service
# (docs/proposed/MUSIC_GEN_PLAN.md), validated on-box in the M0 spike. Split-file form
# like Qwen: the XL turbo DiT (diffusion_models), the largest Qwen LM planner used as the
# text encoder (loaded into a DualCLIPLoader with type "ace"), and the ACE VAE. Repo paths
# + sizes are the M0-confirmed values read off the HF repo.
_ACE_REPO = "Comfy-Org/ace_step_1.5_ComfyUI_files"
_ACE_DIFFUSION = ImageFile(
    hf_repo=_ACE_REPO,
    repo_path="split_files/diffusion_models/acestep_v1.5_xl_turbo_bf16.safetensors",
    dest_subdir="diffusion_models",
)
_ACE_TEXT_ENCODER = ImageFile(
    hf_repo=_ACE_REPO,
    repo_path="split_files/text_encoders/qwen_4b_ace15.safetensors",
    dest_subdir="text_encoders",
)
_ACE_VAE = ImageFile(
    hf_repo=_ACE_REPO,
    repo_path="split_files/vae/ace_1.5_vae.safetensors",
    dest_subdir="vae",
)


# Order is the order the settings screen and setup prompt present them.
CATALOG: tuple[ImageModel, ...] = (
    ImageModel(
        id="qwen-image",
        label="Qwen-Image · generate (bf16)",
        kind="generate",
        workflow="qwen_image.json",
        files=(_GEN_DIFFUSION, _TEXT_ENCODER, _VAE),
        size_gb=58.0,
        # Native bf16 now (diffusion ~41 GB + text encoder ~16 GB resident): the same RAM
        # the fp8 build used after its load-time upcast, minus the fp8 quantization loss. The
        # LLMs are unloaded during a render, so this fits the box's unified memory with room.
        vram_gb=58.0,
        # The quality generate path: effort maps to a 20-40 step band (the fast 4-step path
        # is the separate qwen-image-lightning entry below).
        fast_steps=20,
        quality_steps=40,
        recommended=True,
        note="Native bf16 (2512 checkpoint) — no fp8 upcast, so no quantization loss. "
        "~58 GB resident; fits with the LLMs offloaded during the render. VAE decode is "
        "tiled to keep the decode peak in budget. The interactive `fast` path is the "
        "separate qwen-image-lightning model (the 4-step Lightning LoRA).",
    ),
    ImageModel(
        id="qwen-image-lightning",
        label="Qwen-Image · fast (4-step Lightning)",
        kind="generate",
        workflow="qwen_image_lightning.json",
        # The same base model + encoder + VAE as qwen-image, plus the small Lightning LoRA —
        # so a box that already provisioned qwen-image adds only the ~0.85 GB LoRA to gain the
        # fast path (hf skips the shared weights it already has).
        files=(_GEN_DIFFUSION, _TEXT_ENCODER, _VAE, _LIGHTNING_LORA),
        size_gb=58.9,
        vram_gb=58.0,
        # Fixed 4 steps at CFG 1 — the distilled schedule's sweet spot; more steps don't add
        # detail here, so the `fast` knob is not step-tunable (the handler pins it to 4).
        fast_steps=4,
        quality_steps=4,
        recommended=True,
        note="The interactive `fast` generate path: the qwen-image base model driven through "
        "the 4-step Lightning LoRA (lightx2v) at CFG 1 — far higher fidelity than DreamShaper, "
        "the same ~58 GB family as quality but a fraction of the render time. Shares qwen-image's "
        "weights, so it only adds the ~0.85 GB LoRA.",
    ),
    ImageModel(
        id="qwen-image-edit",
        label="Qwen-Image-Edit · edit",
        kind="edit",
        workflow="qwen_image_edit.json",
        files=(_EDIT_DIFFUSION, _TEXT_ENCODER, _VAE),
        size_gb=51.0,
        # bf16 throughout: ~34 GB diffusion + ~16 GB bf16 text encoder (shared with generate)
        # resident. Multi-image edits add encode memory per reference, all within budget once
        # the LLMs are offloaded for the render.
        vram_gb=55.0,
        # The quality edit path: effort maps to a 20-40 step band (the fast 4-step path is the
        # separate qwen-image-edit-lightning entry below).
        fast_steps=20,
        quality_steps=40,
        recommended=True,
        note="Graph validated structurally (exported from the box). ~55 GB resident with the "
        "bf16 text encoder. VAE decode is tiled to keep the decode peak in budget. The "
        "interactive `fast` edit path is the separate qwen-image-edit-lightning model.",
    ),
    ImageModel(
        id="qwen-image-edit-lightning",
        label="Qwen-Image-Edit · fast (4-step Lightning)",
        kind="edit",
        workflow="qwen_image_edit_lightning.json",
        # qwen-image-edit's base model + the shared encoder/VAE + the Lightning LoRA: a box that
        # provisioned qwen-image-edit adds only the ~0.85 GB LoRA to gain the fast edit path.
        files=(_EDIT_DIFFUSION, _TEXT_ENCODER, _VAE, _LIGHTNING_LORA),
        size_gb=51.9,
        vram_gb=55.0,
        # Fixed 4 steps at CFG 1 — the `fast` edit knob is not step-tunable (handler pins to 4).
        fast_steps=4,
        quality_steps=4,
        recommended=True,
        note="The interactive `fast` edit path: the qwen-image-edit base model driven through "
        "the 4-step Lightning LoRA (lightx2v) at CFG 1 — a fraction of the quality render time. "
        "Shares qwen-image-edit's weights, so it only adds the ~0.85 GB LoRA.",
    ),
    ImageModel(
        id="dreamshaper",
        label="DreamShaper XL · lightweight (SDXL Lightning)",
        kind="generate",
        workflow="dreamshaper_xl.json",
        files=(_DREAMSHAPER_CHECKPOINT,),
        size_gb=6.7,
        # Tiny next to Qwen: one ~6.7 GB SDXL checkpoint, baked VAE, loads in seconds and
        # leaves the unified pool almost untouched — it can even sit alongside a local LLM.
        vram_gb=7.0,
        # Distilled: 4 steps is the model's sweet spot, ~8 the useful ceiling — more steps
        # don't add detail, they just cost time. CFG/sampler (2.0 / dpmpp_sde+karras) are
        # authored into the workflow, not driven per request.
        fast_steps=4,
        quality_steps=8,
        # A standalone lightweight checkpoint, no longer wired to the `speed` knob (the fast
        # path is now qwen-image-lightning). Opt-in, so a box that doesn't want it stays lean.
        recommended=False,
        note="All-in-one SDXL checkpoint (model+CLIP+baked VAE) — a tiny ~6.7 GB option that "
        "renders in seconds on the iGPU at 4-8 steps. Lower fidelity than Qwen; kept as a "
        "lightweight standalone (the agent's fast path is qwen-image-lightning). Install with "
        "`comfyui-setup.sh dreamshaper`.",
    ),
    ImageModel(
        id="ace-step-xl",
        label="ACE-Step 1.5 XL Turbo · music",
        kind="music",
        workflow="ace_step_music.json",
        # Split-file form: the XL turbo DiT (~10 GB) + the largest Qwen LM planner (~8.4 GB,
        # the text encoder) + the ACE VAE (~0.3 GB). ~18.7 GB on disk; round up.
        files=(_ACE_DIFFUSION, _ACE_TEXT_ENCODER, _ACE_VAE),
        size_gb=19.0,
        # M0-measured: the VAE-decode stage peaks ~48 GB of unified memory — well above the
        # ~19 GB resident weights. That peak is what the time-share must budget (and is why
        # music can't co-reside with the full ~91 GB local-LLM set under the ~124 GB pool).
        vram_gb=48.0,
        # The distilled turbo schedule's M0-validated band: 8 steps at CFG 1 (authored into the
        # workflow). Not a fast/quality split — an sft variant would be a separate quality entry.
        fast_steps=8,
        quality_steps=8,
        # Opt-in: a default comfyui-setup.sh run SKIPS it (recommended_ids()); the operator
        # provisions it explicitly with `comfyui-setup.sh ace-step-xl`. Stays non-recommended
        # while the plan lives in docs/proposed/ (not yet on the roadmap).
        recommended=False,
        note="ACE-Step 1.5 XL Turbo (4B DiT, native bf16) — text tags + lyrics → a full song, "
        "served by the same opt-in comfyui service as image gen. ~19 GB on disk, ~48 GB "
        "unified-memory peak at VAE decode (time-shared with the LLMs, never co-resident). "
        "Opt-in: install with `comfyui-setup.sh ace-step-xl`. Lyrics want lowercase "
        "[verse]/[chorus] structure tags or the model renders an instrumental.",
    ),
)

_BY_ID = {m.id: m for m in CATALOG}


def get(model_id: str) -> ImageModel | None:
    return _BY_ID.get(model_id)


def recommended_ids() -> tuple[str, ...]:
    """The default-provisioned set the setup script offers when none are named."""
    return tuple(m.id for m in CATALOG if m.recommended)


def selected(ids: Sequence[str]) -> tuple[ImageModel, ...]:
    """Catalog entries for the given ids, in catalog order; unknown ids dropped."""
    wanted = set(ids)
    return tuple(m for m in CATALOG if m.id in wanted)


def _manifest(ids: Sequence[str]) -> str:
    """JSON download manifest for the setup script (one object per model)."""
    models = selected(ids) if ids else CATALOG
    return json.dumps([asdict(m) for m in models], indent=2)


if __name__ == "__main__":  # scripts/comfyui-setup.sh reads this
    print(_manifest(sys.argv[1:]))
