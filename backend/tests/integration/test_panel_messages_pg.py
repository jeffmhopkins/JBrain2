"""Voice post on the owner's desk panel (firmware-c6 0.0.16), against real Postgres and the app.

What must hold: only the panel the talk lets through (a `display` the owner switched on) reaches
these routes — the twins' `jpet` panels never, whatever their row says; what it sees is the
owner's conversation with ONE twin, never twin-to-twin post or the other twin's; a play and a
"played" are the PWA's own, so the PWA agrees; and a voice message sent from the desk lands on
exactly the twin it was addressed to, as if Dad had sent it from the PWA.
"""

from __future__ import annotations

import hashlib
import math
import struct
import uuid
from typing import Any

import pytest

from tests.conftest import docker_available
from tests.integration import test_panel_agent_pg as agent_pg
from tests.integration.test_panel_agent_pg import Box, _owner_put
from tests.integration.test_rls import database_url  # noqa: F401

maker = agent_pg.maker
box = agent_pg.box

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


def _voice(seconds: float = 1.0, hz: int = 440) -> bytes:
    """A tone loud enough that the speech trim keeps it: 16 kHz mono s16."""
    n = int(16000 * seconds)
    return b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * hz * i / 16000))) for i in range(n)
    )


@pytest.fixture(autouse=True)
async def _no_post(maker: Any) -> None:
    """Every test starts with no voice post on the box."""
    from sqlalchemy import text

    from jbrain.db.session import scoped_session
    from tests.integration.test_rls import OWNER

    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("DELETE FROM app.jpanel_message"))
        await s.commit()


def _thread(box: Box, who: str, twin: str, **kw: Any) -> Any:
    return box.panel(who, "GET", f"/api/endpoint/messages/{box.devices[twin]}", **kw)


def _to_dad(box: Box, twin: str, audio: bytes | None = None) -> str:
    r = box.panel(twin, "POST", "/api/jpanel/send?to=dad", content=audio or _voice())
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


def _pickup_tag(box: Box, who: str) -> str | None:
    r = box.panel(who, "GET", "/api/endpoint/debug/next?board=c6-amoled-216")
    assert r.status_code in (200, 204), r.text
    return r.headers.get("X-Jpanel-Post")


def _send(box: Box, twin: str, body: bytes, sha: str | None = "trailer") -> Any:
    headers = {"Authorization": f"Bearer {box.keys['Jeff']}"}
    if sha:
        headers["X-Jpanel-Sha256"] = sha
    box.client.cookies.clear()
    return box.client.post(
        f"/api/endpoint/messages/{box.devices[twin]}/send", content=body, headers=headers
    )


def _framed(audio: bytes) -> bytes:
    return audio + hashlib.sha256(audio).digest()


# ---- the gate ---------------------------------------------------------------------------------


def test_only_the_switched_on_desk_panel_reaches_the_post(box: Box) -> None:
    mid = _to_dad(box, "Quorra")
    q = box.devices["Quorra"]
    calls = (
        ("GET", f"/api/endpoint/messages/{q}"),
        ("GET", f"/api/endpoint/messages/{q}/{mid}/pcm"),
        ("POST", f"/api/endpoint/messages/{q}/{mid}/played"),
        ("POST", f"/api/endpoint/messages/{q}/send"),
    )
    for method, path in calls:
        assert box.panel("Jeff", method, path, content=b"").status_code == 403, path
    # The twins' panels, even with the owner's flag forced onto their rows behind the API.
    box.sql(
        "INSERT INTO app.endpoint_panel (subject_id, agent_access)"
        " SELECT subject_id, true FROM app.principals WHERE id = CAST(:d AS uuid)",
        d=box.devices["Quorra"],
    )
    _owner_put(box, "Jeff", True)
    for method, path in calls:
        for twin in ("Quorra", "Tamsin"):
            r = box.panel(twin, method, path, content=_framed(_voice()))
            assert r.status_code == 403, (twin, path)
    assert _pickup_tag(box, "Quorra") is None
    assert _pickup_tag(box, "Tamsin") is None
    box.client.cookies.clear()
    assert box.client.get(f"/api/endpoint/messages/{q}").status_code == 401
    # Nothing a twin tried was filed or stamped.
    owner_rows = box.panel("Jeff", "GET", f"/api/endpoint/messages/{q}").json()["messages"]
    assert [(m["id"], m["played"]) for m in owner_rows] == [(mid, False)]


