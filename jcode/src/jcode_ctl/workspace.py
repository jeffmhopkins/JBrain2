"""The workspace port: provision and tear down a per-session git checkout.

Isolated behind a port so the session logic is testable without git or a network
(:class:`FakeWorkspace`). The real implementation shells to ``git`` inside the
sandbox volume; the clone is the only place a remote is contacted, and only
against the configured egress allowlist (enforcement is Wave J5).
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Protocol


class WorkspaceError(RuntimeError):
    """A clone/checkout/reset failed — surfaced as a clean control-API error."""


class Workspace(Protocol):
    """Owns the on-disk lifecycle of one session's checkout."""

    async def clone(
        self, path: Path, repo: str, branch: str, work_branch: str
    ) -> None: ...

    async def reset(self, path: Path) -> None: ...

    def remove(self, path: Path) -> None: ...


class GitWorkspace:
    """Real workspace: ``git clone`` + a fresh work branch, in the sandbox volume."""

    def __init__(self, allowlist: list[str]) -> None:
        self._allowlist = allowlist

    async def _git(self, *args: str, cwd: Path | None = None) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            detail = err.decode(errors="replace").strip()
            raise WorkspaceError(f"git {args[0]} failed: {detail}")

    async def clone(self, path: Path, repo: str, branch: str, work_branch: str) -> None:
        if repo:  # empty repo = a scratch workspace (no clone)
            await self._git(
                "clone", "--branch", branch, "--depth", "1", repo, str(path)
            )
            await self._git("checkout", "-b", work_branch, cwd=path)
        else:
            path.mkdir(parents=True, exist_ok=True)
            await self._git("init", "-b", work_branch or "main", cwd=path)

    async def reset(self, path: Path) -> None:
        await self._git("reset", "--hard", cwd=path)
        await self._git("clean", "-fd", cwd=path)

    def remove(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)


class FakeWorkspace:
    """In-memory workspace for tests: records lifecycle calls, touches no disk."""

    def __init__(self) -> None:
        self.cloned: list[tuple[str, str, str]] = []
        self.reset_paths: list[Path] = []
        self.removed: list[Path] = []

    async def clone(self, path: Path, repo: str, branch: str, work_branch: str) -> None:
        self.cloned.append((repo, branch, work_branch))

    async def reset(self, path: Path) -> None:
        self.reset_paths.append(path)

    def remove(self, path: Path) -> None:
        self.removed.append(path)
