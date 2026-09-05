"""jerv's radio tools.

`sdr_listen` is load-bearing beyond chat: the composer's radio icon exists only
while a session holds the tuner, so this tool is the only thing that can put the
tuner surface in front of the owner. What matters is that it takes the lease, says
so plainly when it cannot, and points at the icon rather than narrating settings.
"""

import ast
from pathlib import Path
from typing import Any

import httpx
import pytest

from jbrain.agent import sdrtools
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


# --- what the DESCRIPTIONS claim -----------------------------------------------------
# The prose is the only part of a tool the model reads before it acts, and it drifts
# silently: nothing runs it. Two claims here went stale and shipped — a box with one
# tuner (the lease has been per radio since APRS_CONTROL_PLAN P0b) and a refusal that
# can only name one of two jobs (there are four). These are the guards for both.


_TOOLS = Path(__file__).resolve().parents[2] / "src" / "jbrain" / "agent" / "tools"
_LISTEN_PY = Path(__file__).resolve().parents[3] / "deploy" / "sdr" / "listen.py"

#: Ways of saying the thing that must survive a rewrite: taking a radio does not take
#: them all, because the lease is per radio and the box may have several.
#:
#: **Positive on purpose.** This replaced a regex that BLACKLISTED "one tuner" and its
#: neighbours, which is the wrong shape of guard twice over. It missed every rewrite
#: that drops the number — "APRS logging reserves the tuner, so while it is logging
#: nothing else can be listened to" says something MORE wrong and matches nothing —
#: while failing correct sentences like "if the box has one radio this takes it; with
#: two, another stays free". A guard that fires on true prose and passes false prose
#: does not protect the claim, it selects for guard-shaped writing. Any new wording is
#: welcome here; what is not welcome is a description that says nothing about the
#: other radio.
_PER_RADIO = ("per radio", "another radio", "other radio", "another stays free")


def _flow(path: Path) -> str:
    """A description as the model reads it. The file is hand-wrapped; the model sees
    one run of prose — so match on that, and a rewrap is not a test failure."""
    return " ".join(path.read_text(encoding="utf-8").split())


def _purpose_labels() -> list[str]:
    """The phrases a sidecar refusal can name, read from the sidecar's own source.

    Parsed rather than imported: `deploy/sdr/` is not on the backend's path (it is the
    sidecar image), and hardcoding the list here would be the same drift one file over.
    """
    tree = ast.parse(_LISTEN_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "PURPOSE_LABEL" for t in node.targets):
            continue
        assert isinstance(node.value, ast.Dict)
        held = [v for v in node.value.values if isinstance(v, ast.Constant)]
        return [v.value for v in held if isinstance(v.value, str)]
    raise AssertionError(f"PURPOSE_LABEL is gone from {_LISTEN_PY}")


def test_every_tool_that_takes_a_radio_says_the_others_stay_free() -> None:
    """It has two, and the lease has been per radio since APRS_CONTROL_PLAN P0b.

    `sdr_listen` and `sdr_aprs_logging` both said otherwise for a release: the second
    one told the model "while it is logging nothing can be listened to" while its own
    handler, in `sdrtools.py`, returned "another dongle, if this box has one, is still
    free". What the model does with that is report a radio busy while the other one
    sits idle — so the claim these tools must CARRY is the per-radio one, and a
    description that has gone quiet about it is the drift worth failing on."""
    silent = []
    for name in ("sdr_listen", "sdr_aprs_logging"):
        said = _flow(_TOOLS / f"{name}.tool").lower()
        if not any(phrase in said for phrase in _PER_RADIO):
            silent.append(name)

    assert silent == [], f"tools that never tell the model the lease is per radio: {silent}"


def test_the_radio_module_docstring_says_it_too() -> None:
    """It is the file that does the per-radio resolving, so it was the sharpest case:
    the false sentence sat in the docstring of the module whose code contradicts it."""
    doc = (sdrtools.__doc__ or "").lower()

    assert any(phrase in doc for phrase in _PER_RADIO)
    assert "for_purpose" in doc  # the thing that actually decides which radio


def test_sdr_listen_names_every_job_a_refusal_can_name() -> None:
    """A refusal quotes the sidecar's own label, so a job the description has never
    heard of arrives as an unexplained sentence. `sdr_listen` is where the four are
    enumerated; when a fifth purpose lands in `listen.py`, this is what says so."""
    labels = _purpose_labels()
    described = _flow(_TOOLS / "sdr_listen.tool")

    assert len(labels) == len(set(labels)) >= 4
    missing = [label for label in labels if label not in described]
    assert missing == [], f"sdr_listen.tool never mentions: {missing}"


def test_the_radio_tools_agree_a_refusal_is_recoverable() -> None:
    """The three things true of every refusal: it names a radio, it names the job, and
    it is not an error — the owner may have another radio free, so retrying after they
    act is the point rather than a mistake."""
    for name in ("sdr_listen", "sdr_aprs_logging"):
        text = _flow(_TOOLS / f"{name}.tool")
        assert "refusal names the radio" in text, name
        assert "not an error" in text.lower(), name