def test_the_pickup_carries_a_tag_only_for_the_switched_on_panel(box: Box) -> None:
    assert _pickup_tag(box, "Jeff") is None
    _owner_put(box, "Jeff", True)
    first = _pickup_tag(box, "Jeff")
    assert first and len(first) == 8
    assert _pickup_tag(box, "Jeff") == first, "nothing moved, so neither does the tag"
    mid = _to_dad(box, "Tamsin")
    moved = _pickup_tag(box, "Jeff")
    assert moved and moved != first, "a girl's message moves it"
    assert _thread(box, "Jeff", "Tamsin").json()["tag"] == moved
    box.panel("Jeff", "POST", f"/api/endpoint/messages/{box.devices['Tamsin']}/{mid}/played")
    assert _pickup_tag(box, "Jeff") not in (None, moved), "so does playing it"


# ---- a thread ---------------------------------------------------------------------------------


def test_a_thread_is_the_owner_s_conversation_with_that_twin_only(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    to_dad = _to_dad(box, "Quorra")
    _to_dad(box, "Tamsin")
    # Twin-to-twin post: neither of his threads.
    assert (
        box.panel("Quorra", "POST", "/api/jpanel/send?to=panel", content=_voice()).status_code
        == 200
    )
    sent = _send(box, "Quorra", _framed(_voice(1.5)))
    assert sent.status_code == 201, sent.text

    r = _thread(box, "Jeff", "Quorra")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["device_id"] == box.devices["Quorra"]
    assert body["name"] == "QUORRA" or body["name"].casefold() == "quorra"
    got = [(m["id"], m["mine"], m["played"], m["voice"]) for m in body["messages"]]
    assert got == [(to_dad, False, False, True), (sent.json()["id"], True, False, True)]
    first = body["messages"][0]
    assert first["text"] == "what is on my list"
    assert 900 <= first["ms"] <= 1100
    assert len(_thread(box, "Jeff", "Tamsin").json()["messages"]) == 1


def test_the_thread_is_bounded_and_its_text_fits_a_bubble(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    for _ in range(6):
        _to_dad(box, "Quorra")
    box.sql("UPDATE app.jpanel_message SET transcript = :t", t="word " * 200)
    msgs = _thread(box, "Jeff", "Quorra").json()["messages"]
    assert len(msgs) == 4, "the panel keeps four a person"
    assert all(len(m["text"].encode()) <= 255 and m["text"].endswith("…") for m in msgs)
    assert len(_thread(box, "Jeff", "Quorra", params={"limit": 8}).json()["messages"]) == 6
    assert _thread(box, "Jeff", "Quorra", params={"limit": 9}).status_code == 422
    assert box.panel("Jeff", "GET", f"/api/endpoint/messages/{uuid.uuid4()}").status_code == 404
    # A display is not somebody to talk to either.
    assert _thread(box, "Jeff", "Jeff").status_code == 404


# ---- play and played --------------------------------------------------------------------------


def test_play_streams_the_twins_own_pcm_body(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    mid = _to_dad(box, "Quorra")
    q = box.devices["Quorra"]
    r = box.panel("Jeff", "GET", f"/api/endpoint/messages/{q}/{mid}/pcm")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["X-Jpanel-Sha256"] == hashlib.sha256(r.content).hexdigest()
    assert 28000 <= len(r.content) <= 36000, "about a second of 16 kHz s16"
    # Not in this conversation, or not an id at all.
    t = box.devices["Tamsin"]
    assert box.panel("Jeff", "GET", f"/api/endpoint/messages/{t}/{mid}/pcm").status_code == 404
    assert box.panel("Jeff", "GET", f"/api/endpoint/messages/{q}/nope/pcm").status_code == 404
    between = box.panel("Tamsin", "POST", "/api/jpanel/send?to=panel", content=_voice()).json()
    for dev in (q, t):
        path = f"/api/endpoint/messages/{dev}/{between['id']}/pcm"
        assert box.panel("Jeff", "GET", path).status_code == 404


def test_played_on_the_desk_is_played_in_the_pwa(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    mid = _to_dad(box, "Quorra")
    q = box.devices["Quorra"]
    assert box.panel("Jeff", "POST", f"/api/endpoint/messages/{q}/{mid}/played").status_code == 204
    assert _thread(box, "Jeff", "Quorra").json()["messages"][0]["played"] is True
    rows = _rows(box)
    assert rows[mid] is not None, "the PWA's own column: its thread shows it played"
    # Twice is fine and keeps the first time.
    assert box.panel("Jeff", "POST", f"/api/endpoint/messages/{q}/{mid}/played").status_code == 204
    assert _rows(box)[mid] == rows[mid]
    # His own message is hers to play, not his.
    own = _send(box, "Quorra", _framed(_voice())).json()["id"]
    assert box.panel("Jeff", "POST", f"/api/endpoint/messages/{q}/{own}/played").status_code == 404
    assert _rows(box)[own] is None
    t = box.devices["Tamsin"]
    assert box.panel("Jeff", "POST", f"/api/endpoint/messages/{t}/{mid}/played").status_code == 404


def _rows(box: Box) -> dict[str, Any]:
    import asyncio

    from sqlalchemy import text

    from jbrain.db.session import scoped_session
    from tests.integration.test_rls import OWNER

    async def run() -> dict[str, Any]:
        async with scoped_session(box.maker, OWNER) as s:
            got = (
                await s.execute(text("SELECT id::text, played_at FROM app.jpanel_message"))
            ).all()
        return {str(i): p for i, p in got}

    return asyncio.run(run())


# ---- send -------------------------------------------------------------------------------------


def test_a_voice_message_from_the_desk_lands_on_the_right_twin(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    audio = _voice(1.2)
    r = _send(box, "Tamsin", _framed(audio))
    assert r.status_code == 201, r.text
    sent = r.json()
    assert sent["text"] == "what is on my list" and 1100 <= sent["ms"] <= 1300
    waiting = box.panel("Tamsin", "GET", "/api/jpanel/waiting").json()
    assert waiting["count"] == 1 and waiting["from_owner"] is True
    assert box.panel("Quorra", "GET", "/api/jpanel/waiting").json()["count"] == 0
    got = box.panel("Tamsin", "GET", "/api/jpanel/next")
    assert got.status_code == 200
    assert got.headers["X-Jpanel-From-Kind"] == "owner"
    assert got.headers["X-Jpanel-Id"] == sent["id"]
    # In the PWA it is Dad's voice message to her, exactly as one sent from the phone.
    msgs = _thread(box, "Jeff", "Tamsin").json()["messages"]
    assert [(m["id"], m["mine"], m["voice"]) for m in msgs] == [(sent["id"], True, True)]


def test_a_damaged_upload_is_refused_and_nothing_is_filed(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    audio = _voice()
    bad = audio[:-2000] + hashlib.sha256(audio).digest()
    assert _send(box, "Quorra", bad).status_code == 422
    assert _send(box, "Quorra", b"short").status_code == 422
    assert _send(box, "Quorra", audio, sha="0" * 64).status_code == 422
    assert _rows(box) == {}
    # The hex header the twins' panels use works too, and so does none at all.
    assert _send(box, "Quorra", audio, sha=hashlib.sha256(audio).hexdigest()).status_code == 201
    assert _send(box, "Quorra", audio, sha=None).status_code == 201


def test_send_refuses_what_is_not_a_message(box: Box) -> None:
    _owner_put(box, "Jeff", True)
    assert _send(box, "Quorra", _framed(b"")).status_code == 400
    assert _send(box, "Quorra", _framed(_voice(31.0))).status_code == 413
    nowhere = box.panel(
        "Jeff", "POST", f"/api/endpoint/messages/{uuid.uuid4()}/send", content=_framed(_voice())
    )
    assert nowhere.status_code == 404
    assert _rows(box) == {}
