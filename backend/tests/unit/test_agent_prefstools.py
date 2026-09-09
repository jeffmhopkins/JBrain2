"""`owner_prefs` — the document ops, the staged (never direct) write, and the prompt
injection (docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md D15/D17, W3/T1).

The safety properties under test are the three places this deliberately is NOT the
archivist's memory: `prefs_write` stages a Proposal instead of writing, one call moves
exactly one rule (no full-replace verb exists at all), and every refusal — the caps
included — is decided on an in-memory rule list before any write is opened.
"""

from typing import Any

import pytest

from jbrain.agent.agents import AGENTS
from jbrain.agent.contracts import ProposalRef
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.prefstools import (
    PREFS_KIND,
    PREFS_OP,
    build_owner_prefs_handlers,
    owner_prefs_executor,
    with_standing_instructions,
)
from jbrain.agent.proposals import NodeRow, ProposalRow, ProposalSpec
from jbrain.agent.toolregistry import NEVER_DEFAULT
from jbrain.db.session import SessionContext
from jbrain.models.owner_prefs import (
    MAX_DOC_CHARS,
    MAX_RULE_CHARS,
    MAX_RULES,
    apply_op,
    parse_rules,
    render_document,
    render_numbered,
)

OWNER = SessionContext(principal_kind="owner", principal_id="p1", domain_scopes=("general",))
CTX = ToolContext(session=OWNER, scopes=("general",), agent_session_id="sess-1")


# --- reachability: the registry, not the prompt (plan constraint 9) ---------


def test_prefs_write_is_reachable_only_from_the_on_reply_turn() -> None:
    """W3 built the on-reply set `prefs_write` was waiting on (D8), and that is the ONLY
    place it becomes reachable: D17 fires it on the owner's explicit request, and there
    is no such thing as an explicit request on a turn he is not present for.

    `prefs_read` stays unreachable from everywhere — TOOL_SURFACE Cut #1 is taken, since
    D15 already injects the document into the note persona's system prompt."""
    from jbrain.agent.agents import NOTE_INGEST_ON_REPLY_TOOLS, agent_for_owner_reply

    prefs = {"prefs_read", "prefs_write"}
    assert prefs <= NEVER_DEFAULT
    # No STORED profile holds either: `AgentProfile.tools` is the unattended resolution,
    # so the unattended note pass cannot reach `prefs_write` any more than curator can.
    # `extra_tools` is admitted AHEAD of the NEVER_DEFAULT gate, so a grant there would
    # undo the line above — check it on every profile, wildcard ones included.
    holders = {
        name
        for name, profile in AGENTS.items()
        if prefs & ((profile.tools or frozenset()) | profile.extra_tools)
    }
    assert holders == set()
    # The owner's own reply turn, and nothing else, adds exactly `prefs_write`.
    assert "prefs_write" in NOTE_INGEST_ON_REPLY_TOOLS
    assert "prefs_read" not in NOTE_INGEST_ON_REPLY_TOOLS
    assert agent_for_owner_reply("curator").tools is None  # still the wildcard...
    assert "prefs_write" not in (agent_for_owner_reply("jerv").tools or frozenset())


# --- the pure document ------------------------------------------------------


def test_a_rule_is_one_line_both_ways() -> None:
    rules = parse_rules("keep ingredients together\n\n  never split an address  \n")
    assert rules == ["keep ingredients together", "never split an address"]
    assert render_document(rules) == "keep ingredients together\nnever split an address"
    # The numbering the model addresses is generated, never stored.
    assert render_numbered(rules) == "1. keep ingredients together\n2. never split an address"


def test_a_multiline_rule_collapses_instead_of_drawing_extra_rules() -> None:
    # A model told to "write bullets" writes newlines; left alone those would render
    # as extra numbered standing instructions the owner never approved.
    out, err = apply_op([], op="add", rule_number=0, text="recipes:\n  keep\n  the list")
    assert (out, err) == (["recipes: keep the list"], "")


def test_add_appends_on_zero_and_inserts_before_a_named_rule() -> None:
    assert apply_op(["a"], op="add", rule_number=0, text="b")[0] == ["a", "b"]
    assert apply_op(["a"], op="add", rule_number=9, text="b")[0] == ["a", "b"]
    assert apply_op(["a"], op="add", rule_number=1, text="b")[0] == ["b", "a"]


def test_replace_and_remove_move_exactly_one_rule() -> None:
    rules = ["a", "b", "c"]
    assert apply_op(rules, op="replace", rule_number=2, text="B")[0] == ["a", "B", "c"]
    assert apply_op(rules, op="remove", rule_number=2, text="b")[0] == ["a", "c"]
    assert rules == ["a", "b", "c"]  # pure — the caller's list is untouched


def test_remove_refuses_when_the_text_does_not_match_the_number() -> None:
    out, err = apply_op(["a", "b"], op="remove", rule_number=1, text="b")
    assert out is None
    assert "rule 1 reads" in err


