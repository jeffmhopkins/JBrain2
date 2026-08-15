"""Per-model + per-task sampling: the value object, the recommended-value registry,
and how each provider client renders (or drops) the knobs onto its payload.

The gap this whole surface closes: before it, every call inherited the engine default
(llama.cpp temp 0.8 / top_p 0.95 / top_k 40 / min_p 0.1) regardless of model. These
tests pin that a model now runs at its card's values, that a per-task override merges on
top, and that provider quirks (Anthropic's temp-xor-top_p, xAI's reasoning-model penalty
rejection, llama.cpp's `repeat_penalty` name) are enforced at the client boundary.
"""

import pytest

from jbrain.llm import local_catalog, model_sampling
from jbrain.llm.anthropic import AnthropicClient
from jbrain.llm.openai_compat import OpenAiCompatClient
from jbrain.llm.types import Sampling

# --- the Sampling value object ---------------------------------------------


def test_merge_lets_override_win_field_by_field() -> None:
    base = Sampling(temperature=0.7, top_p=0.8, top_k=20, presence_penalty=1.5)
    merged = base.merge(Sampling(temperature=0.1))
    # The override's field wins; every other field keeps the base value.
    assert merged.temperature == 0.1
    assert merged.top_p == 0.8 and merged.top_k == 20 and merged.presence_penalty == 1.5


def test_merge_treats_zero_as_a_real_value_not_unset() -> None:
    # The bug an `or`-based merge would have: a greedy temperature (0.0) or a top_k=0
    # (disable) override must WIN, not be swallowed as falsy.
    base = Sampling(temperature=0.7, top_k=40, min_p=0.1)
    merged = base.merge(Sampling(temperature=0.0, top_k=0, min_p=0.0))
    assert merged.temperature == 0.0 and merged.top_k == 0 and merged.min_p == 0.0


def test_merge_with_none_is_identity() -> None:
    base = Sampling(temperature=0.6)
    assert base.merge(None) == base


def test_is_empty() -> None:
    assert Sampling().is_empty
    assert not Sampling(temperature=0.0).is_empty  # a set-to-zero field is NOT empty


def test_from_mapping_parses_and_types() -> None:
    s = Sampling.from_mapping({"temperature": 0.1, "top_k": 20, "presence_penalty": 1.5})
    assert s == Sampling(temperature=0.1, top_k=20, presence_penalty=1.5)
    assert isinstance(s.top_k, int) and isinstance(s.temperature, float)


def test_from_mapping_rejects_unknown_key() -> None:
    with pytest.raises(ValueError, match="unknown sampling keys"):
        Sampling.from_mapping({"temperature": 0.1, "top_pp": 0.9})


def test_from_mapping_rejects_non_numeric_and_bool() -> None:
    with pytest.raises(ValueError, match="must be a number"):
        Sampling.from_mapping({"temperature": "hot"})
    # A bool is an int subclass in Python — reject it explicitly, it's never a valid knob.
    with pytest.raises(ValueError, match="must be a number"):
        Sampling.from_mapping({"top_k": True})


# --- the recommended-value registry ----------------------------------------


def test_every_local_model_carries_recommended_sampling() -> None:
    # The whole point: no catalog model is left at the engine default. Each entry pins
    # at least a temperature (its card's headline knob).
    for m in local_catalog.CATALOG:
        assert not m.sampling.is_empty, f"{m.id} has no recommended sampling"
        assert m.sampling.temperature is not None, f"{m.id} pins no temperature"


def test_gpt_oss_disables_min_p_the_llama_default_would_wrongly_prune() -> None:
    # OpenAI says sample from the full distribution; llama.cpp's default min_p 0.1 would
    # fight that. The recommended bundle sets min_p 0.0 (and top_k 0) explicitly.
    s = model_sampling.default_sampling("local", "gpt-oss-120b", "high")
    assert s.temperature == 1.0 and s.top_p == 1.0 and s.top_k == 0 and s.min_p == 0.0


