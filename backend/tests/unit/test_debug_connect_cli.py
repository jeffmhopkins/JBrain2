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


class _Handler(BaseHTTPRequestHandler):
    """Answers every path with the status encoded in it, so one server covers both cases."""

    def _respond(self) -> None:
        status = 422 if "refuse" in self.path else 200
        payload: dict[str, object] = (
            {"detail": "unknown provider: local:x"} if status == 422 else {"git_sha": "abc123"}
        )
        if status == 200 and self.command == "POST":
            # Echo the request body back, so a test can assert what the script actually SENT
            # rather than only that it exited zero.
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode() if length else "{}"
            payload = {"sent": json.loads(raw or "{}")}
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


# --- the jmolt simulator verb (JMOLT_LEDGER_ENGINE_PLAN.md, S1) --------------


@pytest.mark.skipif(not _SCRIPT.exists(), reason="the console script is not in this checkout")
def test_a_baseline_run_does_not_blank_the_owners_advisory_note(box: str) -> None:
    """The trap this covers: `advisory` absent means "whatever the box's note says", and
    `advisory: ""` means "run with no note". A CLI that always sent a value would turn every
    baseline into a silently different arm, and the difference would be invisible in the
    result — it would just look like the box behaves differently than it does."""
    sent = json.loads(_run(box, "sim", "run", "cid-1", "--nights", "5").stdout)["sent"]
    assert "advisory" not in sent
    assert sent == {"corpus_id": "cid-1", "nights": 5, "label": "arm"}

    with_note = json.loads(_run(box, "sim", "run", "cid-1", "--advisory", "").stdout)["sent"]
    assert with_note["advisory"] == ""  # deliberately no note, which is a different arm


@pytest.mark.skipif(not _SCRIPT.exists(), reason="the console script is not in this checkout")
def test_the_sim_verb_json_escapes_what_it_is_given(box: str) -> None:
    """Notes and labels are free text the operator types. Built by string interpolation, a
    quote in one would produce a body the box rejects — or worse, a different body."""
    note = 'the "2026-08-29" night \\ with quotes'
    sent = json.loads(_run(box, "sim", "harvest", note).stdout)["sent"]
    assert sent == {"note": note}


@pytest.mark.skipif(not _SCRIPT.exists(), reason="the console script is not in this checkout")
def test_an_unknown_sim_subcommand_is_a_failure_not_a_no_op(box: str) -> None:
    result = _run(box, "sim", "nonsense")
    assert result.returncode != 0
    assert "harvest" in result.stderr  # and it says what the options are
