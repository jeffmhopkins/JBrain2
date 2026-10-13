"""Load earlier and Listen on the desk panel (firmware-c6 0.0.21), against Postgres and the app.

What must hold (owner, approved 2026-10-13): the panel pages back through its own conversations
ten at a time with the count left and the true start; Listen speaks a stored answer WHOLE, not
the bubble's 255 bytes; and both are behind the talk's gate and its scoping — never a twin's
panel, never another agent's chat, never a chat opened with domain scopes (health, finance,
location), whatever id the panel sends.
"""

from __future__ import annotations

import struct
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text

from jbrain.api import panel_convo
from jbrain.api.panel_agent import LEGACY_VOICE_PREFIX
from jbrain.db.session import scoped_session
from tests.conftest import docker_available
from tests.integration import test_panel_agent_pg as agent_pg
from tests.integration import test_panel_convo_pg as convo_pg
from tests.integration.test_panel_agent_pg import Box, _owner_put
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

box = agent_pg.box
maker = agent_pg.maker
_run = convo_pg._run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def parse(body: bytes) -> list[tuple[int, bytes]]:
    assert body[:3] == b"JT2"
    out, i = [], 3
    while i < len(body):
        kind, n = struct.unpack_from("<BH", body, i)
        out.append((kind, body[i + 3 : i + 3 + n]))
        i += 3 + n
    assert i == len(body)
    return out


def _turns(n: int, long: str = "") -> tuple[tuple[str, str], ...]:
    out: list[tuple[str, str]] = []
    for i in range(n // 2):
        out.append(("user", long or f"{LEGACY_VOICE_PREFIX}Question {i}?"))
        out.append(("assistant", long or f"Answer {i}."))
    return tuple(out)


def _pointed(box: Box, turns: tuple[tuple[str, str], ...], **kw: Any) -> str:
    """A chat of the owner's with Jerv, pointed at as the desk panel's current one."""
    m = box.maker
    owner = _run(convo_pg._owner_ctx(m))
    sid = _run(convo_pg._chat(m, owner, title="T-Rex fossils", turns=turns, **kw))
    subject = _run(convo_pg._subject(m, box.devices["Jeff"]))
    _run(convo_pg._point(m, subject, kw.get("agent", "jerv"), sid))
    return sid


def _seq_of(box: Box, sid: str) -> list[int]:
    async def go() -> list[int]:
        async with scoped_session(box.maker, OWNER) as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT seq FROM app.agent_turns WHERE session_id = CAST(:s AS uuid)"
                        " ORDER BY seq"
                    ),
                    {"s": sid},
                )
            ).all()
        return [int(r[0]) for r in rows]

    return _run(go())


def _hist(box: Box, query: str = "", agent: str = "jerv", who: str = "Jeff") -> Any:
    return box.panel(who, "GET", f"/api/endpoint/convo/{agent}/history?{query}")


# ---- the gate -----------------------------------------------------------------------------------


def test_only_the_switched_on_desk_panel_pages_or_listens(box: Box) -> None:
    calls = (
        "/api/endpoint/convo/jerv/history",
        "/api/endpoint/convo/jerv/speech/1",
        f"/api/endpoint/messages/{box.devices['Quorra']}/history",
    )
    for path in calls:
        assert box.panel("Jeff", "GET", path).status_code == 403, path
    _owner_put(box, "Jeff", True)
    for path in calls:
        for twin in ("Quorra", "Tamsin"):
            assert box.panel(twin, "GET", path).status_code == 403, (twin, path)
    box.client.cookies.clear()
    assert box.client.get(calls[0]).status_code == 401


# ---- Load earlier, Jerv and Dave ----------------------------------------------------------------


