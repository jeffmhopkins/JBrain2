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
_HARDENING_SETUP = _DEPLOY.parent / "scripts" / "strix-halo-host-setup.sh"


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


def _merge(grub_text: str, params: str, tmp_path: Path) -> tuple[str, str]:
    """Run the host script's grub-merge block verbatim, so the test cannot drift from it."""
    import os
    import re as _re
    import subprocess
    import sys

    marker = (
        'ADDED="$(GRUB_FILE="$GRUB_FILE" GRUB_TMP="$GRUB_TMP" PARAMS="$PARAMS" python3 - <<\'PY\'\n'
    )
    body = _HARDENING_SETUP.read_text().split(marker, 1)[1].split("\nPY\n", 1)[0]
    src, out = tmp_path / "grub", tmp_path / "out"
    src.write_text(grub_text)
    result = subprocess.run(
        [sys.executable, "-c", body],
        env={**os.environ, "GRUB_FILE": str(src), "GRUB_TMP": str(out), "PARAMS": params},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    line = _re.search(r'^GRUB_CMDLINE_LINUX_DEFAULT="(.*)"$', out.read_text(), _re.M)
    assert line
    return result.stdout.strip(), line.group(1)


def test_an_existing_pages_limit_is_replaced_not_skipped(tmp_path: Path) -> None:
    """The bug this closes made the script a no-op on the one box that needed it.

    Merging by key-ABSENCE meant a host already carrying `ttm.pages_limit=32505856`
    (124 GiB — above MemTotal, therefore binding nothing) printed "already present —
    nothing to do" and changed nothing, forever. The value is load-bearing, so it has to
    be replaced."""
    before = 'GRUB_CMDLINE_LINUX_DEFAULT="quiet amd_iommu=off ttm.pages_limit=32505856"\n'
    changed, after = _merge(before, "amd_iommu=off ttm.pages_limit=27262976", tmp_path)
    assert "ttm.pages_limit=27262976" in after
    assert "32505856" not in after, "the stale, non-binding value survived"
    assert "quiet" in after and "amd_iommu=off" in after, "unrelated params were dropped"
    assert changed, "a real change reported nothing, so the operator sees no diff to confirm"


def test_the_deprecated_gttsize_is_removed(tmp_path: Path) -> None:
    """The kernel itself prints a deprecation warning pointing at ttm.pages_limit, and
    carrying both invites the two disagreeing."""
    before = 'GRUB_CMDLINE_LINUX_DEFAULT="amdgpu.gttsize=126976 ttm.pages_limit=32505856"\n'
    _changed, after = _merge(before, "ttm.pages_limit=27262976", tmp_path)
    assert "gttsize" not in after


def test_the_merge_is_idempotent(tmp_path: Path) -> None:
    """A second run must report nothing, or the operator is asked to confirm a no-op
    reboot every time."""
    before = 'GRUB_CMDLINE_LINUX_DEFAULT="quiet ttm.pages_limit=27262976"\n'
    changed, after = _merge(before, "ttm.pages_limit=27262976", tmp_path)
    assert changed == "", f"reported a change against an already-correct file: {changed}"
    assert "ttm.pages_limit=27262976" in after


def test_no_runtime_write_of_pages_limit_returns() -> None:
    """`man->size` is snapshotted at probe from ttm_tt_pages_limit(), so a write to
    /sys/module/ttm/parameters/pages_limit succeeds, reads back the new value, and moves
    no cap. The update used to do exactly that and report it as a fix."""
    text = _UPDATE.read_text()
    live = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    assert "ttm/parameters/pages_limit" not in live, (
        "the update is writing ttm.pages_limit at runtime again — it cannot work, and it "
        "reports success while changing nothing"
    )
