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
