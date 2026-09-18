"""jerv_prime_spec: the (system, tools) a gateway warm-up sends to prime jerv's turn-one
prefix, and the openai_tools serialization it shares with a real turn's payload. The
invariant under test is that the primed shape tracks a real turn (empty read scope, jerv's
allowlist + extra grant, the same hidden set), so the gateway's --cache-reuse can reuse it.
"""

import ast
import pathlib
from collections.abc import Collection
from typing import Any, cast

from jbrain.agent.agents import AGENTS
from jbrain.agent.priming import jerv_prime_spec
from jbrain.agent.readtools import CANVAS_MODELS, OPTIONAL_CANVAS_TOOLS, OPTIONAL_CROP_TOOLS
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
    system, primed = await jerv_prime_spec(cast(ToolRegistry, reg))

    profile = AGENTS["jerv"]
    assert system == profile.prompt
    # Same query a real turn makes (api.agent): empty read scope, jerv's allowlist + extra
    # grant. The canvas pair is hidden, because it is model-gated and no served model was
    # named for this prime.
    (scopes, allow, extra, hidden) = reg.calls[0]
    assert scopes == () and allow == profile.tools and extra == tuple(profile.extra_tools)
    assert set(hidden) == GATED
    assert primed == openai_tools(tools)


async def test_jerv_prime_spec_shows_the_canvas_pair_on_a_qualified_model() -> None:
    # A served model on the canvas allowlist hides nothing — the primed tool block matches
    # the turn a real canvas-capable route would send.
    reg = _RecordingRegistry([])
    await jerv_prime_spec(cast(ToolRegistry, reg), next(iter(CANVAS_MODELS)))
    assert set(reg.calls[0][3]) == set()


async def test_jerv_prime_spec_hides_the_canvas_pair_on_an_unqualified_model() -> None:
    # An unqualified (or unknown) served model keeps the model-gated pair hidden, exactly
    # as a turn routed to it would.
    reg = _RecordingRegistry([])
    await jerv_prime_spec(cast(ToolRegistry, reg), "gpt-oss-120b")
    assert set(reg.calls[0][3]) == GATED


# ---- every agent.turn caller must hide what the prime hides -------------------------------
#
# The prime's shape only buys anything if REAL turns send it. `run_stream` builds its tool
# array from `hidden_tools_provider`, so a caller that wires none hides nothing and sends a
# longer array than the prime did — and because `schemas_for` emits alphabetically, `canvas`
# sorts fourth, so the divergence lands ~40 tokens into a ~21k-token tool block. That is not
# one slow turn: the diverged ~30k prompt satisfies `KvPrefixStore`'s prefix-sized guard, so
# the store declines to restore the real prefix, while the WarmKeeper's memo still reads
# primed and declines to re-prime it. The correct prefix is then absent from RAM and
# unrestorable from disk until an eviction or a restart.
#
# `tasks/runner.py` was exactly that caller — every scheduled task and plan continuation.
# A behavioural test would have covered only the caller it was written against, so this is an
# AST scan: the next one fails here until it is wired deliberately.

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "jbrain"

# Loops whose persona's allowlist holds NONE of the model-gated trio, so the provider would
# resolve to `None` and change nothing. Exempt by name rather than by inference, because the
# thing that makes them safe is the persona — give one of these a canvas tool and it silently
# becomes the bug this test exists to catch, which is why the reason is written down here
# rather than left to whoever reads the call site next.
_NO_GATED_TOOLS = {
    "wiki/editor.py",  # the wiki editor's own tools; not an agent.turn persona
    "api/intake.py",  # INTAKE_TOOLS, fail-closed to the intake persona
}


def _agent_loop_constructions() -> list[tuple[pathlib.Path, ast.Call]]:
    found: list[tuple[pathlib.Path, ast.Call]] = []
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            if name == "AgentLoop":
                found.append((path, node))
    return found


def test_every_agent_loop_is_given_a_hidden_tools_provider() -> None:
    """Omitting it is silent, costs a full prefill on the owner's next turn, and leaves the
    prefix unrecoverable until a restart — so it is not a default anyone should get by
    forgetting. Passing an explicitly-None provider is fine and visible; leaving the keyword
    off entirely is what this catches."""
    calls = _agent_loop_constructions()
    assert calls, "the AST scan found no AgentLoop constructions — the check has rotted"
    missing = [
        f"{path.relative_to(_SRC)}:{call.lineno}"
        for path, call in calls
        if not any(kw.arg == "hidden_tools_provider" for kw in call.keywords)
        and str(path.relative_to(_SRC)) not in _NO_GATED_TOOLS
    ]
    assert not missing, (
        "these build an agent turn that hides nothing, so its tool array diverges from the "
        "primed prefix and re-prefills ~21k tokens: " + ", ".join(missing)
    )
