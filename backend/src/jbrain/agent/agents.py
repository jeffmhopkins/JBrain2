"""Agent selection: the personas a Full Brain session can run as.

Full Brain mode lets the owner start a chat as one of several agents, chosen at
session start (docs/ASSISTANT.md "Agent selection"). An agent bundles the three
choices a chat turn reads — which system prompt frames it, which tools it may
call, and whether it reads the owner's knowledge base:

- `curator` — the default Full Brain personal agent: every in-scope knowledge
  tool, narrowed to the session's selected domains via the RLS firewall.
- `teacher` — a Socratic homework tutor: no tools, no retrieval; it guides by
  questioning, grounded only in the conversation.
- `jerv` — a sandboxed web chatbot: the internet tools (`web_search`, `web_fetch`),
  the dataless `current_time`, the owner-approved `current_location` (a `web`-
  gated, jerv-only on-box read of the owner's coarse, coordinate-free presence), and
  the read-only host-metrics summary (`query_server_metrics` — hardware telemetry,
  not owner data), but NO knowledge-base tools at all. Its web calls run directly
  (the owner-approved
  exception to invariant #9). `current_location` is the deliberate, narrow relaxation
  of the empty-context sandbox so jerv can answer "near me" / local questions — it
  returns a place name only, never a coordinate, and jerv's prompt forbids
  volunteering it or sending it to the web. jerv still calls no knowledge-base tool
  and reads no note/entity/list/appointment.
- `archivist` — a sandboxed Gmail organizer: the `gmail_*` tools (search/read,
  list/create labels, label/archive), present only when Gmail is configured, plus a
  private cross-session memory (`archivist_memory_read`/`write`) over an owner-only
  scratchpad table so a 20-year cleanup continues across sessions. Like jerv it reads
  no knowledge base, so no owner note/entity data is in context while it triages mail;
  its Gmail writes act only on the owner's own mailbox and never delete; its memory is
  its own notes, not the owner's (docs/EMAIL_ARCHIVIST_PLAN.md).

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

# jerv's full allowlist: the internet tools, the dataless clock read, the
# owner-approved coarse location read, the local image-generation tools, the
# local audio transcription, the local video analysis, and the host-metrics read.
# `current_time` is allowlisted explicitly (a default-knowledge tool jerv's closed
# allowlist could not otherwise reach); `current_location`, `generate_image`/
# `edit_image`/`analyze_image`, `transcribe`, and `analyze_video` are `web`-gated
# jerv-only tools (on-box, no egress, opt-in — the image tools drive the localhost
# ComfyUI, docs/IMAGE_GEN_PLAN.md; `transcribe` drives the on-box whisper gateway,
# docs/WHISPER_TRANSCRIPTION_PLAN.md; `analyze_video` reads a video via frame
# sampling + whisper, docs/VIDEO_ANALYSIS_PLAN.md). The image/transcribe/video tools
# are absent from the registry when their backend is unconfigured, so allowlisting
# them here is harmless on a box without it. `query_server_metrics` is host
# hardware telemetry (CPU/mem/disk/GPU/fans), not owner knowledge — owner-opted
# in here so jerv can answer "how's the box doing?"; the metrics tables' owner-only
# RLS is still the boundary, and jerv reads no note/entity/list/appointment.
JERV_TOOLS = WEB_TOOLS | frozenset(
    {
        "current_time",
        "current_location",
        "generate_image",
        "edit_image",
        "analyze_image",
        "transcribe",
        "analyze_video",
        "query_server_metrics",
    }
)

# The archivist persona's allowlist: the Gmail organize-an-inbox tools and nothing
# else (the `web` permission class, opt-in like jerv's). The archivist reads no
# knowledge base and holds no other tool, so no owner note/entity data is in context
# while it triages mail (docs/EMAIL_ARCHIVIST_PLAN.md).
GMAIL_TOOLS = frozenset(
    {
        "gmail_search",
        "gmail_read",
        "gmail_list_labels",
        "gmail_create_label",
        "gmail_label",
        "gmail_archive",
        "gmail_count",
        "gmail_sender_breakdown",
        "gmail_bulk_label",
    }
)

# The archivist's cross-session memory: a `web`-gated read/write pair over the
# owner-only `archivist_memory` scratchpad, so it continues a 20-year cleanup across
# sessions instead of starting blind. Owner-only (its own notes), never the knowledge
# base (docs/EMAIL_ARCHIVIST_PLAN.md).
MEMORY_TOOLS = frozenset({"archivist_memory_read", "archivist_memory_write"})

# The archivist's full allowlist: the Gmail organize-an-inbox tools, its memory, and
# `current_time` — a shared default-knowledge tool (also in JERV_TOOLS) it needs to
# ground relative date queries (older_than:, before:/after:) against today, since
# date-by-date filing is the heart of the job. Every turn already prepends today's date
# (now_block); the tool covers an explicit fresh / other-zone read.
ARCHIVIST_TOOLS = GMAIL_TOOLS | MEMORY_TOOLS | frozenset({"current_time"})

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
    "teacher": _profile("teacher", "teacher.prompt", tools=frozenset(), reads_knowledge_base=False),
    "jerv": _profile("jerv", "jerv.prompt", tools=JERV_TOOLS, reads_knowledge_base=False),
    "archivist": _profile(
        "archivist", "archivist.prompt", tools=ARCHIVIST_TOOLS, reads_knowledge_base=False
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
