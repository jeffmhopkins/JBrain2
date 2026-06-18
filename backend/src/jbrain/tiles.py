"""Server-side basemap tile proxy + cache (location map).

The phone's Leaflet map fetches tiles from THIS box (`/api/tiles/{z}/{x}/{y}.png`),
never a third-party tile host: the proxy fetches a tile from the configured
upstream once, caches it on disk, and serves every later request locally. The
upstream therefore sees only the server's coarse tile requests (tied to the server
IP), never the owner's device or a fine coordinate — a deliberate, bounded
relaxation of L1 ("no tiles leave the box"), recorded in PHASE7_LOCATION_PLAN.md.

File I/O goes through a storage abstraction (CLAUDE.md rule 2). An empty upstream
disables tiles (the map falls back to the on-box schematic).
"""

import asyncio
from pathlib import Path
from typing import Protocol

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 15.0


def valid_tile(z: int, x: int, y: int, max_zoom: int) -> bool:
    """A well-formed slippy-map coordinate: 0 ≤ z ≤ max_zoom and x/y within the
    2^z grid. Rejects out-of-range requests before any disk or network touch."""
    if not (0 <= z <= max_zoom):
        return False
    span = 1 << z
    return 0 <= x < span and 0 <= y < span


class TileCache(Protocol):
    async def get(self, z: int, x: int, y: int) -> bytes | None: ...

    async def put(self, z: int, x: int, y: int, data: bytes) -> None: ...


class FsTileCache:
    """A z/x/y directory tree of cached PNG tiles. Tiles are public map data (not
    owner content), so they carry no RLS — the endpoint that fills the cache is the
    owner gate."""

    def __init__(self, root: str | Path):
        self._root = Path(root)

    def _path(self, z: int, x: int, y: int) -> Path:
        return self._root / str(z) / str(x) / f"{y}.png"

    async def get(self, z: int, x: int, y: int) -> bytes | None:
        path = self._path(z, x, y)
        if not path.exists():
            return None
        return await asyncio.to_thread(path.read_bytes)

    async def put(self, z: int, x: int, y: int, data: bytes) -> None:
        target = self._path(z, x, y)
        await asyncio.to_thread(self._write, target, data)

    @staticmethod
    def _write(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.rename(target)  # atomic: a crash never leaves a partial tile served


class TileFetcher(Protocol):
    async def fetch(self, url: str) -> bytes | None: ...


class HttpTileFetcher:
    def __init__(self, user_agent: str, transport: httpx.AsyncBaseTransport | None = None):
        self._user_agent = user_agent
        self._transport = transport

    async def fetch(self, url: str) -> bytes | None:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers={"User-Agent": self._user_agent})
                resp.raise_for_status()
                return resp.content
        except Exception as exc:  # noqa: BLE001 - a tile miss must degrade, never 500
            log.warning("tiles.fetch_failed", url=url, error=repr(exc))
            return None


class TileService:
    """Cache-first tile resolution: a hit serves from disk; a miss fetches the
    configured upstream once, caches it, and returns it. Disabled (no upstream) or
    a failed fetch returns None — the endpoint then 404s and the map degrades to the
    schematic rather than erroring."""

    def __init__(
        self,
        cache: TileCache,
        fetcher: TileFetcher,
        *,
        upstream_template: str,
        max_zoom: int,
    ):
        self._cache = cache
        self._fetcher = fetcher
        self._upstream = upstream_template
        self._max_zoom = max_zoom

    @property
    def enabled(self) -> bool:
        return bool(self._upstream)

    async def tile(self, z: int, x: int, y: int) -> bytes | None:
        if not self._upstream or not valid_tile(z, x, y, self._max_zoom):
            return None
        cached = await self._cache.get(z, x, y)
        if cached is not None:
            return cached
        data = await self._fetcher.fetch(self._upstream.format(z=z, x=x, y=y))
        if data is None:
            return None
        await self._cache.put(z, x, y, data)
        return data
