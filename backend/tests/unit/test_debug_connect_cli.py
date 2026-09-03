"""`scripts/debug-connect.sh` must report a refusal as a failure.

This is a safety test, not a tidiness one. The script drives a LIVE box: `llm-set` re-points
which model serves a task, and the very next `complete` LOADS whatever it now resolves to. So
a control call that quietly does nothing leaves the caller acting on a false premise, and on
this hardware the premise is "nothing else is about to be pulled into memory".

MEASURED 2026-08-21: `llm-set agent.turn local:gpt-oss-120b` returns 422 "unknown provider"
— the ids are bare, with no `local:` prefix, and the runbook's own example carried the wrong
form. The script printed the refusal and exited 0, so a guarded `llm-set … || exit` sailed
past it. Twice, a 120B was then loaded on top of an already-resident model, on a box whose
documented failure mode is a reclaim livelock needing a power cycle.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
from base64 import urlsafe_b64encode
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "debug-connect.sh"

# The smallest valid PNG: a 1x1 image, so the stub returns something a decoder accepts.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAA"
    "DUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _Handler(BaseHTTPRequestHandler):
    """Answers every path with the status encoded in it, so one server covers both cases."""

    def _respond(self) -> None:
        status = 422 if "refuse" in self.path else 200
        if "/sdr/sweep" in self.path:
            payload: dict[str, object] = {"job_id": "sweep-1"}
        elif "/jobs/" in self.path:
            # Done on the first poll, so the test does not sit through a sleep.
            payload = {
                "job_id": "sweep-1",
                "status": "done",
                "result": {
                    "rows": 8,
                    "busy": [],
                    "floor_db": -98.0,
                    # A one-pixel PNG and two CSV lines stand in for the megabytes the
                    # real route returns; what matters is that neither reaches stdout.
                    "png_base64": _PNG_B64,
                    "csv": "2026-09-03, 15:00:00, 144000000, 144005000, 5000, 12, -71.2\n",
                },
            }
        elif status == 422:
            payload = {"detail": "unknown provider: local:x"}
        else:
            payload = {"git_sha": "abc123"}
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _respond
    do_PUT = _respond
    do_POST = _respond

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 — base signature
        """Silenced: the handler's default writes a line per request to stderr, which would
        bury the assertion output this test exists to read."""
        return


@pytest.fixture
def box() -> object:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _token(base: str) -> str:
    """The payload shape the script decodes: base64url(JSON{v, u, k})."""
    raw = json.dumps({"v": 1, "u": base, "k": "test-key"}).encode()
    return urlsafe_b64encode(raw).decode().rstrip("=")


def _run(base: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), "--token", _token(base), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.skipif(not _SCRIPT.exists(), reason="the console script is not in this checkout")
def test_a_refused_call_exits_non_zero(box: str) -> None:
    # `raw` reaches an arbitrary path, which is how one stub server serves both cases.
    result = _run(box, "raw", "GET", "/api/debug/refuse")
    assert result.returncode != 0, "a 422 exited 0 — a guarded call would sail past it"
    # The detail still reaches the operator: it is the most useful thing on the screen, and
    # the point is to add a failure signal, not to swallow the explanation.
    assert "unknown provider" in result.stdout
    assert "422" in result.stderr


@pytest.mark.skipif(not _SCRIPT.exists(), reason="the console script is not in this checkout")
def test_a_successful_call_still_exits_zero_and_prints_only_the_body(box: str) -> None:
    result = _run(box, "raw", "GET", "/api/debug/version")
    assert result.returncode == 0, result.stderr
    # The status code rides on curl's `-w` and must be stripped back off, or every consumer
    # that parses this output as JSON breaks.
    assert json.loads(result.stdout)["git_sha"] == "abc123"


@pytest.mark.skipif(not _SCRIPT.exists(), reason="the console script is not in this checkout")
def test_an_unreachable_box_exits_non_zero(box: str) -> None:
    # Port 1 is reserved and refuses immediately: the network-level failure has to be as loud
    # as the HTTP-level one, since "the box did not answer" and "the box said no" are equally
    # bad premises to keep working from.
    result = _run("http://127.0.0.1:1", "raw", "GET", "/api/debug/version")
    assert result.returncode != 0


@pytest.mark.skipif(not _SCRIPT.exists(), reason="the console script is not in this checkout")
def test_sweep_submits_and_then_polls_to_the_result(box: str) -> None:
    """The sweep verb is two calls, not one.

    A five-minute sweep cannot be held open through the tunnel, so the route returns a
    job id and the CLI has to poll. A verb that printed the id and stopped would leave
    the operator — who has no terminal — holding a handle with no way to redeem it."""
    result = _run(box, "sweep", "144", "148", "--seconds", "2")

    assert result.returncode == 0, result.stderr
    # What comes back is the RESULT, not the submission receipt.
    assert json.loads(result.stdout)["status"] == "done"
    assert json.loads(result.stdout)["result"]["rows"] == 8


@pytest.mark.skipif(not _SCRIPT.exists(), reason="the console script is not in this checkout")
def test_sweep_writes_the_waterfall_and_the_raw_numbers_to_files(
    box: str, tmp_path: pathlib.Path
) -> None:
    """The two big blobs go to disk; the reading goes to the screen.

    A base64 PNG of a five-minute sweep is megabytes, and printed inline it buries the
    eight lines that are the actual result. The CSV has to be RETRIEVABLE, though —
    calibrating the detector against the detector's own summary is circular, and the
    first pass of thresholds was set by eyeballing brightness off the PNG."""
    result = _run(box, "sweep", "144", "148", "--seconds", "2", "--csv", "--out", str(tmp_path))

    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)["result"]
    # Named, not embedded.
    assert "png_base64" not in doc
    assert "csv" not in doc
    png, csv = pathlib.Path(doc["png_file"]), pathlib.Path(doc["csv_file"])
    assert png.read_bytes().startswith(b"\x89PNG")
    assert "144000000" in csv.read_text()
    # And the rest of the reading survived the split.
    assert doc["rows"] == 8


@pytest.mark.skipif(not _SCRIPT.exists(), reason="the console script is not in this checkout")
def test_sweep_can_hand_back_the_job_id_without_waiting(box: str) -> None:
    result = _run(box, "sweep", "144", "148", "--no-wait")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sweep-1"
