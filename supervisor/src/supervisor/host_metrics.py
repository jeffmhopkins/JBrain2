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
    # Fan speeds in RPM keyed by sensor label, or None when the host exposes no
    # fan telemetry at all — the card omits the row rather than imply a dead fan.
    fan_rpm: dict[str, int] | None = None
    # APU/SoC package power in watts (amdgpu power1_average), or None when absent.
    # On Strix Halo the CPU+iGPU share one die, so this is the whole-chip draw —
    # the dominant consumer, though not wall power.
    apu_power_w: float | None = None
    # Bytes the iGPU has pinned out of system RAM through the amdgpu GTT (and the
    # small VRAM carveout), or None when the driver exposes no mem_info counters.
    # On Strix Halo the iGPU has no dedicated VRAM — a loaded model's device
    # buffers live in GTT, which is carved from the same unified RAM the meter
    # totals. GTT is NOT process-resident, so the per-process RSS table can never
    # show it: this is the honest answer to the gap between "memory used" and the
    # sum of the processes, and — because it should fall back to ~0 once every
    # model unloads — the read that reveals device memory a teardown failed to
    # release.
    gpu_mem: GpuMem | None = None
    # A curated slice of /proc/meminfo (bytes) beyond total/available: the fields
    # that attribute "used" to a kind — Shmem/tmpfs, anonymous, kernel slab,
    # unevictable, page cache. None only when meminfo can't be read. Lets the
    # breakdown say WHICH category holds the RAM the total can't explain.
    mem_breakdown: dict[str, int] | None = None
    # Cumulative bytes received/transmitted since boot, summed over physical
    # interfaces (loopback and docker veth/bridge excluded so container-internal
    # traffic isn't double-counted). None off Linux / when /proc/net/dev is
    # unreadable. These are monotonic counters — the sampler derives a throughput
    # RATE from the delta between ticks; the raw counter alone isn't the graph.
    net: NetCounters | None = None
    # Cumulative bytes read/written since boot, summed over whole physical block
    # devices (partitions and virtual loop/ram/dm devices excluded so the same I/O
    # isn't counted twice). None when /proc/diskstats is unreadable. Monotonic —
    # the sampler turns the delta into a read/write throughput rate.
    disk_io: DiskCounters | None = None


@dataclass(frozen=True, slots=True)
class NetCounters:
    """Monotonic byte counters summed across the host's physical interfaces."""

    rx_bytes: int
    tx_bytes: int


@dataclass(frozen=True, slots=True)
class DiskCounters:
    """Monotonic byte counters summed across the host's whole block devices."""

    read_bytes: int
    write_bytes: int


@dataclass(frozen=True, slots=True)
class GpuMem:
    """amdgpu unified-memory counters, summed across DRM cards (bytes).

    On an APU the iGPU draws from system RAM: `gtt_used` is the translation-table
    pool (the bulk of a resident model's device footprint) and `vram_used` is the
    small stolen-VRAM carveout. Totals are the driver's ceilings, useful only as
    context for the used figures."""

    gtt_used_bytes: int
    gtt_total_bytes: int
    vram_used_bytes: int
    vram_total_bytes: int


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


# The /proc/meminfo lines (kB) the breakdown keeps: enough to attribute "used" to
# a kind without shipping the whole file. Shmem/tmpfs and unevictable are the
# usual suspects for RAM that no process shows as RSS yet MemAvailable excludes.
_MEMINFO_BREAKDOWN = (
    "MemFree",
    "Buffers",
    "Cached",
    "Shmem",
    "AnonPages",
    "Mapped",
    "Slab",
    "SReclaimable",
    "SUnreclaim",
    "KReclaimable",
    "Unevictable",
    "Mlocked",
)


# Interface-name prefixes whose traffic is host-internal plumbing, not the box's
# real network activity: loopback, and Docker's bridge + the veth halves of every
# container. Summing them would double-count (a container's bytes also cross a
# veth) and swamp the uplink, so the throughput graph excludes them.
_NET_SKIP_PREFIXES = ("lo", "veth", "docker", "br-")


