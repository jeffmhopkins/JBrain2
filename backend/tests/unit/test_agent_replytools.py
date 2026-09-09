"""The on-reply half of the note persona's surface, without a database.

Three things are under test here, and only the first is about the tools themselves:

1. **The seam.** D8 says the full surface unlocks when the owner replies, and
   TOOL_SURFACE R2 says the mechanism is two frozensets chosen at turn assembly rather
   than one set with a flag. The unattended pass and the reply turn are DIFFERENT CODE
   (`analysis/converse.py` in the worker; `chat()` in the API), and both read
   `AgentProfile.tools` — so the hazard is real in both directions and both are asserted:
   the worker never widens, and `/chat` always does. The worker-side half lives in
   `test_note_converse.py`, next to the registry it builds.
2. **The refusals.** Every handler reports failure as TEXT (`loop.py` turns a raise into
   a generic "hit an internal error" the model learns nothing from), and both tools
   refuse outright outside a note conversation — neither takes a note id, so a turn with
   no conversation behind it has nothing to write to.
3. **`read_note`'s frame.** Plan risk 1 says note bodies reach the model unframed and
   that this is safe only while the persona reading them holds no tools; W3 is the wave
   that makes it false. The frame is keyed on the TURN's write authority, so curator and
   jerv are byte-for-byte unchanged.
"""

from typing import Any

import pytest

from jbrain.agent.agents import (
    AGENTS,
    NOTE_INGEST_ON_REPLY_TOOLS,
    NOTE_INGEST_UNATTENDED_TOOLS,
    agent_for,
    agent_for_owner_reply,
)
from jbrain.agent.loop import ToolContext
from jbrain.agent.readtools import GRAPH_WRITE_AUTHORITY, _holds_graph_writes
from jbrain.agent.replytools import (
    CORRECT_FACT,
    MERGE_ENTITIES,
    REPLY_WRITE_TOOLS,
    build_reply_write_handlers,
)
from jbrain.agent.toolfile import load_tool
from jbrain.agent.toolregistry import NEVER_DEFAULT
from jbrain.db.session import SessionContext

TOOLS_DIR = __import__("jbrain.agent.readtools", fromlist=["x"]).TOOLS_DIR

OWNER = SessionContext(principal_kind="owner", principal_id="p1", domain_scopes=("general",))


def _ctx(session_id: str | None = "sess-1", scopes: tuple[str, ...] = ("general",)) -> ToolContext:
    return ToolContext(session=OWNER, scopes=scopes, agent_session_id=session_id)


# --- the seam ----------------------------------------------------------------


def test_the_two_sets_are_two_frozensets_and_the_choice_is_a_function() -> None:
    """R2's shape: `JERV_TOOLS`/`ARCHIVIST_TOOLS`, not one set with flags. Neither set
    carries a marker, a predicate or a per-tool condition — the ONLY thing that decides
    which one a turn runs under is which function assembled the profile."""
    assert isinstance(NOTE_INGEST_UNATTENDED_TOOLS, frozenset)
    assert isinstance(NOTE_INGEST_ON_REPLY_TOOLS, frozenset)
    assert agent_for("note_ingest").tools == NOTE_INGEST_UNATTENDED_TOOLS
    assert agent_for_owner_reply("note_ingest").tools == NOTE_INGEST_ON_REPLY_TOOLS


def test_chat_resolves_the_persona_through_the_reply_selector() -> None:
    """`/chat` IS the on-reply path, so it must not call `agent_for`. Asserted on the
    module's own source because the alternative is a full streaming turn, and what is
    actually at stake is one name at one call site — the exact thing a later refactor
    can silently revert.

    The import list is checked too: leaving `agent_for` importable here is how the
    narrow resolution creeps back in on a second call site."""
    from pathlib import Path

    from jbrain.api import agent as chat_module

    src = Path(chat_module.__file__).read_text(encoding="utf-8")
    assert "profile = agent_for_owner_reply(session.agent)" in src
    assert "profile = agent_for(session.agent)" not in src
    assert "import agent_for\n" not in src and " agent_for,\n" not in src


def test_the_worker_never_imports_the_reply_selector() -> None:
    """The other direction of the same argument: the unattended pass resolves its
    persona in `analysis/converse.py`, and the widening function must not be reachable
    from there at all. A note pass that widened itself is the whole of what D8 forbids."""
    from pathlib import Path

    from jbrain.analysis import converse

    src = Path(converse.__file__).read_text(encoding="utf-8")
    assert "agent_for_owner_reply" not in src
    assert "agent_for(NOTE_CONVERSE_AGENT)" in src


