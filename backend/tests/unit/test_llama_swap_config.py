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


def test_render_emits_np_and_scaled_c_for_a_two_slot_model(tmp_path: Path) -> None:
    # A model with a dedicated interactive slot gets `-np 2`, and `-c` is scaled to
    # window*slots so each of the two slots still serves the full window (llama-server
    # divides -c evenly across -np). The single-slot model keeps its plain `-c`.
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path), slots={"gpt-oss-120b": 2})
    assert "-np 2" in text
    assert "-c 262144" in text  # 131072 * 2 for the two-slot model
    assert "-c 32768" in text  # the single-slot model keeps its plain window


def test_render_always_emits_np_because_the_llama_server_default_is_not_one(
    tmp_path: Path,
) -> None:
    # `-np` is emitted for EVERY model, including single-slot ones. llama-server's own default
    # is `auto`, which current builds resolve to a multi-slot value — so omitting the flag does
    # not mean one slot, it means "whatever this build picked". A serving mode that requires a
    # single sequence would then be violated silently, which is exactly the failure this guards.
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path))
    assert text.count("-np 1") == 2  # both models in the fixture manifest, explicitly


def test_render_clamps_a_speculative_model_to_one_slot(tmp_path: Path) -> None:
    # llama.cpp's speculative paths serve ONE sequence: MTP takes no second slot, and draft
    # acceptance collapses as concurrent sequences rise. So a saved `-np` override must NOT
    # reach a model served with --spec-type — the serving mode wins over the stored setting,
    # rather than the operator having to know the interaction. `-c` follows the clamped count,
    # so the window is not silently multiplied for slots the engine will never allocate.
    _lay_down(tmp_path)
    manifest = [dict(m) for m in _manifest()]
    manifest[0]["extra_server_args"] = ("--spec-type", "draft-mtp", "--spec-draft-n-max", "3")
    text = llama_swap_config.render(manifest, str(tmp_path), slots={manifest[0]["id"]: 2})
    speculative_block = text.split("  gpt-oss-120b:")[0]
    assert "-np 1" in speculative_block and "-np 2" not in speculative_block
    assert "-c 32768" in speculative_block  # NOT 65536 — the override never applied
    assert "--spec-type draft-mtp" in speculative_block


def test_render_scales_c_off_the_overridden_window_not_the_default(tmp_path: Path) -> None:
    # Slots multiply whatever window is in effect — an override, when present.
    _lay_down(tmp_path)
    text = llama_swap_config.render(
        _manifest(), str(tmp_path), windows={"gpt-oss-120b": 65536}, slots={"gpt-oss-120b": 2}
    )
    assert "-c 131072" in text and "-np 2" in text  # 65536 * 2


def test_render_enables_prompt_prefix_cache_reuse_for_every_model(tmp_path: Path) -> None:
    # docs/plans/LLM_PROMPT_CACHE_PLAN.md W2: every model's llama-server command carries
    # --cache-reuse so a stable system-prompt + history prefix is reused, not re-prefilled.
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path))
    assert text.count("--cache-reuse 256") == len(_manifest())