def read_net_counters(proc: Path = Path("/proc")) -> NetCounters | None:
    """Cumulative rx/tx bytes summed over physical interfaces from /proc/net/dev,
    or None if it can't be read. Skips loopback and Docker veth/bridge devices (see
    _NET_SKIP_PREFIXES) so the total reflects the box's real uplink, not the
    container-internal traffic that also crosses a veth.

    The counters are monotonic (since boot); the throughput a graph wants is their
    delta over time, which the sampler computes — this only exposes the raw totals."""
    try:
        lines = (proc / "net" / "dev").read_text().splitlines()
    except OSError:
        return None
    rx_total = tx_total = 0
    seen = False
    for line in lines:
        name, _, rest = line.partition(":")
        name = name.strip()
        if not rest or name.startswith(_NET_SKIP_PREFIXES):
            continue  # header rows have no ':'; skip loopback + docker plumbing
        fields = rest.split()
        # /proc/net/dev columns: rx_bytes is [0], tx_bytes is [8] (rx has 8 fields).
        if len(fields) < 9:
            continue
        try:
            rx_total += int(fields[0])
            tx_total += int(fields[8])
        except ValueError:
            continue
        seen = True
    if not seen:
        return None
    return NetCounters(rx_bytes=rx_total, tx_bytes=tx_total)


# Block-device name prefixes that are virtual or duplicate the physical disk's
# I/O: loopback mounts, ramdisks, device-mapper (LVM/crypt sits ON a real disk, so
# counting it double-counts), zram swap, and optical. Whole physical disks
# (nvme0n1, sda, mmcblk0) are kept; their partitions aren't in /sys/block's top
# level, so the /sys/block membership test already excludes those.
_DISK_SKIP_PREFIXES = ("loop", "ram", "dm-", "zram", "sr", "md")

# A disk sector is 512 bytes in /proc/diskstats regardless of the device's real
# logical block size — the kernel reports these fields in 512B units by convention.
_SECTOR_BYTES = 512


def read_disk_counters(
    proc: Path = Path("/proc"), sysblock: Path = Path("/sys/block")
) -> DiskCounters | None:
    """Cumulative bytes read/written summed over whole physical block devices from
    /proc/diskstats, or None if it can't be read. A device counts only when it is a
    top-level /sys/block entry (so partitions, which live under it, are excluded)
    and isn't virtual (see _DISK_SKIP_PREFIXES) — otherwise a partition's and its
    disk's I/O, or a dm layer's and its backing disk's, would be summed twice.

    Monotonic since boot; the sampler derives the read/write throughput rate from
    the delta, so this exposes only the raw byte totals (sectors x 512)."""
    try:
        lines = (proc / "diskstats").read_text().splitlines()
    except OSError:
        return None
    read_total = write_total = 0
    seen = False
    for line in lines:
        fields = line.split()
        # /proc/diskstats: [2]=device name, [5]=sectors read, [9]=sectors written.
        if len(fields) < 10:
            continue
        name = fields[2]
        if name.startswith(_DISK_SKIP_PREFIXES) or not (sysblock / name).exists():
            continue
        try:
            read_total += int(fields[5]) * _SECTOR_BYTES
            write_total += int(fields[9]) * _SECTOR_BYTES
        except ValueError:
            continue
        seen = True
    if not seen:
        return None
    return DiskCounters(read_bytes=read_total, write_bytes=write_total)


def read_amdgpu_mem(drm: Path = Path("/sys/class/drm")) -> GpuMem | None:
    """amdgpu GTT/VRAM usage (bytes) summed across DRM cards, or None if the driver
    exposes no `mem_info_*` counters (non-AMD box / no GPU / /sys not exposed).

    The values live at /sys/class/drm/card*/device/mem_info_{gtt,vram}_{used,total}
    and are already in bytes. We sum across cards so a single iGPU reports
    regardless of its card index, and skip any card whose files are missing or
    malformed. None (not a zeroed struct) means "no GPU-memory telemetry here" so
    the caller can drop the attribution rather than imply the iGPU holds nothing."""
    fields = (
        "mem_info_gtt_used",
        "mem_info_gtt_total",
        "mem_info_vram_used",
        "mem_info_vram_total",
    )
    totals = {f: 0 for f in fields}
    seen = False
    try:
        devices = sorted({p.parent for p in drm.glob("card*/device/mem_info_gtt_used")})
    except OSError:
        return None
    for device in devices:
        values: dict[str, int] = {}
        for field in fields:
            try:
                values[field] = int((device / field).read_text().strip())
            except (OSError, ValueError):
                break
        else:  # every field parsed for this card — count it
            seen = True
            for field, value in values.items():
                totals[field] += value
    if not seen:
        return None
    return GpuMem(
        gtt_used_bytes=totals["mem_info_gtt_used"],
        gtt_total_bytes=totals["mem_info_gtt_total"],
        vram_used_bytes=totals["mem_info_vram_used"],
        vram_total_bytes=totals["mem_info_vram_total"],
    )


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


