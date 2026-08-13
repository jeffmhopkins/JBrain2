"""Prime spec for the interactive `jerv` agent — the (system, tools) a warm-up sends so its
KV prefix matches a real jerv turn's, letting the gateway's `--cache-reuse` make the first
message after a (re)load instant instead of paying the cold persona+tools prefill (the
tens-of-seconds "slow first token" on a big local model).

The shape MUST track a real turn (`jbrain.agent.loop.run_stream`, invoked from
`jbrain.api.agent`): `system` = jerv's persona prompt, `tools` = `schemas_for` over jerv's
allowlist + extra grant with the SAME runtime `hidden` set. Any drift — a tool present in
one and not the other — breaks the leading-prefix match, because under the gateway's
`--jinja` the chat template renders the tool definitions into the prompt's leading tokens.
So both build the tool list the same way, through the same `registry.schemas_for`.

The `hidden` set (image-gen tools removed when ComfyUI is down) is returned alongside so a
caller can detect a liveness FLIP and re-prime: a prime taken while ComfyUI was unreachable
hides the image tools, but a later real turn (ComfyUI up) shows them — a mismatch that
silently defeats the reuse. `jerv_prime_inputs` is the raw form the WarmKeeper primes with
via the router (`LlmTool` objects); `jerv_prime_spec` serializes to the OpenAI tool JSON the
gateway warm-up path (`local_gateway`) sends.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Protocol

import structlog

from jbrain.agent.agents import AGENTS
from jbrain.agent.readtools import IMAGE_TOOL_NAMES
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.openai_compat import openai_tools
from jbrain.llm.types import LlmTool

log = structlog.get_logger()

# The interactive persona whose turn-one latency this prewarm targets (the only agent an
# owner sends a cold first message to right after a restart).
JERV = "jerv"


class HiddenToolsProbe(Protocol):
    """The liveness surface (`jbrain.image_gen.liveness.ImageGenLiveness`) — the tool names
    a backend outage removes this turn. Taken structurally so a test fake satisfies it."""

    async def hidden_tools(self) -> Collection[str]: ...


async def jerv_prime_inputs(
    registry: ToolRegistry, liveness: HiddenToolsProbe | None
) -> tuple[str, list[LlmTool], frozenset[str]]:
    """The (system prompt, tool objects, hidden set) a warm-up primes jerv's turn-one prefix
    with. Mirrors `api.agent`'s turn assembly: an empty read scope (jerv reads no knowledge
    base), jerv's tool allowlist + its extra grant, and the image-gen tools hidden when
    ComfyUI is down. Best-effort on the probe — a failure hides nothing (so the prime bets on
    the steady state, a live ComfyUI). The `hidden` set is returned so the keeper can re-prime
    when it flips (a prime taken while ComfyUI was unreachable no longer matches a live turn)."""
    profile = AGENTS[JERV]
    hidden: frozenset[str] = frozenset()
    if liveness is not None and profile.tools and (profile.tools & IMAGE_TOOL_NAMES):
        try:
            hidden = frozenset(await liveness.hidden_tools())
        except Exception:  # noqa: BLE001 — liveness is best-effort; a probe error hides nothing
            log.warning("priming.hidden_tools_probe_failed", exc_info=True)
    tools = registry.schemas_for((), profile.tools, profile.extra_tools, hidden)
    return profile.prompt, tools, hidden


async def jerv_prime_spec(
    registry: ToolRegistry, liveness: HiddenToolsProbe | None
) -> tuple[str, list[dict[str, Any]]]:
    """The (system prompt, OpenAI tools JSON) the gateway warm-up path sends. The JSON form
    of `jerv_prime_inputs`, serialized through the same `openai_tools` a real turn's payload
    uses so the two shapes can't drift."""
    system, tools, _ = await jerv_prime_inputs(registry, liveness)
    return system, openai_tools(tools)
