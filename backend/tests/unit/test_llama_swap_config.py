"""llama-swap config generation: filename resolution, the `-c` window (default +
override), the co-resident (non-swapping) group, and atomic write."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

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


def test_a_slot_request_drops_speculation_rather_than_being_ignored(tmp_path: Path) -> None:
    """Speculation and parallel slots are mutually exclusive (llama.cpp's speculative paths serve
    ONE sequence). This used to resolve by clamping `-np` to 1 and discarding the request.

    It now resolves the other way, because a second slot is the only thing that keeps a
    background task out of the slot holding the interactive prefix — and losing that prefix costs
    a ~100 s cold prefill, measured. The operator gets whichever of the two they asked for, and
    the whole `--spec-draft-*` family comes out with `--spec-type` (llama-server rejects a
    tuning flag with no mode to tune)."""
    _lay_down(tmp_path)
    manifest = [dict(m) for m in _manifest()]
    manifest[0]["extra_server_args"] = ("--spec-type", "draft-mtp", "--spec-draft-n-max", "3")
    text = llama_swap_config.render(manifest, str(tmp_path), slots={"qwen3-vl-30b": 2})
    block = text.split("  gpt-oss-120b:")[0]
    assert "-np 2" in block  # the request is honoured...
    assert "--spec-type" not in block and "--spec-draft-n-max" not in block  # ...at this cost
    assert "-c 65536" in block  # and `-c` scales with the slots actually served

    # At one slot nothing changes: speculation stays, and so does the unmultiplied window.
    text1 = llama_swap_config.render(manifest, str(tmp_path))
    block1 = text1.split("  gpt-oss-120b:")[0]
    assert "-np 1" in block1 and "--spec-type draft-mtp" in block1 and "-c 32768" in block1


def test_render_scales_c_off_the_overridden_window_not_the_default(tmp_path: Path) -> None:
    # Slots multiply whatever window is in effect — an override, when present.
    _lay_down(tmp_path)
    text = llama_swap_config.render(
        _manifest(), str(tmp_path), windows={"gpt-oss-120b": 65536}, slots={"gpt-oss-120b": 2}
    )
    assert "-c 131072" in text and "-np 2" in text  # 65536 * 2


def test_cache_reuse_is_served_for_attention_models_and_withheld_from_recurrent_ones(
    tmp_path: Path,
) -> None:
    """docs/archive/LLM_PROMPT_CACHE_PLAN.md W2: prefix KV reuse, so a stable system-prompt +
    history prefix is not re-prefilled every turn. Real on an ATTENTION model.

    Withheld from a recurrent/hybrid one, where it is a latent CRASH rather than a no-op: you
    cannot KV-shift a recurrent state, and the partial-range `seq_rm` this path calls returns
    false for recurrent memory, which reaches GGML_ABORT — the server dies. It has never fired
    because on an identical prompt the reuse loop never executes. Prefix reuse on those models
    is mediated entirely by context checkpoints instead."""
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path))
    assert text.count("--cache-reuse 256") == len(_manifest())  # both fixtures are attention

    manifest = [dict(m) for m in _manifest()]
    manifest[0]["recurrent"] = True
    mixed = llama_swap_config.render(manifest, str(tmp_path))
    recurrent_block = mixed.split("  gpt-oss-120b:")[0]
    assert "--cache-reuse" not in recurrent_block
    assert mixed.count("--cache-reuse 256") == 1  # the attention model still gets it


def test_every_recurrent_catalog_entry_is_served_without_cache_reuse(tmp_path: Path) -> None:
    """The real entries, not a fixture: a hybrid that slipped through with `recurrent` unset
    would ship the crash path, and nothing else in the config would look wrong."""
    recurrent = [m for m in local_catalog.CATALOG if m.recurrent]
    assert {m.id for m in recurrent} == {
        "qwen3.8-27b",
        "qwen3.8-27b-q4",
        "qwen3.8-27b-abliterated",
        "nemotron-3-super-120b",
        "nemotron-3.5-lightning-30b",
    }
    for model in recurrent:
        (tmp_path / model.id).mkdir(exist_ok=True)
        (tmp_path / model.id / "w.gguf").write_bytes(b"\0")
        entry = dict(asdict(model), gguf_include="*.gguf", mmproj_include=None)
        assert "--cache-reuse" not in llama_swap_config.render([entry], str(tmp_path))


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


def test_render_appends_operator_extra_args_after_the_catalog_flags(tmp_path: Path) -> None:
    """An operator's remote flag override lands in the model's cmd, AFTER its catalog flags —
    so it can only add to the launch line, never reorder or displace what the catalog set."""
    _lay_down(tmp_path)
    text = llama_swap_config.render(
        _manifest(),
        str(tmp_path),
        extra_args={"gpt-oss-120b": ["--swa-full", "--slot-save-path", "/tmp/kv/"]},
    )
    line = next(ln for ln in text.splitlines() if "--swa-full" in ln)
    assert "--slot-save-path /tmp/kv/" in line
    # Only the targeted model is affected — a bad flag can never take the whole gateway down.
    assert sum("--swa-full" in ln for ln in text.splitlines()) == 1


def test_main_applies_saved_extra_args_so_an_update_keeps_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deploy re-stamp must carry the saved FLAG overrides too. Without this an
    Ops → Update silently drops a flag the operator set remotely, and the box comes back
    behaving differently than the settings say it does."""
    _lay_down(tmp_path)
    monkeypatch.setattr(
        llama_swap_config,
        "_saved_overrides",
        lambda: ({}, {}, {"gpt-oss-120b": ["--swa-full"]}, {"qwen3-vl-30b": 4096}),
    )
    monkeypatch.setenv("MANIFEST", json.dumps(_manifest()))
    assert llama_swap_config._main([str(tmp_path)]) == 0
    text = (tmp_path / "llama-swap.yaml").read_text()
    assert "--swa-full" in text
    # The IMAGE FLOOR too. It was added to the overrides after this path was written and was
    # left out of `_saved_overrides`, so every Ops → Update reverted it to the catalog value —
    # the same silent-reset bug this test exists to prevent, reproduced for a newer knob.
    assert "--image-min-tokens 4096" in text


