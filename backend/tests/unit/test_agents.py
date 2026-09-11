"""Agent selection: the persona registry that sets each session's prompt, tool
allowlist, and knowledge-base access (docs/reference/ASSISTANT.md "Agent selection")."""

import hashlib

import pytest

from jbrain.agent.agents import (
    AGENT_NAMES,
    AGENTS,
    ARCHIVIST_TOOLS,
    DEFAULT_AGENT,
    ENGINE_ONLY_PERSONAS,
    GMAIL_TOOLS,
    INTAKE_TOOLS,
    JERV_TOOLS,
    MEMORY_TOOLS,
    NON_OWNER_PERSONAS,
    NOTE_INGEST_ON_REPLY_TOOLS,
    NOTE_INGEST_THIRD_PARTY_TOOLS,
    NOTE_INGEST_UNATTENDED_TOOLS,
    OWNER_AGENTS,
    RESEARCH_TOOLS,
    REVIEW_TOOLS,
    SPAWN_TOOL,
    STORABLE_OWNER_AGENTS,
    SUBAGENT_PERSONAS,
    SUMMARIZE_TOOLS,
    WEB_TOOLS,
    PersonaResolutionError,
    agent_for,
    agent_for_intake,
    agent_for_owner_reply,
    is_agent,
    is_owner_agent,
    narrow_for_third_party_note,
)
from jbrain.agent.readtools import TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import RegisteredTool, ToolRegistry


