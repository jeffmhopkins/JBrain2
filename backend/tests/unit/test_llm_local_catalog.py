"""The local-model catalog and how it drives the opt-in settings choices."""

import json
from typing import Any

from jbrain.config import Settings
from jbrain.llm import local_catalog
from jbrain.llm.providers import provider_choices
from jbrain.llm.router import PROVIDERS, _split_spec


def test_catalog_entries_are_well_formed() -> None:
    ids = [m.id for m in local_catalog.CATALOG]
    assert len(ids) == len(set(ids)), "catalog ids must be unique"
    for m in local_catalog.CATALOG:
        # Every spec parses and names the local provider the router knows.
        provider, model = _split_spec(m.id, m.spec)
        assert provider == "local" and provider in PROVIDERS
        assert model == m.served_model
        assert m.tiers, "a model must serve at least one tier"
        # A vision-tier model must actually be vision-capable (and ship a projector).
        if "vision" in m.tiers:
            assert m.supports_vision and m.mmproj_include is not None


def test_reasoning_served_models_are_exactly_the_reasoning_capable_ones() -> None:
    # The router's gating set is derived from the catalog flag; gpt-oss and GLM-Air
    # are the reasoning models, the Qwen Instruct/VL and Llama variants are not.
    expected = {m.served_model for m in local_catalog.CATALOG if m.supports_reasoning}
    assert expected == local_catalog.REASONING_SERVED_MODELS
    assert "gpt-oss-120b" in local_catalog.REASONING_SERVED_MODELS
    assert "glm-4.5-air" in local_catalog.REASONING_SERVED_MODELS
    assert "qwen3-30b-a3b" not in local_catalog.REASONING_SERVED_MODELS


def test_recommended_set_is_the_two_resident_models() -> None:
    assert local_catalog.recommended_ids() == ("qwen3-vl-30b", "gpt-oss-120b")


def test_context_window_reads_the_catalog_then_falls_back() -> None:
    # Every catalog model serves its own window (the same value the setup script
    # stamps into the llama-swap config and the meter divides by).
    for m in local_catalog.CATALOG:
        assert local_catalog.context_window(m.served_model) == m.context_window
    # gpt-oss-120b runs its full native window; the rest use the gateway default.
    gpt_oss = local_catalog.get("gpt-oss-120b")
    assert gpt_oss is not None and gpt_oss.context_window == 131072
    # An unknown served name (a model outside the catalog) gets the safe default.
    assert (
        local_catalog.context_window("mystery-model")
        == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
        == 32768
    )


def test_max_context_window_is_native_when_known_else_the_served_default() -> None:
    # The override ceiling is the model's native window; never below its served
    # default (the picker must always be able to keep the default selected).
    for m in local_catalog.CATALOG:
        assert m.max_context_window >= m.context_window
        if m.native_context_window:
            assert m.max_context_window == m.native_context_window
        else:
            assert m.max_context_window == m.context_window
    # The coder serves its FULL native 256k window — code mode wants the whole context,
    # so its served default and native ceiling coincide.
    coder = local_catalog.get("qwen3-coder-next")
    assert coder is not None
    assert coder.context_window == 262144 and coder.max_context_window == 262144
    # gpt-oss already serves its full native window, so default and ceiling coincide.
    gpt_oss = local_catalog.get("gpt-oss-120b")
    assert gpt_oss is not None and gpt_oss.max_context_window == 131072


def test_selected_keeps_catalog_order_and_drops_unknown() -> None:
    got = local_catalog.selected(["gpt-oss-120b", "nope", "qwen3-vl-30b"])
    assert [m.id for m in got] == ["qwen3-vl-30b", "gpt-oss-120b"]


def test_manifest_is_json_with_provisioning_fields() -> None:
    manifest = json.loads(local_catalog._manifest(["qwen3-vl-30b"]))
    (entry,) = manifest
    assert entry["hf_repo"] == "Qwen/Qwen3-VL-30B-A3B-Instruct-GGUF"
    assert entry["gguf_include"] and entry["mmproj_include"]
    assert entry["served_model"] == "qwen3-vl-30b-a3b"