def test_main_applies_the_operators_saved_overrides_not_just_catalog_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The DEPLOY re-stamp (_main) must apply the operator's SAVED window/slot overrides, not only
    # the catalog defaults — otherwise an update silently resets a raised -c (the 128k→32k overflow
    # the operator saw as "ran out of context" at 25%). _saved_overrides reads them from the
    # settings store; here we stand in for that read.
    _lay_down(tmp_path)
    saved = ({"gpt-oss-120b": 65536}, {}, {}, {})
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


def test_unresolved_ids_is_empty_when_every_required_file_is_present(tmp_path: Path) -> None:
    _lay_down(tmp_path)
    assert llama_swap_config.unresolved_ids(str(tmp_path), _manifest()) == ()


def test_unresolved_ids_catches_a_model_that_gained_a_required_file(tmp_path: Path) -> None:
    # THE regression this exists for. A model's required file set is a catalog property that
    # changes between releases: an entry that shipped text-only gains a vision projector. Its
    # directory still holds last release's weights, so a `*.gguf` presence check reads it as
    # complete and the provisioning run skips the download — and then `render`, which resolves
    # every glob for the WHOLE roster in one pass, raises on the missing projector and takes
    # every other model's config down with it. The deploy re-stamp aborts; the boot reconcile
    # swallows render failures (so a bad glob can't block startup) and silently applies nothing.
    _lay_down(tmp_path)
    (tmp_path / "gpt-oss-120b" / "mmproj-F16.gguf").unlink(missing_ok=True)
    manifest = [dict(m) for m in _manifest()]
    manifest[1]["mmproj_include"] = "mmproj-F16.gguf"  # newly required, not on disk
    assert llama_swap_config.unresolved_ids(str(tmp_path), manifest) == ("gpt-oss-120b",)
    # And the check agrees with reality: render really does fail for the whole roster here.
    with pytest.raises(FileNotFoundError):
        llama_swap_config.render(manifest, str(tmp_path))
    # Once the projector lands, both agree the other way — no stale "incomplete" verdict.
    (tmp_path / "gpt-oss-120b" / "mmproj-F16.gguf").write_bytes(b"\0")
    assert llama_swap_config.unresolved_ids(str(tmp_path), manifest) == ()
    assert "--mmproj /models/gpt-oss-120b/mmproj-F16.gguf" in llama_swap_config.render(
        manifest, str(tmp_path)
    )


