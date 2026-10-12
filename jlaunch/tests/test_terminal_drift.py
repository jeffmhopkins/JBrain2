"""``terminal.py`` and the ``sandbox.py`` it imports are VENDORED byte-for-byte
copies of jcode's PTY core. This guards them against drift: if a jcode original
changes, this fails until the copy is re-synced (re-copy jcode_ctl/<file> over
jlaunch_ctl/<file>). Skipped when the jcode tree isn't present (e.g. in the jlaunch
image, which copies only jlaunch)."""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JCODE = _REPO_ROOT / "jcode" / "src" / "jcode_ctl"
_JLAUNCH = _REPO_ROOT / "jlaunch" / "src" / "jlaunch_ctl"


@pytest.mark.skipif(not _JCODE.exists(), reason="jcode source not in this tree")
@pytest.mark.parametrize("name", ["terminal.py", "sandbox.py"])
def test_vendored_terminal_matches_jcode(name: str) -> None:
    assert (_JLAUNCH / name).read_bytes() == (_JCODE / name).read_bytes(), (
        f"jlaunch/src/jlaunch_ctl/{name} has drifted from jcode's original; "
        f"re-sync with: cp jcode/src/jcode_ctl/{name} jlaunch/src/jlaunch_ctl/{name}"
    )
