"""The sandbox's instruction file for the in-session coding agents (grok / claude).

Both CLIs read ``AGENTS.md`` / ``CLAUDE.md`` as instruction context, but neither treats
the shell's login banner as available tools — so without this, grok inspects only its
MCP registry, finds nothing, and tells the owner it can't search the web or read an
image (observed on-box). The file sits at the workspace ROOT, the parent of every
checkout, so both CLIs find it as a parent-directory memory file without it ever
showing in the owner's ``git status``.

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