def test_unresolved_ids_catches_missing_weights_and_incomplete_shards(tmp_path: Path) -> None:
    # The two older failure modes the `*.gguf` check did catch must keep working: a model with
    # no weights at all, and a sharded model missing part of its set (present-but-INCOMPLETE).
    _lay_down(tmp_path)
    (tmp_path / "nothing-here").mkdir()
    manifest = [dict(m) for m in _manifest()]
    manifest.append({**manifest[0], "id": "nothing-here", "served_model": "nothing-here"})
    assert "nothing-here" in llama_swap_config.unresolved_ids(str(tmp_path), manifest)
    # A shard set claiming 2 parts but holding 1 resolves as incomplete, not as "present".
    shard_dir = tmp_path / "sharded"
    shard_dir.mkdir()
    (shard_dir / "model-Q4_K_M-00001-of-00002.gguf").write_bytes(b"\0")
    manifest.append({**manifest[0], "id": "sharded", "served_model": "sharded"})
    assert "sharded" in llama_swap_config.unresolved_ids(str(tmp_path), manifest)


def test_check_cli_prints_the_incomplete_ids(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    # The provisioning scripts' download filter runs through this CLI, so its contract — ids one
    # per line on stdout, exit 0, empty when everything resolves — is what the shell parses.
    _lay_down(tmp_path)
    manifest = [dict(m) for m in _manifest()]
    manifest[1]["mmproj_include"] = "mmproj-F16.gguf"
    monkeypatch.setenv("MANIFEST", json.dumps(manifest))
    assert llama_swap_config._main(["--check", str(tmp_path)]) == 0
    assert capsys.readouterr().out.split() == ["gpt-oss-120b"]
    monkeypatch.setenv("MANIFEST", json.dumps(_manifest()))
    assert llama_swap_config._main(["--check", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == ""


def test_every_model_gets_a_slot_save_path_and_only_swa_models_get_swa_full(
    tmp_path: Path,
) -> None:
    """`--slot-save-path` is unconditional (the named volume guarantees the dir exists, and a
    missing one would stop llama-server booting), while `--swa-full` is per-model: it is the
    precondition for a KV restore doing anything on a sliding-window model, and pure cost
    elsewhere."""
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path))
    cmds = [ln for ln in text.splitlines() if "llama-server" in ln]
    assert cmds and all("--slot-save-path /kv/" in ln for ln in cmds)
    # Only the model the catalog flags carries --swa-full.
    flagged = [ln for ln in cmds if "--swa-full" in ln]
    assert len(flagged) == sum(1 for m in _manifest() if m.get("kv_full_history"))


def test_an_operator_set_spec_flag_loses_to_an_operator_set_slot(tmp_path: Path) -> None:
    """Speculation can be turned on two ways — the catalog's static flags, or an operator trying
    `--spec-type` live — so the resolution must read the flags actually going onto the command
    line, not just the catalog's. With both set, the SLOT wins and the spec flag comes out;
    otherwise a remote experiment would run multi-slot while measuring acceptance the real
    serving mode could never produce."""
    _lay_down(tmp_path)
    text = llama_swap_config.render(
        _manifest(),
        str(tmp_path),
        slots={"gpt-oss-120b": 2},
        extra_args={"gpt-oss-120b": ["--spec-type", "draft-mtp"]},
    )
    gpt_oss_cmd = next(ln for ln in text.splitlines() if "/gpt-oss-120b/" in ln)
    assert "-np 2" in gpt_oss_cmd
    assert "--spec-type" not in gpt_oss_cmd
    # Without the slot request the operator's spec flag stands.
    one = llama_swap_config.render(
        _manifest(), str(tmp_path), extra_args={"gpt-oss-120b": ["--spec-type", "draft-mtp"]}
    )
    one_cmd = next(ln for ln in one.splitlines() if "/gpt-oss-120b/" in ln)
    assert "--spec-type draft-mtp" in one_cmd and "-np 1" in one_cmd
    # And `-c` now scales with the slots actually served, because they really are allocated.
    assert "-c 262144" in gpt_oss_cmd  # 131072 * 2


def test_operator_extra_args_replace_the_catalog_flag_they_target(tmp_path: Path) -> None:
    # An operator override still WINS over a catalog flag — raising the MTP entry's pinned
    # --spec-draft-n-max without a release is the whole point. What changed is how: the catalog's
    # copy is now removed rather than left on the line to be outranked.
    #
    # This test used to assert the opposite (both occurrences present, operator's last), on the
    # grounds that llama-server takes the last of a repeated flag. #1152 overruled that reasoning
    # for `--image-min-tokens` — last-wins is "true today, undocumented, and impossible to read
    # back from a command line that says both 1024 and 4096" — and the same argument holds for
    # every other flag, so the rule is now general. The SERVED VALUE is unchanged either way;
    # only the command line differs, and it is the box's one window into the engine.
    _lay_down(tmp_path)
    manifest = [dict(m) for m in _manifest()]
    manifest[1]["extra_server_args"] = ("--spec-type", "draft-mtp", "--spec-draft-n-max", "3")
    text = llama_swap_config.render(
        manifest, str(tmp_path), extra_args={"gpt-oss-120b": ["--spec-draft-n-max", "5"]}
    )
    cmd = next(ln for ln in text.splitlines() if "/gpt-oss-120b/" in ln)
    assert cmd.count("--spec-draft-n-max") == 1
    assert "--spec-draft-n-max 5" in cmd and "--spec-draft-n-max 3" not in cmd
    # Untargeted catalog flags survive: overriding one knob must not disarm the serving mode.
    assert "--spec-type draft-mtp" in cmd


def test_every_model_is_launched_observable(tmp_path: Path) -> None:
    # `--slots` and `--metrics` are NOT optional garnish. llama-server exposes whether a model
    # is really speculating in exactly two places — `/slots[].speculative` and the /metrics
    # spec-decode counters — and NEITHER endpoint exists without these flags. The only other
    # readable field, `/props`'s `speculative.types`, is dead: the server builds it from a
    # task_params it never populates, so it reads "none" on every build whether or not
    # speculation runs. This box spent an entire investigation concluding MTP was off from
    # that field. If a serving mode cannot be observed it cannot be tuned — it gets
    # misdiagnosed instead, which is why this is pinned.
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path))
    cmds = [ln for ln in text.splitlines() if "llama-server" in ln]
    assert cmds and all("--slots" in ln and "--metrics" in ln for ln in cmds)


