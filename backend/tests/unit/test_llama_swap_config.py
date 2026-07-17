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
    # Every model joins one non-swapping group so the gateway never auto-evicts — the app is
    # the sole evictor (jbrain.llm.residency).
    assert "groups:" in text and "swap: false" in text and "exclusive: false" in text
    assert "- qwen3-vl-30b-a3b" in text and "- gpt-oss-120b" in text


def test_render_enables_prompt_prefix_cache_reuse_for_every_model(tmp_path: Path) -> None:
    # docs/plans/LLM_PROMPT_CACHE_PLAN.md W2: every model's llama-server command carries
    # --cache-reuse so a stable system-prompt + history prefix is reused, not re-prefilled.
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path))
    assert text.count("--cache-reuse 256") == len(_manifest())


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


def test_render_appends_extra_server_args_off_the_real_catalog_manifest(tmp_path: Path) -> None:
    # Guards the field-name contract end to end: the MTP variant's self-speculative-decoding
    # flags must reach the gateway command. Feed the REAL catalog entry through asdict →
    # render (not a hand-built dict) so renaming the dataclass field would fail here.
    mtp = local_catalog.get("qwen3.5-122b-a10b-mtp")
    assert mtp is not None
    (tmp_path / mtp.id).mkdir()
    (tmp_path / mtp.id / "model-UD-Q4_K_XL.gguf").write_bytes(b"\0")
    # The MTP entry is vision-capable, so its manifest carries an mmproj glob render()
    # resolves — lay the projector down too, else resolve_weight raises before the args.
    (tmp_path / mtp.id / "mmproj-F16.gguf").write_bytes(b"\0")
    text = llama_swap_config.render([asdict(mtp)], str(tmp_path))
    assert "--spec-type draft-mtp --spec-draft-n-max 6" in text
    assert "--mmproj" in text
    # A model with no extra_server_args emits none of it.
    _lay_down(tmp_path)
    plain = llama_swap_config.render(_manifest(), str(tmp_path))
    assert "--spec-type" not in plain


def test_render_applies_a_per_model_window_override(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path), windows={"gpt-oss-120b": 65536})
    assert "-c 65536" in text  # overridden
    assert "-c 32768" in text  # qwen keeps its default
    assert "-c 131072" not in text


def test_render_makes_every_model_a_non_swapping_member_so_the_app_evicts(
    tmp_path: Path,
) -> None:
    # EVERY provisioned model (not just a chosen pair) joins the swap:false group, so
    # llama-swap never auto-evicts — the app (jbrain.llm.residency) is the sole evictor.
    # Here a small extra model is a member alongside the two larger ones.
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
    text = llama_swap_config.render(manifest, str(tmp_path))
    assert "groups:" in text and "swap: false" in text and "exclusive: false" in text
    assert "- qwen3.5-0.8b" in text
    assert "- gpt-oss-120b" in text and "- qwen3-vl-30b-a3b" in text


def test_render_emits_no_group_for_an_empty_roster(tmp_path: Path) -> None:
    # No models → no group block (nothing to keep resident).
    text = llama_swap_config.render([], str(tmp_path))
    assert "groups:" not in text


def test_main_always_emits_the_full_non_swapping_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The CLI (invoked by the update's model-sync and by enable-local-models) always stamps
    # every model into the swap:false group — the app is the sole evictor, so the gateway
    # never auto-evicts and nothing pins ~91 GB and hard-freezes.
    _lay_down(tmp_path)
    monkeypatch.setenv("MANIFEST", json.dumps(_manifest()))
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
