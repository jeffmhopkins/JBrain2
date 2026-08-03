#!/usr/bin/env python3
"""es_1e12_server.py — stdlib backend + PWA host for the Erdős–Straus launcher.

Wraps ``es_1e12_launcher.sh`` in a small HTTP API and serves the PWA dashboard.
It exists so the long, non-resumable 10^12 run can be watched, started, and
killed from a browser (installable, works on a phone) and so the finished
artifact gets a public share link.

Design notes:
- Standard library only (no pip deps): http.server, threading, subprocess, os.
  The launcher already requires python3 + tmux, so this adds nothing to install.
- The live terminal tails the on-disk ``console.log`` the launcher writes. That
  file — and the run itself, which lives in tmux — persist independently of any
  browser connection, so closing and reopening the PWA simply re-attaches to the
  same feed (Server-Sent Events, resumable via Last-Event-ID / byte offset).
- The public share link is built from the request Host, so when the dashboard is
  reached through the operator's Cloudflare tunnel the ``/share/`` URL is public.
- Control endpoints can be gated behind ES_GUI_TOKEN; ``/share/`` stays open so
  the artifact link is shareable without hanging the token.

Run directly (usually via ``es_1e12_launcher.sh gui start``):
    ES_GUI_PORT=8787 python3 scripts/es_1e12_server.py
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import threading
import time
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HOME = os.path.expanduser("~")
WORKDIR = Path(os.environ.get("ES_WORKDIR", f"{HOME}/erdos-straus-1e12"))
REPO_DIR = WORKDIR / "Erd-s-Straus-attack"
PUBLIC_DIR = Path(os.environ.get("ES_PUBLIC_DIR", str(WORKDIR / "public")))
SESSION = os.environ.get("ES_SESSION", "es1e12")
CONSOLE = WORKDIR / "console.log"
SPECS = WORKDIR / "machine_specs.txt"
META = REPO_DIR / "data" / "hard_primes_1e12_minimalR.meta.json"
SCRATCH = REPO_DIR / "data" / "hard_primes_1e12_scratch.npz"
VERIFY_LOG = REPO_DIR / "run_1e12_verify.log"
BUNDLE_NAME = "es_1e12_artifacts.tar.gz"
LAUNCHER = Path(os.environ.get("ES_LAUNCHER", str(Path(__file__).with_name("es_1e12_launcher.sh"))))
ASSETS = Path(__file__).with_name("es_gui")
REPO_ROOT = Path(__file__).resolve().parents[1]  # the JBrain2 checkout serving this code
TOKEN = os.environ.get("ES_GUI_TOKEN", "")
PORT = int(os.environ.get("ES_GUI_PORT", "8787"))

# The GUI runs under a supervisor (see es_1e12_launcher.sh `gui start`) that
# relaunches the process on exit. Self-update and restart therefore just pull the
# new code and exit; the supervisor brings the server back with it.
RESTART_EXIT = 42

# ~10.5 GB scratch npz is the generation-progress denominator (see run_1e12.sh).
SCRATCH_TARGET_MB = 10752
TERMINAL_TAIL_BYTES = 200_000  # initial paint / reopen shows this much recent feed

# --- shared mutable state ---------------------------------------------------
_starting = False           # a launcher `start` subprocess is running its setup
_start_lock = threading.Lock()
_cpu_pct = 0.0
_icon_cache: dict[int, bytes] = {}
_scan_cache: dict[tuple, dict] = {}


def _tmux_running(session: str) -> bool:
    if not shutil.which("tmux"):
        return False
    return subprocess.run(
        ["tmux", "has-session", "-t", session],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def _strip_ts(line: str) -> str:
    """Console lines from the run are '<ISO-UTC>\\t<text>'; setup lines are raw."""
    return line.split("\t", 1)[1] if "\t" in line else line


def _epoch_of(line: str):
    if "\t" not in line:
        return None
    ts = line.split("\t", 1)[0]
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _scan_console() -> dict:
    """Parse phase markers, timings, headline, and exit code from the feed.

    Cached by (size, mtime) so repeated status polls don't re-read a large log.
    """
    if not CONSOLE.exists():
        return {"phase": "idle", "exit_code": None, "marks": {}, "headline": []}
    st = CONSOLE.stat()
    key = (st.st_size, int(st.st_mtime))
    cached = _scan_cache.get(key)
    if cached is not None:
        return cached

    marks: dict[str, float] = {}
    exit_code = None
    headline: list[str] = []
    in_headline = False
    has_verif_ok = has_done = has_gen = has_ver = has_pkg = False

    for raw in CONSOLE.read_text("utf-8", "replace").splitlines():
        text = _strip_ts(raw)
        if "== [1/3] generation" in text:
            has_gen = True
            marks.setdefault("generation", _epoch_of(raw))
        elif "== [2/3] verification" in text:
            has_ver = True
            marks.setdefault("verification", _epoch_of(raw))
        elif "== [3/3] packaging" in text:
            has_pkg = True
            marks.setdefault("packaging", _epoch_of(raw))
        elif "== done:" in text:
            has_done = True
            marks.setdefault("done", _epoch_of(raw))
        if "VERIFICATION OK" in text:
            has_verif_ok = True
        if text.startswith("ES_LAUNCHER_EXIT="):
            try:
                exit_code = int(text.split("=", 1)[1])
            except ValueError:
                pass
        if in_headline:
            if text and "ES_LAUNCHER_EXIT" not in text and not text.startswith("=="):
                headline.append(text)
        if "-- headline" in text:
            in_headline = True

    if has_verif_ok and has_done:
        phase = "complete"
    elif has_pkg:
        phase = "packaging"
    elif has_ver:
        phase = "verification"
    elif has_gen:
        phase = "generation"
    else:
        phase = "starting"

    result = {"phase": phase, "exit_code": exit_code, "marks": marks, "headline": headline}
    _scan_cache.clear()
    _scan_cache[key] = result
    return result


def _human_dur(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h {s % 3600 // 60:02d}m {s % 60:02d}s"


def _timings(marks: dict) -> dict:
    def dur(a, b):
        if marks.get(a) and marks.get(b):
            return _human_dur(marks[b] - marks[a])
        return None
    now = time.time()
    gen_end = marks.get("verification") or (now if marks.get("generation") else None)
    out = {
        "generation": dur("generation", "verification"),
        "verification": dur("verification", "packaging"),
        "packaging": dur("packaging", "done"),
        "total": dur("generation", "done"),
    }
    if marks.get("generation") and not marks.get("verification"):
        out["generation"] = _human_dur(now - marks["generation"]) + " (running)"
    if marks.get("generation"):
        end = marks.get("done") or now
        out["elapsed"] = _human_dur(end - marks["generation"])
    return out


def _machine_specs() -> dict:
    if not SPECS.exists():
        return {}
    out = {}
    for line in SPECS.read_text("utf-8", "replace").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _mem() -> dict:
    total = avail = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                total = int(line.split()[1]) * 1024
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1]) * 1024
    except OSError:
        return {}
    used = total - avail
    gb = 1024 ** 3
    return {
        "used_gb": round(used / gb, 1),
        "total_gb": round(total / gb, 1),
        "pct": round(used / total * 100) if total else 0,
    }


def _cpu_sampler():
    """Continuously sample /proc/stat so metrics reads are cheap and non-blocking."""
    global _cpu_pct

    def snap():
        parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
        return idle, sum(vals)

    try:
        pi, pt = snap()
    except OSError:
        return
    while True:
        time.sleep(1.0)
        try:
            i, t = snap()
        except OSError:
            continue
        dt, di = t - pt, i - pi
        if dt > 0:
            _cpu_pct = max(0.0, min(100.0, (1 - di / dt) * 100))
        pi, pt = i, t


def _scratch_progress(phase: str) -> dict | None:
    if phase != "generation" or not SCRATCH.exists():
        return None
    mb = SCRATCH.stat().st_size // (1024 * 1024)
    return {"mb": mb, "pct": min(100, mb * 100 // SCRATCH_TARGET_MB)}


def _status(scheme: str, host: str) -> dict:
    scan = _scan_console()
    phase = scan["phase"]
    running = _tmux_running(SESSION)
    if _starting and not running:
        phase = "setup"
    verify_ok = VERIFY_LOG.exists() and "VERIFICATION OK" in "\n".join(
        VERIFY_LOG.read_text("utf-8", "replace").splitlines()[-4:]
    )
    published = (PUBLIC_DIR / BUNDLE_NAME).exists()
    share = artifact = None
    if published and host:
        base = f"{scheme}://{host}/share/"
        share, artifact = base, base + BUNDLE_NAME
    return {
        "phase": phase,
        "running": running,
        "starting": _starting,
        "exit_code": scan["exit_code"],
        "timings": _timings(scan["marks"]),
        "scratch": _scratch_progress(phase),
        "headline": scan["headline"],
        "specs": _machine_specs(),
        "verify_ok": verify_ok,
        "published": published,
        "sharelink": share,
        "artifact_url": artifact,
        "version": _version(),
    }


def _version() -> str:
    """Short git SHA of the checkout serving this code, so the PWA can confirm an
    update landed after a self-update restart."""
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _delayed_exit(code: int, delay: float = 0.8):
    """Exit shortly after the HTTP response is flushed; the supervisor relaunches."""
    def worker():
        time.sleep(delay)
        os._exit(code)
    threading.Thread(target=worker, daemon=True).start()


def _launcher(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    return subprocess.run(
        ["bash", str(LAUNCHER), *args],
        cwd=str(WORKDIR if WORKDIR.exists() else Path.cwd()),
        env=env, capture_output=True, text=True,
    )


def _do_start():
    global _starting
    with _start_lock:
        if _starting or _tmux_running(SESSION):
            return False
        _starting = True

    def worker():
        global _starting
        try:
            WORKDIR.mkdir(parents=True, exist_ok=True)
            _launcher("start")
        finally:
            _starting = False

    threading.Thread(target=worker, daemon=True).start()
    return True


def _make_icon(size: int) -> bytes:
    """A gradient square PNG, generated in memory so the repo carries no binaries."""
    if size in _icon_cache:
        return _icon_cache[size]
    c1, c2 = (37, 99, 235), (14, 165, 233)  # blue → sky, a diagonal gradient
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # PNG filter type 0 (none) per scanline
        for x in range(size):
            t = (x + y) / (2 * size)
            raw += bytes(int(a + (b - a) * t) for a, b in zip(c1, c2))
    comp = zlib.compress(bytes(raw), 9)

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", comp) + chunk(b"IEND", b""))
    _icon_cache[size] = png
    return png


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml",
    ".png": "image/png", ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8", ".gz": "application/gzip",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):  # keep the tmux pane quiet
        pass

    # -- helpers -------------------------------------------------------------
    def _scheme(self) -> str:
        return self.headers.get("X-Forwarded-Proto", "http")

    def _authed(self, qs: dict) -> bool:
        if not TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {TOKEN}":
            return True
        return qs.get("token", [""])[0] == TOKEN

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes, ctype: str, code: int = 200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    # -- routing -------------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                if not self._authed(qs):
                    return self._json({"error": "unauthorized"}, 401)
                return self._api_get(path, qs)
            if path.startswith("/share/") or path == "/share":
                return self._serve_share(path)
            if path.startswith("/icon-") and path.endswith(".png"):
                try:
                    size = int(path[len("/icon-"):-len(".png")])
                except ValueError:
                    return self._bytes(b"bad size", "text/plain", 400)
                if size not in (192, 512, 180):
                    size = 512
                return self._bytes(_make_icon(size), "image/png")
            return self._serve_asset(path)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if not self._authed(qs):
            return self._json({"error": "unauthorized"}, 401)
        if parsed.path == "/api/start":
            ok = _do_start()
            return self._json({"ok": ok, "error": None if ok else "already running"},
                              200 if ok else 409)
        if parsed.path == "/api/stop":
            r = _launcher("stop")
            return self._json({"ok": r.returncode == 0, "output": (r.stdout + r.stderr)[-4000:]})
        if parsed.path == "/api/publish":
            r = _launcher("publish")
            out = {"ok": r.returncode == 0, "output": (r.stdout + r.stderr)[-8000:]}
            out["status"] = _status(self._scheme(), self.headers.get("Host", ""))
            return self._json(out, 200 if r.returncode == 0 else 400)
        if parsed.path == "/api/update":
            # Pull new code into this checkout, then (if anything changed) exit so
            # the supervisor relaunches with it — the whole update is PWA-driven.
            before = _version()
            r = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=180,
            )
            output = (r.stdout + r.stderr).strip()
            changed = r.returncode == 0 and "up to date" not in output.lower()
            self._json({
                "ok": r.returncode == 0, "output": output[-4000:],
                "version_before": before, "restarting": changed,
            }, 200 if r.returncode == 0 else 400)
            if changed:
                _delayed_exit(RESTART_EXIT)
            return
        if parsed.path == "/api/restart":
            self._json({"ok": True, "restarting": True})
            _delayed_exit(RESTART_EXIT)
            return
        return self._json({"error": "not found"}, 404)

    def _api_get(self, path: str, qs: dict):
        if path == "/api/status":
            return self._json(_status(self._scheme(), self.headers.get("Host", "")))
        if path == "/api/metrics":
            specs = _machine_specs()
            try:
                load1 = os.getloadavg()[0]
            except OSError:
                load1 = None
            return self._json({
                "cpu_pct": round(_cpu_pct, 1),
                "mem": _mem(),
                "cores": os.cpu_count(),
                "load1": round(load1, 2) if load1 is not None else None,
                "cpu_model": specs.get("cpu", ""),
            })
        if path == "/api/stream":
            return self._stream(qs)
        return self._json({"error": "not found"}, 404)

    def _stream(self, qs: dict):
        """SSE tail of console.log. Resumable: honors Last-Event-ID (a byte offset);
        otherwise ?from=0 sends the whole feed, default sends the recent tail."""
        size = CONSOLE.stat().st_size if CONSOLE.exists() else 0
        last = self.headers.get("Last-Event-ID")
        if last and last.isdigit():
            offset = int(last)
        elif qs.get("from", ["tail"])[0] == "0":
            offset = 0
        else:
            offset = max(0, size - TERMINAL_TAIL_BYTES)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                size = CONSOLE.stat().st_size if CONSOLE.exists() else 0
                if size < offset:  # feed truncated → a new run started; reset the view
                    offset = 0
                    self.wfile.write(b"event: reset\ndata: \n\n")
                    self.wfile.flush()
                if size > offset:
                    with open(CONSOLE, "rb") as f:
                        f.seek(offset)
                        data = f.read()
                        offset = f.tell()
                    payload = "".join(
                        f"data: {line}\n"
                        for line in data.decode("utf-8", "replace").splitlines()
                    )
                    self.wfile.write((payload + f"id: {offset}\n\n").encode())
                    self.wfile.flush()
                else:
                    self.wfile.write(b": ping\n\n")  # heartbeat keeps proxies open
                    self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_share(self, path: str):
        rel = path[len("/share"):].lstrip("/") or "index.html"
        target = (PUBLIC_DIR / rel).resolve()
        if not str(target).startswith(str(PUBLIC_DIR.resolve())):
            return self._bytes(b"forbidden", "text/plain", 403)
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            return self._bytes(b"not published yet", "text/plain", 404)
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        return self._bytes(target.read_bytes(), ctype)

    def _serve_asset(self, path: str):
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = (ASSETS / rel).resolve()
        if not str(target).startswith(str(ASSETS.resolve())) or not target.is_file():
            return self._bytes(b"not found", "text/plain", 404)
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        return self._bytes(target.read_bytes(), ctype)


def main():
    threading.Thread(target=_cpu_sampler, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    banner = f"es_1e12 GUI on http://0.0.0.0:{PORT}  workdir={WORKDIR}"
    if TOKEN:
        banner += "  (token required)"
    print(banner, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