def test_ubatch_avoids_the_vulkan_batch_512_corruption(tmp_path: Path) -> None:
    # llama.cpp #27237: the qwen35 hybrid (Gated DeltaNet) emits GARBAGE OUTPUT at ubatch 512
    # on Vulkan, and is clean at 1024/4096. llama-server's own default is 512 — exactly the
    # broken value — and this deployment serves that arch on that backend, so the default must
    # never be inherited. 1024 also trims the big-vocab graph reserve (#23527), which sizes a
    # worst-case logits buffer off n_ubatch x n_vocab (~3 GiB at Qwen3.8's 248k vocab).
    _lay_down(tmp_path)
    text = llama_swap_config.render(_manifest(), str(tmp_path))
    for ln in [x for x in text.splitlines() if "llama-server" in x]:
        assert "-ub 1024" in ln
        assert "-ub 512" not in ln


def test_context_checkpoints_are_pinned_low_as_a_memory_trade(tmp_path: Path) -> None:
    """We serve 2, not llama-server's 32: each checkpoint is a full copy of the recurrent state
    on a hybrid (~150 MiB for Qwen3.8) and device-resident, so the default is ~4.7 GiB per slot
    (llama.cpp #20145, #23371).

    This test used to assert the same value while calling it "rollback depth nothing here uses",
    which is backwards and is the more damaging half: a green test stating the setting is
    correct-by-design stops the next slow-prefill investigation before it starts. Because a
    recurrent state cannot be partially rewound, checkpoints are the ONLY mid-sequence resume
    path a hybrid has — this is the prefix-reuse budget, and 2 is close to none.

    The sweep this docstring asked for has now been run on the box, and 2 lost badly. Against a
    real 33k-token conversation on the hybrid 27B, every turn re-prefilled the WHOLE prompt:
    33,648 tokens, 232 s, repeatedly — because both checkpoints sat bunched at the end and
    `--checkpoint-min-step` was left at llama.cpp's 8192 default, so none covered the divergence
    point. At 16 checkpoints with a 1024 step the same conversation processes only its delta:
    814 tokens in 8.4 s, 1,084 in 15.9 s, and `erasing old context checkpoint` went from 12
    occurrences to 0.

    The count and the step are pinned TOGETHER because either alone is inert: 16 checkpoints
    forced 8192 apart still cannot cover a conversation densely enough.

    The raise is GATED ON THE MEASUREMENT, which is the other half of what this pins. A checkpoint
    is per slot and unbudgeted above `checkpoint_gb`, and the Nemotron hybrids are unmeasured while
    one of them runs two slots — so a flat raise would have put 32 checkpoints of unknown size on
    the box against a budget of zero. A model earns the higher count by having its cost measured;
    everything else keeps the conservative one."""
    _lay_down(tmp_path)
    manifest = _manifest()
    # The fixture carries no `checkpoint_gb`, i.e. every model in it is unmeasured.
    text = llama_swap_config.render(manifest, str(tmp_path))
    server_lines = [ln for ln in text.splitlines() if "llama-server" in ln]
    assert server_lines
    assert all("--ctx-checkpoints 2" in ln for ln in server_lines)
    # The step is shared and unconditional — it is useless on its own and harmless at any count.
    assert all("--checkpoint-min-step 1024" in ln for ln in server_lines)

    # A measured model earns the raise.
    measured = [{**m, "checkpoint_gb": 0.28} for m in manifest]
    text = llama_swap_config.render(measured, str(tmp_path))
    lines = [ln for ln in text.splitlines() if "llama-server" in ln]
    assert lines
    assert all("--ctx-checkpoints 16" in ln for ln in lines)


