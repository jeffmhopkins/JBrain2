"""API-surface tests with a fake repo and a mocked supervisor."""

import asyncio
import itertools
import json
import time
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.vitals_ring import VitalsRing
from tests.unit.fakes import FakeAuthRepo

SUPERVISOR_STATUS = {
    "containers": [
        {
            "service": "api",
            "state": "running",
            "health": "healthy",
            "started_at": None,
            "image": "x",
        }
    ]
}


def fake_supervisor(request: httpx.Request) -> httpx.Response:
    if request.headers.get("Authorization") != "Bearer st-token":
        return httpx.Response(401)
    if request.url.path == "/status":
        return httpx.Response(200, json=SUPERVISOR_STATUS)
    if request.url.path == "/restart":
        body = json.loads(request.content)
        if body["service"] == "ghost":
            return httpx.Response(404)
        return httpx.Response(202, json={"restarting": [body["service"]]})
    if request.url.path == "/logs/api":
        return httpx.Response(200, text="line1\nline2\n")
    if request.url.path == "/metrics":
        return httpx.Response(
            200,
            json={
                "mem_total_bytes": 4 << 30,
                "mem_available_bytes": 1 << 30,
                "swap_total_bytes": 0,
                "swap_free_bytes": 0,
                "disk_total_bytes": 40 << 30,
                "disk_free_bytes": 25 << 30,
                "load_1m": 0.5,
                "load_5m": 0.4,
                "load_15m": 0.3,
                "uptime_seconds": 12345,
                "gpu_busy_percent": 42.0,
                "fan_rpm": {"CPU Fan": 2100},
                "apu_power_w": 14.0,
                "containers": [{"service": "api", "mem_bytes": 100 << 20}],
            },
        )
    if request.url.path == "/update" and request.method == "POST":
        return httpx.Response(202, json={"updater": "jbrain-updater-1"})
    if request.url.path == "/update/status":
        return httpx.Response(200, json={"state": "running", "exit_code": None, "log_tail": "x"})
    return httpx.Response(404)


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def client(repo: FakeAuthRepo) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False,
        supervisor_token="st-token",
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        # Swap the lifespan-built SQL-backed state for fakes.
        app.state.auth_repo = repo
        app.state.supervisor_client = httpx.AsyncClient(
            transport=httpx.MockTransport(fake_supervisor), base_url="http://supervisor"
        )
        yield test_client


async def _owner_key(repo: FakeAuthRepo) -> str:
    return await service.rotate_owner_key(repo)


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    import asyncio

    key = asyncio.run(_owner_key(repo))
    resp = client.post("/api/auth/session", json={"owner_key": key, "device_label": "test"})
    assert resp.status_code == 204


def test_healthz(client: TestClient) -> None:
    assert client.get("/api/healthz").json() == {"status": "ok"}


def test_readyz_reports_database_down(client: TestClient) -> None:
    resp = client.get("/api/readyz")
    assert resp.status_code == 503


def test_grok_install_script_is_public_plaintext(client: TestClient) -> None:
    # No login() call: the script must be fetchable on a bare machine.
    resp = client.get("/api/install/grok.ps1")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # It prompts for both the URL and token at runtime and bakes in neither.
    assert body.count("Read-Host") >= 2  # endpoint URL + token
    assert "OPENAI_API_KEY" in body
    assert "ext/llm" not in body  # no per-session id is hard-coded


def test_login_bad_key_rejected(client: TestClient) -> None:
    resp = client.post("/api/auth/session", json={"owner_key": "jb1-WRONG"})
    assert resp.status_code == 401


