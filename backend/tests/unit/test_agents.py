"""Agent selection: the persona registry that sets each session's prompt, tool
allowlist, and knowledge-base access (docs/ASSISTANT.md "Agent selection")."""

import hashlib

from jbrain.agent.agents import (
    AGENT_NAMES,
    AGENTS,
    ARCHIVIST_TOOLS,
    DEFAULT_AGENT,
    GMAIL_TOOLS,
    JERV_TOOLS,
    MEMORY_TOOLS,
    RESEARCH_TOOLS,
    REVIEW_TOOLS,
    SPAWN_TOOL,
    SUBAGENT_PERSONAS,
    SUMMARIZE_TOOLS,
    WEB_TOOLS,
    agent_for,
    is_agent,
)


def test_seven_agents_are_defined() -> None:
    assert (
        frozenset({"curator", "teacher", "jerv", "archivist", "research", "review", "summarize"})
        == AGENT_NAMES
    )
    assert DEFAULT_AGENT == "curator"


def test_curator_is_the_full_brain_default() -> None:
    """curator keeps the original Full Brain system prompt and every in-scope tool
    (allow=None), and reads the knowledge base — i.e. today's behavior unchanged."""
    curator = AGENTS["curator"]
    assert curator.tools is None
    assert curator.reads_knowledge_base is True
    assert curator.version == "agent-system-v6"


def test_teacher_is_a_tool_less_socratic_tutor() -> None:
    """teacher has no tools (an empty allowlist) and no knowledge-base access — it
    teaches only from the conversation."""
    teacher = AGENTS["teacher"]
    assert teacher.tools == frozenset()
    assert teacher.reads_knowledge_base is False


def test_jerv_is_a_sandboxed_web_chatbot() -> None:
    """jerv may call the web tools, the dataless clock, the owner-approved
    coarse-location read, the local image-gen tools, and the read-only host-metrics
    summary; it reads no knowledge base."""
    jerv = AGENTS["jerv"]
    assert (
        jerv.tools
        == JERV_TOOLS
        == WEB_TOOLS
        | {
            "current_time",
            "current_location",
            "weather",
            "hurricane",
            "generate_image",
            "edit_image",
            "analyze_image",
            "transcribe",
            "analyze_video",
            "query_server_metrics",
            "spawn_subagent",
        }
    )
    assert jerv.reads_knowledge_base is False
    assert jerv.tools is not None and SPAWN_TOOL in jerv.tools  # jerv is the spawner


def test_image_tools_are_jerv_only() -> None:
    """The image-gen tools live in jerv's allowlist and nowhere else — curator (the
    default knowledge agent, allow=None) never offers the opt-in `web` class, and the
    tool-less teacher offers nothing."""
    assert {"generate_image", "edit_image"} <= JERV_TOOLS
    assert AGENTS["curator"].tools is None
    assert AGENTS["teacher"].tools == frozenset()


def test_archivist_is_a_sandboxed_gmail_organizer() -> None:
    """archivist may call the gmail_* tools, its own cross-session memory, and the
    shared current_time read (to ground date queries), and reads no knowledge base, so
    no owner note/entity data is in context while it triages mail."""
    archivist = AGENTS["archivist"]
    assert archivist.tools == ARCHIVIST_TOOLS == GMAIL_TOOLS | MEMORY_TOOLS | {"current_time"}
    assert "current_time" in ARCHIVIST_TOOLS  # date awareness for older_than:/before: queries
    assert {
        "gmail_search",
        "gmail_read",
        "gmail_list_labels",
        "gmail_create_label",
        "gmail_label",
        "gmail_archive",
        "gmail_count",
        "gmail_sender_breakdown",
        "gmail_bulk_label",
    } == GMAIL_TOOLS
    assert {"archivist_memory_read", "archivist_memory_write"} == MEMORY_TOOLS
    assert archivist.reads_knowledge_base is False


def test_archivist_earns_a_4x_turn_budget() -> None:
    """The archivist and jerv each run a long, many-tool ReAct chain (a date-by-date
    mailbox cleanup; a multi-source web thread), so each gets a 4x budget_multiplier
    (the loop scales both the step cap and the cost-token budget by it); the curator
    and teacher keep the shared 1x default."""
    assert AGENTS["archivist"].budget_multiplier == 4
    assert AGENTS["jerv"].budget_multiplier == 4
    assert AGENTS["curator"].budget_multiplier == 1
    assert AGENTS["teacher"].budget_multiplier == 1


