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
