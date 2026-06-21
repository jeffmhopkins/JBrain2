"""The basemap tile cache + cache-first service (location map proxy). The upstream
HTTP fetch is faked — CI never reaches a real tile host."""

from jbrain.tiles import FsTileCache, TileService, TileSet, tile_cache_namespace, valid_tile

_PNG = b"\x89PNG\r\n\x1a\n-fake-tile"


def test_tile_cache_namespace_is_stable_and_per_upstream() -> None:
    osm = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    carto = "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png"
    # Stable for a given upstream, distinct across styles (so a switch re-fetches),
    # and a short filesystem-safe slug.
    assert tile_cache_namespace(osm) == tile_cache_namespace(osm)
    assert tile_cache_namespace(osm) != tile_cache_namespace(carto)
    key = tile_cache_namespace(carto)
    assert len(key) == 8 and key.isalnum()


def test_valid_tile_bounds() -> None:
    assert valid_tile(0, 0, 0, 19)
    assert valid_tile(2, 3, 3, 19)  # 2^2 = 4 → x/y in 0..3
    assert not valid_tile(2, 4, 0, 19)  # x out of grid
    assert not valid_tile(20, 0, 0, 19)  # zoom over max
    assert not valid_tile(-1, 0, 0, 19)


async def test_fs_cache_round_trips(tmp_path) -> None:  # noqa: ANN001
    cache = FsTileCache(tmp_path)
    assert await cache.get(3, 1, 2) is None
    await cache.put(3, 1, 2, _PNG)
    assert await cache.get(3, 1, 2) == _PNG


class FakeFetcher:
    def __init__(self, data: bytes | None) -> None:
        self.data = data
        self.calls: list[str] = []

    async def fetch(self, url: str) -> bytes | None:
        self.calls.append(url)
        return self.data


def _service(tmp_path, fetcher: FakeFetcher, upstream: str = "https://up/{z}/{x}/{y}.png"):  # noqa: ANN001
    return TileService(FsTileCache(tmp_path), fetcher, upstream_template=upstream, max_zoom=19)


async def test_miss_fetches_then_caches(tmp_path) -> None:  # noqa: ANN001
    fetcher = FakeFetcher(_PNG)
    svc = _service(tmp_path, fetcher)
    assert await svc.tile(5, 1, 1) == _PNG
    assert fetcher.calls == ["https://up/5/1/1.png"]
    # Second call is served from disk — no second upstream fetch.
    assert await svc.tile(5, 1, 1) == _PNG
    assert len(fetcher.calls) == 1


async def test_disabled_when_no_upstream(tmp_path) -> None:  # noqa: ANN001
    fetcher = FakeFetcher(_PNG)
    svc = _service(tmp_path, fetcher, upstream="")
    assert svc.enabled is False
    assert await svc.tile(5, 1, 1) is None
    assert fetcher.calls == []


async def test_out_of_range_never_fetches(tmp_path) -> None:  # noqa: ANN001
    fetcher = FakeFetcher(_PNG)
    svc = _service(tmp_path, fetcher)
    assert await svc.tile(2, 9, 0) is None  # x outside the 2^2 grid
    assert fetcher.calls == []


async def test_upstream_failure_returns_none(tmp_path) -> None:  # noqa: ANN001
    fetcher = FakeFetcher(None)
    svc = _service(tmp_path, fetcher)
    assert await svc.tile(5, 1, 1) is None
    # A failed fetch is not cached, so a later success can still fill it.
    assert await FsTileCache(tmp_path).get(5, 1, 1) is None


class BrokenCache:
    """A cache whose disk I/O always fails — e.g. a root-owned volume the
    non-root app can't write (the bug that blanked the basemap)."""

    async def get(self, z: int, x: int, y: int) -> bytes | None:
        raise PermissionError("cache dir unreadable")

    async def put(self, z: int, x: int, y: int, data: bytes) -> None:
        raise PermissionError("cache dir unwritable")


async def test_unwritable_cache_still_serves_the_tile(tmp_path) -> None:  # noqa: ANN001
    # A cache that can't read or write must degrade to a plain fetch, never 500.
    fetcher = FakeFetcher(_PNG)
    svc = TileService(
        BrokenCache(), fetcher, upstream_template="https://up/{z}/{x}/{y}.png", max_zoom=19
    )
    assert await svc.tile(5, 1, 1) == _PNG
    assert fetcher.calls == ["https://up/5/1/1.png"]


def test_tileset_resolves_per_scheme_with_default(tmp_path) -> None:  # noqa: ANN001
    dark = _service(tmp_path / "d", FakeFetcher(_PNG), upstream="https://dark/{z}/{x}/{y}.png")
    light = _service(tmp_path / "l", FakeFetcher(_PNG), upstream="https://light/{z}/{x}/{y}.png")
    tiles = TileSet({"dark": dark, "light": light}, default="dark")

    assert tiles.schemes == frozenset({"dark", "light"})
    assert tiles.service("dark") is dark
    assert tiles.service("light") is light
    # An empty/None scheme falls back to the configured default; an unknown one is None.
    assert tiles.service(None) is dark
    assert tiles.service("") is dark
    assert tiles.service("sepia") is None


async def test_tileset_schemes_use_separate_caches(tmp_path) -> None:  # noqa: ANN001
    # The two schemes fetch from their own upstreams and cache independently — a dark
    # hit never serves the light tile, so the app's toggle stays clean.
    dark_fetch = FakeFetcher(b"dark-png")
    light_fetch = FakeFetcher(b"light-png")
    tiles = TileSet(
        {
            "dark": _service(tmp_path / "d", dark_fetch, upstream="https://dark/{z}/{x}/{y}.png"),
            "light": _service(tmp_path / "l", light_fetch, upstream="https://light/{z}/{x}/{y}.png"),
        },
        default="dark",
    )
    dark_svc = tiles.service("dark")
    light_svc = tiles.service("light")
    assert dark_svc is not None and light_svc is not None
    assert await dark_svc.tile(5, 1, 1) == b"dark-png"
    assert await light_svc.tile(5, 1, 1) == b"light-png"
    assert dark_fetch.calls == ["https://dark/5/1/1.png"]
    assert light_fetch.calls == ["https://light/5/1/1.png"]