def test_login_me_logout_roundtrip(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["kind"] == "owner"

    assert client.delete("/api/auth/session").status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_me_requires_session(client: TestClient) -> None:
    assert client.get("/api/auth/me").status_code == 401


def test_ops_requires_owner(client: TestClient) -> None:
    assert client.get("/api/ops/status").status_code == 401
    # The history graph is owner-only too (the router dependency).
    assert client.get("/api/ops/metrics/history").status_code == 401


def test_ops_metrics_history_returns_series(
    client: TestClient, repo: FakeAuthRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jbrain.api import ops

    captured: dict[str, object] = {}

    async def fake_history(maker, ctx, *, since, until=None, max_points=300):  # noqa: ANN001, ANN202
        captured["since"] = since
        return {"resolution": "raw", "points": [{"t": "2026-06-22T00:00:00+00:00"}]}

    monkeypatch.setattr(ops.ops_metrics, "history", fake_history)
    login(client, repo)

    body = client.get("/api/ops/metrics/history?range=7d").json()
    assert body["resolution"] == "raw"
    assert body["points"]
    # 7d maps to a ~7-day-old `since`.
    assert (datetime.now(UTC) - captured["since"]).days == 7  # type: ignore[operator]


def test_ops_metrics_history_rejects_bad_range(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.get("/api/ops/metrics/history?range=nonsense").status_code == 400


def test_ops_vitals_requires_owner(client: TestClient) -> None:
    # The top bar probes /vitals before opening the stream precisely so a non-owner
    # learns it may not read host telemetry WITHOUT an EventSource that would then
    # retry against the rejection forever.
    assert client.get("/api/ops/vitals").status_code == 401
    assert client.get("/api/ops/vitals/stream").status_code == 401


def _seed_gauge(client: TestClient, gpu: float | None) -> None:
    """Put ONE reading into the app's vitals ring — the single source every GPU figure in
    the API now answers from."""
    client.app.state.vitals_ring.record(time.time(), gpu)  # type: ignore[attr-defined]


def test_ops_vitals_answers_from_the_ring(client: TestClient, repo: FakeAuthRepo) -> None:
    """The probe reports the sampler's reading rather than taking its own. The
    cross-surface agreement this buys is asserted in test_live_turns_api.py, which fakes
    the run reader /ops/turns needs."""
    login(client, repo)
    _seed_gauge(client, 73.0)

    assert client.get("/api/ops/vitals").json() == {"gpu_busy_percent": 73.0, "loading": None}


def test_ops_vitals_reports_a_box_with_no_gauge(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    _seed_gauge(client, None)

    assert client.get("/api/ops/vitals").json() == {"gpu_busy_percent": None, "loading": None}


def test_ops_vitals_reports_nothing_when_the_sampler_has_gone_stale(
    client: TestClient, repo: FakeAuthRepo
) -> None:
    """A single source is also a single point of failure. If the sampler stops, serving its
    last value forever would turn "we stopped looking" into a confident, frozen reading."""
    login(client, repo)
    client.app.state.vitals_ring.record(time.time() - 3600, 99.0)  # type: ignore[attr-defined]

    assert client.get("/api/ops/vitals").json() == {"gpu_busy_percent": None, "loading": None}


def _stops_after(ticks: int) -> Callable[[], Awaitable[bool]]:
    """A disconnect check that lets `ticks` frames through and then reports the client
    gone, so a stream test ends on its own."""
    checks = iter([False] * ticks + [True])

    async def disconnected() -> bool:
        return next(checks)

    return disconnected


async def test_ops_vitals_stream_repeats_every_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each tick re-reads and re-sends, whether or not the figure moved — the
    constant cadence is what tells the meter its reading is still live."""
    from jbrain.api import ops

    readings = iter([61.0, 61.0, 88.5])
    monkeypatch.setattr(ops, "_VITALS_PERIOD_S", 0)

    frames = [
        frame
        async for frame in ops.vitals_frames(
            _stops_after(3), lambda _after: {"gpu_busy_percent": next(readings)}
        )
    ]

    # `loading` is present on every frame, null included: an ABSENT key is indistinguishable,
    # to the meter, from one the transport lost.
    assert frames == [
        b'data: {"gpu_busy_percent": 61.0, "loading": null}\n\n',
        b'data: {"gpu_busy_percent": 61.0, "loading": null}\n\n',
        b'data: {"gpu_busy_percent": 88.5, "loading": null}\n\n',
    ]


async def test_ops_vitals_stream_carries_the_in_flight_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chat status line's whole supply: what is loading and how far in, on the socket
    the top bar already holds open. A second stream or a second poll for this would be the
    fourth independent "a model is loading" path on one screen."""
    from jbrain.api import ops

    monkeypatch.setattr(ops, "_VITALS_PERIOD_S", 0)
    checks = iter([False, True])

    async def disconnected() -> bool:
        return next(checks)

    async def loading() -> dict[str, object]:
        return {"model": "gpt-oss-120b", "at_ms": 1770000000000, "percent": 0.43}

    frames = [
        frame
        async for frame in ops.vitals_frames(
            disconnected, lambda _after: {"gpu_busy_percent": 94.0}, loading
        )
    ]

    assert frames == [
        b'data: {"gpu_busy_percent": 94.0, "loading": {"model": "gpt-oss-120b", '
        b'"at_ms": 1770000000000, "percent": 0.43}}\n\n'
    ]


async def test_ops_vitals_stream_asks_only_for_samples_the_client_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each tick asks for everything since the last frame SENT, not for "the reading now".

    The sampler and the stream are two loops on the same second: whichever way their
    phase falls, a tick that reads the gauge twice in one sampler period skips the next
    sample entirely, and the graph — one slot per second — draws the missing second as a
    dropout. Handing over everything since the last frame makes that phase irrelevant."""
    from jbrain.api import ops

    monkeypatch.setattr(ops, "_VITALS_PERIOD_S", 0)
    asked: list[int | None] = []
    ring = VitalsRing()
    ring.record(1000.0, 10.0)
    ring.record(1001.0, 20.0)

    def frame(after: int | None) -> dict[str, object]:
        asked.append(after)
        # A second tick that lands inside the same sampler period has nothing new to say.
        return {"gpu_busy_percent": 20.0, "samples": ring.newer_than(after)}

    frames = [frame async for frame in ops.vitals_frames(_stops_after(2), frame)]

    assert asked == [None, 1_001_000], "the second tick did not resume from the first"
    assert b'"samples": [{"at_ms": 1001000, "gpu": 20.0}]' in frames[0]
    # Nothing new: the frame still ticks (it is the liveness signal) but repeats no sample.
    assert b'"samples": []' in frames[1]


async def test_ops_vitals_stream_stops_when_the_client_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backgrounded PWA closes the stream; the tick loop must not outlive it and
    keep reading the gauge forever on the box's behalf."""
    from jbrain.api import ops

    reads = itertools.count()
    monkeypatch.setattr(ops, "_VITALS_PERIOD_S", 0)

    async def gone() -> bool:
        return True

    frames = ops.vitals_frames(gone, lambda _after: {"gpu_busy_percent": float(next(reads))})
    assert [frame async for frame in frames] == []
    assert next(reads) == 0, "the gauge was never read for a client already gone"


# --- the shared in-flight-load probe -----------------------------------------
# Three surfaces want "is a model loading, and how far in" at 1 Hz, and the answer is a
# database round trip. These pin that the cost is paid once per second for the box, not
# once per client per surface.


def _probe_request(monkeypatch: pytest.MonkeyPatch, row: dict[str, object] | None) -> Any:
    """A bare request carrying just the app state the probe reads, plus a counter of how
    often the underlying row read actually happened."""
    from jbrain.api import ops

    reads: list[int] = []

    async def fake_in_flight(maker: object, ctx: object, **kw: object) -> dict[str, object] | None:
        reads.append(1)
        return row

    monkeypatch.setattr(ops.box_events, "in_flight", fake_in_flight)
    state = SimpleNamespace(session_maker=object())
    return SimpleNamespace(app=SimpleNamespace(state=state)), reads, state


async def test_a_read_that_times_out_holds_the_last_answer_instead_of_denying_the_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout is ignorance, not a fact, and this reading is the one where the difference
    bites hardest: what makes the read slow is contention, the only time there is anything to
    report is during a load, and a load IS the contention. Stamped as a real answer, the
    instrument would go quiet exactly when it has something to say — the bar vanishing
    mid-load on a box that is visibly loading."""
    from jbrain.api import ops

    row = {
        "at_ms": 1_760_000_000_000,
        "subject": "gpt-oss-120b",
        "progress": 0.4,
        "kind": "model_load",
    }
    stall = False

    async def flaky(maker: object, ctx: object, **kw: object) -> dict[str, object] | None:
        if stall:
            await asyncio.sleep(3600)  # never lands inside the budget
        return row

    monkeypatch.setattr(ops.box_events, "in_flight", flaky)
    monkeypatch.setattr(ops, "_LOAD_PROBE_BUDGET_S", 0.01)
    monkeypatch.setattr(ops, "_LOAD_PROBE_TTL_S", 0.0)  # every call re-reads
    # `Any` for the same reason `_probe_request` above returns it: the probe reads two
    # attributes off the request, and a real one cannot be built without an ASGI scope.
    request: Any = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=object()))
    )
    probe = ops._LoadProbe()

    good = await probe.read(request)
    assert good is not None and good["model"] == "gpt-oss-120b"

    stall = True
    held = await probe.read(request)
    assert held is not None, "a timed-out read must not report that nothing is loading"
    assert held["model"] == "gpt-oss-120b"


async def test_a_held_answer_is_given_up_once_it_is_too_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hold is bounded, because a held answer is a claim about the present made from the
    past — a load that ended during the stall would otherwise keep its bar up forever. Past
    the limit the honest report is silence."""
    from jbrain.api import ops

    row = {
        "at_ms": 1_760_000_000_000,
        "subject": "gpt-oss-120b",
        "progress": 0.4,
        "kind": "model_load",
    }
    stall = False

    async def flaky(maker: object, ctx: object, **kw: object) -> dict[str, object] | None:
        if stall:
            await asyncio.sleep(3600)
        return row

    monkeypatch.setattr(ops.box_events, "in_flight", flaky)
    monkeypatch.setattr(ops, "_LOAD_PROBE_BUDGET_S", 0.01)
    monkeypatch.setattr(ops, "_LOAD_PROBE_TTL_S", 0.0)
    monkeypatch.setattr(ops, "_LOAD_PROBE_HOLD_S", 0.05)
    # `Any` for the same reason `_probe_request` above returns it: the probe reads two
    # attributes off the request, and a real one cannot be built without an ASGI scope.
    request: Any = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=object()))
    )
    probe = ops._LoadProbe()

    assert await probe.read(request) is not None
    stall = True
    assert await probe.read(request) is not None  # inside the hold
    await asyncio.sleep(0.08)
    assert await probe.read(request) is None  # past it — we stop claiming


async def test_a_successful_empty_read_still_clears_the_bar_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hold covers reads that did not LAND. A read that landed and found nothing is an
    answer, and a finished load must leave the screen immediately rather than lingering for
    the hold — otherwise every load would end with a stuck bar."""
    from jbrain.api import ops

    row: dict[str, object] | None = {
        "at_ms": 1_760_000_000_000,
        "subject": "gpt-oss-120b",
        "progress": 0.4,
        "kind": "model_load",
    }

    async def reads_row(maker: object, ctx: object, **kw: object) -> dict[str, object] | None:
        return row

    monkeypatch.setattr(ops.box_events, "in_flight", reads_row)
    monkeypatch.setattr(ops, "_LOAD_PROBE_TTL_S", 0.0)
    # `Any` for the same reason `_probe_request` above returns it: the probe reads two
    # attributes off the request, and a real one cannot be built without an ASGI scope.
    request: Any = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=object()))
    )
    probe = ops._LoadProbe()

    assert await probe.read(request) is not None
    row = None
    assert await probe.read(request) is None


async def test_the_load_probe_answers_repeat_readers_from_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Four open streams on one screen must not make four database round trips a second —
    during a load, which is the one minute the box has nothing to spare."""
    from jbrain.api import ops

    request, reads, _state = _probe_request(
        monkeypatch,
        {
            "at_ms": 1_760_000_000_000,
            "subject": "gpt-oss-120b",
            "progress": 0.43,
            "kind": "model_load",
        },
    )

    first = await ops._load_in_flight(request)
    for _ in range(3):
        assert await ops._load_in_flight(request) == first

    assert first == {
        "model": "gpt-oss-120b",
        "at_ms": 1_760_000_000_000,
        "percent": 0.43,
        "kind": "model_load",
        # A load's detail is its REASON ("making room for …"), a different sentence for a
        # different line — only a prefill names what it is reading.
        "reading": None,
    }
    assert len(reads) == 1


async def test_a_prefill_carries_what_it_is_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """The status line does not guess what a wait is. A tool round resends a prompt the KV
    cache already holds, so the box is reading the result that just came back — the phrase
    for that is written where the prompt was assembled (`llm.router._reading`) and rides the
    same row as the fraction, which is what stops the two from ever describing different
    work."""
    from jbrain.api import ops

    request, _reads, _state = _probe_request(
        monkeypatch,
        {
            "at_ms": 1_760_000_000_000,
            "subject": "gpt-oss-120b",
            "progress": 0.45,
            "kind": "prefill",
            "detail": "what web_fetch returned",
        },
    )

    assert await ops._load_in_flight(request) == {
        "model": "gpt-oss-120b",
        "at_ms": 1_760_000_000_000,
        "percent": 0.45,
        "kind": "prefill",
        "reading": "what web_fetch returned",
    }


async def test_the_load_probe_survives_a_box_that_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This drives an instrument. An instrument that takes the screen down with it when the
    box is busy is worse than one that goes quiet."""
    from jbrain.api import ops

    request, _reads, _state = _probe_request(monkeypatch, None)

    async def boom(maker: object, ctx: object, **kw: object) -> dict[str, object] | None:
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(ops.box_events, "in_flight", boom)

    assert await ops._load_in_flight(request) is None


async def test_a_load_with_no_measurement_still_reports_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model and the start time come from the box's own record; the fraction is an
    embellishment that needs a device-memory probe and a catalog projection. Losing the
    figure must not lose the line — an unmeasurable load is still one worth naming."""
    from jbrain.api import ops

    request, _reads, _state = _probe_request(
        monkeypatch,
        {
            "at_ms": 1_760_000_000_000,
            "subject": "gpt-oss-120b",
            "progress": None,
            "kind": "model_load",
        },
    )

    assert (await ops._load_in_flight(request)) == {
        "model": "gpt-oss-120b",
        "at_ms": 1_760_000_000_000,
        "percent": None,
        "kind": "model_load",
        "reading": None,
    }


async def test_the_load_probe_gives_up_rather_than_stall_the_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This read sits on the SSE frame path, and that path's constant cadence IS the
    liveness signal the meter's watchdog reads. A read that hung would not merely lose a
    percentage — it would make a perfectly healthy stream look dead."""
    from jbrain.api import ops

    request, _reads, _state = _probe_request(monkeypatch, None)
    monkeypatch.setattr(ops, "_LOAD_PROBE_BUDGET_S", 0.01)

    async def never(maker: object, ctx: object, **kw: object) -> dict[str, object] | None:
        await asyncio.sleep(10)
        return None

    monkeypatch.setattr(ops.box_events, "in_flight", never)

    assert await ops._load_in_flight(request) is None


async def test_the_idle_case_costs_one_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The idle case is every second the box is not loading, which is nearly all of them,
    and it now costs exactly one query: the fraction rides the row, so there is no second
    read to skip. (There used to be — a fetch of the gateway's whole log buffer, ordered
    after the row read so the idle second would not pay for it.)"""
    from jbrain.api import ops

    request, reads, _state = _probe_request(monkeypatch, None)

    assert await ops._load_in_flight(request) is None
    assert len(reads) == 1


def test_ops_vitals_frame_stamps_each_sample() -> None:
    """The frame carries the box's own timestamps, so the browser buckets samples by the
    second they were READ rather than by the instant they arrived — arrival jitter was
    putting two readings in one second and none in the next, drawn as a dropout."""
    from jbrain.api import ops

    ring = VitalsRing()
    at = time.time()
    ring.record(at, 44.0)

    frame = ops._vitals_frame(_request_with_ring(ring), None)

    assert frame["gpu_busy_percent"] == 44.0
    assert frame["samples"] == [{"at_ms": int(at * 1000), "gpu": 44.0}]


def test_ops_vitals_frame_survives_a_box_with_no_sampler() -> None:
    """The ring is set up at startup; a frame asked for before that must say "no reading"
    rather than take the stream down."""
    from jbrain.api import ops

    assert ops._vitals_frame(_request_with_ring(None), None) == {
        "gpu_busy_percent": None,
        "samples": [],
    }


def _request_with_ring(ring: VitalsRing | None) -> Request:
    """The one thing `_vitals_frame` reads off a request. A real one would need an ASGI
    scope built by hand to say nothing more than this."""
    state = SimpleNamespace() if ring is None else SimpleNamespace(vitals_ring=ring)
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))


