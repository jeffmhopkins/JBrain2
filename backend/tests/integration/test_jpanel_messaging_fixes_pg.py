"""Wave W1 of the messaging fixes plan, against real Postgres: the box and the owner's routes.

Each case is named in `docs/plans/JPANEL_MESSAGING_FIXES_PLAN.md` §3 (Traceability) by the
finding it proves — the panel's cap (A3), the addressee checked before the synthesis (C2),
twin-to-twin post in both threads (C5), the per-thread page and its badge (C6, E3.9), the
audio route a phone can play (C7), the kept count that names the right child (C9), and the
owner's recording trimmed rather than refused (C10). The names the panel is sent are cut to
fit its poll buffer (§6).
"""

import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api import jpanel
from jbrain.api.jpanel import _wav
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.main import create_app
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_jpanel_rename_pg import _names, _plant, maker  # noqa: F401
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_ROOT = Path(__file__).resolve().parents[3]


def _tone(ms: int) -> bytes:
    """`ms` of a loud square wave at the panel rate: speech as far as `_trim_to_speech` and the
    leveller are concerned, so nothing but the cap decides how much of it is kept."""
    period = 40
    one = (b"\x00\x40" * (period // 2)) + (b"\x00\xc0" * (period // 2))
    n = jpanel.PANEL_RATE * ms // 1000
    return (one * (n // period + 1))[: n * 2]


def _login(client: TestClient, key: str) -> None:
    client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})


def _enrol(client: TestClient, name: str) -> tuple[str, str]:
    """A panel flashed as `name`: its principal id and its key."""
    made = client.post("/api/devices", json={"label": f"panel {name}", "device_role": "jpet"})
    assert made.status_code in (200, 201), made.text
    pid = next(p for p, n in _names(client).items() if n == name)
    return pid, str(made.json()["key"])


def _as_panel(client: TestClient, panel_key: str) -> dict[str, str]:
    """A bearer header, with the owner's cookie gone: it authenticates first otherwise."""
    client.cookies.clear()
    return {"Authorization": f"Bearer {panel_key}"}


async def _row(
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    *,
    sender: tuple[str, str | None],
    recipient: tuple[str, str | None],
    sha: str = "w1-sha",
    played: bool = False,
    age_s: int = 0,
) -> str:
    mid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.jpanel_message
                    (id, sender_kind, sender_device, recipient_kind, recipient_device,
                     blob_sha256, transcript, composed, duration_ms, created_at, played_at)
                VALUES (CAST(:id AS uuid), :sk, :sd, :rk, :rd, :sha, 'hi', 'voice', 1000,
                        now() - make_interval(secs => :age),
                        CASE WHEN :played THEN now() END)
                """
            ),
            {
                "id": mid,
                "sk": sender[0],
                "sd": sender[1],
                "rk": recipient[0],
                "rd": recipient[1],
                "sha": sha,
                "age": age_s,
                "played": played,
            },
        )
        await s.commit()
    return mid


def _app(database_url: str, tmp_path, **extra) -> TestClient:  # noqa: F811
    return TestClient(
        create_app(
            Settings(
                secure_cookies=False, database_url=database_url, blob_dir=str(tmp_path), **extra
            )
        )
    )


# --- A3: the panel's longest recording is one the box accepts --------------------------------


async def test_send_accepts_exactly_the_capture_cap(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    """The firmware's own cap, read out of `stream.h`, then the box's ceiling: half a second
    over `MAX_MESSAGE_MS` is a message, and one sample more is a 413 (E1.1)."""
    m = re.search(
        r"^#define CAPTURE_MAX_MS (\d+)$",
        (_ROOT / "firmware" / "main" / "stream.h").read_text(encoding="utf-8"),
        re.M,
    )
    assert m
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        _, panel_key = _enrol(client, "Quorra")
        head = _as_panel(client, panel_key)
        for size in (int(m.group(1)) * 32, jpanel.MAX_SEND_BYTES):
            body = _tone(31_000)[:size]
            assert len(body) == size
            sent = client.post("/api/jpanel/send?to=dad", content=body, headers=head)
            assert sent.status_code == 200, (size, sent.text)
        over = client.post(
            "/api/jpanel/send?to=dad",
            content=_tone(31_000)[: jpanel.MAX_SEND_BYTES + 2],
            headers=head,
        )
        assert over.status_code == 413, over.text


async def test_transcribe_timeout_returns_empty_and_sends(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A whisper that never answers costs the transcript, never the message: the timeout is
    inside `_transcribe`'s `try`, so it is one more STT failure."""

    class Stalls:
        def __init__(self, **_: object) -> None: ...

        async def transcribe(self, *_: object, **__: object) -> None:
            import asyncio

            await asyncio.sleep(30)

    monkeypatch.setattr(jpanel, "WhisperCppClient", Stalls)
    monkeypatch.setattr(jpanel, "STT_TIMEOUT_S", 0.05)
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path, whisper_url="http://whisper.invalid") as client:
        _login(client, key)
        _, panel_key = _enrol(client, "Quorra")
        sent = client.post(
            "/api/jpanel/send?to=dad", content=_tone(1000), headers=_as_panel(client, panel_key)
        )
        assert sent.status_code == 200, sent.text
        _login(client, key)
        thread = client.get("/api/jpanel/messages").json()["panels"][0]
        assert [m["transcript"] for m in thread["messages"]] == [""]


# --- C2: an unknown panel is a 404 before anything is synthesised ----------------------------


async def test_send_text_unknown_panel_404_before_tts(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    """The synthesiser here is a closed port: reaching it would be a 503, so a 404 proves the
    addressee was checked first, and nothing is filed."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path, brain_tts_url="http://127.0.0.1:9") as client:
        _login(client, key)
        _enrol(client, "Quorra")
        for ghost in (str(uuid.uuid4()), "not-a-panel"):
            sent = client.post("/api/jpanel/messages", json={"to_device": ghost, "text": "hi"})
            assert sent.status_code == 404, sent.text
    async with scoped_session(maker, OWNER) as s:
        assert (await s.execute(text("SELECT count(*) FROM app.jpanel_message"))).scalar_one() == 0


# --- C5, C6, E3.9: what the owner's list and badge show --------------------------------------


async def test_between_message_in_both_threads(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        quorra, _ = _enrol(client, "Quorra")
        tamsin, _ = _enrol(client, "Tamsin")
        mid = await _row(maker, sender=("panel", quorra), recipient=("panel", tamsin))
        threads = {t["device_id"]: t for t in client.get("/api/jpanel/messages").json()["panels"]}
    for pid in (quorra, tamsin):
        got = [(m["id"], m["direction"]) for m in threads[pid]["messages"]]
        assert got == [(mid, "between")], (pid, got)
        # Not the owner's to read, so neither chip counts it.
        assert threads[pid]["unplayed"] == 0


async def test_every_unplayed_owner_row_is_returned(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    """The limit is per thread, and an unread message to the owner is returned however far
    past it — so the badge never counts a message the screen cannot draw. A chatty sister no
    longer pushes the quiet twin's thread off the page either."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        quorra, _ = _enrol(client, "Quorra")
        tamsin, _ = _enrol(client, "Tamsin")
        old_unread = await _row(
            maker, sender=("panel", quorra), recipient=("owner", None), age_s=900
        )
        played = await _row(
            maker, sender=("panel", quorra), recipient=("owner", None), played=True, age_s=800
        )
        recent = [
            await _row(maker, sender=("owner", None), recipient=("panel", quorra), age_s=n)
            for n in (30, 20, 10)
        ]
        quiet = await _row(maker, sender=("panel", tamsin), recipient=("owner", None), age_s=5000)

        page = client.get("/api/jpanel/messages?limit=2").json()["panels"]
        threads = {t["device_id"]: t for t in page}
        assert [m["id"] for m in threads[quorra]["messages"]] == [
            recent[2],
            recent[1],
            old_unread,
        ]
        assert [m["id"] for m in threads[tamsin]["messages"]] == [quiet]

        # The cursor is the limit-th row, not the old unread one drawn below it: paging
        # from that would skip what lies between.
        assert threads[quorra]["earlier"] == recent[1]
        assert threads[tamsin]["earlier"] is None
        cursor = threads[quorra]["earlier"]
        earlier = client.get(f"/api/jpanel/messages?limit=2&before={cursor}").json()["panels"]
        back = {t["device_id"]: t for t in earlier}
        assert [m["id"] for m in back[quorra]["messages"]] == [recent[0], played]
        assert back[quorra]["earlier"] == played
        last = client.get(f"/api/jpanel/messages?limit=2&before={played}").json()["panels"]
        tail = {t["device_id"]: t for t in last}
        assert [m["id"] for m in tail[quorra]["messages"]] == [old_unread]
        assert tail[quorra]["earlier"] is None
        assert client.get("/api/jpanel/messages?before=nope").status_code == 404
        # A cursor whose message is gone is a 404, not an empty page.
        missing = client.get(f"/api/jpanel/messages?before={uuid.uuid4()}")
        assert missing.status_code == 404

        summary = client.get("/api/jpanel/summary").json()
        assert summary == {
            "unread": 2,
            "threads": [{"device_id": t["device_id"], "unread": t["unplayed"]} for t in page],
        }
        assert {t["device_id"]: t["unread"] for t in summary["threads"]} == {
            quorra: 1,
            tamsin: 1,
        }


# --- C5, C9: what a clear deletes, keeps, and frees ------------------------------------------


async def test_clear_kept_names_recipient(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    """Clearing Quorra's thread keeps what Tamsin has not heard yet from her — and says it is
    Tamsin's, where the old copy would have told the owner Quorra had not heard it."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        quorra, _ = _enrol(client, "Quorra")
        tamsin, _ = _enrol(client, "Tamsin")
        await _row(maker, sender=("panel", quorra), recipient=("panel", tamsin))
        await _row(maker, sender=("panel", tamsin), recipient=("panel", quorra), played=True)
        await _row(maker, sender=("owner", None), recipient=("panel", quorra))
        await _row(maker, sender=("panel", quorra), recipient=("owner", None))
        done = client.delete(f"/api/jpanel/messages?device={quorra}").json()
    assert done["deleted"] == 2
    assert done["kept"] == 2
    assert sorted((k["name"], k["kept"]) for k in done["kept_for"]) == [
        ("Quorra", 1),
        ("Tamsin", 1),
    ]


async def test_clear_releases_blobs(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    """A cleared message's recording leaves the store — unless a kept row still plays it."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    store = FsBlobStore(str(tmp_path))
    gone = await store.put(_wav(b"\x01\x02" * 800))
    shared = await store.put(_wav(b"\x03\x04" * 800))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        quorra, _ = _enrol(client, "Quorra")
        await _row(maker, sender=("panel", quorra), recipient=("owner", None), sha=gone)
        await _row(maker, sender=("panel", quorra), recipient=("owner", None), sha=shared)
        await _row(maker, sender=("owner", None), recipient=("panel", quorra), sha=shared)
        done = client.delete(f"/api/jpanel/messages?device={quorra}").json()
    assert (done["deleted"], done["kept"]) == (2, 1)
    assert not await store.exists(gone)
    assert await store.exists(shared), "still the audio of a message she has not heard"


# --- C10: the owner's recording at and past the cap ------------------------------------------


async def test_owner_audio_at_30_02s_is_accepted(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        quorra, _ = _enrol(client, "Quorra")
        sent = client.post(f"/api/jpanel/messages/audio?to_device={quorra}", content=_tone(30_020))
    assert sent.status_code == 201, sent.text
    assert sent.json()["duration_ms"] >= 30_000


async def test_owner_audio_well_over_cap_is_trimmed(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        quorra, _ = _enrol(client, "Quorra")
        sent = client.post(f"/api/jpanel/messages/audio?to_device={quorra}", content=_tone(34_000))
    assert sent.status_code == 201, sent.text
    assert sent.json()["duration_ms"] <= jpanel.MAX_MESSAGE_MS + jpanel.SEND_SLACK_MS


# --- C7: the owner's audio route -------------------------------------------------------------


async def test_audio_range_206(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    """What iOS Safari asks first, then the rest, then a range past the end."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        quorra, _ = _enrol(client, "Quorra")
        mid, _ = await _plant(maker, tmp_path, quorra, b"\x10\x20")
        url = f"/api/jpanel/messages/{mid}/audio"
        whole = client.get(url)
        assert whole.status_code == 200
        assert whole.headers["accept-ranges"] == "bytes"
        size = len(whole.content)

        probe = client.get(url, headers={"Range": "bytes=0-1"})
        assert probe.status_code == 206
        assert probe.content == whole.content[:2]
        assert probe.headers["content-range"] == f"bytes 0-1/{size}"

        rest = client.get(url, headers={"Range": "bytes=2-"})
        assert rest.status_code == 206 and rest.content == whole.content[2:]
        tail = client.get(url, headers={"Range": "bytes=-4"})
        assert tail.status_code == 206 and tail.content == whole.content[-4:]

        past = client.get(url, headers={"Range": f"bytes={size}-"})
        assert past.status_code == 416
        assert past.headers["content-range"] == f"bytes */{size}"


async def test_non_uuid_is_404(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        assert client.get("/api/jpanel/messages/not-a-uuid/audio").status_code == 404
        assert client.post("/api/jpanel/messages/not-a-uuid/played").status_code == 404
        assert client.get(f"/api/jpanel/messages/{uuid.uuid4()}/audio").status_code == 404


# --- §6: names the panel is sent fit its poll buffer -----------------------------------------


async def test_names_on_the_wire_are_cut_to_fit_the_poll(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path,
) -> None:
    """A label stored before the rename rule — long, non-ASCII, with a quote — reaches the
    panel cut to 31 escaped bytes, and as a printable-ASCII header on `/next`. The sibling is
    still found, because the comparison is made on the uncut name."""
    legacy = 'Ünïcødé "twin" ' * 6
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    with _app(database_url, tmp_path) as client:
        _login(client, key)
        quorra, quorra_key = _enrol(client, "Quorra")
        tamsin, _ = _enrol(client, "Tamsin")
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text("UPDATE app.principals SET label = :l WHERE id = CAST(:id AS uuid)"),
                {"l": f"panel {legacy}", "id": tamsin},
            )
            await s.commit()
        sha = await FsBlobStore(str(tmp_path)).put(_wav(b"\x10\x20" * 400))
        await _row(maker, sender=("panel", tamsin), recipient=("panel", quorra), sha=sha)

        head = _as_panel(client, quorra_key)
        polled = client.get("/api/jpanel/waiting", headers=head).json()
        names = [polled["sibling"]] + [q["from_name"] for q in polled["queue"]]
        assert polled["sibling"] and legacy.startswith(polled["sibling"])
        for name in names:
            assert len(json.dumps(name)) - 2 <= 31, name
        assert polled["queue"][0]["from_name"] == polled["sibling"]

        served = client.get("/api/jpanel/next", headers=head)
        assert served.status_code == 200
        said = served.headers["X-Jpanel-From"]
        assert all(" " <= c <= "~" for c in said) and len(json.dumps(said)) - 2 <= 31
        assert said.startswith("?n?c?d? ")
