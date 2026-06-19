"""Agent selection: the personas a Full Brain session can run as.

Full Brain mode lets the owner start a chat as one of several agents, chosen at
session start (docs/ASSISTANT.md "Agent selection"). An agent bundles the three
choices a chat turn reads — which system prompt frames it, which tools it may
call, and whether it reads the owner's knowledge base:

- `curator` — the default Full Brain personal agent: every in-scope knowledge
  tool, narrowed to the session's selected domains via the RLS firewall.
- `teacher` — a Socratic homework tutor: no tools, no retrieval; it guides by
  questioning, grounded only in the conversation.
- `jerv` — a sandboxed web chatbot: the internet tools (`web_search`, `web_fetch`)
  plus the dataless `current_time`, and NO knowledge-base *tools* at all. Its web
  calls run directly (the owner-approved exception to invariant #9). By owner opt-in
  (`location_aware`) it also receives the owner's coarse, coordinate-free presence as
  injected context — a deliberate, narrow relaxation of the empty-context sandbox so
  jerv can answer "near me" / time-and-place questions; the presence line is
  data-framed (never to be volunteered or sent to the web) and names only, never a
  coordinate. jerv still calls no knowledge-base tool and reads no note/entity/list.

The set is closed and code-defined: a session's stored `agent` is validated
against `AGENT_NAMES` before it is honoured.
"""

from dataclasses import dataclass
from pathlib import Path

from jbrain.llm.promptfile import load_prompt

_PROMPTS = Path(__file__).parent / "prompts"

# The jerv chatbot's internet tools (the `web` permission class). Named here so an
# agent opts in explicitly and the registry's web-tool gate has a single source.
WEB_TOOLS = frozenset({"web_search", "web_fetch"})

# jerv's full allowlist: the web tools plus the dataless clock read. `current_time`
# is not a `web` tool, so it must be allowlisted explicitly (it would otherwise be a
# default-knowledge tool jerv, with its closed allowlist, could not reach).
JERV_TOOLS = WEB_TOOLS | frozenset({"current_time"})

DEFAULT_AGENT = "curator"


@dataclass(frozen=True)
class AgentProfile:
    """One selectable persona. `prompt`/`version`/`strength` come from the agent's
    `.prompt` sidecar; `tools` is the registry allowlist (None = every in-scope
    knowledge tool, a frozenset = exactly those, the empty set = none);
    `reads_knowledge_base` gates retrieval, episodic memory, and skill recall — a
    False agent runs with empty read scopes so even a mis-scoped session reads no
    domain data."""

    name: str
    prompt: str
    version: str
    strength: str
    tools: frozenset[str] | None
    reads_knowledge_base: bool
    # Whether this agent receives the owner's coarse, coordinate-free presence as
    # injected context EVEN without the `location` session scope — an owner opt-in
    # for an agent (jerv) the scope dial doesn't reach. A scope-carrying agent
    # (curator) is location-gated as before; this flag only adds, never removes.
    location_aware: bool = False


def _profile(
    name: str,
    filename: str,
    *,
    tools: frozenset[str] | None,
    reads_knowledge_base: bool,
    location_aware: bool = False,
) -> AgentProfile:
    pf = load_prompt(_PROMPTS / filename)
    return AgentProfile(
        name=name,
        prompt=pf.render(),
        version=pf.version,
        strength=pf.strength,
        tools=tools,
        reads_knowledge_base=reads_knowledge_base,
        location_aware=location_aware,
    )


# Loaded once at import, like every other prompt (DEVELOPMENT.md "Prompts live in
# co-located .prompt files"). `curator` reuses the original Full Brain system
# prompt unchanged, so its persona and pinned version are exactly as before.
AGENTS: dict[str, AgentProfile] = {
    "curator": _profile("curator", "system.prompt", tools=None, reads_knowledge_base=True),
    "teacher": _profile("teacher", "teacher.prompt", tools=frozenset(), reads_knowledge_base=False),
    "jerv": _profile(
        "jerv",
        "jerv.prompt",
        tools=JERV_TOOLS,
        reads_knowledge_base=False,
        location_aware=True,
    ),
}

AGENT_NAMES = frozenset(AGENTS)


def agent_for(name: str) -> AgentProfile:
    """The profile for a stored agent name, falling back to the default for an
    unknown value — a defensive default so an old or malformed row still runs as
    the Full Brain curator rather than failing the turn."""
    return AGENTS.get(name, AGENTS[DEFAULT_AGENT])


def is_agent(name: str) -> bool:
    return name in AGENTS