# --- the registry locks ------------------------------------------------------


def test_both_write_verbs_are_never_default_and_in_no_stored_profile() -> None:
    """Constraint 9. `permission: sensitive` is documentation — `outcome_for` /
    `DEFAULT_OWNER_POLICY` is defined and never consulted by the loop — so NEVER_DEFAULT
    plus the allowlist is the whole of the enforcement."""
    assert {CORRECT_FACT, MERGE_ENTITIES} == REPLY_WRITE_TOOLS
    assert REPLY_WRITE_TOOLS <= NEVER_DEFAULT
    holders = {
        name
        for name, profile in AGENTS.items()
        if REPLY_WRITE_TOOLS & ((profile.tools or frozenset()) | profile.extra_tools)
    }
    assert holders == set()
    assert REPLY_WRITE_TOOLS <= NOTE_INGEST_ON_REPLY_TOOLS


@pytest.mark.parametrize("name", sorted(REPLY_WRITE_TOOLS))
def test_the_sidecars_carry_no_enum_and_require_every_field(name: str) -> None:
    """Constraint 8 (an enum in a sidecar segfaults gpt-oss's harmony grammar) and R3
    (across 85 consecutive calls the model filled the required field every time and the
    optional one never once). Both tools therefore make every field required and give
    the ones that can be absent an explicit empty-string escape, exactly as
    `assert_fact.when` does."""
    spec = load_tool(TOOLS_DIR / f"{name}.tool").spec
    assert spec.permission == "sensitive"
    props = spec.params["properties"]
    assert set(spec.params["required"]) == set(props)

    def _no_enum(node: Any) -> None:
        if isinstance(node, dict):
            assert "enum" not in node, f"{name}: enum in the sidecar schema"
            for value in node.values():
                _no_enum(value)
        elif isinstance(node, list):
            for value in node:
                _no_enum(value)

    _no_enum(spec.params)
    # Nothing nested: the model is never asked to build an object inside an argument.
    assert all(p.get("type") == "string" for p in props.values())


def test_correct_fact_addresses_by_identity_key_and_never_by_fact_id() -> None:
    """TOOL_SURFACE's load-bearing schema decision. `readtools._edge_line` prints
    `predicate: statement` and no fact id, so an id-addressed tool would force a
    `read_entity` v5 and a second addressing vocabulary. The key the model can SEE is
    the key it writes."""
    props = load_tool(TOOLS_DIR / "correct_fact.tool").spec.params["properties"]
    assert {"entity", "predicate", "qualifier"} <= set(props)
    assert not [k for k in props if "fact_id" in k or k == "id"]
    # And no `replaces` either. It was the multi-row escape — a handle over the entity
    # page's grouping — and it could not work: the only keys that hold several live rows
    # are non-functional relationships, which are exactly the keys `decide()`'s correction
    # branch skips (it acts on a `single_head` address only). The retry it invited left
    # both originals live, added a third row, and reported `ok … replaced`.
    assert "replaces" not in props


def test_the_four_note_graph_verbs_are_all_bound_for_the_reply_turn() -> None:
    """`NOTE_INGEST_ON_REPLY_TOOLS` names six tools; four of them write the graph and all
    four have to be BOUND on the chat registry, which is the reply turn's only registry.

    For a whole wave two of them were not: `build_registry` dropped the `resolve_entity`
    and `assert_fact` sidecars unconditionally, so the reply turn was offered neither and
    could dispatch neither, and its only remaining write verb was `correct_fact` — whose
    empty-address path commits active + PINNED. Every fact the owner taught a note thread
    was pinned against every later note."""
    handlers = build_reply_write_handlers(_unusable_maker(), _proposals(), _entities(), _notes())
    assert set(handlers) == {CORRECT_FACT, MERGE_ENTITIES, "resolve_entity", "assert_fact"}
    assert set(handlers) <= NOTE_INGEST_ON_REPLY_TOOLS
    # Every one of them has a sidecar in the chat registry's directory, or the name is
    # allowlisted with nothing behind it — the failure this test exists for.
    for name in handlers:
        assert (TOOLS_DIR / f"{name}.tool").exists(), name


