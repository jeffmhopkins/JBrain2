"""backup.sh retention and the skip-unchanged blob tarball, run for real in a tmp dir.

backup.sh keeps its pruning and fingerprint compare as POSIX functions and stops
before touching docker when sourced with BACKUP_LIB_ONLY=1, so these drive the
shipped code against fake files with a fixed "now" rather than a copy of it.
restore.sh's tarball pick is lifted out of the script by its markers for the same
reason: the rule that matters is the one the box runs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or shutil.which("bash") is None,
    reason="needs sh and bash",
)

NOW = datetime(2026, 10, 4, 12, 0, 0, tzinfo=UTC)
# Not UTC on purpose: stamps must come out UTC whatever zone the writer runs in.
ENV = {**os.environ, "TZ": "America/New_York", "BACKUP_LIB_ONLY": "1"}


def _stamp(at: datetime) -> str:
    return at.strftime("%Y%m%d-%H%M%S")


def _lib(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "-c", f'set -eu; . "{DEPLOY / "backup.sh"}"; {script}'],
        cwd=cwd,
        env=ENV,
        capture_output=True,
        text=True,
        check=False,
    )


def _prune(d: Path, keep_days: int = 14) -> subprocess.CompletedProcess[str]:
    run = _lib(f"prune_backups . {int(NOW.timestamp())} {keep_days}", d)
    assert run.returncode == 0, run.stderr
    return run


def _names(d: Path, prefix: str, suffix: str) -> set[str]:
    return {
        p.name[len(prefix) : -len(suffix)]
        for p in d.iterdir()
        if p.name.startswith(prefix) and p.name.endswith(suffix)
    }


def test_keeps_everything_recent_then_newest_per_day(tmp_path: Path) -> None:
    # Fourteen updates a day for twenty days: the load that filled the disk.
    stamps = [
        _stamp(NOW - timedelta(days=day, hours=hour))
        for day in range(20)
        for hour in range(0, 24, 2)
        if (NOW - timedelta(days=day, hours=hour)) <= NOW
    ]
    for s in stamps:
        (tmp_path / f"jbrain-{s}.dump").touch()
    (tmp_path / "backup.log").touch()

    _prune(tmp_path)

    kept = _names(tmp_path, "jbrain-", ".dump")
    recent = {s for s in stamps if NOW - _parse(s) <= timedelta(hours=48)}
    assert recent <= kept
    older = kept - recent
    # One per calendar day, the newest of it, back to the 14-day floor and no further.
    days = [s[:8] for s in older]
    assert len(days) == len(set(days))
    for s in older:
        same_day = [t for t in stamps if t[:8] == s[:8]]
        assert s == max(same_day)
    floor = (NOW - timedelta(days=14)).strftime("%Y%m%d")
    assert min(days) == floor
    assert all(s[:8] >= floor for s in kept)
    # A day whose newest backup sits inside the 48 h window keeps nothing older.
    two_days_ago = (NOW - timedelta(days=2)).strftime("%Y%m%d")
    assert {s for s in older if s[:8] == two_days_ago} == set()
    # Files that are not ours are never touched.
    assert (tmp_path / "backup.log").exists()


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)


def test_the_only_dump_of_a_day_survives(tmp_path: Path) -> None:
    lone = _stamp(NOW - timedelta(days=9, hours=3))
    (tmp_path / f"jbrain-{lone}.dump").touch()
    _prune(tmp_path)
    assert _names(tmp_path, "jbrain-", ".dump") == {lone}


def test_blob_tarballs_follow_the_same_rule_and_take_their_sha(tmp_path: Path) -> None:
    morning = _stamp(NOW - timedelta(days=5, hours=6))
    evening = _stamp(NOW - timedelta(days=5, hours=1))
    for s in (morning, evening):
        (tmp_path / f"jbrain-{s}.dump").touch()
        (tmp_path / f"blobs-{s}.tar.gz").touch()
        (tmp_path / f"blobs-{s}.sha").write_text("fp\n")

    run = _prune(tmp_path)

    assert _names(tmp_path, "blobs-", ".tar.gz") == {evening}
    assert _names(tmp_path, "blobs-", ".sha") == {evening}
    assert f"pruned blobs-{morning}.tar.gz" in run.stdout


def test_a_tarball_a_kept_dump_restores_with_is_never_pruned(tmp_path: Path) -> None:
    # Blobs unchanged for weeks: one old tarball covers every dump since. Its own day
    # is past the floor, but the oldest kept dump restores with it.
    old_blob = _stamp(NOW - timedelta(days=30))
    (tmp_path / f"blobs-{old_blob}.tar.gz").touch()
    dumps = [_stamp(NOW - timedelta(days=d)) for d in range(0, 20)]
    for s in dumps:
        (tmp_path / f"jbrain-{s}.dump").touch()
    gone_blob = _stamp(NOW - timedelta(days=31))
    (tmp_path / f"blobs-{gone_blob}.tar.gz").touch()

    _prune(tmp_path)

    assert _names(tmp_path, "blobs-", ".tar.gz") == {old_blob}


def test_older_than_keep_days_goes(tmp_path: Path) -> None:
    old = _stamp(NOW - timedelta(days=15))
    (tmp_path / f"jbrain-{old}.dump").touch()
    (tmp_path / f"blobs-{old}.tar.gz").touch()
    _prune(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_blobs_unchanged_compares_the_newest_tarballs_fingerprint(
    tmp_path: Path,
) -> None:
    older, newest = "20261001-010000", "20261003-010000"
    for s in (older, newest):
        (tmp_path / f"blobs-{s}.tar.gz").touch()
    (tmp_path / f"blobs-{older}.sha").write_text("same\n")
    (tmp_path / f"blobs-{newest}.sha").write_text("abc\n")

    same = _lib("blobs_unchanged_since . abc", tmp_path)
    assert same.returncode == 0 and same.stdout.strip() == newest
    # Only the NEWEST counts: matching an older one means the volume changed back,
    # and the newest tarball is not what it holds.
    assert _lib("blobs_unchanged_since . same", tmp_path).returncode == 1
    # An empty fingerprint is a failed probe, which must always archive.
    assert _lib("blobs_unchanged_since . ''", tmp_path).returncode == 1


def test_a_tarball_without_a_fingerprint_never_reads_unchanged(tmp_path: Path) -> None:
    (tmp_path / "blobs-20261003-010000.tar.gz").touch()
    assert _lib("blobs_unchanged_since . abc", tmp_path).returncode == 1


FAKE_DOCKER = r"""#!/bin/sh
# Stands in for the docker CLI backup.sh calls; behaviour set by FAKE_* env vars.
case "$1" in
  compose)
    [ -n "${FAKE_PGDUMP_FAIL:-}" ] && { printf 'partial'; exit 1; }
    printf 'dump-bytes'
    ;;
  volume)
    [ -n "${FAKE_NO_VOLUME:-}" ] && exit 1
    exit 0
    ;;
  run)
    case "$*" in
      *sha256sum*)
        printf '%s  -\n' "${FAKE_FP:-}"
        ;;
      *"tar czf"*)
        out=""
        dest=""
        for a in "$@"; do
          case "$a" in
            *:/out) out="${a%:/out}" ;;
            /out/*) dest="${a#/out/}" ;;
          esac
        done
        printf 'tar' > "$out/$dest"
        [ -n "${FAKE_TAR_FAIL:-}" ] && exit 1
        exit 0
        ;;
    esac
    ;;
esac
"""


def _backup(root: Path, **fake: str) -> subprocess.CompletedProcess[str]:
    """Run backup.sh's main path against `root` with the fake docker first on PATH."""
    bindir = root / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    env = {k: v for k, v in ENV.items() if k != "BACKUP_LIB_ONLY"}
    env.update(fake)
    env["JBRAIN_DIR"] = str(root)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(
        ["sh", str(DEPLOY / "backup.sh")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _finals(backups: Path) -> list[str]:
    return sorted(p.name for p in backups.iterdir() if not p.name.startswith("."))


def test_a_backup_writes_dump_tarball_sha_and_sidecar_with_a_utc_stamp(
    tmp_path: Path,
) -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    run = _backup(tmp_path, FAKE_FP="fp1")
    assert run.returncode == 0, run.stderr
    backups = tmp_path / "backups"
    (dump,) = _names(backups, "jbrain-", ".dump")
    # The UTC clock, though the writer's TZ is New York.
    assert abs(_parse(dump) - before) < timedelta(minutes=1)
    assert _finals(backups) == [
        f"blobs-{dump}.sha",
        f"blobs-{dump}.tar.gz",
        f"jbrain-{dump}.blobs",
        f"jbrain-{dump}.dump",
    ]
    assert (backups / f"jbrain-{dump}.blobs").read_text().strip() == dump
    assert (backups / f"blobs-{dump}.sha").read_text().strip() == "fp1"
    # No temp file outlives a successful run.
    assert [p.name for p in backups.iterdir() if p.name.startswith(".")] == []


def test_unchanged_blobs_skip_the_tarball_and_the_sidecar_names_the_reused_one(
    tmp_path: Path,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    old = "20261004-010000"
    (backups / f"blobs-{old}.tar.gz").write_text("tar")
    (backups / f"blobs-{old}.sha").write_text("fp1\n")

    run = _backup(tmp_path, FAKE_FP="fp1")

    assert run.returncode == 0, run.stderr
    assert f"blobs unchanged since {old}, skipped" in run.stdout
    (dump,) = _names(backups, "jbrain-", ".dump")
    assert _names(backups, "blobs-", ".tar.gz") == {old}
    assert (backups / f"jbrain-{dump}.blobs").read_text().strip() == old


def test_a_changed_volume_or_a_failed_fingerprint_archives_again(
    tmp_path: Path,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    old = "20261004-010000"
    (backups / f"blobs-{old}.tar.gz").write_text("tar")
    empty_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    (backups / f"blobs-{old}.sha").write_text(f"{empty_sha}\n")

    # The hash of no input is a find that listed nothing: never "unchanged".
    run = _backup(tmp_path, FAKE_FP=empty_sha)
    assert run.returncode == 0, run.stderr
    assert "skipped" not in run.stdout
    assert len(_names(backups, "blobs-", ".tar.gz")) == 2
    (new,) = _names(backups, "blobs-", ".tar.gz") - {old}
    # And it is not recorded as a fingerprint either.
    assert not (backups / f"blobs-{new}.sha").exists()


def test_a_backup_with_no_blob_volume_records_none(tmp_path: Path) -> None:
    run = _backup(tmp_path, FAKE_NO_VOLUME="1")
    assert run.returncode == 0, run.stderr
    backups = tmp_path / "backups"
    (dump,) = _names(backups, "jbrain-", ".dump")
    assert (backups / f"jbrain-{dump}.blobs").read_text().strip() == "none"


@pytest.mark.parametrize("failure", ["FAKE_PGDUMP_FAIL", "FAKE_TAR_FAIL"])
def test_a_failed_step_leaves_no_final_named_file(tmp_path: Path, failure: str) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    # A good backup outside the 48 h window: a 0-byte dump from a failed pg_dump used
    # to land as the newest final-named file and get it pruned.
    good = _stamp(datetime.now(UTC) - timedelta(days=3))
    (backups / f"jbrain-{good}.dump").write_text("good")
    (backups / f"jbrain-{good}.blobs").write_text("none\n")

    run = _backup(tmp_path, FAKE_FP="fp1", **{failure: "1"})

    assert run.returncode != 0
    assert _finals(backups) == [f"jbrain-{good}.blobs", f"jbrain-{good}.dump"]
    assert [p.name for p in backups.iterdir() if p.name.startswith(".")] == []


def test_prune_clears_day_old_temp_files_but_not_a_live_one(tmp_path: Path) -> None:
    stale = tmp_path / ".jbrain-20261001-000000.dump.tmp"
    live = tmp_path / ".blobs-20261004-115900.tar.gz.tmp"
    stale.touch()
    live.touch()
    two_days = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(stale, (two_days, two_days))
    _prune(tmp_path)
    assert not stale.exists() and live.exists()


def test_the_script_prunes_before_and_after_writing() -> None:
    code = [
        ln
        for ln in (DEPLOY / "backup.sh").read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    prunes = [i for i, ln in enumerate(code) if ln.startswith("prune_backups backups")]
    dump = next(i for i, ln in enumerate(code) if "pg_dump" in ln)
    # Before: a full disk gets room for the dump. After: the new one is counted.
    assert len(prunes) == 2 and prunes[0] < dump < prunes[1]
    # Busybox find in the alpine helper has no -printf; pipefail so a failed find is
    # not hashed as an empty volume.
    body = "\n".join(code)
    assert "-printf" not in body and "set -o pipefail;" in body


def test_prune_keeps_the_tarball_a_sidecar_names_and_drops_sidecars_with_dumps(
    tmp_path: Path,
) -> None:
    # The cross-zone case: the tarball's stamp sorts AFTER the dump's (written by a
    # local-time writer ahead of UTC), so "newest at or before" would miss it.
    dump = _stamp(NOW - timedelta(days=5))
    paired = _stamp(NOW - timedelta(days=5) + timedelta(hours=4))
    (tmp_path / f"jbrain-{dump}.dump").touch()
    (tmp_path / f"jbrain-{dump}.blobs").write_text(f"{paired}\n")
    (tmp_path / f"blobs-{paired}.tar.gz").touch()
    # A newer tarball the same day would otherwise make `paired` the older one.
    later = _stamp(NOW - timedelta(days=5) + timedelta(hours=6))
    (tmp_path / f"blobs-{later}.tar.gz").touch()
    old = _stamp(NOW - timedelta(days=20))
    (tmp_path / f"jbrain-{old}.dump").touch()
    (tmp_path / f"jbrain-{old}.blobs").write_text("none\n")
    orphan = _stamp(NOW - timedelta(days=4))
    (tmp_path / f"jbrain-{orphan}.blobs").write_text("none\n")

    _prune(tmp_path)

    assert paired in _names(tmp_path, "blobs-", ".tar.gz")
    assert _names(tmp_path, "jbrain-", ".blobs") == {dump}


def _restore_pick(backups: Path, stamp: str) -> str:
    text = (DEPLOY / "restore.sh").read_text()
    match = re.search(
        r"^# --- pick the blob archive.*?^# --- end pick ---\n", text, re.S | re.M
    )
    assert match, "restore.sh lost its blob archive selection"
    run = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n{match.group(0)}echo "${{BLOBS:-}}"'],
        cwd=backups.parent,
        env={**ENV, "STAMP": stamp},
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return run.stdout.strip()


def test_restore_uses_the_sidecar_over_stamp_order(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    # Dump stamped in UTC at 10:00; its blobs were archived by a writer whose local
    # stamps ran ahead, at "12:00". Ordering would pick the 09:00 tarball — older
    # than the dump's real blob state — and restore wipes /blobs before extracting.
    (backups / "jbrain-20261004-100000.dump").touch()
    (backups / "jbrain-20261004-100000.blobs").write_text("20261004-120000\n")
    for s in ("20261004-090000", "20261004-120000"):
        (backups / f"blobs-{s}.tar.gz").touch()
    assert _restore_pick(backups, "20261004-100000") == (
        "backups/blobs-20261004-120000.tar.gz"
    )


def test_restore_leaves_blobs_alone_when_the_pairing_is_none_or_gone(
    tmp_path: Path,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "blobs-20261004-090000.tar.gz").touch()
    (backups / "jbrain-20261004-100000.blobs").write_text("none\n")
    (backups / "jbrain-20261004-110000.blobs").write_text("20261004-105000\n")
    # No fallback to ordering once a sidecar has spoken: an empty pick means the
    # volume is not wiped.
    assert _restore_pick(backups, "20261004-100000") == ""
    assert _restore_pick(backups, "20261004-110000") == ""


def test_restore_falls_back_to_the_newest_tarball_at_or_before_without_a_sidecar(
    tmp_path: Path,
) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    for s in ("20260901-070000", "20261001-070000", "20261003-070000"):
        (backups / f"blobs-{s}.tar.gz").touch()
    (backups / "blobs-garbage.tar.gz").touch()

    pick = _restore_pick
    assert pick(backups, "20261003-070000") == "backups/blobs-20261003-070000.tar.gz"
    assert pick(backups, "20261002-235959") == "backups/blobs-20261001-070000.tar.gz"
    # Never a LATER tarball: it could hold attachments the dump has no rows for.
    assert pick(backups, "20260801-000000") == ""


def test_restore_and_backup_agree_on_the_tarball(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    for s in ("20261001-070000", "20261003-070000"):
        (backups / f"blobs-{s}.tar.gz").touch()
    (backups / "jbrain-20261002-130000.blobs").write_text("20261003-070000\n")
    for stamp in ("20261002-120000", "20261002-130000"):
        run = _lib(f"blob_for_stamp backups {stamp}", tmp_path)
        assert (
            _restore_pick(backups, stamp)
            == f"backups/blobs-{run.stdout.strip()}.tar.gz"
        )
