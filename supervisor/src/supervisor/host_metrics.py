"""Host-level metrics, read from /proc and statvfs.

These reflect the HOST, not the container: /proc/meminfo, /proc/loadavg and
/proc/uptime are not namespaced, and / sits on the host's root filesystem
via overlayfs. Paths are injectable for tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HostMetrics:
    mem_total_bytes: int
    mem_available_bytes: int
    swap_total_bytes: int
    swap_free_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    load_1m: float
    load_5m: float
    load_15m: float
    uptime_seconds: int


def _meminfo_kb(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].endswith(":"):
            try:
                values[parts[0][:-1]] = int(parts[1])
            except ValueError:
                continue
    return values


def read_host_metrics(proc: Path = Path("/proc"), disk_path: str = "/") -> HostMetrics:
    mem = _meminfo_kb((proc / "meminfo").read_text())
    load_parts = (proc / "loadavg").read_text().split()
    uptime = float((proc / "uptime").read_text().split()[0])
    vfs = os.statvfs(disk_path)
    return HostMetrics(
        mem_total_bytes=mem.get("MemTotal", 0) * 1024,
        mem_available_bytes=mem.get("MemAvailable", 0) * 1024,
        swap_total_bytes=mem.get("SwapTotal", 0) * 1024,
        swap_free_bytes=mem.get("SwapFree", 0) * 1024,
        disk_total_bytes=vfs.f_blocks * vfs.f_frsize,
        disk_free_bytes=vfs.f_bavail * vfs.f_frsize,
        load_1m=float(load_parts[0]),
        load_5m=float(load_parts[1]),
        load_15m=float(load_parts[2]),
        uptime_seconds=int(uptime),
    )
