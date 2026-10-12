"""The desk panel's switcher, apart from the database: what a row and a bubble may hold, the
gate's refusals, and the placeholder title that does not count as a name."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from jbrain.agent.session import PANEL_PLACEHOLDER_TITLE, is_unnamed
from jbrain.api import panel_agent, panel_convo
from jbrain.auth.service import PrincipalInfo

LONG = " ".join(f"Sentence {i} is about the tide and the river mouth." for i in range(40))


def test_a_row_s_title_is_the_name_or_its_maker_s_placeholder_cut_to_forty_bytes() -> None:
    assert panel_convo.title_of("Shopping list", "panel") == "Shopping list"
    assert panel_convo.title_of("  ", "panel") == "Desk panel"
    assert panel_convo.title_of("", "pwa") == "Untitled chat"
    assert panel_convo.title_of("two\nlines", "pwa") == "two lines"
    cut = panel_convo.title_of("é" * 60, "pwa")
    assert len(cut.encode()) <= panel_convo.TITLE_BYTES and cut.endswith("…")


def test_a_row_s_last_line_is_plain_cut_and_says_whose_it_is() -> None:
    assert panel_convo.snippet_of("assistant", "**Added** milk.") == "Added milk."
    assert panel_convo.snippet_of("user", "Add milk.") == "You: Add milk."
    legacy = panel_agent.LEGACY_VOICE_PREFIX + "Add milk."
    assert panel_convo.snippet_of("user", legacy) == "You: Add milk.", "never the old prefix"
    assert panel_convo.snippet_of("assistant", "```\ncode\n```") == ""
    cut = panel_convo.snippet_of("user", LONG)
    assert len(cut.encode()) <= panel_convo.SNIPPET_BYTES and cut.startswith("You: ")


def test_bubbles_keep_the_newest_answer_long_and_every_other_message_short() -> None:
    got = panel_convo.lines(
        [
            ("user", LONG),
            ("assistant", LONG),
            ("assistant", ""),
            ("user", "And now?"),
            ("assistant", LONG),
        ]
    )
    assert [line.mine for line in got] == [True, False, True, False], "an empty answer is skipped"
    assert all(len(line.text.encode()) <= panel_convo.BUBBLE_BYTES for line in got[:-1])
    assert panel_convo.BUBBLE_BYTES < len(got[-1].text.encode()) <= panel_convo.NEWEST_BYTES
    assert got[1].text.endswith(panel_agent.MORE.strip())
    assert panel_convo.lines([]) == []


def _request() -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_maker=object())))


async def _allowed(maker: Any, principal: Any) -> bool:
    return True


async def _refused(maker: Any, principal: Any) -> bool:
    return False


async def _no_owner(maker: Any) -> None:
    return None


@pytest.mark.anyio
async def test_the_gate_refuses_first_and_an_ownerless_box_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    panel = PrincipalInfo(id="p", kind="device_key", label="panel", subject_id="s")
    monkeypatch.setattr(panel_convo, "agent_allowed", _refused)
    with pytest.raises(HTTPException) as refused:
        await panel_convo._owner(_request(), panel)
    assert refused.value.status_code == 403
    monkeypatch.setattr(panel_convo, "agent_allowed", _allowed)
    monkeypatch.setattr(panel_convo, "owner_principal_id", _no_owner)
    with pytest.raises(HTTPException) as ownerless:
        await panel_convo._owner(_request(), panel)
    assert ownerless.value.status_code == 503


def _chat(title: str, origin: str | None = "panel", agent: str = "jerv") -> Any:
    return SimpleNamespace(title=title, origin=origin, agent=agent)


def test_the_panel_s_placeholder_is_not_a_name_for_an_agent_that_can_name_its_chat() -> None:
    assert is_unnamed(_chat(""))
    assert is_unnamed(_chat(PANEL_PLACEHOLDER_TITLE)), "Jerv titles a panel chat in its turn"
    assert not is_unnamed(_chat(PANEL_PLACEHOLDER_TITLE, agent="minecraft_dave")), (
        "Dave has no name_session: his panel chats stay Desk panel"
    )
    assert not is_unnamed(_chat(PANEL_PLACEHOLDER_TITLE, origin="pwa")), (
        "a PWA chat the owner called Desk panel is named"
    )
    assert not is_unnamed(_chat(PANEL_PLACEHOLDER_TITLE, origin=None))
    assert not is_unnamed(_chat("Shopping list"))
    assert is_unnamed(SimpleNamespace(title=""))
