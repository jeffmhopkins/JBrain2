"""The host-settings check, and specifically that it catches the silence that cost a freeze.

`ttm.pages_limit` had been set to 124 GiB on a 121 GiB box by our own installer. That is not
a "slightly loose" value — at or above MemTotal the mechanism is INERT, because the
over-commit it exists to refuse can never occur. Nothing in the product said so, and it was
found weeks later by reading a shell script. These pin the check that makes it visible.
"""

from pathlib import Path

from jbrain.host_settings import check_host_settings

_MEM_TOTAL_KB = 127_098_492  # ~121.2 GiB, this box
_PAGES_PER_GIB = 262_144


def _host(tmp_path: Path, *, pages_limit: int | None, **sysctls: int) -> tuple[Path, Path]:
    """A stand-in /proc and /sys. `pages_limit=None` omits the ttm module entirely."""
    proc, sys_path = tmp_path / "proc", tmp_path / "sys"
    (proc / "sys" / "vm").mkdir(parents=True, exist_ok=True)
    (proc / "meminfo").write_text(f"MemTotal:       {_MEM_TOTAL_KB} kB\n")
    for name, value in {
        "min_free_kbytes": 2097152,
        "watermark_scale_factor": 200,
        "swappiness": 10,
        **sysctls,
    }.items():
        (proc / "sys" / "vm" / name).write_text(f"{value}\n")
    if pages_limit is not None:
        params = sys_path / "module" / "ttm" / "parameters"
        params.mkdir(parents=True, exist_ok=True)
        (params / "pages_limit").write_text(f"{pages_limit}\n")
    else:
        sys_path.mkdir(parents=True, exist_ok=True)
    return proc, sys_path


def _by_key(tmp_path: Path, **kwargs: object) -> dict:
    proc, sys_path = _host(tmp_path, **kwargs)  # type: ignore[arg-type]
    return {c.key: c for c in check_host_settings(proc, sys_path)}


def test_a_limit_at_or_above_total_ram_reads_as_disabled(tmp_path: Path) -> None:
    """The exact live misconfiguration: 124 GiB on a 121 GiB box. It must not read as
    'high but fine' — the over-commit can never trip it, so the protection does not exist."""
    ttm = _by_key(tmp_path, pages_limit=124 * _PAGES_PER_GIB)["ttm.pages_limit"]
    assert ttm.ok is False
    assert "DISABLED" in ttm.impact
    assert ttm.needs_host is True, "grub + reboot — an Update cannot fix it"


def test_a_limit_below_total_ram_is_fine(tmp_path: Path) -> None:
    """MemTotal - 16 GiB, what the host script derives."""
    ttm = _by_key(tmp_path, pages_limit=105 * _PAGES_PER_GIB)["ttm.pages_limit"]
    assert ttm.ok is True and "105" in ttm.current


def test_a_limit_exactly_at_total_ram_is_still_disabled(tmp_path: Path) -> None:
    """The boundary is the whole point: equal to MemTotal is as inert as above it.

    Sized off MemTotal itself rather than the nearest whole GiB — 121 GiB is genuinely BELOW
    this box's 121.2 and would (correctly) pass, which is what the first version of this test
    asserted against."""
    exactly = _MEM_TOTAL_KB // 4  # kB -> 4 KiB pages
    assert _by_key(tmp_path, pages_limit=exactly)["ttm.pages_limit"].ok is False


def test_a_box_with_no_gpu_is_not_reported_as_broken(tmp_path: Path) -> None:
    """A cloud-only deploy has no ttm. Flagging that would train the owner to ignore this
    screen, which defeats the reason it exists."""
    ttm = _by_key(tmp_path, pages_limit=None)["ttm.pages_limit"]
    assert ttm.ok is True and "n/a" in ttm.current


def test_the_sysctls_are_checked_in_the_right_direction(tmp_path: Path) -> None:
    """min_free/watermark are floors, swappiness is a ceiling — inverting one would report a
    hardened box as broken and an unhardened one as fine."""
    good = _by_key(tmp_path, pages_limit=105 * _PAGES_PER_GIB)
    assert all(
        good[f"vm.{n}"].ok for n in ("min_free_kbytes", "watermark_scale_factor", "swappiness")
    )

    bad = _by_key(
        tmp_path,
        pages_limit=105 * _PAGES_PER_GIB,
        min_free_kbytes=67584,  # a stock Ubuntu box
        watermark_scale_factor=10,
        swappiness=60,
    )
    assert not any(
        bad[f"vm.{n}"].ok for n in ("min_free_kbytes", "watermark_scale_factor", "swappiness")
    )


def test_the_sysctls_do_not_claim_to_need_a_shell(tmp_path: Path) -> None:
    """They apply from an ordinary Update through the privileged one-shot, so the remedy is
    a button. Only the boot parameter is genuinely host-bound, and conflating the two would
    make the owner think nothing here is actionable."""
    checks = _by_key(tmp_path, pages_limit=105 * _PAGES_PER_GIB)
    assert checks["vm.swappiness"].needs_host is False
    assert "Update" in checks["vm.swappiness"].remedy


def test_failures_sort_first(tmp_path: Path) -> None:
    """A screen that shows one row must show the broken one."""
    proc, sys_path = _host(tmp_path, pages_limit=124 * _PAGES_PER_GIB)
    assert check_host_settings(proc, sys_path)[0].ok is False


def test_an_unreadable_host_reports_unknown_rather_than_healthy(tmp_path: Path) -> None:
    """Best-effort must not mean optimistic: a check that cannot read its input has to say
    so, or the screen quietly asserts a box is safe when nobody looked."""
    empty = tmp_path / "nothing"
    (empty / "sys").mkdir(parents=True)
    checks = {c.key: c for c in check_host_settings(empty, empty / "sys")}
    assert checks["vm.swappiness"].current == "unknown"
    assert checks["vm.swappiness"].ok is False