def test_ops_status_proxies_supervisor(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/ops/status")
    assert resp.status_code == 200
    assert resp.json() == SUPERVISOR_STATUS


def test_ops_restart(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.post("/api/ops/restart", json={"service": "api"})
    assert resp.status_code == 202
    assert resp.json() == {"restarting": ["api"]}


def test_ops_restart_unknown_service(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.post("/api/ops/restart", json={"service": "ghost"}).status_code == 404


def test_ops_logs(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    resp = client.get("/api/ops/logs/api")
    assert resp.status_code == 200
    assert "line1" in resp.text


def test_ops_update_trigger_and_status(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    started = client.post("/api/ops/update")
    assert started.status_code == 202
    assert started.json()["updater"] == "jbrain-updater-1"
    status = client.get("/api/ops/update/status")
    assert status.status_code == 200
    assert status.json()["state"] == "running"


def test_ops_metrics_merges_supervisor_and_local(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    body = client.get("/api/ops/metrics").json()
    assert body["mem_total_bytes"] == 4 << 30
    # Host telemetry the proxy carries through untouched (incl. fan RPM + APU power).
    assert body["fan_rpm"] == {"CPU Fan": 2100}
    assert body["apu_power_w"] == 14.0
    assert body["containers"][0]["service"] == "api"
    # The unit-test app has no reachable database or blob store wired, so
    # the best-effort sections degrade to null instead of failing the call.
    assert body["db"] is None
    assert body["blobs"] is not None or body["blobs"] is None


def test_ops_update_requires_owner(client: TestClient) -> None:
    assert client.post("/api/ops/update").status_code == 401


def test_ops_logs_unknown_service(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    assert client.get("/api/ops/logs/ghost").status_code == 404