def test_hybrid_reasoner_picks_thinking_vs_non_thinking_by_effort() -> None:
    # Qwen3.8 is hybrid: its card splits sampling by mode. "none" (thinking off) → the
    # Instruct row (temp 0.7, presence_penalty 1.5); a level or None (thinking on) → the
    # thinking row (temp 1.0, no presence penalty).
    off = model_sampling.default_sampling("local", "qwen3.8-27b", "none")
    on_level = model_sampling.default_sampling("local", "qwen3.8-27b", "high")
    on_default = model_sampling.default_sampling("local", "qwen3.8-27b", None)
    assert off.temperature == 0.7 and off.presence_penalty == 1.5
    assert on_level.temperature == 1.0 and on_level.presence_penalty is None
    # None (medium bucket) still means thinking-on for a hybrid — its native default.
    assert on_default == on_level


def test_unified_reasoner_ignores_effort() -> None:
    # Nemotron's card is unified (no split) — same values whether thinking is on or off.
    a = model_sampling.default_sampling("local", "nemotron-3-super-120b", "none")
    b = model_sampling.default_sampling("local", "nemotron-3-super-120b", "high")
    assert a == b == Sampling(temperature=1.0, top_p=0.95, top_k=0, min_p=0.0)


def test_cloud_grok_has_documented_defaults_claude_stays_empty() -> None:
    assert model_sampling.default_sampling("xai", "grok-4.3", "high") == Sampling(
        temperature=0.7, top_p=0.95
    )
    # Anthropic: no entry → empty, so we never send temperature+top_p together (a 4xx) and
    # keep Anthropic's own default (temp 1.0), which is its recommendation.
    assert model_sampling.default_sampling("anthropic", "claude-sonnet-4-6", None).is_empty


def test_unknown_model_returns_empty() -> None:
    assert model_sampling.default_sampling("local", "not-a-model", None).is_empty
    assert model_sampling.default_sampling("xai", "grok-99", None).is_empty


# --- OpenAI-compatible client (xAI + local) --------------------------------


def _openai(provider: str) -> OpenAiCompatClient:
    return OpenAiCompatClient("http://x", "", provider=provider)


def test_local_client_emits_the_full_llama_cpp_sampler_set() -> None:
    payload: dict = {}
    _openai("local")._apply_sampling(
        payload,
        Sampling(
            temperature=0.1,
            top_p=0.8,
            top_k=20,
            min_p=0.0,
            presence_penalty=1.5,
            frequency_penalty=0.2,
            repetition_penalty=1.05,
        ),
    )
    assert payload == {
        "temperature": 0.1,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "frequency_penalty": 0.2,
        # llama.cpp names the repetition knob `repeat_penalty` — the mapping must rename it.
        "repeat_penalty": 1.05,
    }


def test_xai_client_sends_only_temperature_and_top_p() -> None:
    # xAI's OpenAI-compatible API doesn't take top_k/min_p, and Grok 4.x reasoning models
    # REJECT penalties. So a shared override that carries a penalty (the OCR one) reaches
    # xAI as just its temperature/top_p — no 4xx, no separate config path.
    payload: dict = {}
    _openai("xai")._apply_sampling(
        payload,
        Sampling(temperature=0.1, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=1.5),
    )
    assert payload == {"temperature": 0.1, "top_p": 0.95}


def test_openai_client_no_op_on_empty_sampling() -> None:
    payload: dict = {"model": "m"}
    _openai("local")._apply_sampling(payload, Sampling())
    _openai("local")._apply_sampling(payload, None)
    assert payload == {"model": "m"}  # nothing added → provider/engine default stands


# --- Anthropic client -------------------------------------------------------


def test_anthropic_drops_top_p_when_temperature_is_set() -> None:
    # Anthropic rejects temperature AND top_p in one request; temperature (its primary knob)
    # wins and top_p is dropped. top_k pairs with either, penalties/min_p are unsupported.
    payload: dict = {}
    AnthropicClient._apply_sampling(
        payload,
        Sampling(temperature=0.2, top_p=0.9, top_k=20, min_p=0.1, presence_penalty=1.0),
    )
    assert payload == {"temperature": 0.2, "top_k": 20}


def test_anthropic_uses_top_p_only_when_temperature_unset() -> None:
    payload: dict = {}
    AnthropicClient._apply_sampling(payload, Sampling(top_p=0.9))
    assert payload == {"top_p": 0.9}


def test_anthropic_no_op_on_empty() -> None:
    payload: dict = {"model": "m"}
    AnthropicClient._apply_sampling(payload, Sampling())
    assert payload == {"model": "m"}
