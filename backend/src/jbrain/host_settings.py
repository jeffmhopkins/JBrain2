"""Host settings the app depends on but cannot apply, checked and surfaced.

Some of this box's protection lives in the kernel command line and in sysctls. The app reads
them, relies on them, and — for the boot parameters — has no way to change them: they need
`/etc/default/grub` and a reboot. That is a real terminal dependency, and CLAUDE.md #10 says
a terminal dependency is a gap to design out. Where it cannot be removed, the next best thing
is to stop it being SILENT.

It was silent, and it cost a power cycle. `ttm.pages_limit` was set to 124 GiB on a 121 GiB
box by our own installer, which disables it: the limit exists to turn a GTT over-commit into
a clean -ENOSPC before the pages are handed out, and above MemTotal that condition never
trips. Nothing in the product said so. The owner found out because a freeze was investigated
three weeks later and someone read the script.

So this module answers "is the host actually configured the way the app assumes", and the
Ops screen shows it. Read-only, best-effort, and honest: a value it cannot read reports as
unknown rather than as fine.

Everything here comes from /proc and /sys, which Docker maps from the host and does not
namespace for these paths, so the api container reads the HOST's real values — the same
arrangement `host_metrics` already relies on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Pages are 4 KiB; ttm counts its limit in pages.
_KIB_PER_PAGE = 4
_KIB_PER_GIB = 1024 * 1024

# What the host script reserves for everything that is not the GPU
# (scripts/strix-halo-host-setup.sh derives ttm.pages_limit as MemTotal minus this).
HOST_RESERVE_GIB = 16


@dataclass(frozen=True)
class HostSetting:
    """One checked setting. `ok=False` means the app's assumption does not hold."""

    key: str
    current: str
    expected: str
    ok: bool
    # What breaks when it is wrong — the reason this is worth a row on a screen.
    impact: str
    # What the owner has to do. Named honestly, including when it needs the host.
    remedy: str
    # True when applying it needs a shell the owner does not have. These are the ones worth
    # surfacing loudest, because no amount of updating will fix them.
    needs_host: bool = False


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _mem_total_gib(proc: Path) -> float | None:
    text = _read(proc / "meminfo")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) / _KIB_PER_GIB
    return None


def _ttm_pages_limit(sys_path: Path, mem_total_gib: float | None) -> HostSetting:
    """The GTT over-commit ceiling — the setting whose silence cost a power cycle.

    Checked against MemTotal rather than against a target: any value at or above it is
    inert, and that is the failure that matters. A value merely higher than we would choose
    is worth noting; a value that disables the mechanism is worth shouting about."""
    module = sys_path / "module" / "ttm"
    # Only meaningful where ttm is loaded. A cloud-only box has no GPU and no ttm, and
    # reporting that as a failure would train the owner to ignore this screen — which is the
    # opposite of why it exists.
    if not module.exists():
        return HostSetting(
            key="ttm.pages_limit",
            current="n/a — no ttm module",
            expected="n/a",
            ok=True,
            impact="No GPU driver on this box, so there is no GTT to bound.",
            remedy="",
        )
    headroom = (
        f" (we set {mem_total_gib - HOST_RESERVE_GIB:.0f})"
        if mem_total_gib and mem_total_gib > HOST_RESERVE_GIB
        else ""
    )
    expected = (
        f"< {mem_total_gib:.0f} GiB{headroom}"
        if mem_total_gib
        else f"MemTotal less {HOST_RESERVE_GIB} GiB"
    )
    raw = _read(module / "parameters" / "pages_limit")
    if raw is None or not raw.isdigit():
        return HostSetting(
            key="ttm.pages_limit",
            current="unknown",
            expected=expected,
            ok=False,
            impact="Cannot tell whether GTT over-commit is bounded.",
            remedy="The ttm module is loaded but its parameter is unreadable.",
            needs_host=True,
        )
    limit_gib = int(raw) * _KIB_PER_PAGE / _KIB_PER_GIB
    inert = mem_total_gib is not None and limit_gib >= mem_total_gib
    return HostSetting(
        key="ttm.pages_limit",
        current=f"{limit_gib:.0f} GiB",
        expected=expected,
        ok=not inert,
        impact=(
            "DISABLED — the limit is at or above total RAM, so a GTT over-commit cannot be "
            "refused and becomes a reclaim livelock instead of a clean error. This is the "
            "2026-08-19 freeze."
            if inert
            else "Bounds GTT so an over-commit fails cleanly instead of livelocking."
        ),
        remedy=(
            "Edit ttm.pages_limit in /etc/default/grub, run update-grub, reboot. An Update "
            "corrects it for the CURRENT boot where the kernel allows it, but only grub "
            "makes it survive one."
        ),
        needs_host=True,
    )


def _sysctl(proc: Path, name: str, want: int, *, at_least: bool, impact: str) -> HostSetting:
    raw = _read(proc / "sys" / "vm" / name)
    ok = raw is not None and raw.isdigit() and (int(raw) >= want if at_least else int(raw) <= want)
    return HostSetting(
        key=f"vm.{name}",
        current=raw or "unknown",
        expected=f"{'>=' if at_least else '<='} {want}",
        ok=ok,
        impact=impact,
        # These DO apply from an ordinary Update (update-inner.sh writes them through a
        # privileged one-shot), so the remedy is a button rather than a shell.
        remedy="Ops -> Update applies this; deploy/oom-hardening.sh makes it survive a reboot.",
    )


def check_host_settings(
    proc: Path = Path("/proc"), sys_path: Path = Path("/sys")
) -> list[HostSetting]:
    """Every host setting the app assumes, with whether it currently holds.

    Ordered worst-first so a screen showing only the top row shows the one that matters."""
    mem_total_gib = _mem_total_gib(proc)
    checks = [
        _ttm_pages_limit(sys_path, mem_total_gib),
        _sysctl(
            proc,
            "min_free_kbytes",
            2097152,
            at_least=True,
            impact="Reclaim starts earlier, so the killer gets CPU before a livelock.",
        ),
        _sysctl(
            proc,
            "watermark_scale_factor",
            200,
            at_least=True,
            impact="Widens the gap reclaim works in, so it thrashes less under pressure.",
        ),
        _sysctl(
            proc,
            "swappiness",
            10,
            at_least=False,
            impact="Keeps the box out of swap, where every service ran for seven hours.",
        ),
    ]
    return sorted(checks, key=lambda c: (c.ok, not c.needs_host))