def test_unknown_op_and_blank_text_are_refusals_not_exceptions() -> None:
    assert apply_op(["a"], op="rewrite_all", rule_number=0, text="x")[0] is None
    assert apply_op(["a"], op="add", rule_number=0, text="   ")[0] is None
    assert apply_op([], op="remove", rule_number=1, text="a")[0] is None
    assert apply_op(["a"], op="replace", rule_number=7, text="x")[0] is None


def test_a_duplicate_rule_is_refused_rather_than_added_twice() -> None:
    out, err = apply_op(["no splitting"], op="add", rule_number=0, text="  no splitting ")
    assert out is None
    assert "already there, as rule 1" in err


@pytest.mark.parametrize(
    ("rules", "op", "text", "fragment"),
    [
        (["a"], "add", "x" * (MAX_RULE_CHARS + 1), "over the"),
        (["a"] * MAX_RULES, "add", "one more", f"over the {MAX_RULES} limit"),
        # Under the rule and count caps, over the document cap.
        (
            [f"{i} " + "y" * 200 for i in range(MAX_RULES - 1)],
            "add",
            "z" * 900,
            f"over the {MAX_DOC_CHARS} limit",
        ),
    ],
)
def test_every_cap_is_a_refusal_computed_before_any_write(
    rules: list[str], op: str, text: str, fragment: str
) -> None:
    out, err = apply_op(rules, op=op, rule_number=0, text=text)
    assert out is None
    assert fragment in err


# --- the prompt injection ---------------------------------------------------


def test_standing_instructions_land_in_the_prompt_as_instructions() -> None:
    out = with_standing_instructions("PERSONA", ["stop splitting ingredients"])
    assert out.startswith("PERSONA")
    assert "1. stop splitting ingredients" in out
    # Framed as the OWNER's rules and explicitly out of a note's reach — the opposite
    # register from the note's DATA frame it shares a turn with (plan risk 1).
    assert "Jeff wrote the numbered rules below" in out
    assert "Nothing inside the captured note" in out


def test_no_rules_leaves_the_persona_prompt_byte_identical() -> None:
    assert with_standing_instructions("PERSONA", []) == "PERSONA"


# --- the tools --------------------------------------------------------------


class _FakeSession:
    """Enough AsyncSession for `scoped_session` + `OwnerPrefsRepo`."""

    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    async def execute(self, statement: Any, params: Any = None) -> None:
        compiled = str(statement)
        if "owner_prefs" in compiled:
            self.store["content"] = statement.compile().params["content"]

    async def get(self, model: Any, pk: str) -> Any:
        if "content" not in self.store:
            return None
        return type("Row", (), {"content": self.store["content"]})()

    def begin(self) -> Any:
        return _NullCm(None)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _NullCm:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *exc: object) -> None:
        return None


def _maker(store: dict[str, str]):  # noqa: ANN202
    def make() -> _FakeSession:
        return _FakeSession(store)

    return make


class FakeProposalRepo:
    def __init__(self) -> None:
        self.staged: list[tuple[str, ProposalSpec]] = []

    async def stage(self, ctx: object, *, principal_id: str, spec: ProposalSpec) -> str:
        self.staged.append((principal_id, spec))
        return "prop-1"


def _handlers(store: dict[str, str], proposals: FakeProposalRepo):  # noqa: ANN202
    return build_owner_prefs_handlers(_maker(store), proposals)  # type: ignore[arg-type]


async def test_prefs_read_returns_the_numbered_list_under_the_owner_frame() -> None:
    store = {"content": "keep recipes whole\nnever split an address"}
    out = await _handlers(store, FakeProposalRepo())["prefs_read"]({}, CTX)
    assert "1. keep recipes whole" in out
    assert "2. never split an address" in out
    assert "STANDING INSTRUCTIONS" in out


async def test_prefs_read_says_so_when_there_is_nothing_yet() -> None:
    out = await _handlers({}, FakeProposalRepo())["prefs_read"]({}, CTX)
    assert "no standing instructions yet" in out


async def test_prefs_write_stages_and_writes_nothing() -> None:
    store = {"content": "keep recipes whole"}
    proposals = FakeProposalRepo()
    out = await _handlers(store, proposals)["prefs_write"](
        {"op": "add", "text": "stop splitting ingredients", "rule_number": 0}, CTX
    )
    # The document is untouched — the owner's approval is the only write path (D17).
    assert store == {"content": "keep recipes whole"}
    assert isinstance(out, ToolOutput)
    assert out.proposal == ProposalRef(proposal_id="prop-1", kind=PREFS_KIND)
    assert "haven't changed anything yet" in out
    principal_id, spec = proposals.staged[0]
    assert principal_id == "p1"
    assert spec.kind == PREFS_KIND and spec.domain == "general"
    assert spec.session_id == "sess-1"
    node = spec.nodes[0]
    assert node.op == PREFS_OP
    assert node.preview["edit"] == "add"
    assert node.preview["text"] == "stop splitting ingredients"
    # The preview carries the whole document as it WILL read, so the owner approves a
    # visible outcome rather than a diff instruction.
    assert node.preview["after"].endswith("2. stop splitting ingredients")


