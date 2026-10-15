"""The `deploy/cast` sidecar (the jpanel Cast spike), loaded by path like `endpoint`.

The owner's rule is the thing pinned: a notice is cast only over an idle TV or one that
is off, never over anything someone is watching — paused included — and a TV that cannot
be read is refused like a busy one. Then the server: the card is reachable without a
header (a TV cannot send one) and only under its nonce, while every control route fails
closed.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load(name: str, filename: str):
    cast_dir = str(DEPLOY / "cast")
    if cast_dir not in sys.path:
        sys.path.insert(0, cast_dir)
    spec = importlib.util.spec_from_file_location(name, DEPLOY / f"cast/{filename}")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Bare names: server.py imports `gate` and `frame`, so these must be the same modules.
gate = _load("gate", "gate.py")
frame = _load("frame", "frame.py")
server = _load("cast_server", "server.py")
tvs = _load("cast_tvs", "tvs.py")

R = gate.Reading
ON = {"connected": True, "has_status": True}


@pytest.mark.parametrize(
    ("reading", "state"),
    [
        (R(**ON), "idle"),
        (R(**ON, app_id=gate.BACKDROP_APP_ID, display_name="Backdrop"), "idle"),
        (R(**ON, is_stand_by=True), "standby"),
        # An open app wins over standby: Parents (a Philips) reported is_stand_by
        # while it played Netflix opened with the remote.
        (R(**ON, is_stand_by=True, app_id="Netflix", display_name="Netflix"), "busy"),
        (
            R(**ON, is_stand_by=True, app_id="CA5E8412", player_state="PLAYING"),
            "busy",
        ),
        (R(**ON, is_stand_by=True, app_id=gate.BACKDROP_APP_ID), "standby"),
        # Standby before the input: every TV that was really off read exactly this
        # (Big TV, Parents, Twins Room), so the other order would never wake one. A lit
        # set on another input is unmeasured.
        (R(**ON, is_stand_by=True, is_active_input=False), "standby"),
        (
            R(**ON, app_id="233637DE", display_name="YouTube", player_state="PLAYING"),
            "busy",
        ),
        (
            R(**ON, app_id="CA5E8412", display_name="Netflix", player_state="PAUSED"),
            "busy",
        ),
        (R(**ON, app_id="CA5E8412", display_name="Netflix"), "busy"),
        (R(**ON, is_active_input=False), "busy"),
        (R(**ON, is_active_input=None), "idle"),
        (R(connected=True), "unknown"),
        (R(has_status=True), "unknown"),
    ],
)
def test_the_gate(reading: Any, state: str) -> None:
    assert gate.verdict(reading)[0] == state


def test_only_idle_and_standby_are_castable() -> None:
    assert [s for s in ("idle", "standby", "busy", "unknown") if gate.castable(s)] == [
        "idle",
        "standby",
    ]


def test_busy_says_what_is_on_in_words() -> None:
    r = R(**ON, app_id="CA5E8412", display_name="Netflix", player_state="PAUSED")
    assert gate.verdict(r) == ("busy", "Netflix · paused")


def test_history_keeps_only_changes_and_reads_since() -> None:
    h = gate.History()
    assert h.record(R(**ON), now=1.0)
    assert not h.record(R(**ON), now=2.0)
    h.note("test_cast", now=3.0, seconds=15)
    assert h.record(R(**ON, is_stand_by=True), now=4.0)
    entries = h.since(0)
    assert [e.get("state") or e.get("event") for e in entries] == [
        "idle",
        "test_cast",
        "standby",
    ]
    assert [e["at"] for e in h.since(2.5)] == [3.0, 4.0]


def test_the_card_is_a_valid_1080p_png() -> None:
    png = frame.png()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (1920, 1080)
    # The IDAT inflates to exactly one filter byte plus RGB per row: a malformed chunk
    # would fail here rather than as a blank TV.
    idat_len = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41 : 41 + idat_len])
    assert len(raw) == 1080 * (1 + 1920 * 3)


class FakeFleet:
    def __init__(self, refuse: bool = False) -> None:
        self.refuse = refuse
        self.tests: list[tuple[str, str, int]] = []
        self.casts: list[tuple[str, str, int, str]] = []

    def snapshot(self) -> list[dict[str, Any]]:
        return [{"id": TV, "state": "idle"}]

    def ignored(self) -> list[dict[str, Any]]:
        return [{"name": "Kitchen speaker", "cast_type": "audio"}]

    def history(self, key: str, since: float = 0.0) -> list[dict[str, Any]]:
        if key != TV:
            raise KeyError(key)
        return [{"at": since + 1}]

    def host_of(self, key: str) -> str:
        if key != TV:
            raise KeyError(key)
        return "192.168.1.50"

    def test(self, key: str, url: str, seconds: int) -> dict[str, Any]:
        if self.refuse:
            raise gate.Refused("busy", "Netflix · playing")
        self.tests.append((key, url, seconds))
        return {"id": key, "seconds": seconds}

    def cast(self, key: str, url: str, seconds: int, title: str) -> dict[str, Any]:
        if self.refuse:
            raise gate.Refused("busy", "Netflix · playing")
        self.casts.append((key, url, seconds, title))
        return {"id": key, "seconds": seconds, "cast_id": "c1"}

    def stop(self, key: str, cast_id: str) -> bool:
        if key != TV:
            raise KeyError(key)
        return cast_id == "c1"


TV = "0f4e6b0a-6c1e-4b0a-9a5e-3f1a2b3c4d5e"
AUTH = "Bearer tok"


def _route(
    method: str,
    target: str,
    *,
    auth: str = AUTH,
    body: bytes = b"",
    fleet=None,
    frames=None,
) -> tuple[int, str, bytes]:
    return server.route(
        method,
        target,
        auth,
        body,
        fleet or FakeFleet(),
        frames or server.Frames(),
        token=lambda: b"tok",
        address_for=lambda host: "192.168.1.10",
        port=19190,
    )


def test_health_needs_no_token() -> None:
    assert _route("GET", "/healthz", auth="")[0] == 200


@pytest.mark.parametrize("auth", ["", "Bearer nope", "Basic tok", "tok"])
def test_control_routes_fail_closed(auth: str) -> None:
    assert _route("GET", "/tvs", auth=auth)[0] == 401


def test_no_token_minted_yet_refuses_even_an_empty_bearer() -> None:
    code, _, _ = server.route(
        "GET", "/tvs", "Bearer ", b"", FakeFleet(), server.Frames(), token=lambda: None
    )
    assert code == 401


def test_tvs_and_history() -> None:
    code, _, raw = _route("GET", "/tvs")
    assert code == 200 and json.loads(raw)["tvs"][0]["id"] == TV
    code, _, raw = _route("GET", f"/tvs/{TV}/history?since=5")
    assert code == 200 and json.loads(raw)["entries"] == [{"at": 6.0}]
    assert _route("GET", f"/tvs/{TV}/history?since=x")[0] == 400
    assert _route("GET", "/tvs/0f4e6b0a-6c1e-4b0a-9a5e-000000000000/history")[0] == 404


def test_a_test_cast_serves_the_card_under_its_nonce_only() -> None:
    fleet, frames = FakeFleet(), server.Frames()
    code, _, raw = _route(
        "POST", f"/tvs/{TV}/test", body=b'{"seconds": 20}', fleet=fleet, frames=frames
    )
    assert code == 202 and json.loads(raw)["seconds"] == 20
    ((_, url, seconds),) = fleet.tests
    assert seconds == 20
    assert url.startswith("http://192.168.1.10:19190/frame/") and url.endswith(".png")
    code, ctype, card = _route("GET", url.split(":19190", 1)[1], auth="", frames=frames)
    assert (code, ctype) == (200, "image/png") and card[:4] == b"\x89PNG"
    assert (
        _route("GET", "/frame/" + "0" * 32 + ".png", auth="", frames=frames)[0] == 404
    )


def test_seconds_are_clamped_before_the_card_is_offered() -> None:
    """The card's URL is unauthenticated and lives for the test plus a grace; the LAN
    control port is not behind the api's validation, so the bound is enforced here."""
    clock = [1000.0]
    fleet, frames = FakeFleet(), server.Frames(now=lambda: clock[0])
    code, _, _ = _route(
        "POST",
        f"/tvs/{TV}/test",
        body=b'{"seconds": 999999}',
        fleet=fleet,
        frames=frames,
    )
    assert code == 202
    ((_, url, seconds),) = fleet.tests
    assert seconds == gate.MAX_TEST_S
    nonce = url.rsplit("/", 1)[1].removesuffix(".png")
    clock[0] += gate.MAX_TEST_S + server.FRAME_GRACE_S + 1
    assert frames.get(nonce) is None
    _route("POST", f"/tvs/{TV}/test", body=b'{"seconds": 0}', fleet=fleet)
    assert fleet.tests[-1][2] == gate.MIN_TEST_S


