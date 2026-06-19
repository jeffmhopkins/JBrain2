"""Host memory telemetry for the local-model memory meter.

Reads /proc/meminfo directly — this is host system telemetry, not application
data, so it sits outside the storage abstraction (which governs blobs/backups).
On AMD Strix Halo the iGPU draws from unified system RAM, so total/used system
memory is the honest gauge for "can I load another model". Best-effort: returns
None off Linux or when the fields are missing, and the caller renders without a
meter.
"""

from __future__ import annotations

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
