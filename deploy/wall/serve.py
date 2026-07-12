#!/usr/bin/env python3
"""Unauthenticated LAN wall-display server for the JBrain2 `wall` kiosk.

Serves the neural-brain page at `/` and its telemetry at `GET /stats`, reading
host vitals straight from /proc and /sys — the same amdgpu/meminfo sources as
supervisor/src/supervisor/host_metrics.py. `POST /event` accepts a tiny
`{"kind": "web_search"|"web_fetch"}` marker from the JBrain2 agent (-> a reach-out
tendril). It is deliberately decoupled from the authenticated api: it touches NO
database and NO user data, only non-sensitive host vitals (GPU/RAM/power/load,
net + disk throughput) and content-free web-tool markers, so it is safe to expose
without auth *on a trusted LAN*.

SECURITY: there is no authentication. Bind it to your LAN only and never
port-forward it to the public internet. Defaults to 0.0.0.0 so it answers at
http://<host>.local:8800/ via mDNS; set BRAIN_HOST to pin a single interface.

Run:  python3 serve.py            # 0.0.0.0:8800, real sysfs
      BRAIN_DEMO=1 python3 serve.py   # synthetic wandering values (no amdgpu needed)

Stdlib only — no dependencies, no build step.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"
PET_PAGE = HERE / "pet.html"

# The on-box api, reached over the internal docker network for the read-only JPet
# snapshot (`GET /internal/pet`) — the ONE api touch-point: a content-free,
# non-sensitive pet snapshot (mood/position/speech in the safe 'general' domain),
# proxied so the LAN browser reads it same-origin. Never off-box.
API_URL = os.environ.get("BRAIN_API_URL", "http://api:8000")


def fetch_pet_state() -> bytes | None:
    """GET the pet snapshot from the on-box api. None on any error (the wall shows a
    'waiting for pet' state and retries on the next poll)."""
    try:
        with urllib.request.urlopen(f"{API_URL}/internal/pet", timeout=2) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            return resp.read()
    except Exception:  # noqa: BLE001 — any failure just means "not ready"; poll again
        return None

# ── Text-to-speech (proxied to the tts-stt speech service) ─────────────────
# Rendering moved out of the wall into the shared `tts-stt` service (which also serves the
# PWA read-aloud). The wall keeps a thin SAME-ORIGIN forward so its own kiosk browser —
# which can reach neither the internal `tts-stt` name nor the authenticated api — still
# fetches read-aloud audio from wall:8800/tts*.
TTS_URL = os.environ.get("BRAIN_TTS_URL", "http://tts-stt:8801").rstrip("/")
# The on-box whisper.cpp STT (the SAME tts-stt container, llama-swap on :8080, OpenAI audio path).
# The wall's voice listener posts a captured phrase here to transcribe it locally — no cloud, so it
# works in Chromium/Firefox (unlike the browser Web Speech API). Bounded + lightly throttled below.
STT_URL = os.environ.get("BRAIN_STT_URL", "http://tts-stt:8080").rstrip("/")
_STT_MAX_BYTES = 8 * 1024 * 1024
_stt_last = [0.0]  # crude throttle: at most a couple of transcriptions a second (VAD already gates)


def stt_forward(body: bytes, ctype: str) -> tuple[int, bytes, str]:
    """Forward a multipart audio clip to whisper's `/v1/audio/transcriptions` and return
    (status, transcript, content-type). 503 on any failure so the page just tries again."""
    try:
        req = urllib.request.Request(  # noqa: S310
            f"{STT_URL}/v1/audio/transcriptions", data=body, method="POST",
            headers={"Content-Type": ctype},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — whisper can load-on-demand
            return resp.status, resp.read(), resp.headers.get("Content-Type", "text/plain")
    except Exception:  # noqa: BLE001 — STT unavailable (model not provisioned / busy); page retries
        return 503, b"stt unavailable", "text/plain"


def api_post(path: str, data: bytes = b"", ctype: str = "", timeout: float = 2.0) -> tuple[int, bytes, str]:
    """POST `data` to the on-box api and return (status, body, content-type). Used for the wall's
    one-shot `/internal/pet/effects/clear` (empty body) and its voice `/internal/pet/say` (a JSON
    command, which can take a moment when it hits the LLM). 503 on any failure."""
    try:
        headers = {"Content-Type": ctype} if ctype else {}
        req = urllib.request.Request(  # noqa: S310
            f"{API_URL}{path}", data=data, method="POST", headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
    except Exception:  # noqa: BLE001 — any failure just means the command didn't land; harmless
        return 503, b'{"error":"unavailable"}', "application/json"


def tts_forward(path_qs: str) -> tuple[int, bytes, str]:
    """GET `path_qs` (e.g. '/tts?voice=...&text=...') from the tts-stt service and return
    (status, body, content-type). Any failure surfaces as 503 so the page can degrade."""
    try:
        with urllib.request.urlopen(f"{TTS_URL}{path_qs}", timeout=30) as resp:  # noqa: S310
            ctype = resp.headers.get("Content-Type", "application/octet-stream")
            return resp.status, resp.read(), ctype
    except Exception:  # noqa: BLE001 — any failure means tts unavailable; the page falls back
        return 503, b"tts unavailable", "text/plain"


# Strix Halo APU configurable TDP ceiling (package watts) — used to normalise the
# power reading into the 0..1 "heat" the visual expects. Override per box.
POWER_MAX_W = float(os.environ.get("BRAIN_POWER_MAX_W", "90"))
DEMO = os.environ.get("BRAIN_DEMO") == "1"
# Throughput ceilings used to normalise byte-rates into the 0..1 the visual wants.
NET_MAX_BPS = float(os.environ.get("BRAIN_NET_MAX_BPS", str(12_500_000)))    # ~100 Mbit/s
DISK_MAX_BPS = float(os.environ.get("BRAIN_DISK_MAX_BPS", str(500_000_000)))  # ~500 MB/s
# Optional JSONL file the JBrain2 agent/tool layer appends web-tool events to —
# one object per line, e.g. {"kind": "web_search"} or {"kind": "web_fetch"}. Each
# new line is drained once and fires a reach-out tendril. Empty -> no web tendrils.
EVENTS_PATH = os.environ.get("BRAIN_EVENTS_FILE", "")

# Web-tool events POSTed by the JBrain2 agent to `/event` (the primary live path;
# BRAIN_EVENTS_FILE is an alternative). Drained into /stats on each poll.
_posted: deque = deque(maxlen=64)
_posted_lock = threading.Lock()

# Persistent read-aloud switch, pushed by the app ({"kind": "read_aloud", "on": bool} to
# /event) from the brain_read_aloud setting. Unlike the queued tendril events this is a
# held boolean surfaced in every /stats, so the page shows/hides its voice panel on it (in
# addition to piper voices being installed). Default OFF: a fresh/restarted display speaks
# nothing until the app re-pushes the flag (it does so on the setting change and each turn).
_read_aloud = [False]



def _drain_posted() -> list:
    with _posted_lock:
        out = list(_posted)
        _posted.clear()
    return out


# --- host vitals (mirrors supervisor/host_metrics.py, trimmed to what we need) ---


def _read_first_float(path: Path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def gpu_busy_percent() -> float | None:
    """Highest amdgpu gpu_busy_percent across DRM cards, or None if unreadable."""
    best: float | None = None
    try:
        cards = sorted(Path("/sys/class/drm").glob("card*/device/gpu_busy_percent"))
    except OSError:
        return None
    for path in cards:
        v = _read_first_float(path)
        if v is not None and (best is None or v > best):
            best = v
    return best


def _amdgpu_hwmon() -> Path | None:
    try:
        chips = sorted(Path("/sys/class/hwmon").glob("hwmon*"))
    except OSError:
        return None
    for chip in chips:
        try:
            if (chip / "name").read_text().strip() == "amdgpu":
                return chip
        except OSError:
            continue
    return None


def apu_power_w() -> float | None:
    chip = _amdgpu_hwmon()
    if chip is None:
        return None
    for attr in ("power1_average", "power1_input"):
        microwatts = _read_first_float(chip / attr)
        if microwatts is not None:
            return round(microwatts / 1_000_000, 1)
    return None


def gpu_temp_c() -> float | None:
    chip = _amdgpu_hwmon()
    if chip is None:
        return None
    millideg = _read_first_float(chip / "temp1_input")  # edge temp, in m°C
    return round(millideg / 1000, 1) if millideg is not None else None


def sys_mem_bytes() -> tuple[int, int]:
    """(used, total) system RAM in bytes from /proc/meminfo."""
    total = avail = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "MemTotal:":
                total = int(parts[1]) * 1024
            elif len(parts) >= 2 and parts[0] == "MemAvailable:":
                avail = int(parts[1]) * 1024
    except OSError:
        return 0, 0
    return (total - avail, total)


def vram_bytes() -> tuple[int, int]:
    """(used, total) amdgpu VRAM in bytes — the LLM's model footprint on the iGPU,
    or (0, 0) when absent. Folded into the memory signal so 'density' reflects
    RAM + the VRAM the local model is holding."""
    used = total = 0
    try:
        cards = sorted(Path("/sys/class/drm").glob("card*/device"))
    except OSError:
        return 0, 0
    for dev in cards:
        u = _read_first_float(dev / "mem_info_vram_used")
        t = _read_first_float(dev / "mem_info_vram_total")
        if u is not None and t:
            used += int(u)
            total += int(t)
    return used, total


def mem_used_fraction() -> float:
    """Combined RAM + GPU VRAM usage fraction (the 'neural density' signal)."""
    su, st = sys_mem_bytes()
    vu, vt = vram_bytes()
    denom = st + vt
    return round((su + vu) / denom, 4) if denom else 0.0


def load_1m() -> float:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def uptime_hours() -> float:
    try:
        secs = float(Path("/proc/uptime").read_text().split()[0])
        return round(secs / 3600, 1)
    except (OSError, ValueError, IndexError):
        return 0.0


def read_net_bytes() -> tuple[int, int]:
    """(rx, tx) total bytes across real interfaces (lo / virtual excluded)."""
    rx = tx = 0
    try:
        lines = Path("/proc/net/dev").read_text().splitlines()
    except OSError:
        return 0, 0
    for line in lines:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr", "tap")):
            continue
        f = rest.split()
        if len(f) >= 9:
            try:
                rx += int(f[0])
                tx += int(f[8])
            except ValueError:
                continue
    return rx, tx


def _is_whole_disk(name: str) -> bool:
    if name.startswith(("loop", "ram", "dm-", "sr", "fd", "zram", "md")):
        return False
    if name.startswith(("nvme", "mmcblk")):
        return "p" not in name            # nvme0n1 yes, nvme0n1p1 no
    if name.startswith(("sd", "vd", "xvd", "hd")):
        return not name[-1].isdigit()     # sda yes, sda1 no
    return False


def read_disk_read_bytes() -> int:
    """Total bytes READ across whole disks from /proc/diskstats (sectors x 512)."""
    total = 0
    try:
        lines = Path("/proc/diskstats").read_text().splitlines()
    except OSError:
        return 0
    for line in lines:
        f = line.split()
        if len(f) < 6 or not _is_whole_disk(f[2]):
            continue
        try:
            total += int(f[5]) * 512      # field[5] = sectors read
        except ValueError:
            continue
    return total


_rate_state = {"t": None, "rx": 0, "tx": 0, "disk": 0}


def net_disk_rates() -> tuple[float, float, float]:
    """(net_in, net_out, disk_read) as 0..1 fractions of the configured ceilings,
    from byte-counter deltas since the previous call."""
    now = time.time()
    rx, tx = read_net_bytes()
    dsk = read_disk_read_bytes()
    prev = _rate_state
    if prev["t"] is None:
        prev.update(t=now, rx=rx, tx=tx, disk=dsk)
        return 0.0, 0.0, 0.0
    dt = max(0.05, now - prev["t"])
    in_bps = max(0, rx - prev["rx"]) / dt
    out_bps = max(0, tx - prev["tx"]) / dt
    dsk_bps = max(0, dsk - prev["disk"]) / dt
    prev.update(t=now, rx=rx, tx=tx, disk=dsk)
    cl = lambda x: max(0.0, min(1.0, x))  # noqa: E731
    return cl(in_bps / NET_MAX_BPS), cl(out_bps / NET_MAX_BPS), cl(dsk_bps / DISK_MAX_BPS)


_events_pos = [0]


def drain_events() -> list:
    """New web-tool events appended to EVENTS_PATH since the last drain — each a
    JSON object like {"kind": "web_search"}; returns [{"kind", "ts"}, ...]."""
    if not EVENTS_PATH:
        return []
    out = []
    try:
        if os.path.getsize(EVENTS_PATH) < _events_pos[0]:
            _events_pos[0] = 0   # file rotated/truncated -> re-read from the start
        with open(EVENTS_PATH) as f:
            f.seek(_events_pos[0])
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("kind") in ("web_search", "web_fetch"):
                    out.append({"kind": ev["kind"], "ts": ev.get("ts")})
            _events_pos[0] = f.tell()
    except OSError:
        return []
    return out[-20:]   # never flood the page with a huge backlog


_demo_state = {"search": 0.0, "fetch": 0.0, "llm_in": 0.0, "llm_out": 0.0, "task_at": 0.0, "task": ""}
# Content-free synthetic text for the demo LLM tendrils (no owner data — this path
# never sees a real turn), so `BRAIN_DEMO=1` shows the streaming-text + popup effect.
_DEMO_PROMPTS = (
    "what did I note about the roof warranty last spring?",
    "summarize this week's running mileage and how it trended",
    "when is my next dentist appointment and who is it with?",
    "draft a reply to the landlord about the lease renewal",
)
_DEMO_ANSWERS = (
    "The warranty runs 20 years from the March install and covers workmanship.",
    "You logged 24.6 miles across four runs — up ~15% on last week.",
    "Next up: Dr. Alvarez, Thursday 9:40am, for a routine cleaning.",
    "Confirmed you'll renew at the current rate; asked to fix the porch light first.",
)
_DEMO_TASKS = (
    "ingest workflow · new note",
    "nightly wiki rebuild",
    "entity-graph reconciliation",
    "embedding reindex",
)


def _demo_snapshot() -> dict:
    """Synthetic wandering values so the wall is alive without amdgpu present."""
    t = time.time()
    util = max(0.0, min(1.0, 0.45 + 0.35 * math.sin(t / 7) + 0.1 * math.sin(t / 1.3)))
    mem = max(0.1, min(0.98, 0.55 + 0.25 * math.sin(t / 23)))
    power = 25 + util * (POWER_MAX_W - 25)
    temp = 45 + util * 38
    net_in = max(0.0, 0.30 + 0.5 * math.sin(t / 5))
    net_out = max(0.0, 0.18 + 0.3 * math.sin(t / 8 + 1))
    disk_read = max(0.0, 0.20 + 0.4 * math.sin(t / 4 + 2))
    events = []
    if t - _demo_state["search"] > 8:
        _demo_state["search"] = t
        events.append({"kind": "web_search", "ts": int(t * 1000)})
    if t - _demo_state["fetch"] > 14:
        _demo_state["fetch"] = t
        events.append({"kind": "web_fetch", "ts": int(t * 1000)})
    # A synthetic turn: the prompt streams in, then ~5s later the answer streams out.
    if t - _demo_state["llm_in"] > 11:
        _demo_state["llm_in"] = t
        i = int(t / 11) % len(_DEMO_PROMPTS)
        events.append({"kind": "llm_input", "text": _DEMO_PROMPTS[i], "ts": int(t * 1000)})
    if _demo_state["llm_in"] and 5 < t - _demo_state["llm_in"] < 6.5 and t - _demo_state["llm_out"] > 4:
        _demo_state["llm_out"] = t
        i = int(_demo_state["llm_in"] / 11) % len(_DEMO_ANSWERS)
        events.append({"kind": "llm_output", "text": _DEMO_ANSWERS[i], "ts": int(t * 1000)})
    # A synthetic workflow/task: holds a named teal popup for ~6s, then finishes; next
    # one starts a few seconds later (9s cycle).
    if not _demo_state["task"] and t - _demo_state["task_at"] > 9:
        _demo_state["task_at"] = t
        _demo_state["task"] = _DEMO_TASKS[int(t / 9) % len(_DEMO_TASKS)]
        events.append({"kind": "task_start", "text": _demo_state["task"], "ts": int(t * 1000)})
    elif _demo_state["task"] and t - _demo_state["task_at"] > 6:
        events.append({"kind": "task_stop", "text": _demo_state["task"], "ts": int(t * 1000)})
        _demo_state["task"] = ""
    return _shape(util, mem, power, temp, load=util * 6, uptime_h=72.0,
                  net_in=net_in, net_out=net_out, disk_read=disk_read,
                  events=events + _drain_posted(),
                  # Demo previews the voice panel (if voices are installed) without an app.
                  read_aloud=True)


def _shape(util, mem, power, temp, load, uptime_h,
           net_in=0.0, net_out=0.0, disk_read=0.0, events=None, read_aloud=None) -> dict:
    """Assemble the ServerBrain contract shape from raw host vitals."""
    if read_aloud is None:
        read_aloud = _read_aloud[0]
    util = max(0.0, min(1.0, util))
    mem = max(0.0, min(1.0, mem))
    if util > 0.97 or mem > 0.95 or (temp or 0) > 92:
        health = "crit"
    elif util > 0.85 or mem > 0.85 or (temp or 0) > 82:
        health = "warn"
    else:
        health = "ok"
    return {
        "ts": int(time.time() * 1000),
        "health": health,
        # gpu.util -> neural activity, gpu.vram (shared RAM) -> neural density,
        # gpu.powerW -> bloom heat. Unwired signals stay quiet (zeros).
        "gpu": {
            "util": round(util, 4),
            "vram": round(mem, 4),
            "tempC": temp or 0,
            "powerW": round(power or 0, 1),
            "powerMaxW": POWER_MAX_W,
        },
        "llm": {"active": util > 0.5, "model": "", "tokensPerSec": 0, "queue": 0, "ctxUsed": 0},
        "api": {"reqPerSec": 0, "p95Ms": 0, "errorRate": 0, "inflight": 0},
        "db": {"qps": 0, "poolUsed": 0, "slowQueries": 0},
        # net in -> blue rim aura, net out -> coral rim, disk read -> violet rim.
        "net": {"inRate": round(net_in, 4), "outRate": round(net_out, 4)},
        "disk": {"readRate": round(disk_read, 4)},
        # web-tool calls -> reach-out tendrils (drained by the page each poll).
        "events": events or [],
        # Persistent read-aloud switch (brain_read_aloud) — the page shows its voice panel
        # only when this is on AND piper voices are installed.
        "read_aloud": bool(read_aloud),
        "host": {"load_1m": round(load, 2), "uptime_h": uptime_h},
    }


def snapshot() -> dict:
    if DEMO:
        return _demo_snapshot()
    busy = gpu_busy_percent()
    net_in, net_out, disk_read = net_disk_rates()
    return _shape(
        util=(busy or 0) / 100,
        mem=mem_used_fraction(),
        power=apu_power_w() or 0,
        temp=gpu_temp_c() or 0,
        load=load_1m(),
        uptime_h=uptime_hours(),
        net_in=net_in, net_out=net_out, disk_read=disk_read,
        events=drain_events() + _drain_posted(),
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/stats":
            self._send(200, json.dumps(snapshot()).encode(), "application/json")
        elif path in ("/", "/index.html"):
            try:
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"index.html not found next to serve.py", "text/plain")
        elif path in ("/pet", "/pet/"):
            try:
                self._send(200, PET_PAGE.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"pet.html not found next to serve.py", "text/plain")
        elif path == "/pet/state":
            data = fetch_pet_state()
            if data is None:
                self._send(503, b'{"error":"pet not ready"}', "application/json")
            else:
                self._send(200, data, "application/json")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        elif path in ("/tts", "/tts/voices", "/tts/silence"):
            # Forward read-aloud to the tts-stt service, same-origin for the kiosk browser.
            code, body, ctype = tts_forward(self.path)
            self._send(code, body, ctype)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        # The JBrain2 agent POSTs here when it runs a web tool
        # ({"kind": "web_search"|"web_fetch"} — content-free) or, when the owner has
        # opted in, an LLM turn ({"kind": "llm_input"|"llm_output", "text": ...} —
        # the real prompt/answer text). A running workflow/task posts
        # {"kind": "task_start"|"task_stop", "text": name} to hold/retire a teal popup, and
        # the read-aloud setting pushes {"kind": "read_aloud", "on": bool} — a held flag, not
        # a tendril, latched below and surfaced in /stats to show/hide the voice panel.
        # We queue it for the next /stats drain (-> a tendril; the llm kinds stream their
        # text along it + fade an answer popup; the task kinds hold a named popup).
        path = self.path.split("?", 1)[0]
        if path == "/pet/effects/clear":
            # The pet page calls this on load so a reload drops the ephemeral colour/size
            # overrides (they were never persisted). Same-origin forward to the on-box api.
            code, body, ctype = api_post("/internal/pet/effects/clear")
            self._send(code, body, ctype)
            return
        if path == "/pet/stt":
            # The pet page's voice listener sends a captured phrase (multipart WAV) to be
            # transcribed on-box by whisper. Bounded + throttled; VAD on the page already gates it.
            ctype = self.headers.get("Content-Type", "")
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0 or n > _STT_MAX_BYTES or "multipart/form-data" not in ctype:
                self._send(400, b"bad audio", "text/plain")
                return
            body = self.rfile.read(n)  # drain the body regardless, so the connection stays sane
            if time.time() - _stt_last[0] < 0.4:
                self._send(429, b"slow down", "text/plain")
                return
            _stt_last[0] = time.time()
            code, out, octype = stt_forward(body, ctype)
            self._send(code, out, octype)
            return
        if path == "/pet/say":
            # The pet page's voice listener heard "robot, <command>" — forward the spoken text to
            # the internal talk brain (rate-limited on the api). Same trust boundary as the wall
            # (LAN-only). A slow LLM turn can take a few seconds, so allow a longer timeout.
            n = min(int(self.headers.get("Content-Length", 0) or 0), 2048)
            raw = self.rfile.read(n) if n > 0 else b"{}"
            code, body, ctype = api_post("/internal/pet/say", raw, "application/json", timeout=30.0)
            self._send(code, body, ctype)
            return
        if path != "/event":
            self._send(404, b"not found", "text/plain")
            return
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 16384)
            ev = json.loads(self.rfile.read(n) if n > 0 else b"{}")
        except (ValueError, OSError):
            self._send(400, b"bad request", "text/plain")
            return
        kind = ev.get("kind") if isinstance(ev, dict) else None
        if kind == "read_aloud":
            # A held display-config flag, not a tendril event: latch it (the app pushes it
            # from the brain_read_aloud setting) so /stats reflects it until the next push.
            _read_aloud[0] = bool(ev.get("on"))
            self._send(204, b"", "text/plain")
            return
        if kind in (
            "web_search",
            "web_fetch",
            "llm_input",
            "llm_thinking",
            "llm_output",
            "task_start",
            "task_stop",
        ):
            # Optional text (the LLM prompt/answer, or — when the owner enabled it — the
            # web query / URL). Bound it on our side too — the popup shows the whole reply
            # (it scrolls) and read-aloud speaks it all, so the cap is generous, not an
            # excerpt. Absent/blank text just fires a content-free tendril.
            text = ev.get("text")
            text = text[:4000] if isinstance(text, str) else ""
            row = {"kind": kind, "ts": int(time.time() * 1000)}
            if text:
                row["text"] = text
            with _posted_lock:
                _posted.append(row)
            self._send(204, b"", "text/plain")
        else:
            self._send(400, b"unknown kind", "text/plain")

    def log_message(self, *args) -> None:  # quiet; this runs as a background display
        pass


def main() -> None:
    host = os.environ.get("BRAIN_HOST", "0.0.0.0")
    port = int(os.environ.get("BRAIN_PORT", "8800"))
    server = ThreadingHTTPServer((host, port), Handler)
    mode = "DEMO (synthetic)" if DEMO else "live sysfs"
    print(f"wall display on http://{host}:{port}/  ({mode}; power ceiling {POWER_MAX_W:.0f} W)")
    print("  no auth — keep this on the LAN only, never port-forward it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