def test_sdr_stop_says_it_only_stops_listening() -> None:
    """Its handler passes `session_id: None`, which the sidecar reads as the LISTENING
    session — never a service. The handler already degrades correctly; the description
    used to promise more than it does."""
    text = _flow(_TOOLS / "sdr_stop.tool")

    assert "listening only" in text
    assert "own switch" in text  # the answer the handler actually returns


# --- F10: the MEASUREMENT half ------------------------------------------------------


async def test_the_band_table_is_readable_without_taking_a_radio(tools) -> None:
    """It answers "what can this radio hear" before anything is tuned, so it must never
    reach the sidecar and never be refused for a busy radio."""
    touched: list[str] = []

    out = await tools(lambda p, _b: touched.append(p))["sdr_read"]({"what": "bands"}, None)

    assert touched == []
    assert "sections" in out
    # The fields a band plan cannot give: what mode lives there, and how it can be
    # watched. Guessing a frequency from memory gets the edges right and these wrong.
    assert "live=" in out


async def test_one_section_carries_its_named_channels(tools) -> None:
    """The whole table would be unreadable with every channel in it, and a model asking
    for a frequency needs exactly one section's worth."""
    out = await tools(lambda _p, _b: None)["sdr_read"]({"section": "2m-ssb"}, None)

    assert "2m-ssb" in out
    assert "144.1-144.3 MHz" in out


async def test_an_unknown_section_says_how_to_find_a_real_one(tools) -> None:
    out = await tools(lambda _p, _b: None)["sdr_read"]({"section": "no-such-band"}, None)

    assert "no-such-band" in out
    assert "whole table" in out


async def test_a_signal_reading_reports_the_MARGIN_not_the_raw_level(tools) -> None:
    """An absolute dBFS figure means little on this receiver — no calibrated gain, about
    seven effective bits — so what carries information is how far the peak stands over
    the frame's own floor. That is the standard `sweep.steady` is calibrated on."""
    body = {
        "engine": "iq",
        "frames": 96,
        "frame": {"floor_db": -60.1, "peak_db": -48.2, "peak_hz": 144_200_000.0},
    }

    out = await tools(lambda _p, _b: _ok(body))["sdr_signal"]({"frequency_mhz": 144.2}, None)

    assert "11.9 dB over" in out
    assert "-60.1 dBFS" in out
    assert "something is transmitting" in out


async def test_a_quiet_band_is_said_to_be_quiet_rather_than_reported_as_a_level(
    tools,
) -> None:
    """+6 dB is where the sweep detector found all 13 FM stations and nothing on a
    silent band. Under it, "the strongest bin was -58 dBFS" reads as a signal."""
    body = {
        "engine": "iq",
        "frames": 96,
        "frame": {"floor_db": -60.0, "peak_db": -57.0, "peak_hz": 146_000_000.0},
    }

    out = await tools(lambda _p, _b: _ok(body))["sdr_signal"]({"frequency_mhz": 146.0}, None)

    assert "nothing is standing out of the noise" in out


async def test_a_measurement_sends_the_capture_so_the_IQ_engine_answers(tools) -> None:
    """Without it the sidecar hops the span with `rtl_power`, which reports its OWN dB
    scale rather than dBFS — the exact confusion F9 exists to stop, arriving through
    the tool whose whole point is a real power figure."""
    seen: dict[str, Any] = {}

    def handler(path, body):
        seen.update({"path": path, **(body or {})})
        return _ok({"engine": "iq", "frames": 9, "frame": {"floor_db": -60.0, "peak_db": -50.0}})

    await tools(handler)["sdr_signal"]({"section": "2m-ssb"}, None)

    assert seen["path"] == "/spectrum/probe"
    assert seen["rate_hz"] == 1_024_000
    assert seen["bins"] == 4096
    assert seen["bin_hz"] == 250


async def test_a_measurement_cannot_hold_the_radio_for_minutes(tools) -> None:
    """`listen.py` carries a comment written about exactly this surface: an agent will
    ask for an hour, because nothing in its training says the radio is scarce. The
    sidecar's own 900 s ceiling is far too generous to be the only guard."""
    seen: dict[str, Any] = {}

    def handler(path, body):
        seen.update(body or {})
        return _ok({"engine": "iq", "frames": 9, "frame": {"floor_db": -60.0, "peak_db": -50.0}})

    await tools(handler)["sdr_signal"]({"frequency_mhz": 144.2, "seconds": 3600}, None)

    assert seen["seconds"] == sdrtools.MAX_SIGNAL_SECONDS


async def test_a_frame_with_no_floor_blames_the_antenna_not_the_band(tools) -> None:
    """ "Nothing is transmitting" and "nothing is reaching the ADC" need opposite
    actions, and only one of them is about the band."""
    body = {"engine": "iq", "frames": 96, "frame": {"peak_hz": 7_200_000.0}}

    out = await tools(lambda _p, _b: _ok(body))["sdr_signal"]({"frequency_mhz": 7.2}, None)

    assert "antenna or the input" in out
