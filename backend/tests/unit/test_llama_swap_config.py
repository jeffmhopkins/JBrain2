"""llama-swap config generation: filename resolution, the `-c` window (default +
override), the co-resident (non-swapping) group, and atomic write."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from jbrain.llm import llama_swap_config, local_catalog


def _manifest() -> list[dict[str, object]]:
    return [
        {
            "id": "qwen3-vl-30b",
            "served_model": "qwen3-vl-30b-a3b",
            "gguf_include": "*Q8_0*.gguf",
            "mmproj_include": "mmproj*.gguf",
            "context_window": 32768,
            "recommended": True,
        },
        {
            "id": "gpt-oss-120b",
            "served_model": "gpt-oss-120b",
            "gguf_include": "*mxfp4*.gguf",
            "mmproj_include": None,
            "context_window": 131072,
            "recommended": True,
        },
    ]


def _lay_down(root: Path) -> None:
    (root / "qwen3-vl-30b").mkdir()
    (root / "qwen3-vl-30b" / "model-Q8_0.gguf").write_bytes(b"\0")
    (root / "qwen3-vl-30b" / "mmproj-f16.gguf").write_bytes(b"\0")
    (root / "gpt-oss-120b").mkdir()
    (root / "gpt-oss-120b" / "model-mxfp4.gguf").write_bytes(b"\0")


def test_render_stamps_default_windows_and_resolves_files(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path))
    # Catalog defaults become -c; distinct upstream ports; resolved gguf + mmproj.
    assert "-c 32768" in text and "-c 131072" in text
    # --jinja on every model: the tool-use template + tool-call parsing the image
    # tools (and every other tool) depend on. One per served model.
    assert text.count("--jinja") == 2
    assert "--port 9100" in text and "--port 9101" in text
    assert "/models/qwen3-vl-30b/model-Q8_0.gguf" in text
    assert "--mmproj /models/qwen3-vl-30b/mmproj-f16.gguf" in text
    # No co-resident group unless asked (the render() param defaults off; the box's real
    # default comes from config/env, exercised separately).
    assert "groups:" not in text


def test_render_adds_reasoning_format_only_for_thinking_models(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    (tmp_path / "qwen3-next-80b-a3b-thinking").mkdir()
    (tmp_path / "qwen3-next-80b-a3b-thinking" / "model-UD-Q4_K_XL.gguf").write_bytes(b"\0")
    manifest = [
        *_manifest(),
        {
            "id": "qwen3-next-80b-a3b-thinking",
            "served_model": "qwen3-next-80b-a3b-thinking",
            "gguf_include": "*UD-Q4_K_XL*.gguf",
            "mmproj_include": None,
            "context_window": 32768,
            "recommended": False,
            "reasoning_format": "deepseek",
        },
    ]
    text = llama_swap_config.render(manifest, str(tmp_path))
    # The thinking model gets --reasoning-format deepseek; the two non-thinking models
    # (no reasoning_format) don't — they keep llama.cpp's default.
    assert "--reasoning-format deepseek" in text
    assert text.count("--reasoning-format") == 1


def test_render_reads_reasoning_format_off_the_real_catalog_manifest(tmp_path: Path) -> None:
    # Guards the field-name contract end to end: the renderer reads `reasoning_format`
    # off the asdict(LocalModel) manifest, so feed the REAL catalog entry (not a
    # hand-built dict) through asdict → render. Renaming the dataclass field would break
    # production silently; this test would catch it where the literal-key tests can't.
    thinking = local_catalog.get("qwen3-next-80b-a3b-thinking")
    assert thinking is not None
    (tmp_path / thinking.id).mkdir()
    (tmp_path / thinking.id / "model-UD-Q4_K_XL.gguf").write_bytes(b"\0")
    text = llama_swap_config.render([asdict(thinking)], str(tmp_path))
    assert "--reasoning-format deepseek" in text


def test_render_applies_a_per_model_window_override(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path), windows={"gpt-oss-120b": 65536})
    assert "-c 65536" in text  # overridden
    assert "-c 32768" in text  # qwen keeps its default
    assert "-c 131072" not in text


def test_render_emits_resident_group_when_enabled(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path), resident_group=True)
    # A non-swapping group (validated on-box): both recommended models are members and
    # co-reside (swap: false / exclusive: false).
    assert "groups:" in text and "swap: false" in text and "exclusive: false" in text
    assert "- qwen3-vl-30b-a3b" in text and "- gpt-oss-120b" in text


def test_co_residency_makes_every_model_a_member_so_the_app_evicts(tmp_path: Path) -> None:
    # Memory-safe co-residency: with resident_group ON, EVERY provisioned model (not just
    # the recommended set) joins the swap:false group, so llama-swap never auto-evicts —
    # the app (jbrain.llm.residency) is the sole evictor. Here a NON-recommended model is
    # still a member.
    manifest = [
        *_manifest(),
        {
            "id": "qwen3.5-0.8b",
            "served_model": "qwen3.5-0.8b",
            "gguf_include": "*Q8_0*.gguf",
            "mmproj_include": None,
            "context_window": 32768,
            "recommended": False,
        },
    ]
    (tmp_path / "qwen3.5-0.8b").mkdir()
    (tmp_path / "qwen3.5-0.8b" / "model-Q8_0.gguf").write_bytes(b"\0")
    _lay_down(tmp_path)
    text = llama_swap_config.render(manifest, str(tmp_path), resident_group=True)
    assert "- qwen3.5-0.8b" in text  # the non-recommended model co-resides too
    assert "- gpt-oss-120b" in text and "- qwen3-vl-30b-a3b" in text


def test_render_pins_staged_models_into_the_swap_group(tmp_path: Path) -> None:
    # Staging adds a model to the co-resident group: both stay loaded together even with
    # resident_group off (the recommended set's own membership is what the flag gates).
    _lay_down(tmp_path)
    text = llama_swap_config.render(
        _manifest(), str(tmp_path), resident_group=False, pinned=["qwen3-vl-30b", "gpt-oss-120b"]
    )
    assert "groups:" in text and "swap: false" in text
    assert "- qwen3-vl-30b-a3b" in text and "- gpt-oss-120b" in text


def test_render_pins_a_single_staged_model(tmp_path: Path) -> None:
    # A lone staged model is the only group member; the other model is absent (free to swap).
    _lay_down(tmp_path)
    text = llama_swap_config.render(
        _manifest(), str(tmp_path), resident_group=False, pinned=["gpt-oss-120b"]
    )
    assert "groups:" in text and "- gpt-oss-120b" in text
    assert "- qwen3-vl-30b-a3b" not in text


def test_render_no_group_when_nothing_pinned_or_recommended(tmp_path: Path) -> None:
    # resident_group off and nothing staged → no group, every model swaps alone.
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path), resident_group=False, pinned=[])
    assert "groups:" not in text


def test_main_defaults_to_no_co_residency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI (invoked by the update's model-sync and by enable-local-models) must default
    # co-residency OFF: with LOCAL_LLM_RESIDENT_GROUP unset, the recommended set swaps one
    # at a time so the box never pins ~91 GB and hard-freezes. This is the path that makes
    # "disabled on the next update" true without any .env edit.
    _lay_down(tmp_path)
    monkeypatch.setenv("MANIFEST", json.dumps(_manifest()))
    monkeypatch.delenv("LOCAL_LLM_RESIDENT_GROUP", raising=False)
    assert llama_swap_config._main([str(tmp_path)]) == 0
    assert "groups:" not in (tmp_path / "llama-swap.yaml").read_text()


def test_main_opts_into_co_residency_with_truthy_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The escape hatch: an operator with headroom opts in with a truthy value, and both
    # recommended models then join the non-swapping group.
    _lay_down(tmp_path)
    monkeypatch.setenv("MANIFEST", json.dumps(_manifest()))
    monkeypatch.setenv("LOCAL_LLM_RESIDENT_GROUP", "1")
    assert llama_swap_config._main([str(tmp_path)]) == 0
    text = (tmp_path / "llama-swap.yaml").read_text()
    assert "groups:" in text and "- gpt-oss-120b" in text and "- qwen3-vl-30b-a3b" in text


def test_resolve_weight_requires_a_complete_shard_set(tmp_path: Path) -> None:
    d = tmp_path / "m"
    d.mkdir()
    (d / "w-00001-of-00002.gguf").write_bytes(b"\0")  # missing shard 2 of 2
    with pytest.raises(FileNotFoundError):
        llama_swap_config.resolve_weight(str(tmp_path), "m", "*.gguf")


def test_resolve_weight_missing_file_raises(tmp_path: Path) -> None:
    (tmp_path / "m").mkdir()
    with pytest.raises(FileNotFoundError):
        llama_swap_config.resolve_weight(str(tmp_path), "m", "*.gguf")


def test_resolve_weight_finds_shards_nested_in_a_quant_subdir(tmp_path: Path) -> None:
    # Unsloth's UD-Q* repos nest the shards in a quant subdir, so hf saves them under
    # <id>/<quant>/. The resolver must find them recursively and return the path
    # RELATIVE to the model dir (so the gateway's -m /models/<id>/<rel> resolves), not
    # raise "download incomplete" as it did on the box for the 235B.
    sub = tmp_path / "qwen3-235b-a22b" / "UD-Q3_K_XL"
    sub.mkdir(parents=True)
    for i in (1, 2, 3):
        (sub / f"Qwen3-235B-UD-Q3_K_XL-0000{i}-of-00003.gguf").write_bytes(b"\0")
    # An hf .cache staging dir alongside must be ignored.
    cache = tmp_path / "qwen3-235b-a22b" / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    (cache / "Qwen3-235B-UD-Q3_K_XL-00001-of-00003.gguf").write_bytes(b"\0")

    rel = llama_swap_config.resolve_weight(str(tmp_path), "qwen3-235b-a22b", "*UD-Q3_K_XL*.gguf")
    assert rel == "UD-Q3_K_XL/Qwen3-235B-UD-Q3_K_XL-00001-of-00003.gguf"


def test_render_resolves_a_nested_quant_subdir_into_the_model_path(tmp_path: Path) -> None:
    model = tmp_path / "qwen3-235b-a22b" / "UD-Q3_K_XL"
    model.mkdir(parents=True)
    (model / "Qwen3-235B-UD-Q3_K_XL-00001-of-00002.gguf").write_bytes(b"\0")
    (model / "Qwen3-235B-UD-Q3_K_XL-00002-of-00002.gguf").write_bytes(b"\0")
    manifest = [
        {
            "id": "qwen3-235b-a22b",
            "served_model": "qwen3-235b-a22b",
            "gguf_include": "*UD-Q3_K_XL*.gguf",
            "mmproj_include": None,
            "context_window": 32768,
            "recommended": False,
        }
    ]
    text = llama_swap_config.render(manifest, str(tmp_path))
    assert "/models/qwen3-235b-a22b/UD-Q3_K_XL/Qwen3-235B-UD-Q3_K_XL-00001-of-00002.gguf" in text


def test_write_is_atomic_and_round_trips(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    path = llama_swap_config.write(str(tmp_path), _manifest(), windows={"gpt-oss-120b": 16384})
    assert Path(path).name == "llama-swap.yaml"
    text = Path(path).read_text()
    assert "-c 16384" in text
    # No leftover temp file from the atomic rename.
    assert not (tmp_path / "llama-swap.yaml.tmp").exists()
