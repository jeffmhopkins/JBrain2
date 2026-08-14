"""Per-model recommended sampling defaults — the values a model's vendor publishes.

The gap this closes: without it every call inherits the engine default (llama.cpp's
temp 0.8 / top_p 0.95 / top_k 40 / min_p 0.1 for local, the provider default for
cloud), which matches NO model's card. `default_sampling` returns the vendor-recommended
bundle for a resolved (provider, model), which the router applies to every call
(jbrain.llm.router) with a per-task `.prompt` override on top. See
docs/reference/MODEL_PROMPTING.md for the sourcing and the full comparison table.

Where the values live:
  - LOCAL models carry their own `sampling` (and, for a hybrid reasoner whose card
    splits thinking vs non-thinking, `sampling_thinking`) on the catalog entry
    (jbrain.llm.local_catalog.LocalModel) — the same single-source-of-truth as the
    rest of their serving facts.
  - CLOUD models are a small table here (there is no per-model catalog for them).

Client responsibilities, NOT ours: this module returns the vendor's RECOMMENDED
knobs verbatim. Each provider client then drops/renames what its API can't take —
xAI's reasoning Grok rejects presence/frequency penalties and the non-OpenAI knobs
(top_k/min_p); Anthropic rejects temperature+top_p together. So a penalty here for a
local model is honored, the same penalty never reaches Grok even if a task override
adds one. Keeping the recommendation and the provider-quirk enforcement in separate
layers is what lets one `vision.ocr` override (near-greedy + presence_penalty) do the
right thing on the local VL model AND on cloud Grok.
"""

from __future__ import annotations

from jbrain.llm import local_catalog
from jbrain.llm.types import Sampling

# Cloud recommendations, keyed by (provider, served-model). These ARE each vendor's
# documented default, so pinning them is belt-and-suspenders (explicit, and resilient
# to a provider changing its default) rather than a behaviour change:
#   - xAI Grok 4.x: temp 0.7 / top_p 0.95 (AWS/xAI-documented). Penalties are NOT set —
#     Grok 4.x are reasoning models that reject them; the xAI client drops them anyway.
#   - Anthropic Claude: NO entry → empty. Anthropic's own guidance is "just set
#     temperature (default 1.0)"; leaving it unset keeps that default and, crucially,
#     never trips the API's temperature-and-top_p-both-set error. A per-task override
#     can still lower the temperature for an analytical prompt.
CLOUD_SAMPLING: dict[tuple[str, str], Sampling] = {
    ("xai", "grok-4.3"): Sampling(temperature=0.7, top_p=0.95),
}

_EMPTY = Sampling()


def _thinking_active(model: local_catalog.LocalModel, reasoning_effort: str | None) -> bool:
    """Whether this local model is generating a thinking trace for this call — which
    picks `sampling_thinking` over `sampling` for a hybrid whose card splits the two.

    A hybrid reasoner (enable_thinking toggle) thinks unless it was explicitly turned
    OFF (`reasoning_effort == "none"`): the medium bucket sends None to let the model
    use its native default, which for these Qwen hybrids is thinking-on. A non-hybrid
    reasoner (gpt-oss/GLM) only thinks when an effort level is actually set."""
    if model.hybrid_thinking:
        return reasoning_effort != "none"
    return reasoning_effort not in (None, "none")


def default_sampling(provider: str, model: str, reasoning_effort: str | None) -> Sampling:
    """The vendor-recommended sampling bundle for the resolved (provider, model).

    Empty for anything without a recommendation (a cloud model we don't table, or a
    local served name outside the catalog) — the caller then sends nothing and the
    provider default stands. `reasoning_effort` selects a hybrid reasoner's
    thinking-mode values when its card differs from its non-thinking ones."""
    if provider == local_catalog.LOCAL_PROVIDER:
        entry = local_catalog.get_by_served(model)
        if entry is None:
            return _EMPTY
        if entry.sampling_thinking is not None and _thinking_active(entry, reasoning_effort):
            return entry.sampling_thinking
        return entry.sampling
    return CLOUD_SAMPLING.get((provider, model), _EMPTY)
