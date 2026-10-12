"""The sandbox's AGENTS.md / CLAUDE.md: written by the control server (as root, at
startup) into the workspace root so the in-sandbox grok/claude DISCOVER the shell tools
(they read those files, not the login banner). No login hook writes it any more — a
session shell is unprivileged and must not be able to rewrite every agent's
instructions."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from jcode_ctl.agent_files import AGENTS_MD, write_agent_files

_JCODE = Path(__file__).resolve().parents[1]


def test_writes_agents_and_claude_memory(tmp_path: Path) -> None:
    write_agent_files(tmp_path)
    agents = (tmp_path / "AGENTS.md").read_text()
    claude = (tmp_path / "CLAUDE.md").read_text()
    assert agents == claude == AGENTS_MD  # same guidance under both conventions
    for token in ("ocr <file>", "web-search <query>", "web-fetch <url>"):
        assert token in agents
    # The key steer: grok must not conclude "no web tool" from an empty MCP registry.
    assert "NOT MCP tools" in agents
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert stat.S_IMODE(os.lstat(tmp_path / name).st_mode) == 0o644
    assert sorted(os.listdir(tmp_path)) == ["AGENTS.md", "CLAUDE.md"]  # no temp left


def test_a_planted_symlink_is_replaced_not_followed(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep")
    root = tmp_path / "work"
    root.mkdir()
    (root / "AGENTS.md").symlink_to(victim)
    write_agent_files(root)
    assert victim.read_text() == "keep"
    assert not (root / "AGENTS.md").is_symlink()
    assert (root / "AGENTS.md").read_text() == AGENTS_MD


def test_no_login_hook_writes_the_workspace_root() -> None:
    # A non-root shell can't write /work, and must not try on every login.
    dockerfile = (_JCODE / "Dockerfile").read_text()
    assert "agents-md.sh" not in dockerfile
    assert not (_JCODE / "agents-md.sh").exists()