def test_archivist_tools_are_archivist_only() -> None:
    """The gmail_* and memory tools — the archivist's EXCLUSIVE surface — live in its
    allowlist and nowhere else: curator (allow=None) never offers the opt-in `web` class,
    jerv doesn't hold them, and the tool-less teacher offers nothing. (current_time is a
    deliberate shared default-knowledge tool, so it's excluded from the exclusivity
    check.)"""
    assert AGENTS["curator"].tools is None
    assert not ((GMAIL_TOOLS | MEMORY_TOOLS) & JERV_TOOLS)
    shared_with_jerv = ARCHIVIST_TOOLS & JERV_TOOLS
    assert shared_with_jerv == {"current_time"}  # the one deliberate shared tool
    assert AGENTS["teacher"].tools == frozenset()


def test_subagent_personas_are_web_sandboxed_and_kb_less() -> None:
    """research/review read the web + clock + may spawn (template-bound at depth>=1);
    summarize is a pure transform with no tools; none reads the knowledge base, and
    none holds `current_location` (M2 — the location read is never in a child)."""
    research, review, summarize = (AGENTS["research"], AGENTS["review"], AGENTS["summarize"])
    assert research.tools == RESEARCH_TOOLS == WEB_TOOLS | {"current_time", SPAWN_TOOL}
    assert review.tools == REVIEW_TOOLS == RESEARCH_TOOLS
    assert summarize.tools == SUMMARIZE_TOOLS == frozenset()
    for p in (research, review, summarize):
        assert p.reads_knowledge_base is False
        assert "current_location" not in (p.tools or frozenset())
    # summarize cannot spawn (it holds no tools at all).
    assert SPAWN_TOOL not in (summarize.tools or frozenset())


def test_spawn_set_matches_the_subagent_personas() -> None:
    """The closed spawn set is exactly the three child personas — `spawn_subagent`
    validates against it BEFORE agent_for (which would otherwise resolve an unknown
    name to the KB-capable curator)."""
    assert frozenset({"research", "review", "summarize"}) == SUBAGENT_PERSONAS
    assert SUBAGENT_PERSONAS <= AGENT_NAMES
    # The spawnable personas are all KB-less sandboxes — never the curator.
    assert "curator" not in SUBAGENT_PERSONAS
    assert all(AGENTS[p].reads_knowledge_base is False for p in SUBAGENT_PERSONAS)


def test_agent_for_falls_back_to_curator() -> None:
    assert agent_for("jerv").name == "jerv"
    # An unknown/old/malformed stored value never breaks a turn — it runs as curator.
    assert agent_for("nonesuch").name == DEFAULT_AGENT
    assert agent_for("").name == DEFAULT_AGENT


def test_is_agent() -> None:
    assert is_agent("curator") and is_agent("teacher") and is_agent("jerv")
    assert not is_agent("editor")


def test_persona_prompts_pinned_to_their_versions() -> None:
    """Each persona prompt carries a safety policy (the data/instruction boundary,
    the tutor's no-cheating rule, jerv's sandbox); editing one must be a deliberate
    version bump, like every .prompt file (DEVELOPMENT.md)."""
    pins = {
        "curator": (
            "agent-system-v6",
            "5d3056298e2fb0afb311f85970acecc5d2ee92bf50118d532b1268e054e875cc",
        ),
        "teacher": (
            "agent-teacher-v1",
            "e457d7504be94746132de7cc0c7b50fa1567867b3573a64ddfe6030b45909b16",
        ),
        "jerv": (
            "agent-jerv-v22",
            "1c2eaf8dc0902340c5a103c74ef3bb8a53d9d0266e6e0268b0747360f5811377",
        ),
        "archivist": (
            "agent-archivist-v6",
            "19b557040a985b4b1c13b9b3a38e2c6a8e0fd06611a84e7341e6497f8a14b9a0",
        ),
        "research": (
            "agent-research-v3",
            "1638ab8a756ab7b8e3a3f7dff24f827cc2a8a7e1be44b5b0ee5907e1d70d6b98",
        ),
        "review": (
            "agent-review-v1",
            "af8bab6af76414588fde6d1d14cd37ee4c7bbfb3bd6b395240bc1692a7dbdb27",
        ),
        "summarize": (
            "agent-summarize-v1",
            "fc169f821c8aa2031ca710f143c1307c8fc4803895eeef51e4a7426144ddbac0",
        ),
    }
    assert set(pins) == AGENT_NAMES
    for name, (version, digest) in pins.items():
        profile = AGENTS[name]
        assert profile.version == version
        assert hashlib.sha256(profile.prompt.encode()).hexdigest() == digest
