"""jerv's radio tools.

`sdr_listen` is load-bearing beyond chat: the composer's radio icon exists only
while a session holds the tuner, so this tool is the only thing that can put the
tuner surface in front of the owner. What matters is that it takes the lease, says
so plainly when it cannot, and points at the icon rather than narrating settings.
"""

from typing import Any

import httpx
import pytest

from jbrain.agent.sdrtools import build_sdr_handlers


def _client(handler) -> Any:
    class Fake:
        def __init__(self, *_a: Any, **_k: Any) -> None: ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a: Any) -> None: ...
        async def post(self, path: str, json: dict[str, Any] | None = None):
            return handler(path, json or {})

        async def get(self, path: str):
            # sdr_aprs_logging reads /healthz before acting — it must know what the
            # sidecar understands rather than trusting a 200 from an older one.
            return handler(path, None)

    return Fake


@pytest.fixture
def tools(monkeypatch: pytest.MonkeyPatch):
    def install(handler) -> dict:
        monkeypatch.setattr(httpx, "AsyncClient", _client(handler))
        return build_sdr_handlers("http://sdr:8000")

    return install


def _ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body, request=httpx.Request("POST", "http://sdr:8000/x"))


async def test_listening_points_at_the_icon_rather_than_narrating(tools) -> None:
    seen: dict[str, Any] = {}

    def handler(path, body):
        seen.update({"path": path, "body": body})
        return _ok({"session_id": "abc", "frequency_hz": 99_300_000, "mode": "wbfm"})

    out = await tools(handler)["sdr_listen"]({"frequency_mhz": 99.3}, None)

    assert seen["body"]["frequency_hz"] == 99_300_000
    # The owner can drive the tuner far faster than the model can describe it.
    assert "icon" in out
    assert "99.3" in out


async def test_broadcast_fm_defaults_to_wide_fm(tools) -> None:
    # Getting this wrong is audible: narrowband on a broadcast station is mush.
    seen: dict[str, Any] = {}
    out = await tools(lambda p, b: (seen.update(b), _ok({"session_id": "abc"}))[1])["sdr_listen"](
        {"frequency_mhz": 99.3}, None
    )

    assert seen["mode"] == "wbfm"
    assert "WBFM" in out


async def test_everything_else_defaults_to_narrowband(tools) -> None:
    seen: dict[str, Any] = {}
    await tools(lambda p, b: (seen.update(b), _ok({"session_id": "abc"}))[1])["sdr_listen"](
        {"frequency_mhz": 162.55}, None
    )

    assert seen["mode"] == "fm"


async def test_an_explicit_mode_wins_over_the_default(tools) -> None:
    seen: dict[str, Any] = {}
    await tools(lambda p, b: (seen.update(b), _ok({"session_id": "abc"}))[1])["sdr_listen"](
        {"frequency_mhz": 99.3, "mode": "am"}, None
    )

    assert seen["mode"] == "am"


async def test_a_busy_radio_is_explained_not_retried(tools) -> None:
    busy = httpx.Response(
        409,
        json={"detail": "the radio is already listening"},
        request=httpx.Request("POST", "http://sdr:8000/x"),
    )

    out = await tools(lambda _p, _b: busy)["sdr_listen"]({"frequency_mhz": 99.3}, None)

    # One tuner: this is a state to report, not an error to retry into.
    assert "already listening" in out
    assert "release" in out.lower()


async def test_a_busy_radio_says_WHICH_job_is_holding_it(tools) -> None:
    # The sidecar names the holder because the two jobs need opposite advice. This tool
    # used to overwrite that with a hardcoded "already listening", which while the radio
    # was logging APRS was not merely generic but FALSE — and it deleted the one fact
    # that told the owner which switch to throw (APRS_CONTROL_PLAN.md P0).
    busy = httpx.Response(
        409,
        json={"detail": "the radio is already logging APRS"},
        request=httpx.Request("POST", "http://sdr:8000/x"),
    )

    out = await tools(lambda _p, _b: busy)["sdr_listen"]({"frequency_mhz": 99.3}, None)

    assert "logging APRS" in out
    assert "listening" not in out


async def test_a_busy_radio_with_no_reason_still_gets_a_usable_answer(tools) -> None:
    busy = httpx.Response(409, json={}, request=httpx.Request("POST", "http://sdr:8000/x"))

    out = await tools(lambda _p, _b: busy)["sdr_listen"]({"frequency_mhz": 99.3}, None)

    # A sidecar that says nothing must not produce an empty sentence at the owner.
    assert "in use" in out
    assert "release" in out.lower()


async def test_an_out_of_range_frequency_never_reaches_the_radio(tools) -> None:
    called = False

    def handler(_p, _b):
        nonlocal called
        called = True
        return _ok({})

    out = await tools(handler)["sdr_listen"]({"frequency_mhz": 5000}, None)

    assert not called
    # Says which END it is outside. Quoting the whole 0.1-1766 range would invite the
    # reader to conclude the radio is one thing that tunes all of it, and it is two.
    assert "above what this radio reaches" in out