# --- the refusals ------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("name", sorted(REPLY_WRITE_TOOLS | {"resolve_entity", "assert_fact"}))
async def test_no_tool_does_anything_outside_a_note_conversation(name: str) -> None:
    """None takes a note id — a write primitive a hostile body could point at another
    note is a hole, not a tool — so a turn with no conversation behind it is refused. In
    TEXT: a raise here becomes `loop.py`'s generic internal error, which tells the model
    nothing and invites the same call again.

    Parametrized over all FOUR since `resolve_entity`/`assert_fact` joined the chat
    registry: they are the two that mint entities and write facts, so "this turn has no
    note" is the one refusal they most need."""
    handlers = build_reply_write_handlers(_unusable_maker(), _proposals(), _entities(), _notes())
    out = await handlers[name]({"entity": "x", "entity_a": "a", "entity_b": "b"}, _ctx(None))
    assert isinstance(out, str)
    assert "note's conversation" in out
    assert "Nothing was" in out or "changed" in out


@pytest.mark.anyio
async def test_a_handler_that_blows_up_answers_in_text() -> None:
    """The blanket guard. Every failure path is a sentence naming what did NOT happen,
    because the one thing worse than a refused correction is the model telling Jeff his
    correction landed."""
    handlers = build_reply_write_handlers(_boom_maker(), _proposals(), _entities(), _notes())
    out = await handlers[CORRECT_FACT]({"entity": "x"}, _ctx())
    assert isinstance(out, str)
    assert "nothing was changed" in out.lower()
    assert "did not go through" in out


# --- read_note's frame -------------------------------------------------------


def test_the_frame_is_keyed_on_the_turns_write_authority_not_its_persona() -> None:
    """The hazard is untrusted third-party text arriving in a turn that can WRITE the
    graph, so the trigger asks the turn (`ToolContext.agent_tools`, the loop's admitted
    set) rather than guessing at who wired what — the same mechanical-boundary idiom
    `jmoltobservetools` uses."""
    assert {"assert_fact", "correct_fact"} == GRAPH_WRITE_AUTHORITY
    assert not _holds_graph_writes(_ctx())
    assert _holds_graph_writes(
        ToolContext(session=OWNER, scopes=("general",), agent_tools=frozenset({"correct_fact"}))
    )
    assert _holds_graph_writes(
        ToolContext(session=OWNER, scopes=("general",), agent_tools=frozenset({"assert_fact"}))
    )
    # Curator and jerv hold neither verb, so their `read_note` output is unchanged — the
    # frame is a new boundary for the write-holding persona, not a chat-wide change.
    assert not (GRAPH_WRITE_AUTHORITY & (AGENTS["jerv"].tools or frozenset()))
    assert not _holds_graph_writes(
        ToolContext(session=OWNER, scopes=("general",), agent_tools=frozenset({"read_note"}))
    )


def test_the_reply_turn_holds_both_read_note_and_the_authority_that_frames_it() -> None:
    """The pairing that makes the frame matter: the on-reply set is the first allowlist
    in the repo to hold `read_note` AND a graph-write verb at the same time."""
    assert "read_note" in NOTE_INGEST_ON_REPLY_TOOLS
    assert GRAPH_WRITE_AUTHORITY & NOTE_INGEST_ON_REPLY_TOOLS
    # The unattended pass holds neither `read_note` nor any corpus read, so its only
    # untrusted text is turn 0 — which `converse.framed_note` already fences.
    assert "read_note" not in NOTE_INGEST_UNATTENDED_TOOLS


# --- stubs -------------------------------------------------------------------


def _unusable_maker() -> Any:
    """A session maker no test here may open — both refusals must be decided before any
    database work, which is also what makes them safe to reach on a broken box."""

    def maker() -> Any:  # pragma: no cover - opening it is the failure
        raise AssertionError("the refusal must land before a session is opened")

    return maker


def _boom_maker() -> Any:
    def maker() -> Any:
        raise RuntimeError("postgres is gone")

    return maker


def _proposals() -> Any:
    class _P:
        async def stage(self, *_a: Any, **_k: Any) -> str:  # pragma: no cover
            raise AssertionError("nothing here should stage")

    return _P()


def _entities() -> Any:
    class _E:
        async def entity_view(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover
            return None

        async def list_entities(self, *_a: Any, **_k: Any) -> list[Any]:  # pragma: no cover
            return []

    return _E()


def _notes() -> Any:
    class _N:
        async def get_note(self, *_a: Any, **_k: Any) -> None:  # pragma: no cover
            return None

    return _N()
