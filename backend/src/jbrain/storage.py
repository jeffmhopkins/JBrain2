"""Content-addressed blob storage.

All file I/O goes through this abstraction (CLAUDE.md rule 2): blobs are
stored by sha256, so identical attachments dedupe for free and the layout
can move to S3/MinIO without touching callers.
"""

import asyncio
import contextlib
import hashlib
import os
import re
import secrets
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Protocol

#: A stored blob is addressed by its lowercase sha256 hex digest and by nothing else.
#: Every path this module builds is `root / digest[:2] / digest[2:4] / digest`, so a
#: string that is not a digest is a string that can point anywhere under (or above) the
#: root — which matters for exactly one method, `delete`, the only one that unlinks.
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class BlobStore(Protocol):
    async def put(self, data: bytes) -> str:
        """Store bytes, return their sha256 hex digest."""
        ...

    async def put_stream(self, chunks: AsyncIterator[bytes]) -> str:
        """Store a stream of bytes, return their sha256 hex digest — for large blobs
        (a multi-hundred-MB artifact) that must never be buffered whole in memory."""
        ...

    async def get(self, sha256: str) -> bytes:
        """Read a stored blob; raises FileNotFoundError when absent."""
        ...

    def path_for(self, sha256: str) -> Path:
        """Filesystem path for a stored blob (for streaming responses)."""
        ...

    async def exists(self, sha256: str) -> bool: ...

    async def delete(self, sha256: str) -> bool:
        """Remove a stored blob. True if it was there, False if it already wasn't.

        The one destructive operation here, and the reason it exists is the SDR trim
        (docs/plans/SDR_RECORDING_PLAN.md §5): a trim writes the cut clip as a new
        content-addressed blob and repoints the row, so keeping the original would add a
        blob and free nothing — inverting the feature.

        **Idempotent by contract.** A caller deleting a blob that is already gone has
        got what it asked for, and raising there would leave a row that has been
        repointed unable to finish cleaning up after itself. Callers must still check
        that nothing else references the digest: blobs are content-addressed, so two
        rows with identical bytes share one file.
        """
        ...

    def usage(self) -> tuple[int, int]:
        """(blob_count, total_bytes) — fine to walk at personal scale."""
        ...

    def free_bytes(self) -> int:
        """Space left on the volume the blobs live on.

        Here rather than a `shutil.disk_usage` at the call site because where the blobs
        actually are is this abstraction's secret (rule 2), and a caller that had to know
        the path in order to ask would be a caller that could also write to it.
        """
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

    async def put_stream(self, chunks: AsyncIterator[bytes]) -> str:
        # Hash and spool to a temp file as chunks arrive, then rename into place under the
        # digest — so a multi-hundred-MB artifact never sits in memory and a crash never
        # leaves a partial blob addressable. The digest isn't known until the stream ends,
        # so spool under a unique temp name first, then rename to the content path.
        digest = hashlib.sha256()
        self._root.mkdir(parents=True, exist_ok=True)
        tmp = self._root / f".incoming-{uuid.uuid4().hex}.tmp"
        try:
            with tmp.open("wb") as fh:
                async for chunk in chunks:
                    digest.update(chunk)
                    await asyncio.to_thread(fh.write, chunk)
            hexdigest = digest.hexdigest()
            target = self.path_for(hexdigest)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp.rename(target)
            return hexdigest
        finally:
            tmp.unlink(missing_ok=True)

    async def get(self, sha256: str) -> bytes:
        # to_thread keeps large attachment reads off the event loop.
        return await asyncio.to_thread(self.path_for(sha256).read_bytes)

    async def exists(self, sha256: str) -> bool:
        return self.path_for(sha256).exists()

    async def delete(self, sha256: str) -> bool:
        # The one method here that unlinks, so the one that checks what it was handed.
        # Every caller today passes a digest read from an RLS-scoped row, which is why
        # this has never fired — but "unreachable" is a property of today's callers, not
        # of this method, and the cost of being wrong once is someone else's file.
        if not _DIGEST_RE.fullmatch(sha256):
            raise ValueError(f"not a sha256 digest: {sha256!r}")
        # `missing_ok` rather than a prior `exists()` check: two callers deleting the
        # same digest at once would both pass the check and one would raise, and the
        # answer to "is it gone" is the same either way. The now-empty shard directories
        # are left alone — they cost an inode each and removing them races every put
        # that is midway through `mkdir`/`rename` into the same shard.
        path = self.path_for(sha256)

        def _unlink() -> bool:
            existed = path.exists()
            path.unlink(missing_ok=True)
            return existed

        return await asyncio.to_thread(_unlink)

    def usage(self) -> tuple[int, int]:
        count = 0
        total = 0
        if self._root.exists():
            for path in self._root.rglob("*"):
                if path.is_file() and not path.name.endswith(".tmp"):
                    count += 1
                    total += path.stat().st_size
        return count, total

    def free_bytes(self) -> int:
        # The root may not exist yet on a box that has stored nothing; the volume it will
        # be created on is the one whose free space the answer is about.
        probe = self._root if self._root.exists() else self._root.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            # An unreadable mount is not an empty disk. Answering "loads of room" would
            # turn a free-space guard into a rubber stamp, so say nothing is left and let
            # the caller refuse — a refusal with a sentence beats a full volume on a box
            # whose owner has no terminal (CLAUDE.md #10).
            return 0


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


