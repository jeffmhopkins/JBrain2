"""Prime spec for the interactive `jerv` agent — the (system, tools) a gateway warm-up
sends so its KV prefix matches a real jerv turn's, letting the gateway's `--cache-reuse`
make the first message after a (re)load instant instead of paying the cold persona+tools
prefill (the tens-of-seconds "slow first token" on a big local model).

The shape MUST track a real turn (`jbrain.agent.loop.run_stream`, invoked from
`jbrain.api.agent`): `system` = jerv's persona prompt, `tools` = `schemas_for` over jerv's
allowlist + extra grant with the SAME runtime `hidden` set. Any drift — a tool present in
one and not the other — breaks the leading-prefix match, because under the gateway's
`--jinja` the chat template renders the tool definitions into the prompt's leading tokens
(harmony's tool-channel declaration sits in the system header, the `# Tools` block right
after the persona). So both build the tool list the same way, through the same
`registry.schemas_for` and the same `openai_tools` serialization.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, Protocol

import structlog

from jbrain.agent.agents import AGENTS
from jbrain.agent.readtools import IMAGE_TOOL_NAMES
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.openai_compat import openai_tools

log = structlog.get_logger()

# The interactive persona whose turn-one latency this prewarm targets (the only agent an
# owner sends a cold first message to right after a restart).
JERV = "jerv"


class HiddenToolsProbe(Protocol):
    """The liveness surface (`jbrain.image_gen.liveness.ImageGenLiveness`) — the tool names
    a backend outage removes this turn. Taken structurally so a test fake satisfies it."""

    async def hidden_tools(self) -> Collection[str]: ...


async def jerv_prime_spec(
    registry: ToolRegistry, liveness: HiddenToolsProbe | None
) -> tuple[str, list[dict[str, Any]]]:
    """The (system prompt, OpenAI tools JSON) a warm-up sends to prime jerv's turn-one
    prefix. Mirrors `api.agent`'s turn assembly: an empty read scope (jerv reads no
    knowledge base), jerv's tool allowlist + its extra grant, and the image-gen tools
    hidden when ComfyUI is down. Best-effort on the probe — a failure hides nothing (a
    live ComfyUI is the steady state, and the persona, the bulk of the prefill, reuses
    regardless of the tool tail)."""
    profile = AGENTS[JERV]
    hidden: Collection[str] = ()
    if liveness is not None and profile.tools and (profile.tools & IMAGE_TOOL_NAMES):
        try:
            hidden = await liveness.hidden_tools()
        except Exception:  # noqa: BLE001 — liveness is best-effort; a probe error hides nothing
            log.warning("priming.hidden_tools_probe_failed", exc_info=True)
    tools = registry.schemas_for((), profile.tools, profile.extra_tools, hidden)
    return profile.prompt, openai_tools(tools)
