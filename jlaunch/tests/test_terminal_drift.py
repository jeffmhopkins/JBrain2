"""``terminal.py`` is a VENDORED byte-for-byte copy of jcode's PTY core. This guards it
against drift: if the jcode original changes, this fails until the copy is re-synced
(re-copy jcode_ctl/terminal.py over jlaunch_ctl/terminal.py). Skipped when the jcode
tree isn't present (e.g. in the jlaunch image, which copies only jlaunch)."""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORIGINAL = _REPO_ROOT / "jcode" / "src" / "jcode_ctl" / "terminal.py"
_VENDORED = _REPO_ROOT / "jlaunch" / "src" / "jlaunch_ctl" / "terminal.py"


@pytest.mark.skipif(not _ORIGINAL.exists(), reason="jcode source not in this tree")
def test_vendored_terminal_matches_jcode() -> None:
    assert _VENDORED.read_bytes() == _ORIGINAL.read_bytes(), (
        "jlaunch/src/jlaunch_ctl/terminal.py has drifted from jcode's original; "
        "re-sync with: cp jcode/src/jcode_ctl/terminal.py jlaunch/src/jlaunch_ctl/terminal.py"  # noqa: E501
    )
