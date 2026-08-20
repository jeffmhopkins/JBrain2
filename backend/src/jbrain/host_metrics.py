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

    `used = MemTotal - MemFree - SReclaimable`. **Page cache counts as USED.**

    This deliberately does NOT use `MemAvailable`, which is what it used to do, on the
    reasoning that MemAvailable "excludes reclaimable page cache, so it reflects real memory
    pressure". On this box that reasoning is exactly inverted, and it cost a seven-hour
    livelock and a power cycle on 2026-08-19.

    Measured during a single gpt-oss-120b load on the 121 GiB box:

        MemAvailable  35.7 GiB      <- what the budget believed it had
        MemFree        8.0 GiB      <- what it actually had
        Cached        39.4 GiB      <- a second copy of the model weights
        GTT           67.6 GiB      <- the first copy

    The gateway serves with `--no-mmap` (a gfx1151 stability flag), so llama.cpp reads each
    GGUF rather than mapping it and the weights end up resident twice. Reclaiming that cache
    is not the cheap operation MemAvailable assumes: the iGPU has most of RAM pinned as GTT,
    and reclaim under that pressure is precisely what livelocks this host — the failure this
    runbook has documented since the first freeze. The budget read 35.7, admitted another
    model, and the box spent seven hours swapping every container in and out before it died.

    So the honest question for "can I load another model" is how many pages are FREE, not how
    many the kernel thinks it could free. `SReclaimable` is still credited: dentry/inode slab
    really is cheap to give back and is not part of this failure mode.

    The cost of the conservatism is small and shrinking: `local_weights.drop_weights_page_cache`
    now drops each model's cache copy as its load finishes, so the remaining page cache is
    ordinary (Postgres, logs) and was 5.2 GiB at idle on this box. If that drop ever stops
    working, this number notices instead of being blind to it."""
    wanted = ("MemTotal", "MemFree", "SReclaimable")
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
    # SReclaimable is absent on older/uncommon kernels; treat it as zero rather than
    # refusing to report at all, which would leave the budget with no number.
    if "MemTotal" not in fields or "MemFree" not in fields:
        return None
    total = fields["MemTotal"] / _KB_PER_GIB
    free = (fields["MemFree"] + fields.get("SReclaimable", 0)) / _KB_PER_GIB
    return round(total, 1), round(total - free, 1)


def read_page_cache_gb(path: str = "/proc/meminfo") -> float | None:
    """`Cached` from /proc/meminfo, in GiB — or None when it can't be read.

    Reported SEPARATELY from `read_memory_gb`'s used figure, which counts this as used (see
    above) and must keep doing so. This exists because the settings meter drew `used` minus the
    catalog's estimate for each resident model and labelled the whole remainder "System + other
    containers (OS, database, on-box services)". Page cache is neither the OS nor a container,
    and with `--no-mmap` a single model load puts TENS of GB there — measured 39.4 GiB during
    one gpt-oss-120b load. So the segment that named the OS was in fact mostly a second copy of
    the model weights, and an operator watching it bloat had no way to see that from the meter.

    Deliberately the whole `Cached` figure rather than a weights-only slice: nothing on the box
    can attribute page cache to a file cheaply (`cachestat(2)` is per-fd and blocked in the API
    container by seccomp anyway), and a number split by guesswork would recreate the problem
    this is fixing. Postgres and the logs are in here too, and the meter says so."""
    try:
        with open(path) as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key == "Cached":
                    return round(int(rest.strip().split()[0]) / _KB_PER_GIB, 1)
    except (OSError, ValueError, IndexError):
        return None
    return None


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
