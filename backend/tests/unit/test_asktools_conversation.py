"""`ask_owner` outside a note thread (SHOW_THE_WORKING_PLAN.md W4, O1).

The note-thread path is covered by the integration suite, which has a real conversation row
to record against. What is new here is the path where there is NO such row — a plain
conversation — and the whole of it is: record the set on the TURN, end the turn, write
nothing else. So the session it opens is faked to the point of doing nothing, which is
exactly the assertion: this branch must not reach for a ledger row or a state flip that does
not exist for it.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from jbrain.agent import asktools
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.models.note_conversation import AWAITING_OWNER

OWNER = SessionContext(principal_id="p1", principal_kind="owner")


@pytest.fixture
def chat_handler(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`ask_owner` with no conversation behind the session — a plain /chat turn."""

    @contextlib.asynccontextmanager
    async def fake_scoped(_maker: object, _ctx: object) -> AsyncIterator[object]:
        yield object()

    async def no_conversation(_self: object, _session: object, _sid: str) -> None:
        return None

    def explode(*_a: object, **_k: object) -> None:
        raise AssertionError("a plain conversation has no ledger row to record against")

    monkeypatch.setattr(asktools, "scoped_session", fake_scoped)
    monkeypatch.setattr(asktools.NoteConversationRepo, "get", no_conversation)
    monkeypatch.setattr(asktools.NoteConversationRepo, "record_tool_call", explode)
    monkeypatch.setattr(asktools.NoteConversationRepo, "set_state", explode)
    return asktools.build_ask_owner_handlers(maker=None)["ask_owner"]  # type: ignore[arg-type]


def _ctx(session_id: str | None = "chat-1") -> ToolContext:
    return ToolContext(session=OWNER, scopes=("general",), agent_session_id=session_id)


async def test_a_chat_question_is_recorded_on_the_turn_and_ends_it(chat_handler: Any) -> None:
    """The set lives on the turn that asked. `recorded_args` is the ONLY thing the PWA's
    question block is built from — it never reads a conversation row — so the block renders
    in a plain chat exactly as it does in a note thread."""
    out = await chat_handler(
        {"questions": [{"question": "Which Sarah is this?", "blocks": "who you met"}]}, _ctx()
    )
    assert isinstance(out, ToolOutput)
    assert out.halt == AWAITING_OWNER
    assert out.result_brief == "1 question"
    recorded = out.recorded_args
    assert recorded is not None
    questions = asktools.recorded_args(asktools._asked(recorded))["questions"]
    assert questions[0]["question"] == "Which Sarah is this?"
    # Minted server-side, and the id is what an answer is paired against.
    assert questions[0]["id"]


async def test_the_turn_ends_even_though_nothing_was_written(chat_handler: Any) -> None:
    """The halt is the mechanism, not a prose obligation: TOOL_SURFACE.md is explicit that
    gpt-oss does not honour "stop now" stated in a description, so the loop ends the turn.
    That has to hold on the path that writes no row, or a chat question would be asked and
    then talked straight past."""
    out = await chat_handler({"questions": [{"question": "Which one?"}]}, _ctx())
    assert out.halt == AWAITING_OWNER


async def test_an_empty_ask_is_still_refused_without_ending_the_turn(chat_handler: Any) -> None:
    """A question that was not recorded must not stop the turn — the same rule as in a note
    thread, and the reason is the same: a stopped turn with nothing to answer is a dead end.
    """
    out = await chat_handler({"questions": []}, _ctx())
    assert getattr(out, "halt", None) is None


async def test_it_still_refuses_outside_a_session_entirely(chat_handler: Any) -> None:
    """A turn with no session id is a worker pass, not a conversation: there is nowhere to
    put the question and nobody watching for it."""
    out = await chat_handler(
        {"questions": [{"question": "Which one?"}]},
        _ctx(None),
    )
    assert getattr(out, "halt", None) is None
