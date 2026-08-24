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


async def jerv_prime_inputs(
    registry: ToolRegistry,
    served_model: str | None = None,
) -> tuple[str, list[LlmTool], frozenset[str]]:
    """The (system prompt, tool objects, hidden set) a warm-up primes jerv's turn-one prefix
    with. Mirrors `api.agent`'s turn assembly: an empty read scope (jerv reads no knowledge
    base) and jerv's tool allowlist + its extra grant."""
    profile = AGENTS[JERV]
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
