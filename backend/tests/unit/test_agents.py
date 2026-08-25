"""Agent selection: the persona registry that sets each session's prompt, tool
allowlist, and knowledge-base access (docs/reference/ASSISTANT.md "Agent selection")."""

import hashlib

import pytest

from jbrain.agent.agents import (
    AGENT_NAMES,
    AGENTS,
    ARCHIVIST_TOOLS,
    DEFAULT_AGENT,
    GMAIL_TOOLS,
    INTAKE_TOOLS,
    JERV_TOOLS,
    MEMORY_TOOLS,
    NON_OWNER_PERSONAS,
    OWNER_AGENTS,
    RESEARCH_TOOLS,
    REVIEW_TOOLS,
    SPAWN_TOOL,
    SUBAGENT_PERSONAS,
    SUMMARIZE_TOOLS,
    WEB_TOOLS,
    PersonaResolutionError,
    agent_for,
    agent_for_intake,
    is_agent,
    is_owner_agent,
)


def test_seventeen_agents_are_defined() -> None:
    assert (
        frozenset(
            {
                "curator",
                "teacher",
                "jerv",
                "archivist",
                "research",
                "review",
                "summarize",
                "intake",
                "research_library",
                "review_library",
                "research_reports",
                "review_reports",
                "research_deep",
                "research_scout",
                "research_fetch",
                "jmolt",
                "jmolt_observer",
            }
        )
        == AGENT_NAMES
    )
    assert DEFAULT_AGENT == "curator"


def test_curator_is_the_full_brain_default() -> None:
    """curator keeps the original Full Brain system prompt and every in-scope tool
    (allow=None), and reads the knowledge base — i.e. today's behavior unchanged."""
    curator = AGENTS["curator"]
    assert curator.tools is None
    assert curator.reads_knowledge_base is True
    assert curator.version == "agent-system-v8"


def test_teacher_is_a_tool_less_socratic_tutor() -> None:
    """teacher has no tools (an empty allowlist) and no knowledge-base access — it
    teaches only from the conversation."""
    teacher = AGENTS["teacher"]
    assert teacher.tools == frozenset()
    assert teacher.reads_knowledge_base is False


def test_jerv_is_a_sandboxed_web_chatbot() -> None:
    """jerv may call the web tools, the dataless clock, the owner-approved
    coarse-location read, the local vision read, and the read-only host-metrics
    summary; it reads no knowledge base."""
    jerv = AGENTS["jerv"]
    assert (
        jerv.tools
        == JERV_TOOLS
        == WEB_TOOLS
        | {
            "news_search",
            "science_search",
            "news_feed",
            "current_time",
            "current_location",
            "weather",
            "weather_history",
            "hurricane",
            "analyze_image",
            "transcribe",
            "analyze_video",
            "analyze_stream",
            "grab_frame",
            "render_bars",
            "render_chart",
            "fetch_image",
            "compare_images",
            "render_html",
            "canvas",
            "show_canvas",
            "crop_regions",
            "ocr",
            "read_artifact",
            "grokipedia",
            "public_records",
            "portal_search",
            "external_video",
            "show_external_video",
            "remove_external_video",
            "check_channel",
            "query_server_metrics",
            "read_plan",
            "write_plan",
            "write_plan_result",
            "spawn_subagent",
            "deep_research",
            "deep_produce",
            "deepest_research",
            "research_report",
            "show_research_report",
            "remove_research_report",
            # Names THIS chat, from inside the turn — replacing the `session.title`
            # completion that evicted jerv's primed prefix to do the same job.
            "name_session",
        }
    )
    assert jerv.reads_knowledge_base is False
    assert jerv.tools is not None and SPAWN_TOOL in jerv.tools  # jerv is the spawner
    assert "deep_research" in jerv.tools  # jerv is the deep-research orchestrator
    assert "deep_produce" in jerv.tools  # ...and holds the produce verb (DEEP_PRODUCE_PLAN W1)
    # deep_produce is NEVER_DEFAULT, so curator's tools=None wildcard can never absorb it.
    from jbrain.agent.toolregistry import NEVER_DEFAULT

    assert "deep_produce" in NEVER_DEFAULT
    # jerv has no extra_tools grant (it holds deep_produce via its explicit allowlist).
    assert jerv.extra_tools == frozenset()


