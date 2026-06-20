"""Host-level metrics, read from /proc, /sys and statvfs.

These reflect the HOST, not the container: /proc/meminfo, /proc/loadavg and
/proc/uptime are not namespaced, / sits on the host's root filesystem via
overlayfs, and Docker mounts the host's /sys read-only (so the amdgpu driver's
load attribute is readable without /dev/dri — that device is only needed to
USE the GPU, not to read its telemetry). Paths are injectable for tests.
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
    # iGPU/dGPU utilization, 0-100, or None when no amdgpu busy attribute is
    # present (non-AMD box, no GPU, or /sys not exposed) — the card omits the row.
    gpu_busy_percent: float | None = None


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


def read_gpu_busy_percent(drm: Path = Path("/sys/class/drm")) -> float | None:
    """Highest amdgpu `gpu_busy_percent` across DRM cards, or None if unreadable.

    The driver exposes a 0-100 instantaneous-load gauge at
    /sys/class/drm/card*/device/gpu_busy_percent. We take the max over cards so a
    single iGPU is reported regardless of its card index; any unreadable or
    malformed card is skipped, and None (not 0) means "no GPU telemetry here" so
    the caller can drop the row rather than imply an idle GPU."""
    best: float | None = None
    try:
        cards = sorted(drm.glob("card*/device/gpu_busy_percent"))
    except OSError:
        return None
    for path in cards:
        try:
            value = float(path.read_text().strip())
        except (OSError, ValueError):
            continue
        if best is None or value > best:
            best = value
    return best


def read_host_metrics(
    proc: Path = Path("/proc"),
    disk_path: str = "/",
    drm: Path = Path("/sys/class/drm"),
) -> HostMetrics:
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
        gpu_busy_percent=read_gpu_busy_percent(drm),
    )
