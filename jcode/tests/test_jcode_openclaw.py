"""The `jcode-openclaw` helper: per-session openclaw install via the session's npm
prefix. Driven as the real shell script with a FAKE `npm` on PATH (no network) so the
install contract (package, version/tag, refusal outside a session) is exercised offline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_HELPER = Path(__file__).resolve().parents[1] / "jcode-openclaw"


def _fake_npm(bin_dir: Path, *, fail: bool, log: Path) -> None:
    """Drop a fake `npm` that records its args (so the package@version is observable)
    and succeeds or fails on demand."""
    body = f'#!/usr/bin/env bash\necho "args:$*" >> "{log}"\n' + (
        "exit 1\n" if fail else "exit 0\n"
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
    assert "openclaw@1.2.3" in logged
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
    assert "openclaw@latest" in log.read_text()


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
    openclaw = fakebin / "openclaw"
    openclaw.write_text("#!/bin/sh\necho 1.0.0\n")
    openclaw.chmod(0o755)
    res = _run(["version"], env={"PATH": f"{fakebin}:/usr/bin:/bin"})
    assert res.returncode == 0, res.stderr
    assert "session:" in res.stdout and "1.0.0" in res.stdout
    assert "image:" in res.stdout  # no /usr/local/bin/openclaw here → reported as none


def test_unknown_command_prints_usage(tmp_path) -> None:
    res = _run(["wat"], env={"PATH": "/usr/bin:/bin"})
    assert res.returncode == 2
    assert "usage: jcode-openclaw" in res.stderr


def _fake_openclaw_gateway(bin_dir: Path) -> None:
    """A fake `openclaw` whose `gateway` subcommand becomes a long-lived process (so the
    pidfile's pid stays killable), and which is otherwise a no-op."""
    body = (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "gateway" ]; then exec sleep 60; fi\n'
        'echo "openclaw $*"\n'
    )
    oc = bin_dir / "openclaw"
    oc.write_text(body)
    oc.chmod(0o755)


def test_gateway_start_status_stop_lifecycle(tmp_path) -> None:
    # start backgrounds the daemon and records a pidfile; status sees it; stop kills it
    # and clears the pidfile. Driven with a fake `openclaw gateway` that just sleeps.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    _fake_openclaw_gateway(fakebin)
    home = tmp_path / "home"
    env = {
        "PATH": f"{fakebin}:/usr/bin:/bin",
        "HOME": str(home),
        "OPENCLAW_GATEWAY_PORT": "18801",
    }
    pidfile = home / ".openclaw" / "gateway.pid"
    pid = None
    try:
        start = _run(["gateway", "start"], env=env)
        assert start.returncode == 0, start.stderr
        assert "ws://127.0.0.1:18801" in start.stdout
        assert pidfile.is_file()
        pid = int(pidfile.read_text().strip())
        # A second start is idempotent: reports the daemon, doesn't spawn another.
        again = _run(["gateway", "start"], env=env)
        assert "already running" in again.stdout
        assert int(pidfile.read_text().strip()) == pid
        status = _run(["gateway", "status"], env=env)
        assert "running" in status.stdout and "18801" in status.stdout
        stop = _run(["gateway", "stop"], env=env)
        assert stop.returncode == 0, stop.stderr
        assert "stopped" in stop.stdout
        assert not pidfile.exists()
        pid = None
    finally:
        if pid is not None:
            subprocess.run(["kill", str(pid)], capture_output=True)


def test_gateway_status_when_not_running(tmp_path) -> None:
    home = tmp_path / "home"
    res = _run(["gateway", "status"], env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
    assert res.returncode == 0, res.stderr
    assert "not running" in res.stdout


def test_gateway_start_refuses_without_the_cli(tmp_path) -> None:
    # No `openclaw` on PATH: start fails clearly instead of writing a dead pidfile.
    home = tmp_path / "home"
    res = _run(["gateway", "start"], env={"PATH": "/usr/bin:/bin", "HOME": str(home)})
    assert res.returncode == 1
    assert "openclaw not installed" in res.stderr
    assert not (home / ".openclaw" / "gateway.pid").exists()


def test_gateway_unknown_subcommand_prints_usage(tmp_path) -> None:
    res = _run(["gateway", "wat"], env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert res.returncode == 2
    assert "usage: jcode-openclaw gateway" in res.stderr