def test_jerv_is_not_offered_the_task_agent_decompose_tool() -> None:
    """`decompose_research` refuses at depth 0, so offering it to an interactive jerv turn
    only spends prompt on a tool whose every call fails. It reaches the parent⊆child clamp
    through DEEPEST_RUN_TOOLS — the background orchestrator's ceiling — instead, which is the
    only path that ever spawns the `research_deep` task agent that calls it."""
    from jbrain.agent.agents import DECOMPOSE_TOOL, DEEPEST_RUN_TOOLS

    assert DECOMPOSE_TOOL not in JERV_TOOLS
    assert DECOMPOSE_TOOL in DEEPEST_RUN_TOOLS
    # The ceiling is jerv's set plus exactly that one tool — a task agent inherits nothing
    # else it could not have inherited before.
    assert JERV_TOOLS | {DECOMPOSE_TOOL} == DEEPEST_RUN_TOOLS
    # The task-agent persona still holds it, so the clamp has something to intersect.
    deep_tools = AGENTS["research_deep"].tools
    assert deep_tools is not None and DECOMPOSE_TOOL in deep_tools


def test_jerv_holds_both_ungrounded_chart_tools() -> None:
    """`render_bars` and `render_chart` both plot only numbers the model passes (their
    handlers never read the session), so both are safe for the KB-blind jerv. They ship as a
    pair: `render_bars`' own description steers to `render_chart` for a time series, so
    holding one without the other pointed jerv at a tool it could not call."""
    assert {"render_bars", "render_chart"} <= JERV_TOOLS
    # The GROUNDED chart tool stays out — it reads app.facts under the session's scopes.
    assert "chart_measurements" not in JERV_TOOLS


def test_curator_holds_deep_produce_via_extra_tools_only() -> None:
    """The Full Brain curator is a `tools=None` wildcard, so it holds `deep_produce` (a
    NEVER_DEFAULT tool) ONLY through the per-persona `extra_tools` grant (DEEP_PRODUCE_PLAN
    W2) — the wildcard itself never absorbs it, and no other wildcard persona gains it."""
    from jbrain.agent.agents import AGENTS, agent_for

    curator = agent_for("curator")
    assert curator.tools is None  # the wildcard is intact (not converted to an allowlist)
    assert curator.extra_tools == frozenset({"deep_produce"})
    # No other persona carries an extra_tools grant — the grant does not leak.
    for name, profile in AGENTS.items():
        if name != "curator":
            assert profile.extra_tools == frozenset(), name


def test_image_tools_are_jerv_only() -> None:
    """The analyze_image vision read lives in jerv's allowlist and nowhere else — curator
    (the default knowledge agent, allow=None) never offers the opt-in `web` class, and the
    tool-less teacher offers nothing. The gen pair is gone: the launcher owns generation."""
    assert "analyze_image" in JERV_TOOLS
    assert {"generate_image", "edit_image"} & JERV_TOOLS == set()
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


def test_long_chain_personas_earn_a_wider_turn_budget() -> None:
    """The archivist and jerv each run a long, many-tool ReAct chain (a date-by-date
    mailbox cleanup; a multi-source web thread), so each gets a widened budget_multiplier
    (the loop scales both the step cap and the cost-token budget by it); the curator
    and teacher keep the shared 1x default. jerv runs at 6 (not the archivist's 4) because
    its heaviest turn — a breadth-5 two-wave deep_research fan — needs the larger ~15M tree
    pool (tree.py), sized to the widened wall-clock."""
    assert AGENTS["archivist"].budget_multiplier == 4
    assert AGENTS["jerv"].budget_multiplier == 6
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
    """research/review read the web + clock; summarize is a pure transform with no tools;
    none reads the knowledge base, none holds `current_location` (M2), and — since
    child-initiated nesting was removed — NONE holds `spawn_subagent`: children are
    always leaves."""
    research, review, summarize = (AGENTS["research"], AGENTS["review"], AGENTS["summarize"])
    assert (
        research.tools
        == RESEARCH_TOOLS
        == WEB_TOOLS
        | {
            "news_search",
            "science_search",
            "news_feed",
            "current_time",
            "portal_search",
        }
    )
    assert review.tools == REVIEW_TOOLS == RESEARCH_TOOLS
    # The categorized search tools + the curated feed source ride the gather personas, so a
    # deep-research fan can use them regardless of the preset path (research_scout held them too).
    assert {"news_search", "science_search", "news_feed"} <= RESEARCH_TOOLS
    assert summarize.tools == SUMMARIZE_TOOLS == frozenset()
    for p in (research, review, summarize):
        assert p.reads_knowledge_base is False
        assert "current_location" not in (p.tools or frozenset())
        # No child persona can spawn — the tree is exactly two levels (jerv → leaves).
        assert SPAWN_TOOL not in (p.tools or frozenset())