def _fan_label(input_path: Path) -> str:
    """Human label for a fan*_input: its fan*_label if the chip provides one, else
    the hwmon chip `name` plus the fan index (e.g. "amdgpu fan1") so fans from
    different chips stay distinguishable."""
    label_path = input_path.with_name(input_path.name.replace("_input", "_label"))
    try:
        text = label_path.read_text().strip()
    except OSError:
        text = ""
    if text:
        return text
    # input_path.name is like "fan1_input"; drop the suffix to get "fan1".
    fan = input_path.name.removesuffix("_input")
    try:
        chip = (input_path.parent / "name").read_text().strip()
    except OSError:
        chip = ""
    return f"{chip} {fan}".strip()


def read_fan_rpm(hwmon: Path = Path("/sys/class/hwmon")) -> dict[str, int] | None:
    """Fan speeds in RPM keyed by sensor label, or None when no fan telemetry is
    present (a VM, a fanless box, or /sys not exposed) — the caller drops the row
    rather than imply a stalled fan.

    Reads the standard hwmon RPM gauge at /sys/class/hwmon/hwmon*/fan*_input; on
    Strix Halo the EC fan(s) surface here. A reading of 0 is kept (a genuinely
    stopped fan), unlike the None that means "no telemetry at all". Duplicate
    labels across chips are disambiguated with a numeric suffix."""
    fans: dict[str, int] = {}
    try:
        inputs = sorted(hwmon.glob("hwmon*/fan*_input"))
    except OSError:
        return None
    for path in inputs:
        try:
            rpm = int(path.read_text().strip())
        except (OSError, ValueError):
            continue
        label = _fan_label(path)
        key, n = label, 2
        while key in fans:
            key = f"{label} ({n})"
            n += 1
        fans[key] = rpm
    return fans or None


def read_apu_power_w(hwmon: Path = Path("/sys/class/hwmon")) -> float | None:
    """APU package power in watts from the amdgpu hwmon's `power1_average` (µW), or
    None when absent (non-AMD box / no /sys). On Strix Halo the CPU+iGPU are one
    die, so this socket average is the whole-APU draw — the dominant consumer,
    though not the wall total. Falls back to `power1_input` if no average."""
    try:
        chips = sorted(hwmon.glob("hwmon*"))
    except OSError:
        return None
    for chip in chips:
        try:
            if (chip / "name").read_text().strip() != "amdgpu":
                continue
        except OSError:
            continue
        for attr in ("power1_average", "power1_input"):
            try:
                microwatts = int((chip / attr).read_text().strip())
            except (OSError, ValueError):
                continue
            return round(microwatts / 1_000_000, 3)
    return None


def read_host_metrics(
    proc: Path = Path("/proc"),
    disk_path: str = "/",
    drm: Path = Path("/sys/class/drm"),
    hwmon: Path = Path("/sys/class/hwmon"),
    sysblock: Path = Path("/sys/block"),
) -> HostMetrics:
    mem = _meminfo_kb((proc / "meminfo").read_text())
    load_parts = (proc / "loadavg").read_text().split()
    uptime = float((proc / "uptime").read_text().split()[0])
    vfs = os.statvfs(disk_path)
    breakdown = {k: mem[k] * 1024 for k in _MEMINFO_BREAKDOWN if k in mem}
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
        fan_rpm=read_fan_rpm(hwmon),
        apu_power_w=read_apu_power_w(hwmon),
        gpu_mem=read_amdgpu_mem(drm),
        mem_breakdown=breakdown or None,
        net=read_net_counters(proc),
        disk_io=read_disk_counters(proc, sysblock),
    )