# A caller name becomes a file name in a directory other containers mount; keep it to a
# shape that cannot name anything but a sibling token file.
_SECRET_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class InternalSecrets(Protocol):
    """The per-caller bearers for `/internal/*`, one file per caller.

    The api is the only writer; the callers (the wall, mosquitto) mount the directory
    read-only and present their own file's contents. See api/internal_auth.py.
    """

    def ensure(self, name: str) -> None:
        """Mint `name`'s token if it has none; an existing token is kept."""
        ...

    def read(self, name: str) -> bytes | None:
        """The token, or None while it does not exist (yet) or cannot be read."""
        ...


class FsInternalSecrets:
    def __init__(self, root: str | Path):
        self._root = Path(root)
        # A token never changes while this process lives (only this api writes them, and
        # never rewrites one), so the first successful read is cached. A miss is NOT
        # cached: the file may appear after start-up and must then be honoured.
        self._cache: dict[str, bytes] = {}

    def _path(self, name: str) -> Path:
        if not _SECRET_NAME_RE.fullmatch(name):
            raise ValueError(f"not an internal caller name: {name!r}")
        return self._root / f"{name}.token"

    def ensure(self, name: str) -> None:
        target = self._path(name)
        if target.exists():
            return
        token = secrets.token_hex(32).encode()
        # The token is written whole into a private O_EXCL temp file, then hard-linked into
        # place. link() refuses an existing name, so two api processes racing on a fresh
        # volume both finish with ONE token and neither overwrites the other's (a rename
        # would let the loser replace a token a caller may already have read), and no
        # reader ever sees a created-but-still-empty file.
        tmp = self._root / f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)  # the umask can only narrow it, but say it outright
            os.write(fd, token)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            # Another process won the race: its token stands.
            with contextlib.suppress(FileExistsError):
                os.link(tmp, target)
        finally:
            if fd >= 0:
                os.close(fd)
            tmp.unlink(missing_ok=True)

    def read(self, name: str) -> bytes | None:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        try:
            data = self._path(name).read_bytes().strip()
        except OSError:
            return None
        if not data:
            return None
        self._cache[name] = data
        return data


#: Every owner-tunnel key file, and which volume holds it. A CA private key lives only in the
#: api's private volume; the owner site's own key and the public certificates live in the
#: volume the internet-facing Caddy mounts read-only, so Caddy never holds a key that could
#: mint a certificate (docs/plans/OWNER_TUNNEL_PLAN.md §3.1).
OWNER_KEY_FILES: dict[str, bool] = {
    "owner-ca.key": True,
    "enroll-ca.key": True,
    "owner-ca.crt": False,
    "enroll-ca.crt": False,
    "site.crt": False,
    "site.key": False,
}


class OwnerKeyFiles(Protocol):
    """The owner tunnel's CA and site key material, by fixed file name."""

    def read(self, name: str) -> bytes | None:
        """The file's bytes, or None when it does not exist."""
        ...

    def write(self, name: str, data: bytes) -> None:
        """Replace the file whole; a reader sees the old bytes or the new, never a mix."""
        ...


class FsOwnerKeyFiles:
    def __init__(self, private_root: str | Path, shared_root: str | Path):
        self._private = Path(private_root)
        self._shared = Path(shared_root)

    def _path(self, name: str) -> Path:
        private = OWNER_KEY_FILES.get(name)
        if private is None:
            raise ValueError(f"not an owner key file: {name!r}")
        return (self._private if private else self._shared) / name

    def read(self, name: str) -> bytes | None:
        try:
            return self._path(name).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, name: str, data: bytes) -> None:
        target = self._path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Private keys are 0600 even in the shared volume: Caddy reads them as root.
        mode = 0o600 if name.endswith(".key") else 0o644
        tmp = target.parent / f".{name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            os.fchmod(fd, mode)
            os.write(fd, data)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, target)
        finally:
            if fd >= 0:
                os.close(fd)
            tmp.unlink(missing_ok=True)
