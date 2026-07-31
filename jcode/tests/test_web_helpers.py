"""The web-search / web-fetch shell helpers the sandbox CLIs call: the JCODE_INTERNET_SEARCH
gate, and the request they POST to the api bridge. A localhost stub stands in for the api
(no external network) — docs/plans/JCODE_GROK_INTERNET_PLAN.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HELPERS = Path(__file__).resolve().parents[1]
_WEB_SEARCH = _HELPERS / "web-search"
_WEB_FETCH = _HELPERS / "web-fetch"


class _Recorder:
    """Captures the one request the helper makes and replies with canned text."""

    def __init__(self) -> None:
        self.path = ""
        self.auth = ""
        self.body: dict = {}


def _serve(recorder: _Recorder) -> ThreadingHTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            recorder.path = self.path
            recorder.auth = self.headers.get("Authorization", "")
            length = int(self.headers.get("Content-Length", "0"))
            recorder.body = json.loads(self.rfile.read(length) or b"{}")
            payload = b"Web results:\n- Hit\n  https://e.example\n  snip\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a: object) -> None:  # silence the stub
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run(script: Path, *args: str, search: str | None = "1", base: str = "") -> subprocess.CompletedProcess:
    env = {"GROK_API_KEY": "tok-123", "PATH": "/usr/bin:/bin"}
    if search is not None:
        env["JCODE_INTERNET_SEARCH"] = search
    if base:
        env["GROK_MODELS_BASE_URL"] = base
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_web_search_posts_query_to_the_bridge() -> None:
    rec = _Recorder()
    server = _serve(rec)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/api/jcode/llm/v1"
        proc = _run(_WEB_SEARCH, "-n", "3", "grok", "cli", base=base)
    finally:
        server.shutdown()
    assert proc.returncode == 0, proc.stderr
    assert "Web results:" in proc.stdout
    assert rec.path == "/api/jcode/llm/v1/web_search"
    assert rec.auth == "Bearer tok-123"
    assert rec.body == {"query": "grok cli", "limit": 3}


def test_web_fetch_posts_url_to_the_bridge() -> None:
    rec = _Recorder()
    server = _serve(rec)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/api/jcode/llm/v1"
        proc = _run(_WEB_FETCH, "https://example.com/x", base=base)
    finally:
        server.shutdown()
    assert proc.returncode == 0, proc.stderr
    assert rec.path == "/api/jcode/llm/v1/web_fetch"
    assert rec.body == {"url": "https://example.com/x"}


def test_web_search_gated_off_without_the_optin() -> None:
    # No JCODE_INTERNET_SEARCH → the helper refuses before any network call.
    proc = _run(_WEB_SEARCH, "anything", search=None)
    assert proc.returncode == 2
    assert "not enabled" in proc.stderr


def test_web_fetch_gated_off_without_the_optin() -> None:
    proc = _run(_WEB_FETCH, "https://example.com", search=None)
    assert proc.returncode == 2
    assert "not enabled" in proc.stderr


def test_web_search_usage_error_on_empty_query() -> None:
    proc = _run(_WEB_SEARCH)
    assert proc.returncode == 2
    assert "usage" in proc.stderr
