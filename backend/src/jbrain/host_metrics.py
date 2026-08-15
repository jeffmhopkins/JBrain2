"""Host memory + GPU telemetry for the local-model memory meter and the top-bar
vitals stream.

Reads /proc/meminfo and /sys/class/drm directly — this is host system telemetry,
not application data, so it sits outside the storage abstraction (which governs
blobs/backups). On AMD Strix Halo the iGPU draws from unified system RAM, so
total/used system memory is the honest gauge for "can I load another model".
Best-effort: returns None off Linux or when the fields are missing, and the
caller renders without a meter.

The supervisor and deploy/wall carry their own copies of the amdgpu read. That
duplication is deliberate: those are separate deployables with no shared Python
package (wall is a stdlib-only image), so the alternative is a network hop per
tick — far too costly at the once-a-second cadence the top bar reads at.
"""

from __future__ import annotations

from pathlib import Path

# /proc/meminfo reports kB; dividing by this yields GiB.
_KB_PER_GIB = 1024 * 1024


def read_memory_gb(path: str = "/proc/meminfo") -> tuple[float, float] | None:
    """`(total_gb, used_gb)` from /proc/meminfo, or None if unavailable.

    `used = MemTotal - MemAvailable` — it excludes reclaimable page cache, so it
    reflects real memory pressure (what actually limits loading another model)
    rather than counting cache as "used"."""
    wanted = ("MemTotal", "MemAvailable")
    fields: dict[str, int] = {}
    try:
        with open(path) as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in wanted:
                    fields[key] = int(rest.strip().split()[0])
                    if len(fields) == len(wanted):
                        break
    except (OSError, ValueError, IndexError):
        return None
    if not all(k in fields for k in wanted):
        return None
    total = fields["MemTotal"] / _KB_PER_GIB
    used = (fields["MemTotal"] - fields["MemAvailable"]) / _KB_PER_GIB
    return round(total, 1), round(used, 1)


def read_gpu_busy_percent(drm: Path = Path("/sys/class/drm")) -> float | None:
    """Highest amdgpu `gpu_busy_percent` (0-100) across DRM cards, or None when no
    card exposes the attribute — a non-AMD box, no GPU, or /sys absent.

    Docker mounts the host's /sys read-only into every container, so this reads the
    HOST's iGPU from inside the api container with no bind mount and no /dev/dri
    (that device is needed to USE the GPU, not to read its telemetry) — the same
    source, and the same no-mount arrangement, that deploy/wall already relies on.

    Takes the max over cards so a single iGPU reports whatever its card index is;
    an unreadable or malformed card is skipped. None rather than 0.0 so the caller
    can drop the reading instead of claiming an idle GPU it cannot actually see."""
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
