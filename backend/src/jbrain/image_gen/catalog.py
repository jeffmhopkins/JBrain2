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

Validated on-box: the `qwen-image` generate model and its 20-step workflow (now on
native bf16 weights). The `qwen-image-edit` entry is wired structurally (its graph is
real, exported from the box) but its weights/repo path await an on-box
download+run, so it ships non-recommended.
"""

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

# ComfyUI models subdirs each weight file must land in (relative to the models
# mount). The setup script places files by these names; the catalog is validated
# against this set so a typo can't write to a directory ComfyUI never reads.
MODEL_SUBDIRS: frozenset[str] = frozenset({"diffusion_models", "text_encoders", "vae", "loras"})


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
    kind: str  # "generate" (text->image) or "edit" (image->image)
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
        fast_steps=4,
        quality_steps=20,
        recommended=True,
        note="Native bf16 (2512 checkpoint) — no fp8 upcast, so no quantization loss. "
        "~58 GB resident; fits with the LLMs offloaded during the render. VAE decode is "
        "tiled to keep the decode peak in budget. The fast preset needs a step-distill "
        "(Lightning) LoRA — add it once confirmed.",
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
        fast_steps=4,
        quality_steps=20,
        recommended=False,
        note="Graph validated structurally (exported from the box); the bf16 "
        "weights/repo path await an on-box download+run. ~55 GB resident with the bf16 "
        "text encoder. VAE decode is tiled to keep the decode peak in budget.",
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
