"""The `jcode-grok` helper: per-session grok install into $JCODE_TOOLS_BIN, with a
clean egress-failure path and a pinned-version pass-through. Driven as the real shell
script with a FAKE `curl` on PATH (no network, no real x.ai installer) so the install
contract — fetch, target dir, version arg — is exercised without leaving the box.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_HELPER = Path(__file__).resolve().parents[1] / "jcode-grok"


def _fake_curl(bin_dir: Path, *, fail: bool, install_log: Path) -> None:
    """Drop a fake `curl` that, unless FAIL, writes a stub installer to curl's -o path.
    The stub records its args and plants a fake grok in $GROK_BIN_DIR."""
    body = "#!/usr/bin/env bash\nexit 1\n" if fail else (
        "#!/usr/bin/env bash\n"
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        'cat > "$out" <<\'STUB\'\n'
        "#!/usr/bin/env bash\n"
        'echo "args:$*" >> "$FAKE_INSTALL_LOG"\n'
        'mkdir -p "$GROK_BIN_DIR"\n'
        "printf '#!/bin/sh\\necho grok 9.9.9\\n' > \"$GROK_BIN_DIR/grok\"\n"
        'chmod +x "$GROK_BIN_DIR/grok"\n'
        "STUB\n"
    )
    curl = bin_dir / "curl"
    curl.write_text(body)
    curl.chmod(0o755)


def _run(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_HELPER), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_upgrade_installs_into_session_bin_and_forwards_version(tmp_path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    tools = tmp_path / "tools"
    log = tmp_path / "install.log"
    _fake_curl(fakebin, fail=False, install_log=log)
    env = {
        "PATH": f"{fakebin}:{tools}:/usr/bin:/bin",
        "JCODE_TOOLS_BIN": str(tools),
        "FAKE_INSTALL_LOG": str(log),
    }
    res = _run(["upgrade", "0.2.70"], env=env)
    assert res.returncode == 0, res.stderr
    # The pinned version is forwarded to the installer; grok lands in the session bin.
    assert "args:0.2.70" in log.read_text()
    assert (tools / "grok").is_file()
    # The session's grok (the freshly installed one) is what resolves.
    assert "grok 9.9.9" in res.stdout


def test_upgrade_without_a_version_lets_the_installer_default(tmp_path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    tools = tmp_path / "tools"
    log = tmp_path / "install.log"
    _fake_curl(fakebin, fail=False, install_log=log)
    env = {
        "PATH": f"{fakebin}:{tools}:/usr/bin:/bin",
        "JCODE_TOOLS_BIN": str(tools),
        "FAKE_INSTALL_LOG": str(log),
    }
    res = _run(["upgrade"], env=env)
    assert res.returncode == 0, res.stderr
    assert "args:" in log.read_text()  # invoked with no version arg


def test_upgrade_reports_a_clean_error_when_egress_is_locked(tmp_path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    tools = tmp_path / "tools"
    _fake_curl(fakebin, fail=True, install_log=tmp_path / "unused.log")
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "JCODE_TOOLS_BIN": str(tools),
    }
    res = _run(["upgrade"], env=env)
    assert res.returncode == 1
    assert "cannot reach x.ai" in res.stderr
    assert not (tools / "grok").exists()  # nothing installed on a failed fetch


def test_upgrade_refuses_outside_a_session_shell(tmp_path) -> None:
    # No $JCODE_TOOLS_BIN means this isn't a per-session shell — refuse rather than
    # install somewhere global.
    res = _run(["upgrade"], env={"PATH": "/usr/bin:/bin"})
    assert res.returncode == 1
    assert "JCODE_TOOLS_BIN" in res.stderr


def test_unknown_command_prints_usage(tmp_path) -> None:
    res = _run(["wat"], env={"PATH": "/usr/bin:/bin"})
    assert res.returncode == 2
    assert "usage: jcode-grok" in res.stderr