def test_eighteen_agents_are_defined() -> None:
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
                "note_ingest",
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
            # The radio pair: tune the owner's USB SDR and release it. jerv is the only
            # agent that holds them, and sdr_listen is the only thing that takes the
            # tuner lease the composer's radio icon reflects (SDR_RADIO_PLAN.md D7).
            "sdr_listen",
            "sdr_stop",
            "sdr_aprs_logging",
            # The APRS heard log (APRS_CONTROL_PLAN.md P1) — a `read` tool over a table,
            # whose CONTENTS are untrusted radio traffic from anyone in range.
            "aprs_recent",
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
    agent CHECK excludes it). is_owner_agent gates the owner session/task routes.

    OWNER_AGENTS excludes the ENGINE-ONLY personas as well — a different exclusion for a
    different reason (they are owner-side, they are simply not a person's to pick)."""
    assert AGENT_NAMES - frozenset({"intake"}) - ENGINE_ONLY_PERSONAS == OWNER_AGENTS
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


# --- note_ingest: the note conversation's closed allowlist (D16) ----------
#
# AGENT_INGEST_CONVERSATION_PLAN.md D1/D16: a note conversation is the ordinary agent loop
# under its OWN closed tool allowlist, never the curator wildcard. W2 ships the mechanism
# with an empty set (the graph-write tools do not exist yet), so these tests are what make
# W3's widening deliberate rather than accidental.

# The four verbs D16 names as provably outside the persona. `file_correction` and
# `add_source_exclusion` write a NOTE that re-enters ingestion, so a hostile note body could
# launder itself into an owner-attributed source note and a second conversation.
_FORBIDDEN_FOUR = frozenset(
    {"file_correction", "add_source_exclusion", "make_intake_link", "remember"}
)

# Every domain scope a session can hold — the widest a note conversation could ever run at,
# so the closure below is proven by the ALLOWLIST rule and not by domain invisibility.
_EVERY_SCOPE = frozenset({"general", "health", "finance", "location", "external", "jmolt"})


async def _noop(_args: dict, _ctx: object) -> object:  # pragma: no cover - never dispatched
    return None


def _every_shipped_tool() -> ToolRegistry:
    """The real registry over every shipped `.tool` sidecar, handlers stubbed. Going
    through the registry (not the dataclass field) is the point: `allowed_names` is the
    dispatch-time gate the loop actually consults."""
    return ToolRegistry(
        [RegisteredTool(load_tool(p), _noop) for p in sorted(TOOLS_DIR.glob("*.tool"))]
    )


def test_the_profile_carries_the_unattended_set_so_a_forgotten_caller_narrows() -> None:
    """D8's split is asymmetric on purpose, and this is the asymmetry.

    `AgentProfile.tools` — what every resolution path reads by default — is the
    UNATTENDED set. The wider one exists only behind `agent_for_owner_reply`, so a caller
    that never heard of the split (the task runner, a session listing, a future entry
    point) gets the narrow surface. The other arrangement fails the other way: it would
    hand `correct_fact` to a pass the owner is not present for, which is exactly the
    authority D8 withholds."""
    assert AGENTS["note_ingest"].tools == NOTE_INGEST_UNATTENDED_TOOLS
    assert agent_for("note_ingest").tools == NOTE_INGEST_UNATTENDED_TOOLS
    # And the two sets are genuinely different objects, not one aliased twice — the
    # failure mode where "the split" is a rename.
    assert NOTE_INGEST_ON_REPLY_TOOLS != NOTE_INGEST_UNATTENDED_TOOLS


def test_the_on_reply_set_adds_exactly_the_seven_names_and_keeps_the_unattended_six() -> None:
    """TOOL_SURFACE's on-reply rows, enumerated. A SUPERSET: the reply turn is the same
    agent finishing the same note, so it keeps the write path it was recording with — and
    since R3 it is where `assert_fact` lives, because "record one more thing the owner
    just told me" is INCREMENTAL and incremental must never license a sweep. Unattended
    it would be a second fact verb beside the reading the settle sweeps off."""
    assert NOTE_INGEST_ON_REPLY_TOOLS > NOTE_INGEST_UNATTENDED_TOOLS
    assert {
        "assert_fact",
        "correct_fact",
        "merge_entities",
        "prefs_write",
        "search",
        "read_note",
        "relate",
    } == NOTE_INGEST_ON_REPLY_TOOLS - NOTE_INGEST_UNATTENDED_TOOLS
    # `prefs_read` is in NEITHER (TOOL_SURFACE Cut #1: D15 already injects the document
    # into the system prompt, so the tool would be a second overlapping memory surface).
    assert "prefs_read" not in NOTE_INGEST_ON_REPLY_TOOLS
    # Nothing outward-facing. The owner replying does not sanitize the note body still
    # sitting in this turn's context, so the trifecta is just as complete on-reply.
    outward = WEB_TOOLS | GMAIL_TOOLS | {"news_feed", "news_search", "web_fetch", "grokipedia"}
    assert not (outward & NOTE_INGEST_ON_REPLY_TOOLS)


def test_agent_for_owner_reply_widens_only_the_note_persona() -> None:
    """The selector is called unconditionally from `/chat`, so every other persona has to
    come back byte-identical — otherwise the note split would be a chat-wide change."""
    assert agent_for_owner_reply("note_ingest").tools == NOTE_INGEST_ON_REPLY_TOOLS
    # Everything else is the same OBJECT, not merely an equal one.
    for name in AGENT_NAMES - {"note_ingest"}:
        assert agent_for_owner_reply(name) is AGENTS[name]
    # An unknown/malformed stored name still falls back to curator, unwidened — the
    # fallback must not become a door into the note persona's write set.
    assert agent_for_owner_reply("no-such-agent") is AGENTS[DEFAULT_AGENT]
    # The widened profile is otherwise the note persona unchanged: same prompt version,
    # same empty `extra_tools` (which `_admits` honours AHEAD of the NEVER_DEFAULT gate).
    widened = agent_for_owner_reply("note_ingest")
    assert widened.name == "note_ingest"
    assert widened.version == AGENTS["note_ingest"].version
    assert widened.extra_tools == frozenset()
    assert widened.reads_knowledge_base is True


def test_the_on_reply_writes_are_never_default_and_the_reads_are_not() -> None:
    """Constraint 9 at the point it bites: a `mutate`/`sensitive` tool outside
    NEVER_DEFAULT is handed to the CURATOR by `allow=None` on every ordinary chat turn.
    Asserted on the three WRITES only — `search`/`read_note`/`relate` are curator's
    already and belong in its wildcard."""
    from jbrain.agent.toolregistry import NEVER_DEFAULT

    assert {"correct_fact", "merge_entities", "prefs_write"} <= NEVER_DEFAULT
    registry = _every_shipped_tool()
    curator = AGENTS["curator"]
    wildcard = registry.allowed_names(_EVERY_SCOPE, curator.tools, curator.extra_tools)
    assert not ({"correct_fact", "merge_entities", "prefs_write"} & wildcard)


def test_the_unattended_pass_can_never_reach_an_on_reply_verb() -> None:
    """The first of the two directions the split has to prove, at the dispatch gate.

    Every shipped sidecar is in this registry, including the three on-reply writes — so
    the closure is the ALLOWLIST's doing and not an accident of what happens to be
    wired."""
    registry = _every_shipped_tool()
    assert all(n in registry for n in ("correct_fact", "merge_entities", "prefs_write"))
    unattended = AGENTS["note_ingest"]
    for scopes in (frozenset(), frozenset({"general"}), _EVERY_SCOPE):
        admitted = registry.allowed_names(scopes, unattended.tools, unattended.extra_tools)
        assert admitted == NOTE_INGEST_UNATTENDED_TOOLS
        assert not (admitted & (NOTE_INGEST_ON_REPLY_TOOLS - NOTE_INGEST_UNATTENDED_TOOLS))


def test_the_reply_turn_admits_the_on_reply_verbs_and_still_nothing_else() -> None:
    """The other direction: the widened profile really does reach the three writes and
    the three reads — and reaches nothing beyond its own allowlist, at every scope."""
    registry = _every_shipped_tool()
    widened = agent_for_owner_reply("note_ingest")
    for scopes in (frozenset(), frozenset({"general"}), _EVERY_SCOPE):
        admitted = registry.allowed_names(scopes, widened.tools, widened.extra_tools)
        assert admitted == NOTE_INGEST_ON_REPLY_TOOLS
    # The D16 four stay outside on the reply turn too — the owner replying does not
    # unlock the verbs that write a note back into ingestion.
    assert not (_FORBIDDEN_FOUR & NOTE_INGEST_ON_REPLY_TOOLS)


def test_the_third_party_set_is_the_unattended_write_core_minus_the_owner_channel() -> None:
    """D10, enumerated. A stranger's words may cause a FACT and nothing else.

    The whole write path survives, unnarrowed — D10 says intake commits "unrestricted in
    *what* it may write", and narrowing what a stranger's note may SAY would be a
    different decision from the one ratified. Two verbs and not three since R3, and that
    is not a narrowing of this set: it is derived from the unattended one, which now holds
    a single fact verb so that no pass can write a fact its own closing reading omits.
    What goes HERE is the one verb that is a CHANNEL: `ask_owner` writes a model-authored
    question, out of stranger-controlled text, into the owner's notes tab in his own
    agent's voice, and the answer he types is appended to the note as source text and
    re-ingested. (A third-party reading also never SWEEPS — that gate is the settle's,
    `clarify.PassReading.third_party`, not this set's.)"""
    assert {
        "resolve_entity",
        "close_reading",
        "find_entity",
        "read_entity",
        "current_time",
    } == NOTE_INGEST_THIRD_PARTY_TOOLS
    assert {"ask_owner"} == NOTE_INGEST_UNATTENDED_TOOLS - NOTE_INGEST_THIRD_PARTY_TOOLS
    # A strict subset of BOTH shipped sets, which is what makes it a narrowing rather
    # than a third surface with its own reach: it can hold nothing the owner's own note
    # conversation does not already hold.
    assert NOTE_INGEST_THIRD_PARTY_TOOLS < NOTE_INGEST_UNATTENDED_TOOLS
    assert NOTE_INGEST_THIRD_PARTY_TOOLS < NOTE_INGEST_ON_REPLY_TOOLS


def test_a_third_party_note_never_reaches_an_on_reply_verb_on_either_turn() -> None:
    """The claim that makes this one set instead of two: it serves the reply turn too.

    D8 widens the reply turn because the owner is the only voice in the room. On a note
    a stranger wrote he is not — the submitted body is turn 0 and is still in context —
    so the three on-reply WRITES stay out with him present. `correct_fact` is the sharp
    one: its `decide()` branch force-supersedes and PINS, so a fact it writes is one no
    later note can supersede, past every confidence guard in the arbiter."""
    registry = _every_shipped_tool()
    # The verbs are all really in this registry, so the closure below is the allowlist's
    # doing and not an accident of what happens to be wired.
    assert all(n in registry for n in ("correct_fact", "merge_entities", "prefs_write"))
    narrowed = narrow_for_third_party_note(agent_for_owner_reply("note_ingest"))
    for scopes in (frozenset(), frozenset({"general"}), _EVERY_SCOPE):
        admitted = registry.allowed_names(scopes, narrowed.tools, narrowed.extra_tools)
        assert admitted == NOTE_INGEST_THIRD_PARTY_TOOLS
        assert "ask_owner" not in admitted
        assert not (admitted & (NOTE_INGEST_ON_REPLY_TOOLS - NOTE_INGEST_THIRD_PARTY_TOOLS))
    # The D16 four are outside it too, so a stranger's body cannot reach the verbs that
    # would write a note back into ingestion under the owner's own attribution.
    assert not (_FORBIDDEN_FOUR & NOTE_INGEST_THIRD_PARTY_TOOLS)


def test_the_third_party_narrowing_runs_last_and_leaves_every_other_persona_alone() -> None:
    """It UNDOES the on-reply widening, so ordering is load-bearing: applied before
    `agent_for_owner_reply` it would be silently overwritten by it, and the reply turn on
    a stranger's note would hold `prefs_write` while every test of the narrowing passed.

    Idempotent, and identity on every other persona, so `/chat` can apply it without a
    persona test at the call site that a later edit could drop."""
    widened = agent_for_owner_reply("note_ingest")
    once = narrow_for_third_party_note(widened)
    assert once.tools == NOTE_INGEST_THIRD_PARTY_TOOLS
    assert narrow_for_third_party_note(once).tools == NOTE_INGEST_THIRD_PARTY_TOOLS
    # The other order is the bug this asserts against.
    assert agent_for_owner_reply(once.name).tools == NOTE_INGEST_ON_REPLY_TOOLS
    # Otherwise the note persona unchanged: same prompt version, same empty extra_tools
    # (which `_admits` honours AHEAD of the NEVER_DEFAULT gate), same KB access.
    assert once.name == "note_ingest"
    assert once.version == AGENTS["note_ingest"].version
    assert once.extra_tools == frozenset()
    assert once.reads_knowledge_base is True
    for name in AGENT_NAMES - {"note_ingest"}:
        assert narrow_for_third_party_note(AGENTS[name]) is AGENTS[name]
    assert narrow_for_third_party_note(agent_for("no-such-agent")) is AGENTS[DEFAULT_AGENT]


def test_an_emr_note_narrows_both_turns_at_the_dispatch_gate() -> None:
    """W4/D9's narrowing, asserted where the loop actually asks — over every shipped
    sidecar, at every scope, on BOTH turns.

    The reply turn is the one that matters most and the one the split's own asymmetry
    does not cover: `agent_for_owner_reply` WIDENS, so a caller that forgot the EMR
    narrowing would hand a note the deterministic parser owns the full write surface.
    `correct_fact` is the sharpest of the four — at an empty address it commits active +
    PINNED, and a pinned lab head makes every later import of that reading `held`."""
    from jbrain.agent.agents import NOTE_GRAPH_WRITE_TOOLS, narrow_for_emr

    registry = _every_shipped_tool()
    assert all(n in registry for n in NOTE_GRAPH_WRITE_TOOLS)  # not a wiring accident
    for base in (AGENTS["note_ingest"], agent_for_owner_reply("note_ingest")):
        narrowed = narrow_for_emr(base)
        for scopes in (frozenset(), frozenset({"general"}), _EVERY_SCOPE):
            admitted = registry.allowed_names(scopes, narrowed.tools, narrowed.extra_tools)
            assert not (admitted & NOTE_GRAPH_WRITE_TOOLS)
            # ...and nothing else was lost with them: the thread can still ask, read the
            # graph, and be told what the deterministic parse did.
            assert admitted == (base.tools or frozenset()) - NOTE_GRAPH_WRITE_TOOLS
            assert "ask_owner" in admitted


def test_a_note_that_is_both_third_party_and_emr_owned_gets_the_intersection() -> None:
    """W4's two halves over ONE note, which is the case neither half could write alone.

    The predicates are independent and a note can satisfy both: an approved guided-intake
    submission enacts into an `untrusted_origin` note (D10), and if the owner filed that
    submission to health / `Records` with the archive or PDF attached, `emr_owned` reads
    it as importer-owned too (D9). Nothing forbids that note; what must not happen is the
    turn coming out WIDER than either narrowing alone. Getting it wrong is silent — the
    thread looks identical, and the only visible difference is a fact written onto a note
    the deterministic parse is authoritative for, out of a stranger's text.

    So the merged answer is the INTERSECTION of the two, and the composition is a property
    of the functions rather than of the call order: `narrow_for_emr` SUBTRACTS and
    `narrow_for_third_party_note` INTERSECTS, so they commute. Assigning the third-party
    set outright — the shape the intake half shipped, correct while it was alone — would
    hand `resolve_entity` and `assert_fact` straight back whenever it ran second, which is
    exactly the order `/chat` and the unattended runner both use."""
    from jbrain.agent.agents import NOTE_GRAPH_WRITE_TOOLS, narrow_for_emr

    both = NOTE_INGEST_THIRD_PARTY_TOOLS - NOTE_GRAPH_WRITE_TOOLS
    # Enumerated, not derived: what a stranger's words on an EMR note may reach is the two
    # entity reads and the clock. No write verb, and no channel to the owner.
    assert both == {"find_entity", "read_entity", "current_time"}

    registry = _every_shipped_tool()
    for base in (AGENTS["note_ingest"], agent_for_owner_reply("note_ingest")):
        # Both orders, because two call sites apply them and a third could pick either.
        emr_then_third = narrow_for_third_party_note(narrow_for_emr(base))
        third_then_emr = narrow_for_emr(narrow_for_third_party_note(base))
        assert emr_then_third.tools == both
        assert third_then_emr.tools == both
        for scopes in (frozenset(), frozenset({"general"}), _EVERY_SCOPE):
            for narrowed in (emr_then_third, third_then_emr):
                admitted = registry.allowed_names(scopes, narrowed.tools, narrowed.extra_tools)
                assert admitted == both
                # Neither half's own guarantee is weakened by the other being applied.
                assert not (admitted & NOTE_GRAPH_WRITE_TOOLS)
                assert "ask_owner" not in admitted
                assert not (admitted - NOTE_INGEST_THIRD_PARTY_TOOLS)
                assert not (_FORBIDDEN_FOUR & admitted)
    # Applying either one twice, or in either order, is the same set: a caller may narrow
    # unconditionally without knowing what another caller already did.
    once = narrow_for_third_party_note(narrow_for_emr(AGENTS["note_ingest"]))
    assert narrow_for_emr(once).tools == both
    assert narrow_for_third_party_note(once).tools == both


def test_note_ingest_holds_an_explicit_closed_allowlist_not_the_wildcard() -> None:
    """`tools` is a frozenset, never None. `None` is the curator wildcard — the single
    thing D16 forbids for the persona that will hold graph writes — and the difference is
    invisible at the call site (`allow is not None` is the whole gate)."""
    note = AGENTS["note_ingest"]
    assert note.tools is not None
    assert isinstance(note.tools, frozenset)
    assert note.tools == NOTE_INGEST_UNATTENDED_TOOLS
    # The whole unattended set, and nothing else: two graph writes, `ask_owner`, two
    # entity reads, the clock. Enumerated rather than derived, so a tool arrives here by
    # being named and never by inheriting anything — which is how `close_reading` (R1 of
    # `AGENT_INGEST_REWRITE.md`) had to arrive, and how `assert_fact` had to LEAVE in R3.
    assert note.tools == {
        "resolve_entity",
        "close_reading",
        "ask_owner",
        "find_entity",
        "read_entity",
        "current_time",
    }
    # `extra_tools` is admitted AHEAD of the web / NEVER_DEFAULT gates, so it is the one way
    # to hand this persona a tool without touching its allowlist. It stays empty in W3 too.
    assert note.extra_tools == frozenset()
    # W3: True, and the flip is what makes constraint 2 real — a False agent runs with EMPTY
    # read scopes, under which the domain-visible entity reads cannot reach a row and there
    # are no scopes to narrow to `(note_domain, 'general')`.
    assert note.reads_knowledge_base is True
    # 2x, matching the KB-less children: inert while the persona is tool-less, but W3's
    # resolve/assert chain runs many calls per note and a truncated turn is a correctness
    # problem (plan constraint 6: the settle sweep must not run on one), not a short answer.
    assert note.budget_multiplier == 2


def test_note_ingest_admits_only_its_allowlist_through_the_real_registry() -> None:
    """The closure, proven at the dispatch gate rather than on the dataclass: at every
    scope, over every shipped sidecar, the admitted set is EXACTLY the allowlist and never
    more. Rule 2 of `_admits` (`allow is not None and name not in allow`) is what closes
    it, and it fires BEFORE the web and NEVER_DEFAULT gates — so the closure does not
    depend on a tool's permission class, its domains, or its NEVER_DEFAULT membership.

    This registry globs the sidecar directory, so it holds the graph writes too — which
    is the stricter test: even where their sidecars ARE present, the closure holds. The
    registry `analysis.converse` actually builds is narrower still (the same six, with
    the graph writes bound to one note), and `readtools.build_registry` drops those two
    sidecars outright."""
    registry = _every_shipped_tool()
    note = AGENTS["note_ingest"]
    assert note.tools is not None  # the wildcard would make every assertion below vacuous
    assert len(registry) > 100  # the real sidecar set, not a two-tool stub

    # Including NO scopes: every one of the six declares no `domains`, so registry
    # VISIBILITY was never what the `reads_knowledge_base` flip bought — the schemas the
    # model is offered are the same six at every scope. What the flip bought is at the
    # DB: `read_context(pid, ())` is `owner_scoped` with an empty scope list, so
    # `has_domain_scope` is false for every domain and the entity reads would answer
    # "nothing in scope" for every name in the note. RLS is the firewall; the allowlist
    # is the surface.
    for scopes in (frozenset(), frozenset({"general"}), _EVERY_SCOPE):
        assert registry.allowed_names(scopes, note.tools, note.extra_tools) == note.tools
        offered = [t.name for t in registry.schemas_for(scopes, note.tools, note.extra_tools)]
        assert sorted(offered) == sorted(note.tools)


def test_note_ingest_cannot_reach_the_four_verbs_d16_names() -> None:
    """`file_correction`, `add_source_exclusion`, `make_intake_link` and `remember` are
    REAL registered tools that the curator wildcard does admit — they are outside this
    persona because its allowlist excludes them, not because they are absent from the box.
    That contrast is the whole content of D16."""
    registry = _every_shipped_tool()
    note, curator = AGENTS["note_ingest"], AGENTS["curator"]
    assert all(name in registry for name in _FORBIDDEN_FOUR)

    admitted = registry.allowed_names(_EVERY_SCOPE, note.tools, note.extra_tools)
    assert not (_FORBIDDEN_FOUR & admitted)
    # The wildcard reaches all four — what a note conversation would inherit without D16.
    wildcard = registry.allowed_names(_EVERY_SCOPE, curator.tools, curator.extra_tools)
    assert wildcard >= _FORBIDDEN_FOUR
    # And the closure holds for the classes the wildcard itself excludes, so no future
    # loosening of the web / NEVER_DEFAULT gates can leak one in through the back.
    assert not ({"web_search", "web_fetch", SPAWN_TOOL, "deep_produce"} & admitted)


def test_agent_for_resolves_note_ingest_and_never_the_curator_fallback() -> None:
    """`agent_for` falls back to the KB-capable curator — the WILDCARD persona — on any
    unknown name. A typo between this profile's key and the DB CHECK would therefore hand a
    note conversation every tool D16 exists to withhold, silently. Pin the exact key."""
    profile = agent_for("note_ingest")
    assert profile is AGENTS["note_ingest"]
    assert profile.name == "note_ingest"
    assert profile.tools == NOTE_INGEST_UNATTENDED_TOOLS  # a closed set, not curator's None
    assert profile.tools is not None
    assert is_agent("note_ingest")


def test_note_ingest_is_owner_side_and_never_spawnable() -> None:
    """It mints an app.agent_sessions row, so it must be STORABLE owner-side (migration
    0192 widens both agent CHECKs to match; test_agent_session_rls/test_tasks_rls iterate
    STORABLE_OWNER_AGENTS against the DB, which is what keeps AGENTS and the CHECK from
    drifting).

    It is NOT in SUBAGENT_PERSONAS: a spawnable note-ingest persona would be a path for any
    other agent to reach whatever W3 grants this one — jerv could spawn a child holding the
    graph writes. Nothing spawns a note conversation; the ingest path opens it."""
    assert "note_ingest" in STORABLE_OWNER_AGENTS
    assert "note_ingest" not in NON_OWNER_PERSONAS
    assert "note_ingest" not in SUBAGENT_PERSONAS


def test_note_ingest_is_engine_only_and_not_selectable() -> None:
    """ASSISTANT.md says the note persona "is not selectable — the engine opens it, never
    a picker", and this is what makes that true rather than aspirational.

    `POST /sessions {"agent": ...}` and the task launcher both gate on OWNER_AGENTS
    (`api/sessions.py`, `api/tasks.py`), so leaving `note_ingest` in that set accepted a
    hand-started note persona: no note, no frame, no `note_conversations` row, and
    owner-chosen read scopes. Inert today behind the empty allowlist — and exactly the
    door W3 must not find already open when it fills that allowlist with graph writes."""
    assert "note_ingest" in ENGINE_ONLY_PERSONAS
    assert "note_ingest" not in OWNER_AGENTS
    assert not is_owner_agent("note_ingest")
    # The engine-only set narrows what a person may pick, and nothing else: every other
    # owner persona is still selectable, and the storable set is unchanged.
    assert is_owner_agent("curator") and is_owner_agent("archivist")
    assert STORABLE_OWNER_AGENTS == AGENT_NAMES - NON_OWNER_PERSONAS


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
            "agent-jerv-v48",
            "47efedc798419f86b1d91e3cf30b8e8e5b8f5b13a2b89adcef48b5812d2164b9",
        ),
        "archivist": (
            "agent-archivist-v7",
            "1759d150d170e326f5948d3d1ac60ee35e2e6ef24f9d21d8d7453ffae5e14fcd",
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
            "agent-jmolt-v5",
            "1f5bff149da78e6109f474d01ffba3b423752d5ccc2853c7c6fb1a76d2a99599",
        ),
        "jmolt_observer": (
            "agent-jmolt-observer-v1",
            "09e2ace3e0f8c85a92608ff017118e069b8f9729d8c9e13cb820d6f3dabcfa40",
        ),
        "note_ingest": (
            "agent-note-ingest-v8",
            "1cf42e98d2e41c61a397cf31471ffd14be49a244bc5fa3e32399138321e5354d",
        ),
    }
    assert set(pins) == AGENT_NAMES
    for name, (version, digest) in pins.items():
        profile = AGENTS[name]
        assert profile.version == version
        assert hashlib.sha256(profile.prompt.encode()).hexdigest() == digest