def test_a_frame_expires() -> None:
    clock = [100.0]
    frames = server.Frames(now=lambda: clock[0])
    nonce = frames.offer(ttl_s=10)
    assert frames.get(nonce) is not None
    clock[0] = 111.0
    assert frames.get(nonce) is None


def test_a_refusal_is_a_409_with_the_verdict() -> None:
    code, _, raw = _route("POST", f"/tvs/{TV}/test", fleet=FakeFleet(refuse=True))
    assert code == 409
    assert json.loads(raw) == {
        "error": "not idle",
        "state": "busy",
        "why": "Netflix · playing",
    }


def test_bad_bodies_and_routes() -> None:
    assert _route("POST", f"/tvs/{TV}/test", body=b"[1]")[0] == 400
    assert _route("POST", f"/tvs/{TV}/test", body=b"{nope")[0] == 400
    assert _route("GET", f"/tvs/{TV}")[0] == 404
    assert _route("GET", f"/tvs/{TV}/test")[0] == 405


PNG = b"\x89PNG\r\n\x1a\n" + b"notice"


def _cast_body(png: bytes = PNG, **extra: Any) -> bytes:
    return json.dumps(
        {"png_b64": base64.b64encode(png).decode(), "title": "Dinner", **extra}
    ).encode()


def test_a_notice_is_served_its_own_frame() -> None:
    fleet, frames = FakeFleet(), server.Frames()
    code, _, raw = _route(
        "POST",
        f"/tvs/{TV}/cast",
        body=_cast_body(seconds=30),
        fleet=fleet,
        frames=frames,
    )
    assert code == 202 and json.loads(raw)["cast_id"] == "c1"
    ((_, url, seconds, title),) = fleet.casts
    assert (seconds, title) == (30, "Dinner")
    _, ctype, served = _route("GET", url.split(":19190", 1)[1], auth="", frames=frames)
    assert (ctype, served) == ("image/png", PNG)


