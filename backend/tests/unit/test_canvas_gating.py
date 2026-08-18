"""Model gating for the canvas pair (AGENT_CANVAS_PLAN §7).

The gate exists because BOTH ungated failure modes are silent. A text-only model draws
blind — it can neither aim at a photo nor check what it drew. An unmeasured *vision*
model is worse: it answers confidently in whatever coordinate base it was trained on,
so every box lands wrong with no error anywhere.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, cast

import pytest

from jbrain.agent.readtools import (
    CANVAS_MODELS,
    IMAGE_TOOL_NAMES,
    OPTIONAL_CANVAS_TOOLS,
    OPTIONAL_CROP_TOOLS,
    canvas_hidden_for_model,
    canvas_hidden_tools,
    compose_hidden_tools,
)

# The crop tool grounds regions with the same vision path, so it rides the same gate.
GATED = OPTIONAL_CANVAS_TOOLS | OPTIONAL_CROP_TOOLS
JERV_LIKE = GATED | IMAGE_TOOL_NAMES | {"web_search"}


class _Router:
    def __init__(self, model: str, boom: bool = False):
        self._model = model
        self._boom = boom

    async def effective_spec(self, task: str, spec_override: str | None = None):
        if self._boom:
            raise RuntimeError("gateway unreachable")
        return "local", spec_override or self._model


class _Liveness:
    def __init__(self, hidden: Collection[str]):
        self._hidden = hidden

    async def hidden_tools(self) -> Collection[str]:
        return self._hidden


# --- the pure gate ----------------------------------------------------------


@pytest.mark.parametrize("model", sorted(CANVAS_MODELS))
def test_qualified_models_keep_the_canvas(model: str) -> None:
    assert canvas_hidden_for_model(model, JERV_LIKE) == frozenset()


@pytest.mark.parametrize(
    "model",
    [
        "qwen3.8-27b-q4",  # vision projector can't run beside MTP — text-only
        "gpt-oss-120b",  # text-only
        "qwen3-vl-30b-a3b",  # vision, but its base has not been measured
        "xai:grok-4.3",  # cloud multimodal, different convention
        None,  # unresolvable
    ],
)
def test_unqualified_models_hide_the_canvas(model: str | None) -> None:
    assert canvas_hidden_for_model(model, JERV_LIKE) == GATED


def test_a_persona_without_the_canvas_hides_nothing() -> None:
    # curator never holds it, so its turn must not probe or hide on its behalf.
    assert canvas_hidden_for_model("gpt-oss-120b", frozenset({"search"})) == frozenset()


# --- the router-resolving form ----------------------------------------------


@pytest.mark.asyncio
async def test_resolves_through_the_router() -> None:
    router = _Router("qwen3.8-27b")
    assert await canvas_hidden_tools(cast(Any, router), None, JERV_LIKE) == frozenset()


@pytest.mark.asyncio
async def test_an_override_onto_a_text_model_hides_it() -> None:
    # The owner steering the conversation onto the fast MTP twin must not leave the
    # model drawing blind.
    router = _Router("qwen3.8-27b")
    hidden = await canvas_hidden_tools(cast(Any, router), "qwen3.8-27b-q4", JERV_LIKE)
    assert hidden == GATED


@pytest.mark.asyncio
async def test_a_routing_failure_hides_rather_than_guesses() -> None:
    # Fail closed: an unknown route must not become "draw anyway".
    assert await canvas_hidden_tools(cast(Any, _Router("x", boom=True)), None, JERV_LIKE) == (GATED)


@pytest.mark.asyncio
async def test_no_router_hides_it() -> None:
    assert await canvas_hidden_tools(None, None, JERV_LIKE) == GATED


# --- composition with the ComfyUI liveness probe ----------------------------


@pytest.mark.asyncio
async def test_composes_liveness_and_canvas_hiding() -> None:
    provider = compose_hidden_tools(_Liveness({"generate_image"}), JERV_LIKE, GATED)
    assert provider is not None
    assert set(await provider()) == {"generate_image"} | GATED


@pytest.mark.asyncio
async def test_canvas_hiding_alone_still_yields_a_provider() -> None:
    # No liveness (or a persona without the image tools) must not drop the canvas gate.
    provider = compose_hidden_tools(None, JERV_LIKE, GATED)
    assert provider is not None
    assert set(await provider()) == GATED


def test_nothing_to_hide_returns_none() -> None:
    # Keeps the common path allocation-free and byte-identical to before this feature.
    assert compose_hidden_tools(None, JERV_LIKE, frozenset()) is None


@pytest.mark.asyncio
async def test_liveness_is_not_probed_for_a_persona_without_image_tools() -> None:
    class _Boom:
        async def hidden_tools(self) -> Collection[str]:  # pragma: no cover - must not run
            raise AssertionError("should not probe liveness for this persona")

    provider = compose_hidden_tools(_Boom(), frozenset({"search"}), GATED)
    assert provider is not None
    assert set(await provider()) == GATED
