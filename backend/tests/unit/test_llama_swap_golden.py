"""The standard gateway's config must not move when Flash-Next lands beside it.

FLASH_NEXT_ENGINE_PLAN §7: "a box that never provisions Flash-Next [stays] byte-identical in
behaviour". The golden file was rendered by the code BEFORE the engine split (catalog-flag
superseding, per-engine filtering, default slots) over every standard catalog entry, with a
spread of operator overrides; any later change to a standard line has to be deliberate and
re-recorded, never a side effect of the second engine.
"""

from dataclasses import asdict
from pathlib import Path

from jbrain.llm import engine, llama_swap_config, local_catalog

_GOLDEN = Path(__file__).parent / "fixtures" / "llama_swap_standard.golden.yaml"


def _lay_down(root: Path, model_id: str, pattern: str) -> None:
    # A file name every catalog glob matches: each `*` becomes a literal character.
    (root / model_id).mkdir(exist_ok=True)
    (root / model_id / pattern.replace("*", "x")).write_bytes(b"\0")


def golden_inputs(root: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    """The standard roster laid down under `root`, plus overrides exercising every kind the
    renderer takes. Shared with the script that recorded the golden from the old code."""
    manifest: list[dict[str, object]] = []
    for model in local_catalog.CATALOG:
        if model.engine != engine.STANDARD:
            continue
        _lay_down(root, model.id, model.gguf_include)
        if model.mmproj_include:
            _lay_down(root, model.id, model.mmproj_include)
        manifest.append(asdict(model))
    kwargs: dict[str, object] = {
        "windows": {"gpt-oss-120b": 65536, "qwen3.8-27b-q4": 131072},
        "slots": {"gpt-oss-120b": 2, "qwen3.8-27b": 2},
        "extra_args": {"qwen3.5-4b": ["--load-mode", "dio", "-ub", "512"]},
        "image_min_tokens": {"qwen3-vl-30b": 4096},
    }
    return manifest, kwargs


def test_standard_render_is_byte_identical_to_before_the_engine_split(tmp_path: Path) -> None:
    manifest, kwargs = golden_inputs(tmp_path)
    text = llama_swap_config.render(manifest, str(tmp_path), **kwargs)  # type: ignore[arg-type]
    assert text == _GOLDEN.read_text()


def test_adding_flash_next_to_the_manifest_leaves_the_standard_file_unchanged(
    tmp_path: Path,
) -> None:
    manifest, kwargs = golden_inputs(tmp_path)
    flash = local_catalog.get("qwen3.8-flash-next")
    assert flash is not None
    _lay_down(tmp_path, flash.id, flash.gguf_include)
    _lay_down(tmp_path, flash.id, flash.mmproj_include or "")
    text = llama_swap_config.render(
        [*manifest, asdict(flash)],
        str(tmp_path),
        engine=engine.STANDARD,
        **kwargs,  # type: ignore[arg-type]
    )
    assert text == _GOLDEN.read_text()
    assert flash.served_model not in text
