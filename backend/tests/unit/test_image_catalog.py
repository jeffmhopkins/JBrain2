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


def test_generate_model_is_the_recommended_default() -> None:
    # Only the on-box-validated generate model is provisioned by default; edit ships
    # non-recommended until its weights are validated on-box.
    assert catalog.recommended_ids() == ("qwen-image",)
    edit = catalog.get("qwen-image-edit")
    assert edit is not None and not edit.recommended and edit.kind == "edit"


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
    assert diffusion["repo_path"].endswith("qwen_image_fp8_e4m3fn.safetensors")


def test_empty_ids_manifest_dumps_the_whole_catalog() -> None:
    manifest = json.loads(catalog._manifest([]))
    assert {e["id"] for e in manifest} == {m.id for m in catalog.CATALOG}
