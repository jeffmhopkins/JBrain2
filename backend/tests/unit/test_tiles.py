"""The basemap tile cache + cache-first service (location map proxy). The upstream
HTTP fetch is faked — CI never reaches a real tile host."""

from jbrain.tiles import FsTileCache, TileService, valid_tile

_PNG = b"\x89PNG\r\n\x1a\n-fake-tile"


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