async def test_shortwave_is_no_longer_refused(tools) -> None:
    """It was, by a floor of 24 MHz that described the TUNER rather than the radio. The
    NESDR SMArt v5 feeds HF to the ADC directly through an on-board diplexer, so 10 MHz
    is a frequency this hardware can actually receive."""
    seen: list[dict] = []

    def handler(_p, body):
        seen.append(body or {})
        return _ok({"session_id": "s1", "frequency_hz": 10_000_000})

    await tools(handler)["sdr_listen"]({"frequency_mhz": 10.0, "mode": "am"}, None)

    assert seen and seen[0]["frequency_hz"] == 10_000_000


async def test_below_the_ADC_is_still_refused(tools) -> None:
    called = False

    def handler(_p, _b):
        nonlocal called
        called = True
        return _ok({})

    out = await tools(handler)["sdr_listen"]({"frequency_mhz": 0.05}, None)

    assert not called
    assert "below what this radio reaches" in out


async def test_a_non_numeric_frequency_is_refused_kindly(tools) -> None:
    out = await tools(lambda _p, _b: _ok({}))["sdr_listen"]({"frequency_mhz": "ninety nine"}, None)

    assert "isn't a number" in out


async def test_stop_releases_and_says_so(tools) -> None:
    out = await tools(lambda _p, _b: _ok({"stopped": True}))["sdr_stop"]({}, None)

    assert out == "Radio released."


async def test_stopping_an_idle_radio_is_not_an_error(tools) -> None:
    out = await tools(lambda _p, _b: _ok({"stopped": False, "holding": []}))["sdr_stop"]({}, None)

    assert "already free" in out


async def test_release_says_what_is_holding_a_radio_rather_than_stopping_it(tools) -> None:
    """ "Release the radio" names no session, and the sidecar reads that as the LISTENING
    one — never a service, or a scheduled APRS window would end on a casual ask. Without
    naming the holder the answer is a dead end: nothing happened and nothing said why."""
    answer = {"stopped": False, "holding": [{"purpose": "aprs", "serial": "77192819"}]}

    out = await tools(lambda _p, _b: _ok(answer))["sdr_stop"]({}, None)

    assert "Nothing was listening" in out
    assert "aprs" in out and "own switch" in out


def test_a_box_with_no_radio_gets_no_tools() -> None:
    # The same graceful degrade the image and transcription tools use — and it means
    # the tuner surface can never appear on a box that has no radio.
    assert build_sdr_handlers("") == {}


# --- turning APRS logging on and off -------------------------------------------------
# The rules this tool exists to keep (APRS_CONTROL_PLAN.md P1a): it is idempotent, it
# reports the state it ACTUALLY reached rather than "ok", it stops the APRS session
# specifically and never a listening one, and it refuses against a sidecar too old to
# understand the request instead of succeeding at nothing.


