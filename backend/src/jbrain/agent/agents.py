"""Agent selection: the personas a Full Brain session can run as.

Full Brain mode lets the owner start a chat as one of several agents, chosen at
session start (docs/ASSISTANT.md "Agent selection"). An agent bundles the three
choices a chat turn reads — which system prompt frames it, which tools it may
call, and whether it reads the owner's knowledge base:

- `curator` — the default Full Brain personal agent: every in-scope knowledge
  tool, narrowed to the session's selected domains via the RLS firewall.
- `teacher` — a Socratic homework tutor: no tools, no retrieval; it guides by
  questioning, grounded only in the conversation.
- `jerv` — a sandboxed web chatbot: only the internet tools (`web_search`,
  `web_fetch`) and NO knowledge-base access at all, so it touches no owner domain
  data. Its web calls run directly (the owner-approved exception to invariant #9
  is bounded by this empty-context sandbox).

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


def _profile(
    name: str,
    filename: str,
    *,
    tools: frozenset[str] | None,
    reads_knowledge_base: bool,
) -> AgentProfile:
    pf = load_prompt(_PROMPTS / filename)
    return AgentProfile(
        name=name,
        prompt=pf.render(),
        version=pf.version,
        strength=pf.strength,
        tools=tools,
        reads_knowledge_base=reads_knowledge_base,
    )


# Loaded once at import, like every other prompt (DEVELOPMENT.md "Prompts live in
# co-located .prompt files"). `curator` reuses the original Full Brain system
# prompt unchanged, so its persona and pinned version are exactly as before.
AGENTS: dict[str, AgentProfile] = {
    "curator": _profile("curator", "system.prompt", tools=None, reads_knowledge_base=True),
    "teacher": _profile(
        "teacher", "teacher.prompt", tools=frozenset(), reads_knowledge_base=False
    ),
    "jerv": _profile("jerv", "jerv.prompt", tools=WEB_TOOLS, reads_knowledge_base=False),
}

AGENT_NAMES = frozenset(AGENTS)


def agent_for(name: str) -> AgentProfile:
    """The profile for a stored agent name, falling back to the default for an
    unknown value — a defensive default so an old or malformed row still runs as
    the Full Brain curator rather than failing the turn."""
    return AGENTS.get(name, AGENTS[DEFAULT_AGENT])


def is_agent(name: str) -> bool:
    return name in AGENTS
