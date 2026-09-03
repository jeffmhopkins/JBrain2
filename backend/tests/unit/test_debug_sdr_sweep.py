"""The debug band-sweep route: a measuring instrument, not an agent tool.

The thresholds a detector needs are per-BOX facts — this antenna's noise floor, this
tuner's spurs — that nothing documents, which is the same reason `/grounding` exists for
a vision model's coordinate base. So the first thing built is the thing that MEASURES,
and what is pinned here is that it reports honestly: a refused sweep, a sweep that
returned nothing, and a partial one all have to be distinguishable from a quiet band.
"""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from jbrain.api import debug


class _Settings:
    def __init__(self, url: str | None = "http://sdr:8000") -> None:
        self.sdr_url = url


def _request() -> Request:
    app = FastAPI()
    app.state.debug_jobs = {}
    app.state.debug_job_tasks = set()
    return Request({"type": "http", "app": app, "headers": [], "method": "POST", "path": "/"})


def _row(when: str, *values: float) -> str:
    return f"2026-09-03, {when}, 144000000, 144020000, 5000, 12, " + ", ".join(
        f"{v:.1f}" for v in values
    )


QUIET_CSV = "\n".join(_row(f"15:00:0{i}", -98, -98, -98, -98) for i in range(8))
BUSY_CSV = "\n".join(
    _row(f"15:00:0{i}", -98, (-70 if i in (2, 3, 4, 5) else -98), -98, -98) for i in range(8)
)


