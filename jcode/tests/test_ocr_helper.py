"""The `ocr` shell helper the sandbox CLIs call: POST a local image/PDF to the api's OCR
bridge and print the text. Always-on (no JCODE_INTERNET gate). A localhost stub stands
in for the api — no external network (docs/plans/RAPIDOCR_PLAN.md)."""

from __future__ import annotations

import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_OCR = Path(__file__).resolve().parents[1] / "ocr"


class _Recorder:
    def __init__(self) -> None:
        self.path = ""
        self.auth = ""
        self.content_type = ""
        self.body = b""


def _serve(recorder: _Recorder) -> ThreadingHTTPServer:
    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            recorder.path = self.path
            recorder.auth = self.headers.get("Authorization", "")
            recorder.content_type = self.headers.get("Content-Type", "")
            recorder.body = self.rfile.read(
                int(self.headers.get("Content-Length", "0"))
            )
            payload = b"the exact text\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:  # silence the stub
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run(*args: str, base: str = "") -> subprocess.CompletedProcess:
    env = {"GROK_API_KEY": "tok-123", "PATH": "/usr/bin:/bin"}
    if base:
        env["GROK_MODELS_BASE_URL"] = base
    return subprocess.run(
        [sys.executable, str(_OCR), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_ocr_posts_the_file_to_the_bridge(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNGdata")
    rec = _Recorder()
    server = _serve(rec)
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/api/jcode/llm/v1"
        proc = _run(str(img), base=base)
    finally:
        server.shutdown()
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "the exact text\n"
    assert rec.path == "/api/jcode/llm/v1/ocr"
    assert rec.auth == "Bearer tok-123"
    assert rec.content_type == "image/png"  # guessed from the extension
    assert rec.body == b"\x89PNGdata"


def test_ocr_usage_error_without_a_file() -> None:
    proc = _run()
    assert proc.returncode == 2
    assert "usage" in proc.stderr


def test_ocr_reports_a_missing_file() -> None:
    proc = _run("/no/such/file.png")
    assert proc.returncode == 2
    assert "cannot read" in proc.stderr