def test_an_operator_override_of_a_shared_flag_appears_exactly_once(tmp_path: Path) -> None:
    """The single-occurrence invariant, generalised past `--image-min-tokens`.

    `-ub` and `--slot-save-path` are hardcoded in the shared command AND settable through
    `EXTRA_ARG_FLAGS`, so before this an override emitted the flag twice and the served value
    rested on llama.cpp taking the last one — undocumented, and exactly what #1152 refused to
    rely on for the image floor. The command line is the box's only window into the engine, so
    it has to read as a true record of what is served."""
    _lay_down(tmp_path)
    manifest = _manifest()
    mid = str(manifest[0]["id"])
    text = llama_swap_config.render(manifest, str(tmp_path), extra_args={mid: ["-ub", "4096"]})
    line = next(ln for ln in text.splitlines() if "llama-server" in ln and mid in ln)
    assert line.count("-ub") == 1
    assert "-ub 4096" in line
    assert "-ub 1024" not in line  # the shared default is gone, not merely outranked


def test_the_cache_flags_are_overridable_for_the_slow_prefill_investigation(
    tmp_path: Path,
) -> None:
    """`--ctx-checkpoints` and `--cache-reuse` are the two knobs a hybrid's slow-prefill
    investigation turns on, and both are shipped as hardcoded defaults. An operator sweep has
    to REPLACE them, not stack a second copy after them."""
    _lay_down(tmp_path)
    manifest = _manifest()
    mid = str(manifest[0]["id"])
    text = llama_swap_config.render(
        manifest,
        str(tmp_path),
        extra_args={mid: ["--ctx-checkpoints", "16", "--cache-reuse", "0"]},
    )
    line = next(ln for ln in text.splitlines() if "llama-server" in ln and mid in ln)
    assert line.count("--ctx-checkpoints") == 1 and "--ctx-checkpoints 16" in line
    assert line.count("--cache-reuse") == 1 and "--cache-reuse 0" in line
    assert "--ctx-checkpoints 2" not in line and "--cache-reuse 256" not in line


