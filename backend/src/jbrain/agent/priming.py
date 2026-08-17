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

import hashlib
import json
from collections.abc import Collection
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    pass

import structlog

from jbrain.agent.agents import AGENTS
from jbrain.agent.readtools import IMAGE_TOOL_NAMES, canvas_hidden_for_model
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
    registry: ToolRegistry,
    liveness: HiddenToolsProbe | None,
    served_model: str | None = None,
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
    # The canvas pair is model-gated (readtools.canvas_hidden_for_model), and this prefix
    # must match the turn's tool block exactly or the KV prefix it primed is useless from
    # the tools block onward. The warm path knows WHICH model it is loading, so the gate
    # is answered exactly rather than guessed; `hidden` is returned so the keeper
    # re-primes if the answer ever flips.
    hidden |= canvas_hidden_for_model(served_model, profile.tools or frozenset())
    tools = registry.schemas_for((), profile.tools, profile.extra_tools, hidden)
    return profile.prompt, tools, hidden


async def jerv_prime_spec(
    registry: ToolRegistry,
    liveness: HiddenToolsProbe | None,
    served_model: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The (system prompt, OpenAI tools JSON) the gateway warm-up path sends. The JSON form
    of `jerv_prime_inputs`, serialized through the same `openai_tools` a real turn's payload
    uses so the two shapes can't drift."""
    system, tools, _ = await jerv_prime_inputs(registry, liveness, served_model)
    return system, openai_tools(tools)


# The KV-slot state file carries NO model identity — llama.cpp validates layer count, KV row
# size and `n_stream`, and nothing else. A file written by a different build, quant or prompt
# therefore restores cleanly and is wrong, so the CALLER owns validity. We put the fingerprint
# in the FILENAME rather than a sidecar: a changed input yields a different name, the restore
# simply misses, and the keeper falls through to a normal prefill. Self-invalidating, with no
# metadata file to drift from the bytes it describes.
def jerv_prime_fingerprint(
    system: str,
    tools: list[dict[str, Any]],
    *,
    build_info: str,
    chat_template: str,
    n_ctx: object,
    n_slots: object,
    today_utc: str,
) -> str:
    """A short digest of everything that changes the bytes llama-server would prefill.

    Deliberately hashes the CONTENT, not version numbers: `.prompt`/`.tool` versions are
    hand-authored and a prose edit can land without one being bumped, which would leave a stale
    cache looking valid. `chat_template` and `build_info` come from the gateway's own `/props`,
    and cover the case the box makes likely — updates rebuild llama.cpp on master by default,
    and a template change rewrites the prefix upstream of everything.

    `today_utc` is in here because the gpt-oss template renders `Current date:` into the system
    header (verified on-box), so a cache is stale at UTC midnight — the CONTAINER's midnight,
    which is not the owner's.
    """
    payload = json.dumps(
        {
            "v": 1,  # bump when the SET of inputs changes, else old names look valid to new code
            "system": system,
            "tools": tools,
            "build": build_info,
            "template": chat_template,
            "n_ctx": n_ctx,
            "n_slots": n_slots,
            "date": today_utc,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def jerv_slot_filename(served_model: str, fingerprint: str) -> str:
    """The state file's name. Bare basename with no separator or colon — llama.cpp validates
    it with `fs_validate_filename` and rejects anything else."""
    safe = "".join(c if c.isalnum() or c in "-_." else "-" for c in served_model)
    return f"jerv-{safe}-{fingerprint}.bin"