def test_scout_and_fetch_personas_split_the_gather_by_role() -> None:
    """The two-phase gather personas split by ROLE: research_scout is the lead-follower
    (web_search + web_fetch — it searches AND opens hubs to reach the real article URLs), and
    research_fetch is the reader (web_fetch, and NO web_search so it can't wander off searching).
    Both are KB-less leaves, hold no location, and ⊆ jerv (the parent⊆child clamp keeps them)."""
    from jbrain.agent.agents import FETCH_TOOLS, SCOUT_TOOLS

    scout, fetch = (AGENTS["research_scout"], AGENTS["research_fetch"])
    assert (
        scout.tools
        == SCOUT_TOOLS
        == frozenset(
            {
                "web_search",
                "news_search",
                "science_search",
                "news_feed",
                "web_fetch",
                "current_time",
            }
        )
    )
    assert fetch.tools == FETCH_TOOLS == frozenset({"web_fetch", "current_time"})
    # The scout can follow leads (fetch), search news, and pull curated feeds (news_feed); the
    # reader is fetch-only — it never searches (no web_search AND no news_search) and holds no
    # discovery tool (no news_feed), so it can't wander off from its handed URL list.
    assert "web_fetch" in (scout.tools or frozenset())
    assert "news_feed" in (scout.tools or frozenset())
    assert "web_search" not in (fetch.tools or frozenset())
    assert "news_search" not in (fetch.tools or frozenset())
    assert "news_feed" not in (fetch.tools or frozenset())
    for p in (scout, fetch):
        assert p.reads_knowledge_base is False
        assert "current_location" not in (p.tools or frozenset())
        assert SPAWN_TOOL not in (p.tools or frozenset())
        assert (p.tools or frozenset()) <= (AGENTS["jerv"].tools or frozenset())


def test_library_subagent_personas_are_corpus_sandboxed_and_kb_less() -> None:
    """research_library/review_library are the corpus twins of research/review: their
    tools are the video-library reads (NO web), they read no knowledge base, hold no
    location, and cannot spawn — leaves, exactly like the web children."""
    from jbrain.agent.agents import RESEARCH_LIBRARY_TOOLS, REVIEW_LIBRARY_TOOLS

    research_lib, review_lib = (AGENTS["research_library"], AGENTS["review_library"])
    assert (
        research_lib.tools
        == RESEARCH_LIBRARY_TOOLS
        == frozenset({"external_video", "current_time"})
    )
    assert review_lib.tools == REVIEW_LIBRARY_TOOLS == RESEARCH_LIBRARY_TOOLS
    for p in (research_lib, review_lib):
        assert p.reads_knowledge_base is False
        # No web egress: the library personas never hold web_search/web_fetch.
        assert not ({"web_search", "web_fetch"} & (p.tools or frozenset()))
        assert "current_location" not in (p.tools or frozenset())
        assert SPAWN_TOOL not in (p.tools or frozenset())
        # jerv (the only spawner) holds every corpus tool, so the parent⊆child clamp
        # keeps them — a library child is never stripped to nothing.
        assert (p.tools or frozenset()) <= (AGENTS["jerv"].tools or frozenset())


def test_spawn_set_matches_the_subagent_personas() -> None:
    """The closed spawn set is exactly the ten child personas — `spawn_subagent`
    validates against it BEFORE agent_for (which would otherwise resolve an unknown
    name to the KB-capable curator)."""
    assert (
        frozenset(
            {
                "research",
                "review",
                "summarize",
                "research_library",
                "review_library",
                "research_reports",
                "review_reports",
                "research_deep",
                "research_scout",
                "research_fetch",
            }
        )
        == SUBAGENT_PERSONAS
    )
    assert SUBAGENT_PERSONAS <= AGENT_NAMES
    # The spawnable personas are all KB-less sandboxes — never the curator.
    assert "curator" not in SUBAGENT_PERSONAS
    assert all(AGENTS[p].reads_knowledge_base is False for p in SUBAGENT_PERSONAS)


def test_intake_is_a_capture_only_non_owner_persona() -> None:
    """The intake interviewer a stranger runs: EMPTY tool allowlist (so dispatch refuses
    every tool), no knowledge base, and a 1x budget — not jerv/archivist's 4x cost lever
    (docs/archive/GUIDED_INTAKE_PLAN.md §5)."""
    intake = AGENTS["intake"]
    assert intake.tools == INTAKE_TOOLS == frozenset()
    assert intake.reads_knowledge_base is False
    assert intake.budget_multiplier == 1
    # It shares no tool with any owner/jerv/archivist persona — it holds none.
    assert not ((intake.tools or frozenset()) & (JERV_TOOLS | ARCHIVIST_TOOLS))


