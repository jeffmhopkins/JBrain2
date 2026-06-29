"""The `jcode-claude` helper: per-session claude install via the session's npm prefix.
Driven as the real shell script with a FAKE `npm` on PATH (no network) so the install
contract — package, version/tag, refusal outside a session — is exercised offline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_HELPER = Path(__file__).resolve().parents[1] / "jcode-claude"


def _fake_npm(bin_dir: Path, *, fail: bool, log: Path) -> None:
    """Drop a fake `npm` that records its args (so the package@version is observable)
    and succeeds or fails on demand."""
    body = (
        "#!/usr/bin/env bash\n"
        f'echo "args:$*" >> "{log}"\n'
        + ("exit 1\n" if fail else "exit 0\n")
    )
    npm = bin_dir / "npm"
    npm.write_text(body)
    npm.chmod(0o755)


def _run(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_HELPER), *args], capture_output=True, text=True, env=env
    )


def test_upgrade_installs_the_pinned_version_via_npm(tmp_path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "npm.log"
    _fake_npm(fakebin, fail=False, log=log)
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "JCODE_TOOLS_BIN": str(tmp_path / "tools"),
        "NPM_CONFIG_PREFIX": str(tmp_path / "npm-global"),
    }
    res = _run(["upgrade", "1.2.3"], env=env)
    assert res.returncode == 0, res.stderr
    # The package + pinned version are forwarded, and --prefix explicitly targets the
    # session's npm prefix (so the env alone can't redirect the install elsewhere).
    logged = log.read_text()
    assert "@anthropic-ai/claude-code@1.2.3" in logged
    assert f"--prefix {tmp_path / 'npm-global'}" in logged


def test_upgrade_defaults_to_latest(tmp_path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "npm.log"
    _fake_npm(fakebin, fail=False, log=log)
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "JCODE_TOOLS_BIN": str(tmp_path / "tools"),
        "NPM_CONFIG_PREFIX": str(tmp_path / "npm-global"),
    }
    res = _run(["upgrade"], env=env)
    assert res.returncode == 0, res.stderr
    assert "@anthropic-ai/claude-code@latest" in log.read_text()


def test_upgrade_reports_a_clean_error_on_npm_failure(tmp_path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _fake_npm(fakebin, fail=True, log=tmp_path / "npm.log")
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "JCODE_TOOLS_BIN": str(tmp_path / "tools"),
        "NPM_CONFIG_PREFIX": str(tmp_path / "npm-global"),
    }
    res = _run(["upgrade"], env=env)
    assert res.returncode == 1
    assert "npm install failed" in res.stderr


def test_upgrade_refuses_when_the_npm_prefix_is_unset(tmp_path) -> None:
    # The guard is on NPM_CONFIG_PREFIX (the actual install target), not just the
    # session marker — so a clobbered prefix can't silently install into the image.
    res = _run(["upgrade"], env={"PATH": "/usr/bin:/bin", "JCODE_TOOLS_BIN": "/x"})
    assert res.returncode == 1
    assert "NPM_CONFIG_PREFIX" in res.stderr


def test_version_reports_session_and_image(tmp_path) -> None:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    claude = fakebin / "claude"
    claude.write_text("#!/bin/sh\necho 1.0.0\n")
    claude.chmod(0o755)
    res = _run(["version"], env={"PATH": f"{fakebin}:/usr/bin:/bin"})
    assert res.returncode == 0, res.stderr
    assert "session:" in res.stdout and "1.0.0" in res.stdout
    assert "image:" in res.stdout  # no /usr/local/bin/claude here → reported as none


def test_unknown_command_prints_usage(tmp_path) -> None:
    res = _run(["wat"], env={"PATH": "/usr/bin:/bin"})
    assert res.returncode == 2
    assert "usage: jcode-claude" in res.stderr
