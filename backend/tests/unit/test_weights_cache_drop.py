"""What `drop_weights_page_cache` reports, and why it is now a measurement.

The function shipped on 2026-08-19 did:

    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    dropped += os.fstat(fd).st_size

`posix_fadvise` returns 0 unconditionally — `generic_fadvise()` discards the count from
`invalidate_mapping_pages()` and reports success whether it evicted every page or none.
So the reported "GiB dropped" was the total size of every matching file, evicted or not.
It could not tell a working drop from a total no-op, on the one code path whose whole job
is proving the second copy of the weights is gone.

These pin the measurement against a real page cache, not a fake.
"""

import os
from pathlib import Path

import pytest

from jbrain.llm.local_weights import _cached_pages, drop_weights_page_cache

_MIB = 1024 * 1024


def _write(path: Path, size: int, *, clean: bool) -> None:
    """A file whose pages are clean (fsync'd) or still dirty."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT)
    try:
        os.write(fd, os.urandom(size))
        if clean:
            os.fsync(fd)
    finally:
        os.close(fd)
    with open(path, "rb") as handle:  # pull it into the page cache
        while handle.read(1 << 20):
            pass


def _cached(path: Path) -> int | None:
    fd = os.open(path, os.O_RDONLY)
    try:
        return _cached_pages(fd)
    finally:
        os.close(fd)


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    root = tmp_path / "models" / "some-model"
    root.mkdir(parents=True)
    return root


def _skip_without_cachestat(path: Path) -> None:
    if _cached(path) is None:
        pytest.skip("cachestat(2) unavailable (needs Linux 6.5+)")


def test_a_clean_drop_reports_what_it_actually_freed(model_dir: Path) -> None:
    weights = model_dir / "model.gguf"
    _write(weights, 8 * _MIB, clean=True)
    _skip_without_cachestat(weights)
    assert _cached(weights) == 8 * _MIB // os.sysconf("SC_PAGE_SIZE")

    freed = drop_weights_page_cache(str(model_dir.parent), model_dir.name)

    assert _cached(weights) == 0, "the page cache copy survived"
    # Compared at the function's own stated precision (2dp) rather than with a tolerance:
    # at 8 MiB the rounding step is a third of the value, so a `rel=` check here would be
    # testing the rounding, not the measurement.
    assert freed == round(8 * _MIB / 1024**3, 2)


def test_a_drop_that_frees_nothing_reports_nothing(model_dir: Path) -> None:
    """The regression that matters. Dirty folios are skipped — `filemap_flush_range()`
    starts writeback `WB_SYNC_NONE` and does not wait — so this is a real no-op, and the
    OLD code would have reported the full 8 MiB as dropped."""
    weights = model_dir / "model.gguf"
    _write(weights, 8 * _MIB, clean=False)
    _skip_without_cachestat(weights)
    before = _cached(weights)

    freed = drop_weights_page_cache(str(model_dir.parent), model_dir.name)

    assert _cached(weights) == before, "expected the dirty pages to be skipped"
    assert freed == 0.0, f"reported {freed} GiB freed while the cache was untouched"


def test_the_reported_number_can_distinguish_a_small_drop_from_none(model_dir: Path) -> None:
    """Rounded to one decimal, 8 MiB and zero both render as 0.0 — which would reinstate
    the exact ambiguity this rewrite removes."""
    weights = model_dir / "model.gguf"
    _write(weights, 8 * _MIB, clean=True)
    _skip_without_cachestat(weights)
    assert drop_weights_page_cache(str(model_dir.parent), model_dir.name) != 0.0


def test_a_missing_model_directory_reports_none(tmp_path: Path) -> None:
    assert drop_weights_page_cache(str(tmp_path), "not-installed") is None


def test_only_weight_files_are_touched(model_dir: Path) -> None:
    """Config and tokenizer files share the directory and are tiny; dropping them buys
    nothing and would make the measurement harder to attribute."""
    weights, other = model_dir / "model.gguf", model_dir / "tokenizer.json"
    _write(weights, 4 * _MIB, clean=True)
    _write(other, 4 * _MIB, clean=True)
    _skip_without_cachestat(weights)

    drop_weights_page_cache(str(model_dir.parent), model_dir.name)

    assert _cached(weights) == 0
    assert _cached(other) != 0, "a non-weight file had its cache dropped"


def test_download_staging_is_skipped(model_dir: Path) -> None:
    """hf's `.cache` staging holds partial shards nothing has read into a model."""
    staging = model_dir / ".cache" / "huggingface"
    staging.mkdir(parents=True)
    partial = staging / "partial.gguf"
    _write(partial, 4 * _MIB, clean=True)
    _skip_without_cachestat(partial)

    drop_weights_page_cache(str(model_dir.parent), model_dir.name)

    assert _cached(partial) != 0, "staging shards were dropped"


def test_every_drop_outcome_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """`0.0` and `None` are the readings worth having, and both used to be silent.

    The gateway logged on `if freed:`, which was harmless while the figure was the sum of
    file sizes and therefore always truthy. Once it became a MEASUREMENT, a drop that freed
    nothing and a kernel that cannot measure both stopped saying anything — verified on the
    live box, where a successful load dropped its cache and logged no line at all."""
    from jbrain.llm import local_gateway, local_weights

    events: list[str] = []
    monkeypatch.setattr(local_gateway.log, "info", lambda event, **_kw: events.append(event))
    client = local_gateway.LocalGatewayClient("http://gw", models_dir="/models")
    model = next(iter(__import__("jbrain.llm.local_catalog", fromlist=["CATALOG"]).CATALOG))

    for value, expected in (
        (None, "local_gateway.weights_cache_unmeasured"),
        (0.0, "local_gateway.weights_cache_drop_freed_nothing"),
        (4.29, "local_gateway.weights_cache_dropped"),
    ):
        events.clear()
        monkeypatch.setattr(local_weights, "drop_weights_page_cache", lambda _d, _m, v=value: v)
        client._drop_weights_cache(model)
        assert events == [expected], f"{value!r} logged {events}"
