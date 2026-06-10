"""Content-addressed blob storage.

All file I/O goes through this abstraction (CLAUDE.md rule 2): blobs are
stored by sha256, so identical attachments dedupe for free and the layout
can move to S3/MinIO without touching callers.
"""

import asyncio
import hashlib
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    async def put(self, data: bytes) -> str:
        """Store bytes, return their sha256 hex digest."""
        ...

    async def get(self, sha256: str) -> bytes:
        """Read a stored blob; raises FileNotFoundError when absent."""
        ...

    def path_for(self, sha256: str) -> Path:
        """Filesystem path for a stored blob (for streaming responses)."""
        ...

    async def exists(self, sha256: str) -> bool: ...

    def usage(self) -> tuple[int, int]:
        """(blob_count, total_bytes) — fine to walk at personal scale."""
        ...


class FsBlobStore:
    """Sharded directory layout (ab/cd/abcd…) keeps directories small."""

    def __init__(self, root: str | Path):
        self._root = Path(root)

    def path_for(self, sha256: str) -> Path:
        return self._root / sha256[:2] / sha256[2:4] / sha256

    async def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        target = self.path_for(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a crash never leaves a partial blob
            # addressable under its digest.
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.rename(target)
        return digest

    async def get(self, sha256: str) -> bytes:
        # to_thread keeps large attachment reads off the event loop.
        return await asyncio.to_thread(self.path_for(sha256).read_bytes)

    async def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()

    def usage(self) -> tuple[int, int]:
        count = 0
        total = 0
        if self._root.exists():
            for path in self._root.rglob("*"):
                if path.is_file() and not path.name.endswith(".tmp"):
                    count += 1
                    total += path.stat().st_size
        return count, total


# Archives are named by this code or by export-inner.sh — anything else in
# the shared backups directory (nightly dumps, logs) stays invisible here.
_EXPORT_RE = re.compile(r"^export-\d{8}-\d{6}\.jbrain\.tar$")
_IMPORT_RE = re.compile(r"^import-\d{8}-\d{6}\.jbrain\.tar$")


class BackupShelf(Protocol):
    """The host backups directory, as far as the api may touch it.

    Exports are read-only handoffs from the supervisor's one-shot; imports
    are uploads parked here for the one-shot to consume. The api never
    reads or writes any other file in the directory.
    """

    def latest_export(self) -> str | None: ...

    def export_path(self, name: str) -> Path:
        """Path for a named export; raises ValueError on foreign names."""
        ...

    async def save_import(self, chunks: AsyncIterator[bytes]) -> str:
        """Persist an uploaded archive, return its generated name."""
        ...


class FsBackupShelf:
    def __init__(self, root: str | Path):
        self._root = Path(root)

    def latest_export(self) -> str | None:
        if not self._root.exists():
            return None
        names = sorted(p.name for p in self._root.iterdir() if _EXPORT_RE.fullmatch(p.name))
        return names[-1] if names else None

    def export_path(self, name: str) -> Path:
        if not _EXPORT_RE.fullmatch(name):
            raise ValueError(f"not an export archive: {name!r}")
        return self._root / name

    async def save_import(self, chunks: AsyncIterator[bytes]) -> str:
        name = f"import-{time.strftime('%Y%m%d-%H%M%S')}.jbrain.tar"
        assert _IMPORT_RE.fullmatch(name)
        self._root.mkdir(parents=True, exist_ok=True)
        target = self._root / name
        tmp = target.with_suffix(".tmp")
        # Chunked write keeps multi-GB archives out of memory; write-then-
        # rename means the one-shot can never see a partial upload.
        with tmp.open("wb") as fh:
            async for chunk in chunks:
                await asyncio.to_thread(fh.write, chunk)
        tmp.rename(target)
        return name
