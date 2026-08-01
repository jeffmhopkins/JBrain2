"""The jcode-agents.sh login hook: writes an AGENTS.md / CLAUDE.md into the workspace
root so the in-sandbox grok/claude DISCOVER the shell tools (they read those files, not
the login banner). Runs the hook against a tmp workspace root — no /work needed."""

from __future__ import annotations

import subprocess
from pathlib import Path

_HOOK = Path(__file__).resolve().parents[1] / "agents-md.sh"


def _run(root: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_HOOK)],
        capture_output=True,
        text=True,
        env={"JCODE_WORKSPACE_ROOT": root, "PATH": "/usr/bin:/bin"},
        timeout=15,
    )


def test_writes_agents_and_claude_memory(tmp_path: Path) -> None:
    proc = _run(str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == ""  # writes files, prints nothing (no banner noise)
    agents = (tmp_path / "AGENTS.md").read_text()
    claude = (tmp_path / "CLAUDE.md").read_text()
    assert agents == claude  # same guidance under both conventions
    for token in ("ocr <file>", "web-search <query>", "web-fetch <url>"):
        assert token in agents
    # The key steer: grok must not conclude "no web tool" from an empty MCP registry.
    assert "NOT MCP tools" in agents


def test_silent_noop_when_root_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    proc = _run(str(missing))
    assert proc.returncode == 0  # never fails the login
    assert not missing.exists()
