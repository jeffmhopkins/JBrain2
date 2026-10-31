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

The `hidden` set (the model-gated canvas pair today) is returned alongside so a caller can
detect a FLIP and re-prime: a prime taken with a different hidden set than a later real
turn's silently defeats the reuse. `jerv_prime_inputs` is the raw form the WarmKeeper primes
with via the router (`LlmTool` objects); `jerv_prime_spec` serializes to the OpenAI tool
JSON the gateway warm-up path (`local_gateway`) sends.
"""

from __future__ import annotations

from typing import Any

from jbrain.agent.agents import AGENTS
from jbrain.agent.readtools import canvas_hidden_for_model
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.llm.openai_compat import openai_tools
from jbrain.llm.types import LlmTool

# The interactive persona whose turn-one latency this prewarm targets (the only agent an
# owner sends a cold first message to right after a restart).
JERV = "jerv"

# Chat personas whose prefix the keeper also primes ONCE and saves to disk, so a new chat with
# one restores it into the chat slot (~2 s) instead of re-reading it there: the warm slot holds
# jerv's prefix, which another persona's prompt leaves at its first token. Each must be sent
# exactly as `prime_inputs` builds it — a profile prompt and tool set fixed at turn assembly,
# no read scope — and the files hold only that prompt and those schemas, no owner data. Not
# the note persona or Minecraft_Dave (their system prompt carries per-chat text), and not the
# curator (its tools follow the session's scopes).
DISK_PRIMED_PERSONAS: tuple[str, ...] = ("archivist",)


async def jerv_prime_inputs(
    registry: ToolRegistry,
    served_model: str | None = None,
) -> tuple[str, list[LlmTool], frozenset[str]]:
    """The (system prompt, tool objects, hidden set) a warm-up primes jerv's turn-one prefix
    with."""
    return await prime_inputs(registry, served_model, JERV)


async def prime_inputs(
    registry: ToolRegistry,
    served_model: str | None = None,
    agent: str = JERV,
) -> tuple[str, list[LlmTool], frozenset[str]]:
    """`agent`'s prime prefix: (system prompt, tool objects, hidden set). Mirrors
    `api.agent`'s turn assembly for a persona that reads no knowledge base: an empty read
    scope and the persona's tool allowlist + its extra grant."""
    profile = AGENTS[agent]
    # The canvas pair is model-gated (readtools.canvas_hidden_for_model), and this prefix
    # must match the turn's tool block exactly or the KV prefix it primed is useless from
    # the tools block onward. The warm path knows WHICH model it is loading, so the gate
    # is answered exactly rather than guessed; `hidden` is returned so the keeper
    # re-primes if the answer ever flips.
    hidden = canvas_hidden_for_model(served_model, profile.tools or frozenset())
    tools = registry.schemas_for((), profile.tools, profile.extra_tools, hidden)
    return profile.prompt, tools, hidden


async def jerv_prime_spec(
    registry: ToolRegistry,
    served_model: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The (system prompt, OpenAI tools JSON) the gateway warm-up path sends. The JSON form
    of `jerv_prime_inputs`, serialized through the same `openai_tools` a real turn's payload
    uses so the two shapes can't drift."""
    system, tools, _ = await jerv_prime_inputs(registry, served_model)
    return system, openai_tools(tools)
