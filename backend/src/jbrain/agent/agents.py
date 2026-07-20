"""Agent selection: the personas a Full Brain session can run as.

Full Brain mode lets the owner start a chat as one of several agents, chosen at
session start (docs/reference/ASSISTANT.md "Agent selection"). An agent bundles the three
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
  its own notes, not the owner's (docs/archive/EMAIL_ARCHIVIST_PLAN.md).

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

# The spawn primitive (docs/archive/SUBAGENT_SPAWNING_PLAN.md): the single tool jerv — and,
# for nesting, the research/review children — calls to launch a bounded fan of
# web-sandboxed children. It is `web`-gated (it drives web-class egress through its
# children) and is in the registry's NEVER_DEFAULT set, so curator's `tools=None`
# wildcard can never absorb it (review B3).
SPAWN_TOOL = "spawn_subagent"

# The deep-research primitive (docs/proposed/DEEP_RESEARCH_TOOL_PLAN.md): jerv's one-call
# bounded plan→gather→reflect→refill→synthesize→critique run over the web-sandboxed fan.
# Like `spawn_subagent` it is `web`-gated and NEVER_DEFAULT, so curator's `tools=None`
# wildcard can never absorb it; only jerv holds it, and a child never does (a child is a
# leaf — the tool refuses at depth > 0).
DEEP_RESEARCH_TOOL = "deep_research"

# jerv's full allowlist: the internet tools, the dataless clock read, the
# owner-approved coarse location read, the weather (forecast + history) + hurricane
# lookups, the local
# image-generation tools, the local audio transcription, the local video analysis,
# and the host-metrics read.
# `current_time` is allowlisted explicitly (a default-knowledge tool jerv's closed
# allowlist could not otherwise reach); `current_location`, `weather`, `hurricane`,
# `generate_image`/
# `edit_image`/`analyze_image`, `transcribe`, `analyze_video`, and `analyze_stream`
# are `web`-gated jerv-only tools (`analyze_stream` reads a video URL — live or VOD —
# via yt-dlp + ffmpeg, docs/archive/STREAM_ANALYSIS_PLAN.md; the SSRF-guarded second
# outbound leg after web_fetch). `weather` runs directly over the pinned Open-Meteo upstreams (it
# sends only a public place name / city centre, never the owner's precise fix — the
# location firewall). The on-box tools (image/transcribe/video) drive the localhost
# ComfyUI, docs/archive/IMAGE_GEN_PLAN.md; `transcribe` drives the on-box whisper gateway,
# docs/archive/WHISPER_TRANSCRIPTION_PLAN.md; `analyze_video` reads a video via frame
# sampling + whisper, docs/archive/VIDEO_ANALYSIS_PLAN.md). The image/transcribe/video tools
# are absent from the registry when their backend is unconfigured, so allowlisting
# them here is harmless on a box without it. `query_server_metrics` is host
# hardware telemetry (CPU/mem/disk/GPU/fans), not owner knowledge — owner-opted
# in here so jerv can answer "how's the box doing?"; the metrics tables' owner-only
# RLS is still the boundary, and jerv reads no note/entity/list/appointment.
JERV_TOOLS = WEB_TOOLS | frozenset(
    {
        "current_time",
        "current_location",
        "weather",
        "weather_history",
        "hurricane",
        "generate_image",
        "edit_image",
        "analyze_image",
        "transcribe",
        "analyze_video",
        "analyze_stream",
        # Grab a single still from a video (URL or attachment) at a timestamp as a
        # first-class chat image analyze_image/compare_images can read by id
        # (VIDEO_IMAGE_TOOLS_PLAN.md) — the "screenshot the video at this moment" step.
        "grab_frame",
        # Fetch a web image's bytes so jerv can actually SEE it (web_fetch is text-only) —
        # persisted as a chat image analyze_image/compare_images read by id.
        "fetch_image",
        # Compare two or more chat images (grabbed frames, fetched web images, attachments)
        # and show the owner a side-by-side (VIDEO_IMAGE_TOOLS_PLAN.md).
        "compare_images",
        # Search the external-source video corpus (analysed YouTube videos). Sandboxed
        # jerv-only alongside web_search; reads the general-domain corpus via a
        # purpose-built scope, never the owner's notes (EXTERNAL_VIDEO_INGESTION_PLAN.md).
        "search_external_video",
        # Enumerate / count the whole library (title, channel, date, length per video) with an
        # exact total — the browse/count companion to the content search, so "what's in my
        # library?" answers from a real listing, not a fuzzy query.
        "list_external_video",
        # Read one library video's FULL transcript (search_external_video → read_external_video,
        # the web_search → web_fetch pattern) when a single excerpt isn't enough.
        "read_external_video",
        # SHOW one library video as the video-analysis card (embed + frame timeline + tabs),
        # rebuilt from stored corpus data — when the owner wants to see/watch it, not read it.
        "show_external_video",
        # Stage the removal of one library video for the owner's inline approval — jerv proposes,
        # the owner approves, the trusted executor hard-deletes (jerv never deletes directly).
        "remove_external_video",
        # List a channel's new uploads not yet in the corpus (the scheduling Task calls
        # this, then analyze_stream on each new match).
        "check_channel",
        "query_server_metrics",
        # The spawn primitive — jerv is the spawner (docs/archive/SUBAGENT_SPAWNING_PLAN.md).
        SPAWN_TOOL,
        # The deep-research primitive — jerv orchestrates a bounded research run over the
        # same fan (docs/proposed/DEEP_RESEARCH_TOOL_PLAN.md).
        DEEP_RESEARCH_TOOL,
        # The deep-research report library — browse / search / read / show / remove the
        # reports deep_research persisted, the same corpus pattern as the video tools. read_
        # returns a report's FULL text, so a follow-up turn can reference an earlier run
        # (the chat history keeps only jerv's summary of it).
        "list_research_report",
        "search_research_report",
        "read_research_report",
        "show_research_report",
        "remove_research_report",
    }
)

# The archivist persona's allowlist: the Gmail organize-an-inbox tools and nothing
# else (the `web` permission class, opt-in like jerv's). The archivist reads no
# knowledge base and holds no other tool, so no owner note/entity data is in context
# while it triages mail (docs/archive/EMAIL_ARCHIVIST_PLAN.md).
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
# base (docs/archive/EMAIL_ARCHIVIST_PLAN.md).
MEMORY_TOOLS = frozenset({"archivist_memory_read", "archivist_memory_write"})

# The archivist's full allowlist: the Gmail organize-an-inbox tools, its memory, and
# `current_time` — a shared default-knowledge tool (also in JERV_TOOLS) it needs to
# ground relative date queries (older_than:, before:/after:) against today, since
# date-by-date filing is the heart of the job. Every turn already prepends today's date
# (now_block); the tool covers an explicit fresh / other-zone read.
ARCHIVIST_TOOLS = GMAIL_TOOLS | MEMORY_TOOLS | frozenset({"current_time"})

# The closed set of spawnable child personas. `spawn_subagent` validates a requested
# persona against this set BEFORE calling `agent_for` — which falls back to the
# KB-capable curator on an unknown name — so a malformed or injected persona is
# refused, never resolved to a knowledge agent.
SUBAGENT_PERSONAS = frozenset(
    {"research", "review", "summarize", "research_library", "review_library"}
)

# research / review children: the internet tools and the dataless clock — and NO
# `spawn_subagent`. A child is always a leaf: child-initiated nesting was removed
# (the model wouldn't use it reliably and it carried the depth>=1 brief-laundering
# surface); `waves` gives jerv orchestrator-declared structure instead. Deliberately
# NO `current_location` (M2) either: the location read is never in a child persona.
# This allowlist is a ceiling — a child's effective tools are still clamped to the
# parent's at dispatch.
RESEARCH_TOOLS = WEB_TOOLS | frozenset({"current_time"})
REVIEW_TOOLS = RESEARCH_TOOLS
# research_library / review_library children: the video-library corpus tools and the
# dataless clock, and NO web tools — `deep_research`'s `sources=library` /
# `library_first` gather/refill/analyst fans run these so a corpus-scoped run touches
# the open web only where the mode explicitly allows it (DEEP_RESEARCH_VIDEO_SOURCES_PLAN.md).
# `search_external_video`/`read_external_video` self-scope their own `external`-domain
# read, so a child holding them reaches the corpus and nothing owner-authored. Like the
# web children they are leaves (no `spawn_subagent`) and KB-less. jerv holds both corpus
# tools, so the parent⊆child clamp keeps them.
RESEARCH_LIBRARY_TOOLS = frozenset({"search_external_video", "read_external_video", "current_time"})
REVIEW_LIBRARY_TOOLS = RESEARCH_LIBRARY_TOOLS
# summarize: a pure transform — no tools at all, so it cannot reach the web and
# cannot spawn.
SUMMARIZE_TOOLS: frozenset[str] = frozenset()

# The guided-intake interviewer's allowlist: EMPTY. A non-owner stranger drives this
# persona, so it reads no knowledge base and may call no tool at all — capture is the
# endpoint's job (the recipient confirms a draft; the server writes it), never a tool the
# model invokes. Empty allowlist → `ToolRegistry.allowed_names` is empty → dispatch refuses
# every tool, so a brief or an injected message can never widen what it may call
# (docs/archive/GUIDED_INTAKE_PLAN.md §5, W2).
INTAKE_TOOLS: frozenset[str] = frozenset()

# The closed set of personas a NON-owner principal (an intake_link) may run. Resolution
# for those principals goes through `agent_for_intake`, which fails closed against this
# set — never `agent_for`, whose curator fallback would be catastrophic for a stranger.
NON_OWNER_PERSONAS = frozenset({"intake"})

DEFAULT_AGENT = "curator"


@dataclass(frozen=True)
class AgentProfile:
    """One selectable persona. `prompt`/`version`/`strength` come from the agent's
    `.prompt` sidecar; `tools` is the registry allowlist (None = every in-scope
    knowledge tool, a frozenset = exactly those, the empty set = none);
    `reads_knowledge_base` gates retrieval, episodic memory, and skill recall — a
    False agent runs with empty read scopes so even a mis-scoped session reads no
    domain data.

    `budget_multiplier` scales the loop's per-turn guardrails (the ReAct step cap and
    the cost-token budget) for this persona — 1 keeps the shared defaults; the
    archivist and jerv both run at 4 because their work is a long, many-tool ReAct
    chain (a date-by-date mailbox cleanup; a multi-source web/research thread) that the
    default 10-step / 200k-token budget cut off mid-sweep (docs/archive/EMAIL_ARCHIVIST_PLAN.md)."""

    name: str
    prompt: str
    version: str
    strength: str
    tools: frozenset[str] | None
    reads_knowledge_base: bool
    budget_multiplier: int = 1


def _profile(
    name: str,
    filename: str,
    *,
    tools: frozenset[str] | None,
    reads_knowledge_base: bool,
    budget_multiplier: int = 1,
) -> AgentProfile:
    pf = load_prompt(_PROMPTS / filename)
    return AgentProfile(
        name=name,
        prompt=pf.render(),
        version=pf.version,
        strength=pf.strength,
        tools=tools,
        reads_knowledge_base=reads_knowledge_base,
        budget_multiplier=budget_multiplier,
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
        budget_multiplier=4,
    ),
    "archivist": _profile(
        "archivist",
        "archivist.prompt",
        tools=ARCHIVIST_TOOLS,
        reads_knowledge_base=False,
        budget_multiplier=4,
    ),
    # The three web-sandboxed sub-agent personas jerv spawns (no KB, no location, no
    # memory; their turns are never episodically appended because reads_knowledge_base
    # is False and the spawn helper never records an episode). The tree budget (Wave
    # S2) governs the fan as a whole; each child's own loop runs a focused chain (2x
    # for the web-reading research/review, 1x for the tool-less summarize transform).
    "research": _profile(
        "research",
        "research.prompt",
        tools=RESEARCH_TOOLS,
        reads_knowledge_base=False,
        budget_multiplier=2,
    ),
    "review": _profile(
        "review",
        "review.prompt",
        tools=REVIEW_TOOLS,
        reads_knowledge_base=False,
        budget_multiplier=2,
    ),
    "summarize": _profile(
        "summarize",
        "summarize.prompt",
        tools=SUMMARIZE_TOOLS,
        reads_knowledge_base=False,
        budget_multiplier=1,
    ),
    # The corpus twins of research/review: same sandbox, same budget, but their tools
    # are the video-library corpus reads instead of the web. deep_research routes the
    # gather/refill/analyst fans here for its library source modes.
    "research_library": _profile(
        "research_library",
        "research_library.prompt",
        tools=RESEARCH_LIBRARY_TOOLS,
        reads_knowledge_base=False,
        budget_multiplier=2,
    ),
    "review_library": _profile(
        "review_library",
        "review_library.prompt",
        tools=REVIEW_LIBRARY_TOOLS,
        reads_knowledge_base=False,
        budget_multiplier=2,
    ),
    # The guided-intake interviewer (docs/archive/GUIDED_INTAKE_PLAN.md). A closed, capture-only
    # persona a non-owner stranger runs: empty tool allowlist, no knowledge base, and a 1x
    # budget — a short bounded interview, NOT the 4x many-tool chain jerv/archivist run
    # (§5: per-session caps are the backstop, and the persona must not be a cost lever).
    # Its `.prompt` is the FIXED frame; the per-link brief is assembled in as data at
    # session start (jbrain.intake.persona), never baked into this static, pinned prompt.
    "intake": _profile(
        "intake",
        "intake.prompt",
        tools=INTAKE_TOOLS,
        reads_knowledge_base=False,
        budget_multiplier=1,
    ),
}

AGENT_NAMES = frozenset(AGENTS)

# The personas an OWNER may select for a Full Brain session or task. `intake` lives in
# AGENTS (so it is resolvable + version-pinned) but is a NON-owner persona — it belongs to
# an intake_link principal, is resolved via `agent_for_intake`, and must never be stored in
# app.agent_sessions/app.tasks (whose `agent` CHECK excludes it anyway). Owner-facing
# validation gates on THIS set, not AGENT_NAMES, so an owner can't open an intake session.
OWNER_AGENTS = AGENT_NAMES - NON_OWNER_PERSONAS


def is_owner_agent(name: str) -> bool:
    """Whether an OWNER may run this persona (excludes the non-owner intake persona)."""
    return name in OWNER_AGENTS


def agent_for(name: str) -> AgentProfile:
    """The profile for a stored agent name, falling back to the default for an
    unknown value — a defensive default so an old or malformed row still runs as
    the Full Brain curator rather than failing the turn.

    OWNER sessions only. A non-owner principal must NEVER resolve through this: its
    curator fallback would hand a stranger the Full Brain knowledge agent. Non-owner
    principals use `agent_for_intake`, which fails closed."""
    return AGENTS.get(name, AGENTS[DEFAULT_AGENT])


class PersonaResolutionError(ValueError):
    """A non-owner (intake_link) session resolved to a persona that is not `intake`.
    Raised so the turn FAILS CLOSED — the opposite of `agent_for`'s curator fallback,
    which for a stranger would be catastrophic (GUIDED_INTAKE_PLAN.md §5/§11)."""


def agent_for_intake(name: str) -> AgentProfile:
    """The profile for a NON-owner intake session, resolved fail-closed.

    Unlike `agent_for`, an unknown / tampered / empty name does NOT fall back to a
    knowledge agent — it raises. The only persona a stranger may ever run is `intake`
    (empty scope, no tools), so anything else refuses the turn rather than risk handing
    out the curator. The check is on the closed `NON_OWNER_PERSONAS` set, not a string
    compare, so adding a future non-owner persona stays a deliberate, audited change."""
    if name not in NON_OWNER_PERSONAS:
        raise PersonaResolutionError(
            f"non-owner intake session refuses persona {name!r}: only {sorted(NON_OWNER_PERSONAS)}"
            " may run under a non-owner principal"
        )
    return AGENTS[name]


def is_agent(name: str) -> bool:
    return name in AGENTS
