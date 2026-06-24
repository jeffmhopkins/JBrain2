"""Deploy scripts reachable from a one-shot must be POSIX sh.

The export/import/reset/update one-shots run in the bash-less docker:cli
(Alpine) container (gateway.UPDATER_IMAGE). A script with a
`#!/usr/bin/env bash` shebang dies there with "env: can't execute 'bash'" —
exactly how the reset safety-backup failed when backup.sh was still bash. So
every script the one-shots invoke (the *-inner.sh entrypoints and backup.sh,
which import/reset call by path) must declare `#!/bin/sh` and parse under a
POSIX shell. Host-only scripts (restore.sh, install.sh) may stay bash.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

# Reachable from a one-shot container: the entrypoints sh-exec'd by the
# gateway, plus backup.sh which import-inner/reset-inner call as ./backup.sh.
ONESHOT_SCRIPTS = [
    "export-inner.sh",
    "import-inner.sh",
    "reset-inner.sh",
    "update-inner.sh",
    "backup.sh",
    # Reached from update-inner.sh: the local-model provisioning sync and the
    # weight downloader it calls both run in the bash-less updater, so both must
    # parse under POSIX sh.
    "local-models-sync.sh",
    "download-local-weights.sh",
]


@pytest.mark.parametrize("name", ONESHOT_SCRIPTS)
def test_oneshot_script_uses_posix_sh_shebang(name: str) -> None:
    first_line = (DEPLOY / name).read_text().splitlines()[0]
    assert first_line == "#!/bin/sh", (
        f"{name} is run inside the bash-less docker:cli one-shot; a bash "
        f"shebang fails with 'env: can't execute bash'. Got: {first_line!r}"
    )


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh available")
@pytest.mark.parametrize("name", ONESHOT_SCRIPTS)
def test_oneshot_script_parses_under_posix_sh(name: str) -> None:
    result = subprocess.run(
        ["sh", "-n", str(DEPLOY / name)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{name} is not POSIX sh:\n{result.stderr}"


# The deploy scripts that lay down host files for the stack. Each must copy the
# SearXNG settings host file the compose bind mount points at — without it the
# bind source is missing, Docker mounts an empty dir over /etc/searxng/settings.yml,
# SearXNG drops to its HTML-only defaults, and /search?format=json answers 403, so
# jerv reports web search as unavailable.
DEPLOY_SCRIPTS_THAT_LAY_DOWN_FILES = ["install.sh", "update-inner.sh", "jbrain"]


@pytest.mark.parametrize("name", DEPLOY_SCRIPTS_THAT_LAY_DOWN_FILES)
def test_script_deploys_searxng_settings(name: str) -> None:
    text = (DEPLOY / name).read_text()
    assert "searxng/settings.yml" in text, (
        f"{name} must copy the SearXNG settings to the host bind path; a missing "
        f"file makes Docker mount an empty dir and SearXNG refuse the JSON API"
    )


@pytest.mark.parametrize("name", DEPLOY_SCRIPTS_THAT_LAY_DOWN_FILES)
def test_script_clears_stale_searxng_settings_path(name: str) -> None:
    # On a box already broken by the missing-file bug, the bind path is an empty
    # directory Docker created. `cp file dir/` would drop the file *inside* that
    # directory instead of replacing it, so the dir mount survives and SearXNG
    # still crash-loops. The path must be removed before the copy so the box heals.
    text = (DEPLOY / name).read_text()
    assert "rm -rf searxng/settings.yml" in text, (
        f"{name} must remove any stale searxng/settings.yml (a Docker-made "
        f"directory on an already-broken box) before copying the file in"
    )


@pytest.mark.parametrize("name", DEPLOY_SCRIPTS_THAT_LAY_DOWN_FILES)
def test_script_ensures_searxng_secret(name: str) -> None:
    # SearXNG refuses to start without a secret, so a stack that predates the
    # web-search service (no SEARXNG_SECRET in .env) must have one backfilled or
    # web search stays down. install.sh writes it for fresh .env files and
    # backfills existing ones; the update paths backfill when absent.
    text = (DEPLOY / name).read_text()
    assert "SEARXNG_SECRET" in text, f"{name} must ensure SEARXNG_SECRET is set"


def test_update_marks_worktree_safe_before_pull() -> None:
    # The pull runs as root inside the updater container against a bind-mounted
    # worktree owned by the host operator's UID; without a safe.directory entry
    # git aborts with "dubious ownership" and the PWA update fails. The guard
    # must precede the pull (a host-side config never reaches the container).
    lines = (DEPLOY / "update-inner.sh").read_text().splitlines()
    safe = next((i for i, ln in enumerate(lines) if "safe.directory" in ln), None)
    pull = next((i for i, ln in enumerate(lines) if "pull --ff-only" in ln), None)
    assert safe is not None, "update-inner.sh must mark the worktree safe.directory"
    assert pull is not None, "update-inner.sh must run the pull"
    assert safe < pull, "safe.directory must be set before the pull"
