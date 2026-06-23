"""The image-model catalog and the provisioning manifest it feeds the setup script."""

import json
from importlib import resources

from jbrain.image_gen import catalog


def test_catalog_entries_are_well_formed() -> None:
    ids = [m.id for m in catalog.CATALOG]
    assert len(ids) == len(set(ids)), "catalog ids must be unique"
    for m in catalog.CATALOG:
        assert m.kind in {"generate", "edit"}
        assert m.files, "a model must download at least one file"
        assert 0 < m.fast_steps <= m.quality_steps
        # Every file lands in a ComfyUI subdir the server actually reads.
        for f in m.files:
            assert f.dest_subdir in catalog.MODEL_SUBDIRS
            assert f.repo_path and f.hf_repo


def test_every_model_references_a_real_workflow_template() -> None:
    # The driver loads workflow templates by name; a catalog typo would 404 at run.
    workflows = resources.files("jbrain.image_gen") / "workflows"
    for m in catalog.CATALOG:
        assert (workflows / m.workflow).is_file(), f"{m.id} -> missing {m.workflow}"


def test_recommended_set_covers_both_tools_fast_and_quality() -> None:
    # A default provision downloads generate + edit and both 4-step Lightning siblings, so
    # the `fast` and `quality` paths of generate_image AND edit_image all work after one run.
    assert catalog.recommended_ids() == (
        "qwen-image",
        "qwen-image-lightning",
        "qwen-image-edit",
        "qwen-image-edit-lightning",
    )
    # DreamShaper is the only opt-in entry now (it's no longer the fast path).
    assert not catalog.get("dreamshaper").recommended  # type: ignore[union-attr]


def test_lightning_models_add_the_shared_step_distill_lora() -> None:
    # The fast generate + edit paths are the base models plus the SAME Lightning LoRA (lightx2v),
    # fixed at 4 steps. The LoRA lands in `loras`, and both fast models reference the same file.
    gen = catalog.get("qwen-image-lightning")
    edit = catalog.get("qwen-image-edit-lightning")
    assert gen is not None and edit is not None
    assert gen.workflow == "qwen_image_lightning.json" and gen.fast_steps == 4
    assert edit.workflow == "qwen_image_edit_lightning.json" and edit.kind == "edit"
    gen_lora = next(f for f in gen.files if f.dest_subdir == "loras")
    edit_lora = next(f for f in edit.files if f.dest_subdir == "loras")
    assert gen_lora == edit_lora
    assert gen_lora.hf_repo == "lightx2v/Qwen-Image-Edit-2511-Lightning"
    assert gen_lora.repo_path == "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"
    # Each fast model reuses its base model's weights, so it only adds the LoRA download.
    base_gen = catalog.get("qwen-image")
    assert base_gen is not None
    assert {f.repo_path for f in base_gen.files} < {f.repo_path for f in gen.files}


def test_dreamshaper_is_a_single_all_in_one_checkpoint() -> None:
    # A lightweight standalone (no longer the fast path): ONE checkpoint file (model+CLIP+baked
    # VAE) in the `checkpoints` subdir — no separate encoder/VAE, unlike the Qwen split layout.
    m = catalog.get("dreamshaper")
    assert m is not None
    assert m.kind == "generate" and m.workflow == "dreamshaper_xl.json"
    (only,) = m.files
    assert only.dest_subdir == "checkpoints"
    assert only.hf_repo == "Lykon/dreamshaper-xl-lightning"
    assert only.repo_path == "DreamShaperXL_Lightning.safetensors"
    # Distilled: a tight low step band (its sweet spot through its ceiling).
    assert m.fast_steps == 4 and m.quality_steps == 8
    # Opt-in, so a quality-only box stays lean.
    assert not m.recommended


def test_generate_and_edit_share_the_text_encoder_and_vae() -> None:
    gen = catalog.get("qwen-image")
    edit = catalog.get("qwen-image-edit")
    assert gen is not None and edit is not None
    shared = {(f.repo_path, f.dest_subdir) for f in gen.files} & {
        (f.repo_path, f.dest_subdir) for f in edit.files
    }
    # The CLIP encoder + VAE are common to both graphs.
    assert {sub for _, sub in shared} == {"text_encoders", "vae"}


def test_selected_keeps_catalog_order_and_drops_unknown() -> None:
    got = catalog.selected(["qwen-image-edit", "nope", "qwen-image"])
    assert [m.id for m in got] == ["qwen-image", "qwen-image-edit"]


def test_get_returns_none_for_unknown() -> None:
    assert catalog.get("does-not-exist") is None


def test_manifest_is_json_with_files_and_subdirs() -> None:
    manifest = json.loads(catalog._manifest(["qwen-image"]))
    (entry,) = manifest
    assert entry["kind"] == "generate" and entry["workflow"] == "qwen_image.json"
    subdirs = {f["dest_subdir"] for f in entry["files"]}
    assert subdirs == {"diffusion_models", "text_encoders", "vae"}
    diffusion = next(f for f in entry["files"] if f["dest_subdir"] == "diffusion_models")
    assert diffusion["repo_path"].endswith("qwen_image_2512_bf16.safetensors")


def test_empty_ids_manifest_dumps_the_whole_catalog() -> None:
    manifest = json.loads(catalog._manifest([]))
    assert {e["id"] for e in manifest} == {m.id for m in catalog.CATALOG}