def _sidecar(
    purpose: str | None = None, *, purposes: list[str] | None = None, session_id: str = "s1"
):
    """A sidecar whose /healthz answer the test chooses; POSTs are recorded."""
    posts: list[tuple[str, dict[str, Any]]] = []

    def route(path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        req = httpx.Request("POST", f"http://sdr:8000{path}")
        if path == "/healthz":
            listening = (
                {"purpose": purpose, "session_id": session_id, "frequency_hz": 144_390_000}
                if purpose
                else None
            )
            return httpx.Response(
                200,
                json={
                    "purposes": ["listen", "aprs"] if purposes is None else purposes,
                    "listening": listening,
                },
                request=req,
            )
        posts.append((path, body or {}))
        return httpx.Response(200, json={"stopped": True, "session_id": "new"}, request=req)

    return route, posts


async def test_turning_logging_on_starts_an_aprs_session(tools) -> None:
    route, posts = _sidecar(purpose=None)

    out = await tools(lambda p, b: route(p, b))["sdr_aprs_logging"]({"enabled": True}, None)

    assert posts[0][0] == "/listen/start"
    assert posts[0][1]["purpose"] == "aprs"
    assert posts[0][1]["mode"] == "fm"  # 1200-baud AFSK is narrowband FM, always
    assert "144.39" in out


async def test_turning_it_on_when_it_is_already_on_succeeds_and_changes_nothing(tools) -> None:
    route, posts = _sidecar(purpose="aprs")

    out = await tools(lambda p, b: route(p, b))["sdr_aprs_logging"]({"enabled": True}, None)

    # Idempotent, because a scheduled retry of "turn it on" must not be a failure.
    assert posts == []
    assert "already on" in out


async def test_turning_it_off_stops_THIS_session_by_id(tools) -> None:
    route, posts = _sidecar(purpose="aprs", session_id="abc123")

    out = await tools(lambda p, b: route(p, b))["sdr_aprs_logging"]({"enabled": False}, None)

    # By id, so it cannot release a session that changed under it.
    assert posts[0] == ("/listen/stop", {"session_id": "abc123"})
    assert "off" in out


async def test_turning_it_off_never_releases_a_listening_session(tools) -> None:
    route, posts = _sidecar(purpose="listen")

    out = await tools(lambda p, b: route(p, b))["sdr_aprs_logging"]({"enabled": False}, None)

    # One tuner: "stop logging" reaching a listening session would silence the radio
    # the owner was actually using.
    assert posts == []
    assert "already off" in out


def _two_radios(aprs_id: str = "s-aprs"):
    """A two-dongle box: APRS on the long wire, the tuner on the desk whip.

    `listening` is the TUNER's session, because that is the one the omnibox draws. This
    tool read that field, so it reported "APRS logging is already off" while it ran —
    and "turn it off" would have released the tuner instead."""
    posts: list[tuple[str, dict[str, Any]]] = []
    tuner = {"purpose": "listen", "session_id": "s-tuner", "frequency_hz": 146_520_000}
    aprs = {"purpose": "aprs", "session_id": aprs_id, "frequency_hz": 144_390_000}

    def route(path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        req = httpx.Request("POST", f"http://sdr:8000{path}")
        if path == "/healthz":
            return httpx.Response(
                200,
                json={
                    "purposes": ["listen", "aprs"],
                    "listening": tuner,
                    "sessions": [tuner, aprs],
                },
                request=req,
            )
        posts.append((path, body or {}))
        return httpx.Response(200, json={"stopped": True, "session_id": "new"}, request=req)

    return route, posts


async def test_logging_is_seen_even_when_the_tuner_holds_the_other_radio(tools) -> None:
    route, posts = _two_radios()

    out = await tools(lambda p, b: route(p, b))["sdr_aprs_logging"]({"enabled": True}, None)

    assert posts == []  # no second APRS session started on top of the running one
    assert "already on" in out


async def test_turning_it_off_stops_the_APRS_session_not_the_tuners(tools) -> None:
    route, posts = _two_radios(aprs_id="abc123")

    out = await tools(lambda p, b: route(p, b))["sdr_aprs_logging"]({"enabled": False}, None)

    assert posts[0] == ("/listen/stop", {"session_id": "abc123"})
    assert "off" in out


async def test_it_refuses_a_sidecar_too_old_to_log_rather_than_succeeding_at_nothing(
    tools,
) -> None:
    route, posts = _sidecar(purpose=None, purposes=["listen"])

    out = await tools(lambda p, b: route(p, b))["sdr_aprs_logging"]({"enabled": True}, None)

    # An older sidecar ignores an unknown `purpose` and returns 200 with a plain
    # LISTENING session — so this would otherwise report success while logging nothing.
    assert posts == []
    assert "too old" in out
    assert "Nothing was changed" in out


async def test_a_busy_radio_names_the_job_holding_it(tools) -> None:
    def route(path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        req = httpx.Request("POST", f"http://sdr:8000{path}")
        if path == "/healthz":
            return httpx.Response(200, json={"purposes": ["listen", "aprs"]}, request=req)
        return httpx.Response(409, json={"detail": "the radio is already listening"}, request=req)

    out = await tools(route)["sdr_aprs_logging"]({"enabled": True}, None)

    assert "already listening" in out
    assert "release" in out.lower()


async def test_a_missing_enabled_flag_is_asked_for_not_guessed(tools) -> None:
    route, posts = _sidecar(purpose=None)

    out = await tools(lambda p, b: route(p, b))["sdr_aprs_logging"]({}, None)

    # Guessing a default would either seize the radio or silence it.
    assert posts == []
    assert "on or off" in out


async def test_an_unreachable_radio_does_not_claim_success(tools) -> None:
    def route(path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        req = httpx.Request("POST", f"http://sdr:8000{path}")
        return httpx.Response(502, json={}, request=req)

    out = await tools(route)["sdr_aprs_logging"]({"enabled": True}, None)

    assert "isn't reachable" in out


async def test_a_stop_that_stopped_nothing_is_not_reported_as_off(tools) -> None:
    """The sidecar answers 200 `{"stopped": false}` when the id no longer matched.

    This tool used to read any 200 as done and say "APRS logging is off. The radio is
    free." For the timed window it exists to serve — a 09:00 "turn logging off" whose
    session restarted at 08:59 — that is nothing stopped, the owner told otherwise, and
    the tuner held for the rest of the day. `sdr_stop`, fourteen lines below, checks
    this; this path did not."""

    def route(path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        req = httpx.Request("POST", f"http://sdr:8000{path}")
        if path == "/healthz":
            return httpx.Response(
                200,
                json={
                    "purposes": ["listen", "aprs"],
                    "listening": {"purpose": "aprs", "session_id": "gone"},
                },
                request=req,
            )
        return httpx.Response(200, json={"stopped": False}, request=req)

    out = await tools(route)["sdr_aprs_logging"]({"enabled": False}, None)

    assert "may still be on" in out
    assert "is off" not in out