def test_dropping_an_overridden_flag_never_swallows_the_flag_after_it(tmp_path: Path) -> None:
    """A boolean flag has no value to consume. Overriding one must not eat whatever follows
    it — `--swa-full` is the only valueless entry on the allowlist, and it is emitted right
    before other flags on a model that carries it."""
    got = llama_swap_config._drop_operator_overridden(
        ["--swa-full", "-ngl", "999", "-c", "32768"], ["--swa-full"]
    )
    assert got == ["-ngl", "999", "-c", "32768"]
    # A valued flag still takes its value with it, and only its own.
    got2 = llama_swap_config._drop_operator_overridden(["-ub", "1024", "-ngl", "999"], ["-ub"])
    assert got2 == ["-ngl", "999"]
    # Nothing set by the operator, nothing dropped.
    assert llama_swap_config._drop_operator_overridden(["-ub", "1024"], []) == ["-ub", "1024"]


def test_an_unchanged_config_is_not_rewritten(tmp_path: Path) -> None:
    """Rewriting llama-swap.yaml KILLS EVERY RESIDENT MODEL, so an unchanged render must not
    touch the file at all.

    DIAGNOSED on the box 2026-08-20. `_try_regenerate` runs on every settings PUT; `os.replace`
    lands a fresh mtime even for byte-identical content; llama-swap's `--watch-config` poller
    compares MTIME + SIZE, so it reloads; and its `reload()` calls `old.Shutdown()`, which stops
    every running llama-server. The owner reported this for a long time as "staging a model
    unloads gpt-oss" and was repeatedly told it was a display artifact.

    It leaves no trace in the app: the kill happens inside llama-swap, so no `box_events` row is
    written and the vitals surface stays silent. mtime is therefore the assertion — content
    equality alone would still pass while the file was being replaced underneath.
    """
    _lay_down(tmp_path)
    manifest = _manifest()
    path = Path(llama_swap_config.write(str(tmp_path), manifest))
    first = path.stat().st_mtime_ns
    before = path.read_text()

    path2 = Path(llama_swap_config.write(str(tmp_path), manifest))
    assert path2 == path
    assert path.stat().st_mtime_ns == first, (
        "llama-swap.yaml was rewritten for an unchanged config — every resident model just died"
    )
    assert path.read_text() == before


def test_a_real_change_still_rewrites(tmp_path: Path) -> None:
    """The other half: a PUT that genuinely alters a served command MUST write, and the reload
    that follows is correct — the model has to relaunch to pick the change up."""
    _lay_down(tmp_path)
    manifest = _manifest()
    mid = str(manifest[0]["id"])
    path = Path(llama_swap_config.write(str(tmp_path), manifest))
    first = path.stat().st_mtime_ns
    llama_swap_config.write(str(tmp_path), manifest, extra_args={mid: ["-lm", "mmap"]})
    assert path.stat().st_mtime_ns != first, "a real config change did not reach the gateway"
    assert "-lm mmap" in path.read_text()


def test_load_mode_supersedes_the_hardcoded_no_mmap(tmp_path: Path) -> None:
    """`--load-mode` replaces the deprecated `--mmap`/`--no-mmap`/`--mlock` family upstream, and
    llama.cpp warns when both appear: "only the last flag on the command line will take effect".

    Resting on argv order is precisely what #1152 refused to do, so setting the new flag REMOVES
    the old one rather than out-ordering it. This matters beyond tidiness here: `--no-mmap` is
    why the weights are resident twice (GTT plus the page cache the read fills), and an operator
    measuring whether mmap fixes that needs the served command to say unambiguously which mode
    is in effect."""
    _lay_down(tmp_path)
    manifest = _manifest()
    mid = str(manifest[0]["id"])
    text = llama_swap_config.render(
        manifest, str(tmp_path), extra_args={mid: ["--load-mode", "mmap"]}
    )
    line = next(ln for ln in text.splitlines() if "llama-server" in ln and mid in ln)
    assert "--load-mode mmap" in line
    assert "--no-mmap" not in line, "both flags reached the command line; argv order decides"
    # Every OTHER model keeps the default — the override is per model.
    others = [ln for ln in text.splitlines() if "llama-server" in ln and mid not in ln]
    assert others and all("--no-mmap" in ln for ln in others)


