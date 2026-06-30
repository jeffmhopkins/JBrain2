#!/usr/bin/env python3
"""Unauthenticated LAN wall-display server for the JBrain2 server-brain.

Serves the neural-brain page at `/` and its telemetry at `GET /stats`, reading
host vitals straight from /proc and /sys — the same amdgpu/meminfo sources as
supervisor/src/supervisor/host_metrics.py. It is deliberately decoupled from the
authenticated api: it touches NO database and NO user data, only non-sensitive
host vitals (GPU busy %, RAM, APU power, load), so it is safe to expose without
auth *on a trusted LAN*.

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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "index.html"

# Strix Halo APU configurable TDP ceiling (package watts) — used to normalise the
# power reading into the 0..1 "heat" the visual expects. Override per box.
POWER_MAX_W = float(os.environ.get("BRAIN_POWER_MAX_W", "90"))
DEMO = os.environ.get("BRAIN_DEMO") == "1"


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


def mem_used_fraction() -> float:
    total = avail = 0
    try:
        for line in (Path("/proc/meminfo")).read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "MemTotal:":
                total = int(parts[1])
            elif len(parts) >= 2 and parts[0] == "MemAvailable:":
                avail = int(parts[1])
    except OSError:
        return 0.0
    return round(1 - avail / total, 4) if total else 0.0


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


def _demo_snapshot() -> dict:
    """Synthetic wandering values so the wall is alive without amdgpu present."""
    t = time.time()
    util = max(0.0, min(1.0, 0.45 + 0.35 * math.sin(t / 7) + 0.1 * math.sin(t / 1.3)))
    mem = max(0.1, min(0.98, 0.55 + 0.25 * math.sin(t / 23)))
    power = 25 + util * (POWER_MAX_W - 25)
    temp = 45 + util * 38
    return _shape(util, mem, power, temp, load=util * 6, uptime_h=72.0)


def _shape(util, mem, power, temp, load, uptime_h) -> dict:
    """Assemble the ServerBrain contract shape from raw host vitals."""
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
        "host": {"load_1m": round(load, 2), "uptime_h": uptime_h},
    }


def snapshot() -> dict:
    if DEMO:
        return _demo_snapshot()
    busy = gpu_busy_percent()
    return _shape(
        util=(busy or 0) / 100,
        mem=mem_used_fraction(),
        power=apu_power_w() or 0,
        temp=gpu_temp_c() or 0,
        load=load_1m(),
        uptime_h=uptime_hours(),
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
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:  # quiet; this runs as a background display
        pass


def main() -> None:
    host = os.environ.get("BRAIN_HOST", "0.0.0.0")
    port = int(os.environ.get("BRAIN_PORT", "8800"))
    server = ThreadingHTTPServer((host, port), Handler)
    mode = "DEMO (synthetic)" if DEMO else "live sysfs"
    print(f"server-brain on http://{host}:{port}/  ({mode}; power ceiling {POWER_MAX_W:.0f} W)")
    print("  no auth — keep this on the LAN only, never port-forward it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