async def _sweep(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int = 200,
    payload: dict[str, Any] | None = None,
    url: str | None = "http://sdr:8000",
    **kwargs: Any,
) -> tuple[Request, str]:
    """Submit the job and drain it, returning the request so the caller can read it."""

    async def fake_post(self: Any, path: str, **_k: Any) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(
            status,
            json=payload if payload is not None else {"csv": QUIET_CSV, "complete": True},
            request=httpx.Request("POST", f"http://sdr:8000{path}"),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    request = _request()
    submitted = await debug.sdr_sweep(
        request,
        cast(Any, _Settings(url)),
        cast(Any, object()),
        start_mhz=kwargs.pop("start_mhz", 144.0),
        stop_mhz=kwargs.pop("stop_mhz", 148.0),
        **kwargs,
    )
    for task in list(request.app.state.debug_job_tasks):
        await task
    return request, submitted.job_id


async def test_a_sweep_comes_back_as_a_job_with_an_image_and_a_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred because it has to be: five minutes will not survive the tunnel's ~100 s
    edge limit, which is the same reason `/complete-async` exists."""
    request, job_id = await _sweep(monkeypatch, payload={"csv": BUSY_CSV, "complete": True})

    job = request.app.state.debug_jobs[job_id]
    assert job["status"] == "done"
    out = cast(debug.SdrSweepOut, job["result"])
    assert out.rows == 8
    assert out.bins == 4
    assert [b.hz for b in out.busy] == [144_005_000]
    # The image is for the OWNER; the table is for everything else. At five minutes a
    # waterfall is ~2 s per row, so a burst is several rows tall — the argument that
    # kills the image only applies at a day.
    assert out.png_base64.startswith("iVBOR")


async def test_a_quiet_band_reports_a_floor_and_no_stations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, job_id = await _sweep(monkeypatch)

    out = cast(debug.SdrSweepOut, request.app.state.debug_jobs[job_id]["result"])
    assert out.busy == []
    assert out.floor_db == pytest.approx(-98.0)
    # And it still says how much it measured, so "quiet" is distinguishable from "the
    # sweep never ran" — which is the whole failure this route exists to avoid.
    assert out.rows == 8 and out.csv_chars > 0


async def test_a_sweep_that_returned_nothing_is_not_a_quiet_band(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dead antenna, a wedged rtl_power and a genuinely silent band all produce zero
    # busy channels. `rows` and `csv_chars` are what separate them.
    request, job_id = await _sweep(monkeypatch, payload={"csv": "", "complete": True})

    out = cast(debug.SdrSweepOut, request.app.state.debug_jobs[job_id]["result"])
    assert out.rows == 0 and out.csv_chars == 0
    assert out.png_base64 == ""


async def test_a_partial_sweep_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    request, job_id = await _sweep(monkeypatch, payload={"csv": QUIET_CSV, "complete": False})

    out = cast(debug.SdrSweepOut, request.app.state.debug_jobs[job_id]["result"])
    assert out.complete is False


async def test_a_busy_radio_surfaces_as_the_job_s_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep takes the radio, so APRS logging refuses it — and the reason has to reach
    the caller, because the answer the owner needs differs by which job holds it."""
    request, job_id = await _sweep(
        monkeypatch, status=409, payload={"detail": "the radio is already logging APRS"}
    )

    job = request.app.state.debug_jobs[job_id]
    assert job["status"] == "error"
    assert "logging APRS" in cast(str, job["error"])


async def test_a_box_with_no_radio_says_so_rather_than_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(HTTPException) as refused:
        await _sweep(monkeypatch, url=None)

    assert refused.value.status_code == 503


async def test_the_sidecar_falling_over_does_not_crash_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(self: Any, path: str, **_k: Any) -> httpx.Response:  # noqa: ARG001
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(httpx.AsyncClient, "post", boom)
    request = _request()
    submitted = await debug.sdr_sweep(
        request, cast(Any, _Settings()), cast(Any, object()), start_mhz=144.0, stop_mhz=148.0
    )
    for task in list(request.app.state.debug_job_tasks):
        await task

    job = request.app.state.debug_jobs[submitted.job_id]
    assert job["status"] == "error"
    assert "no route" in cast(str, job["error"])


async def test_channel_folding_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 16 kHz signal in a 5 kHz sweep lights several adjacent bins. Folding is the
    # caller's call because the spacing is a band-plan fact, and regionally variable.
    # A realistic duty cycle. A bin busy MORE than ~75% of the window becomes its own
    # floor and drops out of `busy` entirely — see `steady`, and the test below.
    csv = "\n".join(
        _row(f"15:00:0{i}", -98, (-70 if i in (3, 4) else -98), (-74 if i in (3, 4) else -98), -98)
        for i in range(8)
    )
    request, job_id = await _sweep(
        monkeypatch, payload={"csv": csv, "complete": True}, channel_khz=15.0
    )

    out = cast(debug.SdrSweepOut, request.app.state.debug_jobs[job_id]["result"])
    assert len(out.busy) == 1


def test_the_job_result_can_actually_hold_a_sweep() -> None:
    """`/jobs/{id}` validates its result against a union. A sweep missing from it comes
    back as a null result — a job that says "done" and carries nothing."""
    assert "SdrSweepOut" in str(debug.JobStatusOut.model_fields["result"].annotation)


async def test_a_bin_that_never_goes_quiet_is_reported_not_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honest limit of a per-bin floor, made visible.

    A constant emitter becomes its OWN floor, so occupancy says it is never busy. That
    is exactly right for the tuner's DC spike and its spurs, and exactly wrong for a
    repeater holding a carrier all window — and occupancy statistics cannot tell those
    apart, because they are the same measurement. Dropping them silently would report a
    permanently-keyed channel as a quiet one."""
    csv = "\n".join(_row(f"15:00:0{i}", -98, -70, -98, -98) for i in range(8))

    request, job_id = await _sweep(monkeypatch, payload={"csv": csv, "complete": True})

    out = cast(debug.SdrSweepOut, request.app.state.debug_jobs[job_id]["result"])
    assert out.busy == []
    assert [b.hz for b in out.steady] == [144_005_000]


async def test_the_raw_numbers_are_retrievable_but_not_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calibrating a detector against the summary that detector produced is circular.

    The first pass of thresholds here was set by reading brightness off the waterfall
    PNG, because the route returned `csv_chars` and not the CSV. That is not a
    measurement. It stays off by default — it is megabytes, and it dwarfs the eight
    lines that are the reading — but it has to be gettable."""
    request, job_id = await _sweep(monkeypatch)
    out = cast(debug.SdrSweepOut, request.app.state.debug_jobs[job_id]["result"])
    assert out.csv is None and out.csv_chars > 0

    request, job_id = await _sweep(monkeypatch, include_csv=True)
    out = cast(debug.SdrSweepOut, request.app.state.debug_jobs[job_id]["result"])
    assert out.csv == QUIET_CSV


async def test_a_retune_seam_is_reported_as_unmeasured_not_as_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEASURED 2026-09-03: a 144-148 sweep of this box left 145.872-146.206 unswept —
    a 342 kHz hole at the retune seam, sitting straight across live repeater channels.
    Reported as "nothing busy there" it reads as a quiet band, which inverts what it
    means. Silence is only evidence where the receiver was listening."""
    csv = "\n".join(
        [
            *(
                f"2026-09-03, 15:00:0{i}, 144000000, 144010000, 5000, 12, -98.0, -98.0"
                for i in range(4)
            ),
            *(
                f"2026-09-03, 15:00:0{i}, 144100000, 144110000, 5000, 12, -98.0, -98.0"
                for i in range(4)
            ),
        ]
    )

    request, job_id = await _sweep(monkeypatch, payload={"csv": csv, "complete": True})

    out = cast(debug.SdrSweepOut, request.app.state.debug_jobs[job_id]["result"])
    assert out.busy == []
    assert [(g.start_mhz, g.stop_mhz) for g in out.uncovered] == [(144.01, 144.1)]
    assert out.uncovered[0].khz == pytest.approx(90.0)


async def test_a_sweep_with_no_seams_claims_no_holes(monkeypatch: pytest.MonkeyPatch) -> None:
    request, job_id = await _sweep(monkeypatch)

    out = cast(debug.SdrSweepOut, request.app.state.debug_jobs[job_id]["result"])
    assert out.uncovered == []