def test_intake_is_not_owner_selectable() -> None:
    """intake is a NON-owner persona: resolvable + pinned, but excluded from the set an
    owner may open a session/task as (it must never land in app.agent_sessions, whose
    agent CHECK excludes it). is_owner_agent gates the owner session/task routes."""
    assert AGENT_NAMES - frozenset({"intake"}) == OWNER_AGENTS
    assert "intake" not in OWNER_AGENTS
    assert is_owner_agent("curator") and is_owner_agent("jerv")
    assert not is_owner_agent("intake")


def test_agent_for_intake_fails_closed_never_curator() -> None:
    """A non-owner intake session resolves ONLY to intake; an unknown/tampered/empty
    persona raises rather than falling back to the KB-capable curator (the §5/§11
    fail-closed requirement — the opposite of agent_for)."""
    assert agent_for_intake("intake").name == "intake"
    assert frozenset({"intake"}) == NON_OWNER_PERSONAS
    for bad in ("curator", "jerv", "archivist", "research", "nonesuch", ""):
        with pytest.raises(PersonaResolutionError):
            agent_for_intake(bad)


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
            "agent-system-v8",
            "be091947e2325b07751dd6d0a4aa6f04596ab12bf0719461481d667e4d5a73ed",
        ),
        "teacher": (
            "agent-teacher-v1",
            "e457d7504be94746132de7cc0c7b50fa1567867b3573a64ddfe6030b45909b16",
        ),
        "jerv": (
            "agent-jerv-v47",
            "008aea105c0ae90c24bf2b0312c78bd186f85f8140656d48d6d44b70884f8971",
        ),
        "archivist": (
            "agent-archivist-v6",
            "19b557040a985b4b1c13b9b3a38e2c6a8e0fd06611a84e7341e6497f8a14b9a0",
        ),
        "research": (
            "agent-research-v17",
            "fe7214009384173ccfe5d0fbedfe2ea21613feb651ded4bc792c20a140680795",
        ),
        "review": (
            "agent-review-v8",
            "af54a4fdee68266e8ba6b5494bed81f6a9ebd67bb5f024f51eca9632a5133e17",
        ),
        "summarize": (
            "agent-summarize-v2",
            "eff59feeb739f1bd48546f06e2e8768cdf6158703d69ae4140c096e04e49672e",
        ),
        "intake": (
            "agent-intake-v1",
            "fb03cdd6ff8198855e006cf0ee22de93d2384457cd23fe4f25607ef207f31c38",
        ),
        "research_library": (
            "agent-research-library-v3",
            "06c905079178e08f85625be14236d71737a9513f4b3d6f87d8492b4742c47e24",
        ),
        "review_library": (
            "agent-review-library-v2",
            "f3123fdde9bfbe360e67d5c56812f7a72d55cf25a744d1788f4f7380f0a29564",
        ),
        "research_deep": (
            "agent-research-deep-v2",
            "f155cd5e2a114c3403c295801e6080a7de3c632030ccff81e49ebf0bc166d643",
        ),
        "research_reports": (
            "agent-research-reports-v2",
            "ac162af1e86c43e73b8285ed9cb6ea3e8d3cff7ed498d413dc392ecd93e98baf",
        ),
        "review_reports": (
            "agent-review-reports-v2",
            "487ddb5461ab4b7040bcc894ca13d6d0819632c624ee8c1a0518ce90fbad24df",
        ),
        "research_scout": (
            "agent-research-scout-v8",
            "f85150fcc655f5911da8ba39faf133db98765d7cb9b43555cd49049adfb0cfd0",
        ),
        "research_fetch": (
            "agent-research-fetch-v2",
            "bf5c2bc5214c14940c4a6c9c2dce9f71e6b655dfb33034c64245ddd7bdeca396",
        ),
        "jmolt": (
            "agent-jmolt-v1",
            "df282cd885f8e4319cdb07e7ce61fd6254c772b4902063eca3e2ed4a5cd76633",
        ),
        "jmolt_observer": (
            "agent-jmolt-observer-v1",
            "09e2ace3e0f8c85a92608ff017118e069b8f9729d8c9e13cb820d6f3dabcfa40",
        ),
    }
    assert set(pins) == AGENT_NAMES
    for name, (version, digest) in pins.items():
        profile = AGENTS[name]
        assert profile.version == version
        assert hashlib.sha256(profile.prompt.encode()).hexdigest() == digest
