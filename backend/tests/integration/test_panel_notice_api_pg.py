"""Notices end to end: a sender asks, the panel collects, shows and answers, the sender reads.

Driven through the real app against real Postgres, in the order the box does it: the panel
authenticates by its own device key and reports its switches on the debug pickup, the owner by
his cookie, a debug session by its capability token. The RLS half has its own file
(`test_panel_notice_rls.py`); this one proves the routes run under the contexts those policies
expect, that the refusal table answers at the door, and that what the panel downloads is the
JN1 stream the firmware wave reads.
"""

from __future__ import annotations

import asyncio
import base64
import io
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.jpanel import debug_relay as relay
from jbrain.jpanel import notice as N
from jbrain.jpanel.notice_service import NoticeService
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401
from tests.unit.test_panel_notice import tone, wav

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

C6 = "c6-amoled-216"
Maker = async_sessionmaker[AsyncSession]
#: Accepting, refusing at night, chiming, a night window that never contains now (empty).
DAY = {"na": "1", "nq": "1", "nc": "1", "nf": "0", "nt": "0", "tz": "7"}


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[Maker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.panel_notice"))
        await s.execute(text("DELETE FROM app.panel_debug_request"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        await s.commit()
    relay.forget()
    N.forget_prefs()
    yield sessions
    relay.forget()
    N.forget_prefs()
    await engine.dispose()


@dataclass
class Box:
    client: TestClient
    owner: dict[str, str]
    dbg: dict[str, str]
    panel: dict[str, str]
    sibling: dict[str, str]
    device: str
    sibling_device: str
    maker: Maker
    spoken: list[dict[str, str]]

    def as_panel(self, method: str, path: str, who: dict[str, str] | None = None, **kw: Any):  # noqa: ANN201
        self.client.cookies.clear()
        return self.client.request(method, path, headers=dict(who or self.panel), **kw)

    def pickup(self, *who: dict[str, str], **prefs: str):  # noqa: ANN201
        q = "&".join(f"{k}={v}" for k, v in prefs.items())
        return self.as_panel("GET", f"/api/endpoint/debug/next?board={C6}&{q}", *who)

    def send(self, **body: Any):  # noqa: ANN201
        self.client.cookies.clear()
        self.client.cookies.update(self.owner)
        body.setdefault("title", "Run the cleanup now?")
        return self.client.post("/api/panel/notify", json=body)

    def read(self, nid: str, wait: float = 0):  # noqa: ANN201
        self.client.cookies.clear()
        self.client.cookies.update(self.owner)
        return self.client.get(f"/api/panel/notify/{nid}?wait={wait}")

    def collect(self, who: dict[str, str] | None = None):  # noqa: ANN201
        return self.as_panel("GET", "/api/endpoint/notice/next", who)

    def state(self, nid: str, who: dict[str, str] | None = None, **body: Any):  # noqa: ANN201
        return self.as_panel("POST", f"/api/endpoint/notice/{nid}/state", who, json=body)


@pytest.fixture
def box(database_url: str, maker: Maker, tmp_path: Path) -> Iterator[Box]:  # noqa: F811
    repo = SqlAuthRepo(maker)
    owner_key = asyncio.run(service.rotate_owner_key(repo))
    debug_key, _ = asyncio.run(service.mint_capability(repo, "claude", ttl_hours=1))
    app = create_app(
        Settings(
            secure_cookies=False,
            database_url=database_url,
            debug_access_enabled=True,
            blob_dir=str(tmp_path),
        )
    )
    spoken: list[dict[str, str]] = []

    async def fake_tts(params: dict[str, str]) -> bytes:
        spoken.append(params)
        # Kokoro answers 24 kHz; "long" is the test's way to ask for more than eight seconds.
        ms = 9000 if "long" in params["text"] else 1200
        return wav(tone(ms, rate=24000), rate=24000)

    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        owner = dict(client.cookies)
        panel = client.post(
            "/api/devices", json={"label": "panel Jeff", "device_role": "display"}
        ).json()
        sibling = client.post(
            "/api/devices", json={"label": "panel Quorra", "device_role": "jpet"}
        ).json()
        app.state.panel_notices = NoticeService(maker, app.state.blob_store, tts_fetch=fake_tts)
        client.cookies.clear()
        dbg = {"Authorization": f"Bearer {debug_key}"}
        ids = {
            p["name"]: p["device_id"] for p in client.get("/api/debug/panel", headers=dbg).json()
        }
        yield Box(
            client=client,
            owner=owner,
            dbg=dbg,
            panel={"Authorization": f"Bearer {panel['key']}"},
            sibling={"Authorization": f"Bearer {sibling['key']}"},
            device=ids["Jeff"],
            sibling_device=ids["Quorra"],
            maker=maker,
            spoken=spoken,
        )


def _owner_sql(box: Box, sql: str, **params: object) -> None:
    async def run() -> None:
        async with scoped_session(box.maker, OWNER) as s:
            await s.execute(text(sql), params)
            await s.commit()

    asyncio.run(run())


def _png(w: int, h: int, color: tuple[int, int, int] = (40, 90, 200)) -> str:
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


# ---- the round trip ------------------------------------------------------------------------


def test_a_question_round_trip(box: Box) -> None:
    assert box.pickup(**DAY).status_code == 204

    sent = box.send(body="Frees about 38 GB.", choices=["Yes", "No"], chime=True)
    assert sent.status_code == 202, sent.text
    nid = sent.json()["id"]
    assert sent.json() == {
        "id": nid,
        "device_id": box.device,
        "panel": "Jeff",
        "state": "queued",
        "expires_in_s": 60,
        "image": None,
        "audio_ms": None,
    }

    # The pickup now says one waits — on the 204 and on a 200 alike.
    hinted = box.pickup(**DAY)
    assert hinted.status_code == 204 and hinted.headers["X-Notice"] == "1"

    got = box.collect()
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/x-jpanel-notice"
    d = N.decode(got.content)
    assert d.notice.id == nid and d.notice.sender_label == "You"
    assert (d.notice.title, d.notice.body, d.notice.choices) == (
        "Run the cleanup now?",
        "Frees about 38 GB.",
        ("Yes", "No"),
    )
    assert d.notice.chime and d.notice.hide_after_s is None
    assert 890 <= d.notice.ttl_s <= 900
    assert box.collect().status_code == 204, "a notice is collected once"
    assert "X-Notice" not in box.pickup(**DAY).headers
    assert box.read(nid).json()["state"] == "delivered"

    # A torn download is re-read by id.
    again = box.as_panel("GET", f"/api/endpoint/notice/{nid}/body")
    assert again.status_code == 200 and N.decode(again.content).notice.id == nid

    assert box.state(nid, state="shown").status_code == 204
    assert box.state(nid, state="shown").status_code == 204, "a repeated report is fine"
    assert box.state(nid, state="answered", choice=1).status_code == 204
    out = box.read(nid, wait=5).json()
    assert (out["state"], out["answer"]) == ("answered", "No")
    assert out["delivered_at"] and out["shown_at"] and out["finished_at"]

    # Answered once: the panel can no longer see it.
    assert box.state(nid, state="answered", choice=0).status_code == 404
    assert box.as_panel("GET", f"/api/endpoint/notice/{nid}/body").status_code == 404


def test_a_plain_notice_is_dismissed_or_hides_itself(box: Box) -> None:
    box.pickup(**DAY)
    one = box.send(title="Disk 91% full", hide_after_s=45).json()["id"]
    two = box.send(title="Backup done").json()["id"]
    three = box.send(title="Read me", hide_after_s=0).json()["id"]
    first = N.decode(box.collect().content).notice
    assert first.id == one and first.hide_after_s == 45 and not first.choices
    second = N.decode(box.collect().content).notice
    assert second.id == two, "oldest first"
    assert second.hide_after_s == N.HIDE_DEFAULT_PLAIN_S, (
        "a plain notice given no time hides at 20 s"
    )
    third = N.decode(box.collect().content).notice
    assert third.id == three and third.hide_after_s is None, "0 asks for until tapped"
    assert box.state(one, state="expired").status_code == 204
    assert box.state(two, state="shown").status_code == 204
    assert box.state(two, state="dismissed").status_code == 204
    assert box.read(one).json()["state"] == "expired"
    assert box.read(two).json()["state"] == "dismissed"


def test_the_panel_s_bad_reports_are_refused(box: Box) -> None:
    box.pickup(**DAY)
    nid = box.send(choices=["Yes", "No"]).json()["id"]
    plain = box.send(title="Plain").json()["id"]
    assert box.state(nid, state="shown").status_code == 404, "not collected yet"
    box.collect()
    box.collect()
    assert box.state(nid, state="answered").status_code == 422
    assert box.state(nid, state="answered", choice=3).status_code == 422
    assert box.state(nid, state="dismissed", choice=0).status_code == 422
    assert box.state(nid, state="queued").status_code == 422
    assert box.state(plain, state="answered", choice=0).status_code == 422
    assert box.state("not-a-uuid", state="shown").status_code == 404
    assert box.as_panel("GET", "/api/endpoint/notice/nope/body").status_code == 404
    box.state(nid, state="shown")
    assert box.state(nid, state="dismissed").status_code == 204
    assert box.state(nid, state="shown").status_code == 404


def test_a_move_the_trigger_refuses_is_a_409(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    """The row changes under the panel between the route's read and its write (the owner
    edits the choices): the trigger refuses the stale answer and the route says 409, not 500."""
    from jbrain.jpanel import notice_service

    box.pickup(**DAY)
    nid = box.send(choices=["Yes", "No"]).json()["id"]
    box.collect()
    real = notice_service.scoped_session
    calls = {"n": 0}

    def racing(maker, ctx):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 2:
            _owner_sql(
                box,
                "UPDATE app.panel_notice SET choices = '[\"Si\"]' WHERE id = CAST(:i AS uuid)",
                i=nid,
            )
        return real(maker, ctx)

    monkeypatch.setattr(notice_service, "scoped_session", racing)
    out = box.state(nid, state="answered", choice=0)
    assert out.status_code == 409 and out.json()["detail"] == {
        "reason": "bad_move",
        "now": "delivered",
    }


def test_only_a_panel_key_collects(box: Box) -> None:
    box.client.cookies.update(box.owner)
    assert box.client.get("/api/endpoint/notice/next").status_code == 403
    assert (
        box.client.post(
            "/api/endpoint/notice/00000000-0000-0000-0000-000000000000/state",
            json={"state": "shown"},
        ).status_code
        == 403
    )
    assert (
        box.client.get("/api/endpoint/notice/00000000-0000-0000-0000-000000000000/body").status_code
        == 403
    )
    box.client.cookies.clear()
    assert box.client.get("/api/endpoint/notice/next").status_code == 401


def test_a_sibling_never_sees_collects_or_answers_another_panel_s_notice(box: Box) -> None:
    box.pickup(**DAY)
    box.pickup(box.sibling, **DAY)
    nid = box.send(device_id=box.device, choices=["Yes", "No"]).json()["id"]
    assert "X-Notice" not in box.pickup(box.sibling, **DAY).headers
    assert box.collect(box.sibling).status_code == 204
    assert box.as_panel("GET", f"/api/endpoint/notice/{nid}/body", box.sibling).status_code == 404
    box.collect()
    assert box.state(nid, box.sibling, state="answered", choice=0).status_code == 404
    assert box.read(nid).json()["state"] == "delivered"


# ---- the refusal table, at send time ----------------------------------------------------------


def test_offline_is_refused_with_how_long(box: Box) -> None:
    out = box.send(device_id=box.device)
    assert out.status_code == 503
    assert out.json()["detail"] == {"reason": "offline", "last_seen_s": None}
    box.pickup(**DAY)
    relay._seen[box.device] = (relay._seen[box.device][0] - 45, C6)
    stale = box.send(device_id=box.device).json()["detail"]
    assert stale["reason"] == "offline" and stale["last_seen_s"] >= 45


def test_a_panel_without_notice_firmware_is_refused(box: Box) -> None:
    box.pickup()  # an older image: no notice settings on its pickup
    out = box.send(device_id=box.device)
    assert out.status_code == 409
    assert out.json()["detail"] == {"reason": "no_notice_surface", "board": C6}


def test_switched_off_is_refused(box: Box) -> None:
    box.pickup(**{**DAY, "na": "0"})
    out = box.send()
    assert (out.status_code, out.json()["detail"]) == (409, {"reason": "not_accepting"})
    # The panel switches it back on: the next pickup is the truth.
    box.pickup(**DAY)
    assert box.send().status_code == 202


def test_quiet_hours_are_refused_with_until(box: Box) -> None:
    now = datetime.now(UTC)
    minute = now.hour * 60 + now.minute
    window = {**DAY, "nf": str((minute - 60) % 1440), "nt": str((minute + 60) % 1440), "tz": "7"}
    box.pickup(**window)
    out = box.send()
    assert out.status_code == 409
    detail = out.json()["detail"]
    assert detail["reason"] == "quiet_hours" and detail["tz"] == "UTC"
    assert detail["until"] == N.hhmm((minute + 60) % 1440)
    # Switching "refuse at night" off on the panel lets it through.
    box.pickup(**{**window, "nq": "0"})
    assert box.send().status_code == 202


def test_a_refused_notice_is_never_stored(box: Box) -> None:
    box.pickup(**{**DAY, "na": "0"})
    box.send()

    async def count() -> int:
        async with scoped_session(box.maker, OWNER) as s:
            return int(
                (await s.execute(text("SELECT count(*) FROM app.panel_notice"))).scalar_one()
            )

    assert asyncio.run(count()) == 0


def test_more_than_four_open_is_busy(box: Box) -> None:
    box.pickup(**DAY)
    for i in range(N.MAX_OPEN):
        assert box.send(title=f"n{i}").status_code == 202
    out = box.send(title="one too many")
    assert (out.status_code, out.json()["detail"]) == (429, {"reason": "busy", "open": 4})


def test_which_panel_when_two_take_notices(box: Box) -> None:
    box.pickup(**DAY)
    box.pickup(box.sibling, **DAY)
    out = box.send()
    assert out.status_code == 409 and out.json()["detail"]["reason"] == "which_panel"
    names = {p["name"] for p in out.json()["detail"]["panels"]}
    assert names == {"Jeff", "Quorra"}
    assert box.send(device_id=box.sibling_device).status_code == 202
    assert box.send(device_id="00000000-0000-0000-0000-000000000000").status_code == 404


def test_with_no_panel_reporting_the_one_seen_answers_offline(box: Box) -> None:
    box.pickup()
    relay._seen[box.device] = (relay._seen[box.device][0] - 99, C6)
    out = box.send()
    assert out.status_code == 503 and out.json()["detail"]["reason"] == "offline"


@pytest.mark.parametrize(
    ("body", "status", "reason"),
    [
        ({"title": ""}, 422, "bad_field"),
        ({"title": "x" * 61}, 422, "too_long"),
        ({"choices": ["a", "b", "c", "d", "e"]}, 422, "too_many_choices"),
        ({"hide_after_s": 1}, 422, "bad_field"),
        ({"ttl_s": 10}, 422, "bad_field"),
        ({"sender": "mallory"}, 422, "bad_sender"),
        ({"image_b64": "!!!"}, 422, "bad_field"),
        ({"audio_b64": "AAAA", "say": "hi"}, 422, "bad_field"),
        ({"say": "   "}, 422, "bad_field"),
        ({"image_b64": "A" * (N.IMAGE_UPLOAD_MAX * 4 // 3 + 8)}, 413, "image_too_big"),
    ],
)
def test_bad_input_is_refused_before_anything_is_stored(
    box: Box, body: dict[str, Any], status: int, reason: str
) -> None:
    box.pickup(**DAY)
    out = box.send(**body)
    assert out.status_code == status, out.text
    assert out.json()["detail"]["reason"] == reason


# ---- pictures and sound ---------------------------------------------------------------------


def test_a_banner_picture_reaches_the_panel_as_stripes(box: Box) -> None:
    box.pickup(**DAY)
    sent = box.send(title="Backup done", body="All 3 disks.", image_b64=_png(960, 200))
    assert sent.status_code == 202, sent.text
    assert sent.json()["image"] == {
        "mode": "banner",
        "w": 480,
        "h": 100,
        "bytes": sent.json()["image"]["bytes"],
        "posterize": 0,
    }
    d = N.decode(box.collect().content)
    assert (d.notice.image_mode, d.notice.image_w, d.notice.image_h) == ("banner", 480, 100)
    assert len(d.pixels) == 480 * 100 and set(d.pixels) == {
        (40 >> 3) << 11 | (90 >> 2) << 5 | (200 >> 3)
    }
    view = box.read(sent.json()["id"]).json()
    assert view["image"]["mode"] == "banner" and view["image"]["h"] == 100


def test_a_picture_alone_fills_the_glass(box: Box) -> None:
    box.pickup(**DAY)
    sent = box.send(title="Look", image_b64=_png(100, 100))
    assert sent.json()["image"]["mode"] == "full" and sent.json()["image"]["h"] == 480
    forced = box.send(title="Look", body="words", image_b64=_png(100, 100), image_mode="full")
    assert forced.json()["image"]["mode"] == "full"


def test_a_picture_the_panel_cannot_take_is_refused(box: Box) -> None:
    box.pickup(**DAY)
    gif = io.BytesIO()
    Image.new("RGB", (8, 8)).save(gif, "GIF")
    out = box.send(image_b64=base64.b64encode(gif.getvalue()).decode())
    assert out.status_code == 415 and out.json()["detail"]["reason"] == "image_format"


def test_a_wav_clip_plays_on_the_panel(box: Box) -> None:
    box.pickup(**DAY)
    clip = tone(1500, rate=48000)
    sent = box.send(audio_b64=base64.b64encode(wav(clip, rate=48000)).decode())
    assert sent.status_code == 202, sent.text
    assert sent.json()["audio_ms"] == 1500
    d = N.decode(box.collect().content)
    assert d.notice.audio_ms == 1500 and N.pcm_ms(d.pcm) == 1500


def test_a_clip_over_eight_seconds_is_refused(box: Box) -> None:
    box.pickup(**DAY)
    out = box.send(audio_b64=base64.b64encode(wav(tone(8200))).decode())
    assert out.status_code == 422
    assert out.json()["detail"] == {"reason": "audio_too_long", "ms": 8200, "max_ms": 8000}


def test_say_speaks_in_the_pwa_s_voice(box: Box) -> None:
    box.pickup(**DAY)
    sent = box.send(say="Your backup is done.")
    assert sent.status_code == 202, sent.text
    assert box.spoken and box.spoken[-1]["text"] == "Your backup is done."
    assert 0 < sent.json()["audio_ms"] <= 1200
    too_long = box.send(say="a long speech")
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["reason"] == "audio_too_long"
    assert "hint" in too_long.json()["detail"]


# ---- the sweep ------------------------------------------------------------------------------


def test_an_uncollected_notice_expires_and_a_finished_one_is_swept_with_its_blobs(
    box: Box, tmp_path: Path
) -> None:
    box.pickup(**DAY)
    nid = box.send(title="Look", image_b64=_png(64, 64)).json()["id"]
    _owner_sql(
        box,
        "UPDATE app.panel_notice SET expires_at = now() - interval '1 second'"
        " WHERE id = CAST(:i AS uuid)",
        i=nid,
    )
    assert box.collect().status_code == 204, "past its pickup window it is never delivered"
    assert box.read(nid).json()["state"] == "expired"
    blobs = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert blobs
    _owner_sql(
        box,
        "UPDATE app.panel_notice SET finished_at = now() - interval '2 days'"
        " WHERE id = CAST(:i AS uuid)",
        i=nid,
    )
    box.send(title="trigger a sweep")
    assert box.read(nid).status_code == 404
    assert not any(p.exists() for p in blobs)


# ---- the debug console -------------------------------------------------------------------------


def test_a_debug_session_sends_a_test_notice_and_reads_the_tap(box: Box) -> None:
    box.client.cookies.clear()
    listed = box.client.get("/api/debug/panel", headers=box.dbg).json()
    assert {p["name"]: p["notices"] for p in listed}["Jeff"] is None
    box.pickup(**DAY)
    listed = box.client.get("/api/debug/panel", headers=box.dbg).json()
    assert {p["name"]: p["notices"] for p in listed}["Jeff"] == {
        "accept": True,
        "quiet": True,
        "chime": True,
        "night_from": "00:00",
        "night_to": "00:00",
        "tz": "UTC",
    }
    box.client.cookies.clear()
    sent = box.client.post(
        f"/api/debug/panel/{box.device}/notice",
        headers=box.dbg,
        json={"title": "Test from a session", "choices": ["OK", "Not now"]},
    )
    assert sent.status_code == 202, sent.text
    nid = sent.json()["id"]
    d = N.decode(box.collect().content)
    assert d.notice.sender_label == "Debug"
    box.state(nid, state="answered", choice=0)
    box.client.cookies.clear()
    out = box.client.get(
        f"/api/debug/panel/{box.device}/notice/{nid}?wait=2", headers=box.dbg
    ).json()
    assert (out["state"], out["answer"], out["sender"]) == ("answered", "OK", "debug")
    # Another panel's id in the path does not read this notice.
    other = box.client.get(f"/api/debug/panel/{box.sibling_device}/notice/{nid}", headers=box.dbg)
    assert other.status_code == 404
    refused = box.client.post(
        f"/api/debug/panel/{box.sibling_device}/notice", headers=box.dbg, json={"title": "x"}
    )
    assert refused.status_code == 503


def test_owner_and_debug_reads_404_for_unknown_ids(box: Box) -> None:
    assert box.read("00000000-0000-0000-0000-000000000000").status_code == 404
    assert box.read("nope").status_code == 404


def test_the_pickup_still_hands_over_a_debug_request_with_the_hint(box: Box) -> None:
    box.pickup(**DAY)
    box.send(title="waiting")
    box.client.cookies.clear()
    rid = box.client.post(
        f"/api/debug/panel/{box.device}/command", headers=box.dbg, json={"command": "snapshot"}
    ).json()["request_id"]
    got = box.pickup(**DAY)
    assert got.status_code == 200 and got.json() == {"id": rid, "cmd": "snapshot"}
    assert got.headers["X-Notice"] == "1"