def test_a_notice_is_held_to_its_bounds_and_must_be_a_png() -> None:
    fleet = FakeFleet()
    _route("POST", f"/tvs/{TV}/cast", body=_cast_body(seconds=99999), fleet=fleet)
    assert fleet.casts[-1][2] == gate.MAX_NOTICE_S
    assert _route("POST", f"/tvs/{TV}/cast", body=_cast_body(b"GIF89a"))[0] == 400
    assert _route("POST", f"/tvs/{TV}/cast", body=b'{"title": "x"}')[0] == 400
    assert _route("POST", f"/tvs/{TV}/cast", body=b'{"png_b64": "%%"}')[0] == 400
    code, _, raw = _route(
        "POST", f"/tvs/{TV}/cast", body=_cast_body(), fleet=FakeFleet(refuse=True)
    )
    assert code == 409 and json.loads(raw)["state"] == "busy"


def test_stop_names_the_cast() -> None:
    code, _, raw = _route("POST", f"/tvs/{TV}/stop", body=b'{"cast_id": "c1"}')
    assert code == 200 and json.loads(raw)["stopped"] is True
    code, _, raw = _route("POST", f"/tvs/{TV}/stop", body=b'{"cast_id": "old"}')
    assert json.loads(raw)["stopped"] is False
    assert _route("POST", f"/tvs/{TV}/stop", body=b"{}")[0] == 400


class FakeCc:
    def __init__(self) -> None:
        from types import SimpleNamespace as NS

        self.status = NS(
            app_id=None,
            display_name=None,
            status_text=None,
            is_stand_by=False,
            is_active_input=True,
            volume_level=0.2,
        )
        self.played: list[tuple[str, str]] = []
        self.quits = 0
        self.media_controller = NS(status=NS(player_state=None, title=None))
        self.media_controller.play_media = self._play

    def _play(self, url: str, _mime: str, title: str) -> None:
        self.played.append((url, title))
        self.status.app_id = tvs.APP_MEDIA_RECEIVER

    def quit_app(self) -> None:
        self.quits += 1
        self.status.app_id = None


def _fleet_with_tv() -> tuple[Any, Any]:
    fleet = tvs.Fleet()
    cc = FakeCc()
    tv = tvs.Tv(cc=cc, connected=True)  # type: ignore[arg-type]
    fleet._tvs[TV] = tv
    return fleet, cc