def test_render_adds_reasoning_format_only_for_thinking_models(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    (tmp_path / "nemotron-3-super-120b").mkdir()
    (tmp_path / "nemotron-3-super-120b" / "model-UD-Q4_K_XL.gguf").write_bytes(b"\0")
    manifest = [
        *_manifest(),
        {
            "id": "nemotron-3-super-120b",
            "served_model": "nemotron-3-super-120b",
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
    thinking = local_catalog.get("nemotron-3-super-120b")
    assert thinking is not None
    (tmp_path / thinking.id).mkdir()
    (tmp_path / thinking.id / "model-UD-Q4_K_XL.gguf").write_bytes(b"\0")
    text = llama_swap_config.render([asdict(thinking)], str(tmp_path))
    assert "--reasoning-format deepseek" in text


def test_render_appends_extra_server_args_from_the_manifest(tmp_path: Path) -> None:
    # extra_server_args (e.g. an MTP build's self-speculative-decoding flags) must reach the
    # gateway command verbatim. No catalog model currently ships them, so feed a synthetic
    # manifest entry carrying the field — this guards the render path that reads it.
    (tmp_path / "spec-model").mkdir()
    (tmp_path / "spec-model" / "model-UD-Q4_K_XL.gguf").write_bytes(b"\0")
    manifest = [
        {
            "id": "spec-model",
            "served_model": "spec-model",
            "gguf_include": "*UD-Q4_K_XL*.gguf",
            "mmproj_include": None,
            "context_window": 32768,
            "recommended": False,
            "extra_server_args": ["--spec-type", "draft-mtp", "--spec-draft-n-max", "6"],
        }
    ]
    text = llama_swap_config.render(manifest, str(tmp_path))
    assert "--spec-type draft-mtp --spec-draft-n-max 6" in text
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


def test_main_applies_the_operators_saved_overrides_not_just_catalog_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The DEPLOY re-stamp (_main) must apply the operator's SAVED window/slot overrides, not only
    # the catalog defaults — otherwise an update silently resets a raised -c (the 128k→32k overflow
    # the operator saw as "ran out of context" at 25%). _saved_overrides reads them from the
    # settings store; here we stand in for that read.
    _lay_down(tmp_path)
    saved = ({"gpt-oss-120b": 65536}, {})
    monkeypatch.setattr(llama_swap_config, "_saved_overrides", lambda: saved)
    monkeypatch.setenv("MANIFEST", json.dumps(_manifest()))
    assert llama_swap_config._main([str(tmp_path)]) == 0
    text = (tmp_path / "llama-swap.yaml").read_text()
    assert "-c 65536" in text  # the saved override wins over the catalog 131072…
    assert "-c 32768" in text  # …and an un-overridden model keeps its catalog default
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
    # raise "download incomplete" as it did on the box for a large sharded model.
    sub = tmp_path / "nemotron-3-super-120b" / "UD-Q4_K_XL"
    sub.mkdir(parents=True)
    for i in (1, 2, 3):
        (sub / f"Nemotron-3-Super-UD-Q4_K_XL-0000{i}-of-00003.gguf").write_bytes(b"\0")
    # An hf .cache staging dir alongside must be ignored.
    cache = tmp_path / "nemotron-3-super-120b" / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    (cache / "Nemotron-3-Super-UD-Q4_K_XL-00001-of-00003.gguf").write_bytes(b"\0")

    rel = llama_swap_config.resolve_weight(
        str(tmp_path), "nemotron-3-super-120b", "*UD-Q4_K_XL*.gguf"
    )
    assert rel == "UD-Q4_K_XL/Nemotron-3-Super-UD-Q4_K_XL-00001-of-00003.gguf"


def test_render_resolves_a_nested_quant_subdir_into_the_model_path(tmp_path: Path) -> None:
    model = tmp_path / "nemotron-3-super-120b" / "UD-Q4_K_XL"
    model.mkdir(parents=True)
    (model / "Nemotron-3-Super-UD-Q4_K_XL-00001-of-00002.gguf").write_bytes(b"\0")
    (model / "Nemotron-3-Super-UD-Q4_K_XL-00002-of-00002.gguf").write_bytes(b"\0")
    manifest = [
        {
            "id": "nemotron-3-super-120b",
            "served_model": "nemotron-3-super-120b",
            "gguf_include": "*UD-Q4_K_XL*.gguf",
            "mmproj_include": None,
            "context_window": 32768,
            "recommended": False,
        }
    ]
    text = llama_swap_config.render(manifest, str(tmp_path))
    assert (
        "/models/nemotron-3-super-120b/UD-Q4_K_XL/Nemotron-3-Super-UD-Q4_K_XL-00001-of-00002.gguf"
        in text
    )


def test_write_is_atomic_and_round_trips(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    path = llama_swap_config.write(str(tmp_path), _manifest(), windows={"gpt-oss-120b": 16384})
    assert Path(path).name == "llama-swap.yaml"
    text = Path(path).read_text()
    assert "-c 16384" in text
    # No leftover temp file from the atomic rename.
    assert not (tmp_path / "llama-swap.yaml.tmp").exists()