def test_an_agent_thread_pages_back_ten_at_a_time_to_its_start(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    sid = _pointed(box, _turns(44))
    seqs = _seq_of(box, sid)
    # A thread that opens: the counts alone.
    counts = _hist(box, "limit=0").json()
    assert counts == {"messages": [], "older": 44, "newer": 0, "start_at": None}
    # Before the four the panel shows: ten older, oldest first, and how many are left.
    page = _hist(box, f"before={seqs[-4]}").json()
    assert [m["id"] for m in page["messages"]] == seqs[30:40]
    assert page["older"] == 30 and page["newer"] == 4 and page["start_at"] is None
    assert page["messages"][0] == {"id": seqs[30], "mine": True, "text": "Question 15?"}
    assert page["messages"][1]["mine"] is False
    # Paged back to the true start: "Start of conversation · <when>".
    first = _hist(box, f"before={seqs[10]}").json()
    assert [m["id"] for m in first["messages"]] == seqs[:10]
    assert first["older"] == 0 and isinstance(first["start_at"], int)
    # And forward again, for "Newer messages".
    newer = _hist(box, f"after={seqs[9]}").json()
    assert [m["id"] for m in newer["messages"]] == seqs[10:20] and newer["newer"] == 24
    # Before the oldest: nothing, and the start.
    none = _hist(box, f"before={seqs[0]}").json()
    assert none["messages"] == [] and none["older"] == 0 and none["start_at"] is not None
    assert _hist(box, f"before={seqs[5]}&after={seqs[1]}").status_code == 422


def test_a_page_of_long_answers_is_cut_to_the_panel_s_buffer_and_counted(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    # Every byte a quote: JSON doubles each one, so ten full bubbles overrun the panel's buffer.
    sid = _pointed(box, _turns(20, '"' * 300))
    seqs = _seq_of(box, sid)
    page = _hist(box).json()
    raw = _hist(box).content
    assert len(raw) <= panel_convo.PAGE_BYTES + 200
    shown = [m["id"] for m in page["messages"]]
    assert shown == seqs[len(seqs) - len(shown) :] and len(shown) < 10
    assert page["older"] == len(seqs) - len(shown)
    assert all(len(m["text"].encode()) <= panel_convo.BUBBLE_BYTES for m in page["messages"])


def test_a_scoped_or_foreign_chat_is_never_paged(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    _pointed(box, _turns(6), scopes=("health",))
    assert _hist(box).json() == {"messages": [], "older": 0, "newer": 0, "start_at": None}
    # Dave's history is his own current chat, not Jerv's.
    _pointed(box, _turns(6))
    assert _hist(box, agent="dave").json()["messages"] == []


def test_the_switched_thread_names_its_bubbles(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    sid = _pointed(box, _turns(6))
    seqs = _seq_of(box, sid)
    got = box.panel("Jeff", "GET", "/api/endpoint/convo/jerv/messages?limit=2").json()
    assert [m["id"] for m in got["messages"]] == seqs[2:]


# ---- Listen ----------------------------------------------------------------------------------


def _speaking(box: Box, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    said: list[str] = []

    async def fake(settings: Any, words: str, budget: int, voice: Any = None) -> bytes:
        said.append(words)
        return b"\x01\x00" * 800

    monkeypatch.setattr(panel_convo, "panel_speech", fake)
    app: FastAPI = box.client.app  # type: ignore[assignment]
    app.state.settings.brain_tts_url = "http://tts-stt:8801"
    return said


def test_listen_speaks_the_whole_stored_answer(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    _owner_put(box, "Jeff", True)
    said = _speaking(box, monkeypatch)
    long = " ".join(f"Sentence number {i} says a little more about the tides." for i in range(30))
    sid = _pointed(box, (("user", "Tides?"), ("assistant", f"**{long}**")))
    seqs = _seq_of(box, sid)
    r = box.panel("Jeff", "GET", f"/api/endpoint/convo/jerv/speech/{seqs[1]}")
    assert r.status_code == 200 and r.headers["content-type"] == "application/x-jpanel-turn"
    recs = parse(r.content)
    kinds = [k for k, _ in recs]
    assert kinds[0] == 0x09 and kinds[-1] == 0x06 and recs[-1][1][0] == 0
    # The whole answer, far past the 255 bytes a bubble keeps, without its Markdown.
    assert " ".join(said) == long and len(long.encode()) > 1000


def test_listen_is_only_an_answer_in_a_chat_the_panel_may_show(
    box: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    _owner_put(box, "Jeff", True)
    _speaking(box, monkeypatch)
    mine = _pointed(box, _turns(2))
    scoped = _pointed(box, _turns(2), scopes=("finance",))
    dave = _pointed(box, _turns(2), agent="minecraft_dave")
    q, a = _seq_of(box, mine)
    assert box.panel("Jeff", "GET", f"/api/endpoint/convo/jerv/speech/{a}").status_code == 200
    for agent, seq in (
        ("jerv", q),  # a question, not an answer
        ("jerv", _seq_of(box, scoped)[1]),  # behind the firewall
        ("jerv", _seq_of(box, dave)[1]),  # Dave's, asked as Jerv's
        ("dave", a),  # Jerv's, asked as Dave's
        ("jerv", 999_999_999),
    ):
        r = box.panel("Jeff", "GET", f"/api/endpoint/convo/{agent}/speech/{seq}")
        assert r.status_code == 404, (agent, seq)


def test_listen_says_when_the_box_cannot_speak(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    sid = _pointed(box, _turns(2))
    app: FastAPI = box.client.app  # type: ignore[assignment]
    app.state.settings.brain_tts_url = ""
    r = box.panel("Jeff", "GET", f"/api/endpoint/convo/jerv/speech/{_seq_of(box, sid)[1]}")
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "tts_unavailable"


# ---- Load earlier, the twins' threads --------------------------------------------------------


@pytest.fixture
async def _no_post(maker: Any) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.jpanel_message"))
        await s.commit()


def _post_history(box: Box, query: str = "", twin: str = "Quorra") -> Any:
    return box.panel("Jeff", "GET", f"/api/endpoint/messages/{box.devices[twin]}/history?{query}")


@pytest.mark.usefixtures("_no_post")
def test_her_thread_pages_back_and_never_shows_the_other_twin(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    from tests.integration.test_panel_messages_pg import _to_dad

    ids = [_to_dad(box, "Quorra") for _ in range(14)]
    _to_dad(box, "Tamsin")
    counts = _post_history(box, "limit=0").json()
    assert counts["older"] == 14 and counts["messages"] == []
    page = _post_history(box, f"before={ids[-4]}").json()
    assert [m["id"] for m in page["messages"]] == ids[:10]
    assert page["older"] == 0 and page["newer"] == 4 and isinstance(page["start_at"], int)
    assert page["messages"][0]["mine"] is False and page["messages"][0]["voice"] is True
    newer = _post_history(box, f"after={ids[9]}").json()
    assert [m["id"] for m in newer["messages"]] == ids[10:] and newer["newer"] == 0
    # An anchor from the other twin's thread is not in this one.
    other = _post_history(box, twin="Tamsin").json()["messages"][0]["id"]
    assert _post_history(box, f"before={other}").status_code == 404
    assert _post_history(box, "before=not-a-uuid").status_code == 404