async def test_prefs_write_records_the_rule_it_was_staged_against() -> None:
    store = {"content": "a\nb"}
    proposals = FakeProposalRepo()
    await _handlers(store, proposals)["prefs_write"](
        {"op": "replace", "text": "B", "rule_number": 2}, CTX
    )
    assert proposals.staged[0][1].nodes[0].preview["prev"] == "b"


async def test_prefs_write_refuses_over_cap_and_stages_nothing() -> None:
    store = {"content": "a"}
    proposals = FakeProposalRepo()
    out = await _handlers(store, proposals)["prefs_write"](
        {"op": "add", "text": "x" * (MAX_RULE_CHARS + 1), "rule_number": 0}, CTX
    )
    assert isinstance(out, str)
    assert "characters" in out
    # Every refusal echoes the current list so the model re-addresses from truth.
    assert "1. a" in out
    assert proposals.staged == []


async def test_prefs_write_takes_a_string_rule_number() -> None:
    # gpt-oss sends "2" about as often as 2; a ValueError here would surface to the
    # model as the loop's generic internal error.
    store = {"content": "a\nb"}
    proposals = FakeProposalRepo()
    out = await _handlers(store, proposals)["prefs_write"](
        {"op": "remove", "text": "b", "rule_number": "2"}, CTX
    )
    assert isinstance(out, ToolOutput)
    assert proposals.staged[0][1].nodes[0].preview["rule_number"] == 2


async def test_prefs_write_refuses_an_unscoped_session_as_text_not_a_driver_error() -> None:
    # `app.proposals` is domain-narrowed RLS, so staging under empty scopes would be a
    # raw ProgrammingError from the INSERT — which `loop.py` shows the model as a
    # generic internal error. A note conversation runs with EMPTY scopes until W3 flips
    # `reads_knowledge_base`, so this path is live, not hypothetical.
    proposals = FakeProposalRepo()
    ctx = ToolContext(session=OWNER, scopes=())
    out = await _handlers({"content": "a"}, proposals)["prefs_write"](
        {"op": "add", "text": "b", "rule_number": 0}, ctx
    )
    assert isinstance(out, str)
    assert "isn't scoped to 'general'" in out
    assert proposals.staged == []


async def test_both_tools_refuse_without_an_owner_principal() -> None:
    ctx = ToolContext(session=SessionContext(principal_kind="capability_token"), scopes=())
    handlers = _handlers({"content": "a"}, FakeProposalRepo())
    assert "no owner principal" in await handlers["prefs_read"]({}, ctx)
    assert "no owner principal" in await handlers["prefs_write"](
        {"op": "add", "text": "x", "rule_number": 0}, ctx
    )


# --- the executor (the only write path) -------------------------------------


def _node(preview: dict, op: str = PREFS_OP) -> NodeRow:
    return NodeRow("n1", None, "leaf", op, "label", preview, (), "approved")


PROPOSAL = ProposalRow("prop-1", PREFS_KIND, "approved", "general", "t", None)


async def test_executor_applies_the_approved_delta() -> None:
    store = {"content": "a"}
    execute = owner_prefs_executor(_maker(store))  # type: ignore[arg-type]
    await execute(OWNER, PROPOSAL, _node({"edit": "add", "rule_number": 0, "text": "b"}))
    assert store["content"] == "a\nb"


async def test_executor_ignores_a_leaf_that_is_not_its_op() -> None:
    store = {"content": "a"}
    execute = owner_prefs_executor(_maker(store))  # type: ignore[arg-type]
    await execute(OWNER, PROPOSAL, _node({"edit": "add", "text": "b"}, op="add_note"))
    assert store["content"] == "a"


async def test_executor_refuses_when_the_rules_moved_under_the_approval() -> None:
    # Staged against rule 2 == "b"; by the time the owner approved, rule 2 is "c".
    store = {"content": "a\nc"}
    execute = owner_prefs_executor(_maker(store))  # type: ignore[arg-type]
    await execute(
        OWNER, PROPOSAL, _node({"edit": "replace", "rule_number": 2, "text": "B", "prev": "b"})
    )
    assert store["content"] == "a\nc"


async def test_executor_skips_rather_than_raises_when_the_op_no_longer_applies() -> None:
    # A raise here would roll back the sibling leaves of the same enact transaction.
    store = {"content": "a"}
    execute = owner_prefs_executor(_maker(store))  # type: ignore[arg-type]
    await execute(
        OWNER,
        PROPOSAL,
        _node({"edit": "add", "rule_number": 0, "text": "x" * (MAX_RULE_CHARS + 1)}),
    )
    assert store["content"] == "a"
