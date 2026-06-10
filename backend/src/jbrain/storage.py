"""Content-addressed blob storage.

All file I/O goes through this abstraction (CLAUDE.md rule 2): blobs are
stored by sha256, so identical attachments dedupe for free and the layout
can move to S3/MinIO without touching callers.
"""

import hashlib
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    async def put(self, data: bytes) -> str:
        """Store bytes, return their sha256 hex digest."""
        ...

    def path_for(self, sha256: str) -> Path:
        """Filesystem path for a stored blob (for streaming responses)."""
        ...

    async def exists(self, sha256: str) -> bool: ...


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

    async def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()