def test_only_the_cast_on_screen_is_ever_quit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A quit (timed or Take it down) ends its own cast and nothing else: not a newer
    notice that replaced it, and not an app somebody opened since."""
    monkeypatch.setattr(
        tvs.threading,
        "Thread",
        lambda **_k: type("T", (), {"start": lambda self: None})(),
    )
    fleet, cc = _fleet_with_tv()
    first = fleet.cast(TV, "http://x/1.png", 30, "One")["cast_id"]
    # Our own card reads busy, so a second notice waits rather than stacking.
    with pytest.raises(gate.Refused):
        fleet.cast(TV, "http://x/2.png", 30, "Two")
    # The TV went back to idle on its own; a newer cast is now the one on screen.
    cc.status.app_id = None
    second = fleet.cast(TV, "http://x/2.png", 30, "Two")["cast_id"]
    assert [t for _, t in cc.played] == ["One", "Two"]
    assert fleet.stop(TV, first) is False and cc.quits == 0
    assert fleet.stop(TV, second) is True and cc.quits == 1
    third = fleet.cast(TV, "http://x/3.png", 30, "Three")["cast_id"]
    cc.status.app_id = "Netflix"
    assert fleet.stop(TV, third) is False and cc.quits == 1


def test_a_notice_is_refused_over_an_open_app() -> None:
    fleet, cc = _fleet_with_tv()
    cc.status.app_id = "Netflix"
    cc.status.display_name = "Netflix"
    with pytest.raises(gate.Refused):
        fleet.cast(TV, "http://x/1.png", 30, "One")
    assert cc.played == []


def test_the_reading_ignores_media_left_behind_by_a_closed_app() -> None:
    from types import SimpleNamespace as NS

    def tv(app_id: str | None, player_state: str) -> Any:
        status = NS(
            app_id=app_id,
            display_name=None,
            status_text=None,
            is_stand_by=False,
            is_active_input=None,
            volume_level=0.3,
        )
        media = NS(player_state=player_state, title="old")
        cc = NS(status=status, media_controller=NS(status=media))
        return NS(cc=cc, connected=True)

    assert tvs.reading(tv(None, "PLAYING")).player_state is None
    assert gate.verdict(tvs.reading(tv(None, "PLAYING")))[0] == "idle"
    assert tvs.reading(tv("CA5E8412", "PAUSED")).player_state == "PAUSED"


class FakeBrowser:
    def __init__(self, devices: dict[Any, Any]) -> None:
        self.devices = devices


def _info(cast_type: str | None, name: str = "Living room") -> Any:
    from types import SimpleNamespace as NS

    return NS(cast_type=cast_type, friendly_name=name, model_name="m", host="10.0.0.9")


@pytest.mark.parametrize("cast_type", ["group", "audio"])
def test_speakers_and_groups_are_set_aside_never_connected(
    monkeypatch: pytest.MonkeyPatch, cast_type: str
) -> None:
    """A group can read idle while a TV in it is busy, so it is never a target."""
    import uuid

    def never(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("connected to a non-screen")

    monkeypatch.setattr(tvs.pychromecast, "get_chromecast_from_cast_info", never)
    fleet = tvs.Fleet()
    key = uuid.uuid4()
    fleet._browser = FakeBrowser({key: _info(cast_type, "Upstairs")})  # type: ignore[assignment]
    fleet._found(key, "svc")
    assert fleet.snapshot() == []
    assert fleet.ignored() == [
        {"id": str(key), "name": "Upstairs", "model": "m", "cast_type": cast_type}
    ]


def test_an_unknown_model_is_asked_what_it_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery leaves cast_type empty for models it does not know (most Android TVs):
    filtering on that would drop every real TV."""
    import uuid

    asked: list[Any] = []

    def get_cast_type(info: Any, _zconf: Any) -> Any:
        asked.append(info)
        return _info("audio", "Soundbar")

    monkeypatch.setattr(tvs, "get_cast_type", get_cast_type)
    fleet = tvs.Fleet()
    key = uuid.uuid4()
    fleet._browser = FakeBrowser({key: _info(None)})  # type: ignore[assignment]
    fleet._found(key, "svc")
    assert len(asked) == 1
    assert fleet.ignored()[0]["cast_type"] == "audio"


def test_every_sidecar_module_is_copied_into_the_image() -> None:
    dockerfile = (DEPLOY / "Dockerfile.cast").read_text()
    for module in sorted((DEPLOY / "cast").glob("*.py")):
        assert f"COPY deploy/cast/{module.name} " in dockerfile, module.name


def test_the_image_pins_the_version_the_suite_tests() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert '"PyChromecast==14.0.10"' in pyproject
    assert '"PyChromecast==14.0.10"' in (DEPLOY / "Dockerfile.cast").read_text()
