"""deploy/prune-kv-state.sh — the destructive sweep that reclaims orphaned KV state.

Each file it deletes is 1-2 GB of primed prefix, and the one it must NOT delete is what saves
a ~100 s cold prefill after a restart. Nothing else in the repo can restore a wrong delete, so
the four guards in the script's header are pinned here rather than trusted to review.

The sweep runs inside a container (the volume is reachable no other way), so the test extracts
the heredoc body and runs it under `sh` against a temp directory — the same code, minus docker.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import time

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "deploy" / "prune-kv-state.sh"


def _sweep_body() -> str:
    """The `sh -s` heredoc the script pipes into the one-shot container."""
    lines = _SCRIPT.read_text().splitlines()
    starts = [i for i, ln in enumerate(lines) if "<<'INNER'" in ln]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == "INNER"]
    assert starts and ends, "the sweep heredoc markers moved — this test extracts by them"
    return "\n".join(lines[starts[0] + 1 : ends[0]])


def _run(target: pathlib.Path, keep: int) -> str:
    body = _sweep_body().replace("cd /kv 2>/dev/null || exit 0", f"cd '{target}' || exit 0")
    done = subprocess.run(  # noqa: S603 — fixed argv, body comes from the repo
        ["sh", "-s"],
        input=body,
        capture_output=True,
        text=True,
        env={"KEEP_PER_MODEL": str(keep), "PATH": "/usr/bin:/bin", "HOME": str(target)},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _touch_in_order(target: pathlib.Path, names: list[str]) -> None:
    """Create files oldest-first. The sweep ranks by mtime (`ls -t`), so the ORDER is the
    fixture — the last name written is the live one."""
    for i, name in enumerate(names):
        path = target / name
        path.write_text("x")
        # Explicit mtimes rather than sleeps: a 1s spread survives a coarse filesystem clock.
        stamp = time.time() - (len(names) - i) * 60
        os.utime(path, (stamp, stamp))


@pytest.fixture
def kv(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path


def test_it_keeps_the_newest_per_model_and_deletes_the_rest(kv: pathlib.Path) -> None:
    """Two models, three generations each. The point of keeping rather than wiping: after an
    update that did not rebuild llama.cpp the newest file is still LIVE, and deleting it would
    cost exactly the cold prefill the state file exists to avoid."""
    _touch_in_order(
        kv,
        [
            "jerv-gpt-oss-120b-1111111111111111.bin",
            "jerv-qwen3.8-27b-abliterated-aaaaaaaaaaaaaaaa.bin",
            "jerv-gpt-oss-120b-2222222222222222.bin",
            "jerv-qwen3.8-27b-abliterated-bbbbbbbbbbbbbbbb.bin",
            "jerv-gpt-oss-120b-3333333333333333.bin",
            "jerv-qwen3.8-27b-abliterated-cccccccccccccccc.bin",
        ],
    )
    _run(kv, keep=2)
    survivors = sorted(p.name for p in kv.iterdir())
    assert survivors == [
        "jerv-gpt-oss-120b-2222222222222222.bin",
        "jerv-gpt-oss-120b-3333333333333333.bin",
        "jerv-qwen3.8-27b-abliterated-bbbbbbbbbbbbbbbb.bin",
        "jerv-qwen3.8-27b-abliterated-cccccccccccccccc.bin",
    ]


def test_a_model_id_carrying_dots_and_dashes_groups_correctly(kv: pathlib.Path) -> None:
    """`qwen3.8-27b-abliterated` is the real served name. Grouping splits on the trailing
    16-hex fingerprint, so a model whose own id contains dashes must not split into several."""
    _touch_in_order(
        kv,
        [
            "jerv-qwen3.8-27b-abliterated-aaaaaaaaaaaaaaaa.bin",
            "jerv-qwen3.8-27b-abliterated-bbbbbbbbbbbbbbbb.bin",
        ],
    )
    _run(kv, keep=1)
    assert [p.name for p in kv.iterdir()] == ["jerv-qwen3.8-27b-abliterated-bbbbbbbbbbbbbbbb.bin"]


def test_a_name_that_is_not_ours_is_never_a_candidate(kv: pathlib.Path) -> None:
    """Guard 1 (shape). The volume is shared with llama-server, so anything not matching
    `jerv-<model>-<16 lowercase hex>.bin` is somebody else's and stays — including names that
    merely look close."""
    strangers = [
        "notours.bin",
        "jerv-bad-ZZZZ.bin",  # not hex
        "jerv-short-12345.bin",  # not 16 wide
        "jerv-UPPER-AAAAAAAAAAAAAAAA.bin",  # uppercase hex is not our format
        "jerv-nosuffix-1111111111111111",  # no .bin
        # A pair that is fingerprint-SHAPED but not ours. These are the ones that matter: drop
        # the `jerv-` anchor from the pattern and they group as one "model", at which point the
        # older is deleted. Somebody else's rotating sidecar would vanish silently.
        "backup-aaaaaaaaaaaaaaaa.bin",
        "backup-bbbbbbbbbbbbbbbb.bin",
    ]
    for i, name in enumerate(strangers):
        (kv / name).write_text("x")
        stamp = time.time() - (len(strangers) - i) * 60
        os.utime(kv / name, (stamp, stamp))
    _run(kv, keep=1)
    assert sorted(p.name for p in kv.iterdir()) == sorted(strangers)


def test_a_directory_or_symlink_is_spared_even_when_its_rank_says_delete(
    kv: pathlib.Path,
) -> None:
    """Guard 3 (type), isolated from guard 4.

    Each decoy is given a NEWER sibling under the same model, so rank alone would delete it and
    only the type check stands between the sweep and following a symlink out of the volume. An
    earlier version of this test gave them no sibling — rank kept them, the assertion passed,
    and removing the type guard entirely did not fail the suite."""
    decoy_dir = "jerv-adir-4444444444444444.bin"
    decoy_link = "jerv-alink-5555555555555555.bin"
    (kv / decoy_dir).mkdir()
    (kv / decoy_link).symlink_to("/etc/passwd")
    # Newer siblings: same model prefix, so the decoys are rank-superseded.
    now = time.time()
    for name in ("jerv-adir-6666666666666666.bin", "jerv-alink-7777777777777777.bin"):
        (kv / name).write_text("x")
        os.utime(kv / name, (now, now))
    for name in (decoy_dir, decoy_link):
        os.utime(kv / name, (now - 600, now - 600), follow_symlinks=False)

    _run(kv, keep=1)

    survivors = sorted(p.name for p in kv.iterdir())
    assert decoy_dir in survivors and decoy_link in survivors, survivors
    # And the symlink's target survived — a delete that followed it would leave the volume
    # tidy and the host broken.
    assert pathlib.Path("/etc/passwd").exists()


def test_it_is_idempotent(kv: pathlib.Path) -> None:
    """The update runs it every time. A second pass over an already-swept volume must be a
    no-op, not a slow erosion of the files the first pass decided to keep."""
    _touch_in_order(
        kv,
        [
            "jerv-gpt-oss-120b-1111111111111111.bin",
            "jerv-gpt-oss-120b-2222222222222222.bin",
            "jerv-gpt-oss-120b-3333333333333333.bin",
        ],
    )
    _run(kv, keep=2)
    after_first = sorted(p.name for p in kv.iterdir())
    out = _run(kv, keep=2)
    assert sorted(p.name for p in kv.iterdir()) == after_first
    assert "deleting" not in out


def test_an_empty_volume_is_not_an_error(kv: pathlib.Path) -> None:
    """A fresh box has never primed. The update must not report a failure for that."""
    assert _run(kv, keep=2) == ""
