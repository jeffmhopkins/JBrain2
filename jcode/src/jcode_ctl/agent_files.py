"""The sandbox's instruction file for the in-session coding agents (grok / claude).

Both CLIs read ``AGENTS.md`` / ``CLAUDE.md`` as instruction context, but neither treats
the shell's login banner as available tools — so without this, grok inspects only its
MCP registry, finds nothing, and tells the owner it can't search the web or read an
image (observed on-box). It is written to the workspace ROOT (the parent of every
checkout) and seeded into each session's home as the CLIs' global instruction files —
grok reads only the git root down plus ``~/.grok/AGENTS.md`` — so it never shows in the
owner's ``git status``.

The control server writes it as root at startup. It used to be a login-shell hook, but
session shells no longer run as root and can't write ``/work`` — and must not be able
to: one session rewriting this file would be instructing every other session's agent.
The content is static (the helpers self-gate), so one shared file serves every session.
"""

# ruff: noqa: E501 — AGENTS_MD is published text, kept byte-identical to what agents read.
from __future__ import annotations

import os
from pathlib import Path

AGENTS_MD = """\
# Sandbox tools

This coding sandbox gives you a few extra capabilities as **shell commands** you run in
your terminal. They are NOT MCP tools and will NOT appear in any tool/MCP registry — do not
go looking for them there. Just run the command:

- `ocr <file>` — read the EXACT text out of a local image or PDF using the box's on-box OCR
  (deterministic, verbatim; a PDF is read page by page). Always available. Prefer this over
  telling the owner you can't read an image.
- `web-search <query>` — search the web via this box's own SearXNG; prints ranked results
  with URLs.
- `web-fetch <url>` — fetch a URL and print its readable text.

`web-search` / `web-fetch` work when this session has web search enabled; if it isn't, they
print a clear "not enabled" message (nothing to do — just tell the owner it's off). Reach for
these instead of telling the owner you have no web access — in this sandbox, you do.
"""

# grok reads the AGENTS.md family AND CLAUDE.md; claude reads CLAUDE.md. Same content in
# both names so whichever convention a CLI honors, it finds the guidance.
AGENT_FILES = ("AGENTS.md", "CLAUDE.md")


def write_agent_files(root: Path) -> None:
    """Write both files under ``root``, root-owned 0644. Through a temp file and
    ``os.replace``, which swaps the directory entry rather than writing through whatever
    is there, so even a symlink left at the name is replaced, not followed."""
    for name in AGENT_FILES:
        target = root / name
        tmp = root / f".{name}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(AGENTS_MD)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)


# Where each CLI looks in a session's own $HOME. grok reads project instruction files
# from the git root down plus its global ~/.grok/AGENTS.md — never a checkout's parent —
# so /work/AGENTS.md alone doesn't reach it; claude reads ~/.claude/CLAUDE.md likewise.
HOME_AGENT_FILES = (".grok/AGENTS.md", ".claude/CLAUDE.md")


def write_home_agent_files(home: Path, uid: int, gid: int) -> None:
    """Seed the session's global instruction files into a fresh, still-empty home,
    owned by the session (it is their home; editing them is theirs to do). Only ever
    called before anything runs as ``uid``, and ``O_EXCL | O_NOFOLLOW`` refuses any
    path that already exists."""
    for rel in HOME_AGENT_FILES:
        target = home / rel
        if not target.parent.exists():
            target.parent.mkdir(mode=0o700)
            os.chown(target.parent, uid, gid, follow_symlinks=False)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(target, flags, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchown(fh.fileno(), uid, gid)
            fh.write(AGENTS_MD)