def test_qwen3_235b_is_a_text_only_alt_high_tier_at_3bit() -> None:
    m = local_catalog.get("qwen3-235b-a22b")
    assert m is not None
    assert m.tiers == ("high",)
    assert not m.supports_vision and m.mmproj_include is None
    # Instruct-2507 is non-thinking — not in the reasoning gating set.
    assert not m.supports_reasoning
    assert m.served_model not in local_catalog.REASONING_SERVED_MODELS
    # The 3-bit dynamic quant the manifest pulls.
    assert m.quant == "UD-Q3_K_XL"
    assert "UD-Q3_K_XL" in m.gguf_include
    assert m.hf_repo == "unsloth/Qwen3-235B-A22B-Instruct-2507-GGUF"
    # Alternate, not part of the default resident set the install prompt offers.
    assert m.id not in local_catalog.recommended_ids()
    assert m.spec == "local:qwen3-235b-a22b"


def test_qwen3_next_is_a_text_only_alt_high_tier() -> None:
    m = local_catalog.get("qwen3-next-80b-a3b")
    assert m is not None
    assert m.tiers == ("high",)
    assert not m.supports_vision and m.mmproj_include is None
    # Alternate, not part of the default resident set the install prompt offers.
    assert m.id not in local_catalog.recommended_ids()
    assert m.spec == "local:qwen3-next-80b-a3b"


def test_qwen3_next_thinking_is_a_reasoning_deepseek_format_alt() -> None:
    # The Thinking checkpoint is a separate model from the Instruct sibling: same size,
    # but an always-on reasoning model that emits <think> and needs --reasoning-format
    # deepseek wired. It is NOT a hybrid — it has no thinking off switch.
    m = local_catalog.get("qwen3-next-80b-a3b-thinking")
    assert m is not None
    assert m.tiers == ("high",)
    assert not m.supports_vision and m.mmproj_include is None
    assert m.supports_reasoning and m.reasoning_format == "deepseek"
    assert not m.hybrid_thinking
    assert m.supports_tools
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    assert "Thinking" in m.hf_repo
    # Alternate, not part of the default resident set the install prompt offers.
    assert m.id not in local_catalog.recommended_ids()
    # The <think>-emitting entries all pin --reasoning-format deepseek: this always-on
    # checkpoint plus the two Qwen hybrids. The harmony/GLM reasoners keep llama.cpp's
    # auto (empty), so reasoning_format is NOT just a synonym for supports_reasoning.
    assert {x.id for x in local_catalog.CATALOG if x.reasoning_format} == {
        "qwen3-next-80b-a3b-thinking",
        "nemotron-3-super-120b",
        "qwen3.5-0.8b",
        "qwen3.5-4b",
    }


def test_nemotron_3_super_is_a_hybrid_reasoner_alt_high_tier_at_q4() -> None:
    # NVIDIA's US-made 120B/12B agentic reasoner: an alternate high-tier MoE at
    # Unsloth's UD-Q4_K_XL. A HYBRID reasoner (enable_thinking chat-template toggle)
    # that emits <think>, so it pins --reasoning-format deepseek like the Qwen hybrids.
    m = local_catalog.get("nemotron-3-super-120b")
    assert m is not None
    assert m.tiers == ("high",)
    assert not m.supports_vision and m.mmproj_include is None
    assert m.supports_tools
    assert m.supports_reasoning
    assert m.reasoning_format == "deepseek"
    assert m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    # The 4-bit dynamic quant the manifest pulls, from NVIDIA's Unsloth GGUF repo.
    assert m.quant == "UD-Q4_K_XL"
    assert "UD-Q4_K_XL" in m.gguf_include
    assert m.hf_repo == "unsloth/NVIDIA-Nemotron-3-Super-120B-A12B-GGUF"
    assert m.spec == "local:nemotron-3-super-120b"
    # Serves the conservative gateway default with its native 1M window as the ceiling.
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 1048576
    # Alternate, not part of the default resident set the install prompt offers.
    assert m.id not in local_catalog.recommended_ids()


