"""The OS-level memory backstop, and the fact that the PWA can apply it.

Two claims in the deploy scripts said "a test pins the two together" and no such test
existed, so the host script and the containerized fallback were free to drift — which is
exactly how a safety setting ends up applied on one path and not the other.

The delivery question matters as much as the values. The owner runs this box remotely with
no terminal (CLAUDE.md #10), so a hardening step reachable only from `oom-hardening.sh` is a
step that does not happen on their machine. `update-inner.sh` must apply the same thresholds
from its containerized branch, which is the one the PWA's Update button runs.
"""

import re
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
_UPDATE = _DEPLOY / "update-inner.sh"
_HARDENING = _DEPLOY / "oom-hardening.sh"


def _earlyoom_args(text: str) -> str:
    """The single-quoted EARLYOOM_ARGS="..." line, however the script emits it."""
    match = re.search(r"'(EARLYOOM_ARGS=\"[^\"]+\")'", text)
    assert match, "no EARLYOOM_ARGS line found"
    return match.group(1)


def test_the_two_paths_write_the_same_earlyoom_arguments() -> None:
    """Character-identical. A drift here means the box is hardened differently depending on
    which path last ran, and nothing would say so."""
    assert _earlyoom_args(_UPDATE.read_text()) == _earlyoom_args(_HARDENING.read_text())


def test_the_thresholds_can_actually_be_reached() -> None:
    """earlyoom kills only when memory AND swap are both under their limits, and its `-m`
    reads MemAvailable — which counts page cache as free.

    On 2026-08-19 this box livelocked for seven hours with swap 100% consumed (so `-s` was
    satisfied throughout) while MemAvailable held at 29%. The then-current `-m 10` was never
    approached, the AND never closed, and the backstop the residency design leans on did not
    fire once. The memory limit has to sit ABOVE the level a livelocking box reports, and the
    swap limit is what keeps that safe — a healthy box has ~100% swap free, so the AND never
    closes there regardless of the memory figure."""
    args = _earlyoom_args(_HARDENING.read_text())
    mem = [int(v) for v in re.findall(r"-m (\d+)", args)]
    swap = [int(v) for v in re.findall(r"-s (\d+)", args)]
    assert len(mem) == 2 and len(swap) == 2, args
    # earlyoom takes the FIRST -m as the SIGTERM threshold and the second as SIGKILL, so the
    # one that has to be reachable is the higher of the two; SIGKILL sits below it as the
    # escalation when a polite kill did not free anything.
    term, kill = mem
    assert term > kill, f"SIGTERM must trigger before SIGKILL, got {term}/{kill}"
    assert term > 29, (
        f"SIGTERM memory threshold {term}% is at or below the 29% a livelocking box "
        "reported on 2026-08-19 — it could not fire"
    )
    assert max(swap) <= 10, (
        f"swap threshold {max(swap)}% is too loose to gate a memory limit this high — a "
        "healthy box must not trip it"
    )
    # The victim and the protected set are the other half of it being safe to fire at all.
    assert "--prefer ^llama-server$" in args
    assert "postgres" in args and "dockerd" in args


def test_the_pwa_path_applies_earlyoom_not_just_the_sysctls() -> None:
    """The gap this closes. `oom-hardening.sh` is gated on HOST_UPDATE because it needs apt
    and /etc/sysctl.d; the containerized branch used to apply only the vm.* knobs, so an
    Update from the PWA left earlyoom at whatever a past host install wrote."""
    text = _UPDATE.read_text()
    assert "host_file_write /etc/default/earlyoom" in text, (
        "the containerized branch no longer applies earlyoom's thresholds — the PWA is the "
        "only update path the owner can run (CLAUDE.md #10)"
    )
    # And it must be in the branch that runs WITHOUT HOST_UPDATE, i.e. after the else.
    containerized = text.split("applying reclaim headroom for this boot", 1)[1]
    assert "host_file_write /etc/default/earlyoom" in containerized


def test_the_reclaim_knobs_agree_across_both_paths() -> None:
    """Same drift risk as the earlyoom line: these are applied by both paths and were only
    ever kept in step by a comment."""
    update, hardening = _UPDATE.read_text(), _HARDENING.read_text()
    for knob, value in (
        ("min_free_kbytes", "2097152"),
        ("watermark_scale_factor", "200"),
        ("swappiness", "10"),
    ):
        for name, text in (("update-inner.sh", update), ("oom-hardening.sh", hardening)):
            found = re.search(rf"{knob}\D{{0,4}}(\d+)", text)
            assert found, f"{knob} is not set at all in {name}"
            assert found.group(1) == value, f"{knob} is {found.group(1)} in {name}, want {value}"