def test_the_short_load_mode_alias_supersedes_too(tmp_path: Path) -> None:
    # `-lm` is the same flag; a sweep typed either way must not leave `--no-mmap` behind.
    _lay_down(tmp_path)
    manifest = _manifest()
    mid = str(manifest[0]["id"])
    text = llama_swap_config.render(manifest, str(tmp_path), extra_args={mid: ["-lm", "dio"]})
    line = next(ln for ln in text.splitlines() if "llama-server" in ln and mid in ln)
    assert "-lm dio" in line
    assert "--no-mmap" not in line


def test_an_unrelated_operator_flag_leaves_the_shared_defaults_alone(tmp_path: Path) -> None:
    """Only what the operator actually set is stripped — a sweep of one knob must not silently
    drop the tuned defaults beside it."""
    _lay_down(tmp_path)
    manifest = _manifest()
    mid = str(manifest[0]["id"])
    text = llama_swap_config.render(
        manifest, str(tmp_path), extra_args={mid: ["--spec-draft-p-min", "0.75"]}
    )
    line = next(ln for ln in text.splitlines() if "llama-server" in ln and mid in ln)
    assert "-ub 1024" in line
    assert "--ctx-checkpoints 2" in line
    assert "--cache-reuse 256" in line


def test_an_image_floor_override_replaces_the_catalog_value_rather_than_appending(
    tmp_path: Path,
) -> None:
    """One occurrence on the command line, not two.

    The floor is a catalog FIELD, so an override replaces it by construction — there is no
    flag to rewrite. Two of the same flag would work (llama.cpp takes the last) but leave the
    command line unreadable as a record of what is actually served, which matters on a box
    whose only window into the engine is that string."""
    _lay_down(tmp_path)
    manifest = _manifest()
    manifest[0]["image_min_tokens"] = 1024
    manifest[0]["extra_server_args"] = ["--jinja-extra"]
    text = llama_swap_config.render(
        manifest, str(tmp_path), image_min_tokens={str(manifest[0]["id"]): 4096}
    )
    line = next(ln for ln in text.splitlines() if "--image-min-tokens" in ln)
    assert line.count("--image-min-tokens") == 1
    assert "--image-min-tokens 4096" in line
    # Scoped to the flag: a bare "1024" also matches `-ub 1024` elsewhere on the line.
    assert "--image-min-tokens 1024" not in line
    assert "--jinja-extra" in line  # unrelated catalog flags are untouched


def test_an_image_floor_is_emitted_when_the_catalog_declares_none(tmp_path: Path) -> None:
    """A vision entry with no floor of its own still takes an operator one."""
    _lay_down(tmp_path)
    manifest = _manifest()
    mid = str(manifest[0]["id"])
    text = llama_swap_config.render(manifest, str(tmp_path), image_min_tokens={mid: 2048})
    line = next(ln for ln in text.splitlines() if "llama-server" in ln and mid in ln)
    assert "--image-min-tokens 2048" in line


def test_the_catalog_floor_is_served_when_no_operator_override_exists(tmp_path: Path) -> None:
    """The field alone reaches the command line — the reason it is a field and not a flag
    buried in extra_server_args."""
    _lay_down(tmp_path)
    manifest = _manifest()
    manifest[0]["image_min_tokens"] = 1024
    text = llama_swap_config.render(manifest, str(tmp_path))
    line = next(ln for ln in text.splitlines() if "--image-min-tokens" in ln)
    assert "--image-min-tokens 1024" in line
    # A text-only entry never gets the flag: llama.cpp would not read it.
    assert sum("--image-min-tokens" in ln for ln in text.splitlines()) == 1