def test_qwen35_0_8b_is_a_tiny_hybrid_reasoner_low_tier() -> None:
    # The smallest catalog entry: a fast, cheap Q8 worker for undemanding side
    # projects. A Qwen HYBRID reasoner — thinking is a chat-template toggle, so its
    # level is set per task (incl. "none" to run it as a snappy Instruct model).
    m = local_catalog.get("qwen3.5-0.8b")
    assert m is not None
    assert m.tiers == ("low",)
    assert not m.supports_vision and m.mmproj_include is None
    assert m.supports_tools
    # Reasoning-capable and hybrid: in the gating set, <think> split via deepseek,
    # and the adapter drives its on/off through enable_thinking.
    assert m.supports_reasoning
    assert m.reasoning_format == "deepseek"
    assert m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    # Q8_0 (near-lossless at this size), not the Q4 the big MoE entries use.
    assert m.quant == "Q8_0"
    assert "Q8_0" in m.gguf_include
    assert m.hf_repo == "unsloth/Qwen3.5-0.8B-GGUF"
    assert m.spec == "local:qwen3.5-0.8b"
    # Serves the conservative gateway default with the full native 256k as the ceiling.
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 262144
    # Opt-in, not part of the default resident set the install prompt offers.
    assert m.id not in local_catalog.recommended_ids()


def test_qwen35_4b_is_a_small_hybrid_reasoner_low_tier() -> None:
    # The step up from the 0.8b tiny model: a small dense Q8 low-tier daily driver.
    # Also a Qwen hybrid reasoner (thinking is a chat-template toggle).
    m = local_catalog.get("qwen3.5-4b")
    assert m is not None
    assert m.tiers == ("low",)
    assert not m.supports_vision and m.mmproj_include is None
    assert m.supports_tools
    assert m.supports_reasoning
    assert m.reasoning_format == "deepseek"
    assert m.hybrid_thinking
    assert m.served_model in local_catalog.REASONING_SERVED_MODELS
    assert m.quant == "Q8_0"
    assert "Q8_0" in m.gguf_include
    assert m.hf_repo == "unsloth/Qwen3.5-4B-GGUF"
    assert m.spec == "local:qwen3.5-4b"
    assert m.context_window == local_catalog.DEFAULT_LOCAL_CONTEXT_WINDOW
    assert m.native_context_window == 262144
    assert m.id not in local_catalog.recommended_ids()


def test_footprint_gb_is_weights_plus_kv_scaled_by_window() -> None:
    gpt = local_catalog.get("gpt-oss-120b")
    vl = local_catalog.get("qwen3-vl-30b")
    assert gpt is not None and vl is not None
    # gpt-oss at its native 128k window: weights 59 + KV 4.5 (the 128k reference) = 63.5.
    assert local_catalog.footprint_gb(gpt, 131072) == 63.5
    # KV scales linearly with the window: vl at 32k = 32 + 6*(32768/131072) = 33.5.
    assert local_catalog.footprint_gb(vl, 32768) == 33.5
    # Half the window → half the KV term (16k = 32 + 0.75).
    assert local_catalog.footprint_gb(vl, 16384) == 32.75
    # A measured on-disk size overrides the nominal weights estimate.
    assert local_catalog.footprint_gb(vl, 32768, disk_gb=31.9) == 33.4


def test_get_by_served_maps_served_name_to_catalog_entry() -> None:
    m = local_catalog.get_by_served("qwen3-vl-30b-a3b")
    assert m is not None and m.id == "qwen3-vl-30b"
    assert local_catalog.get_by_served("not-a-model") is None


def _settings(**kw: Any) -> Settings:
    # Both cloud keys present — provider_choices hides a keyless cloud provider, so
    # tests that expect grok/claude to be offered must supply the keys.
    kw.setdefault("xai_api_key", "test-xai")
    kw.setdefault("anthropic_api_key", "test-anthropic")
    return Settings(database_url="postgresql+asyncpg://nobody@localhost:1/none", **kw)


def test_choices_are_cloud_only_when_local_hosting_off() -> None:
    ids = [c.id for c in provider_choices(_settings())]
    assert ids == ["grok", "claude"]


def test_choices_add_selected_local_models_when_enabled() -> None:
    choices = provider_choices(
        _settings(local_llm_enabled=True, local_models=["qwen3-vl-30b", "gpt-oss-120b"])
    )
    by_id = {c.id: c for c in choices}
    assert set(by_id) == {"grok", "claude", "qwen3-vl-30b", "gpt-oss-120b"}
    assert by_id["qwen3-vl-30b"].spec == "local:qwen3-vl-30b-a3b"
    assert by_id["qwen3-vl-30b"].supports_vision is True
    assert by_id["gpt-oss-120b"].supports_vision is False


def test_enabled_but_empty_selection_falls_back_to_generic_local() -> None:
    choices = provider_choices(_settings(local_llm_enabled=True, local_llm_model="my-model"))
    by_id = {c.id: c for c in choices}
    assert by_id["local"].spec == "local:my-model"
