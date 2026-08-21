"""jerv_prime_spec: the (system, tools) a gateway warm-up sends to prime jerv's turn-one
prefix, and the openai_tools serialization it shares with a real turn's payload. The
invariant under test is that the primed shape tracks a real turn (empty read scope, jerv's
allowlist + extra grant, the same hidden set), so the gateway's --cache-reuse can reuse it.
"""

from collections.abc import Collection
from typing import Any, cast

from jbrain.agent.agents import AGENTS
from jbrain.agent.priming import jerv_prime_spec
from jbrain.agent.readtools import OPTIONAL_CANVAS_TOOLS, OPTIONAL_CROP_TOOLS
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.openai_compat import openai_tools
from jbrain.llm.types import LlmTool

# Both canvas tools and the crop tool ride the same model gate.
GATED = OPTIONAL_CANVAS_TOOLS | OPTIONAL_CROP_TOOLS


class _RecordingRegistry:
    """Records the schemas_for arguments and returns a fixed tool list, so a test can assert
    the prime asks for exactly what a real jerv turn does without building the real registry."""

    def __init__(self, tools: list[LlmTool]):
        self._tools = tools
        self.calls: list[tuple[Any, ...]] = []

    def schemas_for(
        self,
        scopes: Collection[str],
        allow: Collection[str] | None = None,
        extra: Collection[str] = (),
        hidden: Collection[str] = (),
    ) -> list[LlmTool]:
        self.calls.append((tuple(scopes), allow, tuple(extra), tuple(sorted(hidden))))
        return self._tools


class _Liveness:
    def __init__(self, hidden: Collection[str], *, boom: bool = False):
        self._hidden = hidden
        self._boom = boom

    async def hidden_tools(self) -> Collection[str]:
        if self._boom:
            raise RuntimeError("comfyui probe failed")
        return self._hidden


def test_openai_tools_serializes_to_the_openai_function_shape() -> None:
    tools = [
        LlmTool(name="web_search", description="Search the web.", input_schema={"type": "object"})
    ]
    assert openai_tools(tools) == [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web.",
                "parameters": {"type": "object"},
            },
        }
    ]


async def test_jerv_prime_spec_uses_the_persona_and_a_real_turns_tool_query() -> None:
    tools = [LlmTool(name="web_search", description="d", input_schema={})]
    reg = _RecordingRegistry(tools)
    system, primed = await jerv_prime_spec(cast(ToolRegistry, reg), None)

    profile = AGENTS["jerv"]
    assert system == profile.prompt
    # Same query a real turn makes (api.agent): empty read scope, jerv's allowlist + extra
    # grant. No liveness → the image tools are not hidden; the canvas pair still is,
    # because it is model-gated and no served model was named for this prime.
    (scopes, allow, extra, hidden) = reg.calls[0]
    assert scopes == () and allow == profile.tools and extra == tuple(profile.extra_tools)
    assert set(hidden) == GATED
    assert primed == openai_tools(tools)


async def test_jerv_prime_spec_hides_image_tools_when_comfyui_is_down() -> None:
    # jerv holds the image-gen tools, so a down ComfyUI must hide them from the prime too —
    # matching a real turn, which hides them (else the primed prefix stops being a prefix).
    reg = _RecordingRegistry([])
    jerv_tools = AGENTS["jerv"].tools
    assert jerv_tools is not None
    hidden = next(iter(jerv_tools - GATED))  # any non-canvas tool jerv holds
    await jerv_prime_spec(cast(ToolRegistry, reg), _Liveness({hidden}))
    # The canvas pair rides alongside: it is model-gated and no served model was named,
    # so it is hidden here exactly as it would be on a turn routed to an unqualified model.
    assert set(reg.calls[0][3]) == {hidden} | GATED


async def test_jerv_prime_spec_is_best_effort_when_the_liveness_probe_fails() -> None:
    # A probe error must not break the prime (or a load): it hides nothing and primes the
    # persona + full tool set (a live ComfyUI is the steady state anyway).
    reg = _RecordingRegistry([])
    await jerv_prime_spec(cast(ToolRegistry, reg), _Liveness((), boom=True))
    # Only the model-gated canvas pair is hidden (no served model named); the probe
    # failure itself hides nothing.
    assert set(reg.calls[0][3]) == GATED
