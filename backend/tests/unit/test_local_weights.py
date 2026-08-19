"""The on-disk weights reader: real GGUF footprint, best-effort to None."""

from pathlib import Path

from jbrain.llm import local_weights

_GIB = 1024**3


def _write(path: Path, size_bytes: int) -> None:
    # Sparse: set the file's apparent size without writing size_bytes of real data,
    # so the GiB-scale sizes these tests assert on cost no disk (st_size still reads
    # the full length, which is all weights_size_gb/dir_size_gb measure).
    with path.open("wb") as f:
        f.truncate(size_bytes)


def test_sums_every_gguf_in_the_model_dir(tmp_path: Path) -> None:
    # Weights live in <models_dir>/<model_id>/; a multi-shard model plus its vision
    # projector all count toward the real footprint.
    model = tmp_path / "qwen3-vl-30b"
    model.mkdir()
    _write(model / "model-00001-of-00002.gguf", int(1.5 * _GIB))
    _write(model / "model-00002-of-00002.gguf", int(0.5 * _GIB))
    _write(model / "mmproj.gguf", int(0.1 * _GIB))
    # A README alongside the weights must not inflate the size.
    _write(model / "README.md", 4096)

    assert local_weights.weights_size_gb(str(tmp_path), "qwen3-vl-30b") == 2.1


def test_sums_gguf_nested_in_a_quant_subdir(tmp_path: Path) -> None:
    # Unsloth's UD-Q* shards land in a <id>/<quant>/ subdir; the footprint must count
    # them recursively (so disk_gb isn't null for a sharded model like Nemotron), while
    # ignoring hf's .cache/ staging.
    model = tmp_path / "nemotron-3-super-120b"
    (model / "UD-Q4_K_XL").mkdir(parents=True)
    _write(model / "UD-Q4_K_XL" / "shard-00001-of-00002.gguf", int(1.0 * _GIB))
    _write(model / "UD-Q4_K_XL" / "shard-00002-of-00002.gguf", int(1.5 * _GIB))
    cache = model / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    _write(cache / "stale-00001-of-00002.gguf", int(5.0 * _GIB))  # must NOT count
    assert local_weights.weights_size_gb(str(tmp_path), "nemotron-3-super-120b") == 2.5


def test_missing_model_dir_is_none(tmp_path: Path) -> None:
    assert local_weights.weights_size_gb(str(tmp_path), "not-provisioned") is None


def test_dir_without_weights_is_none(tmp_path: Path) -> None:
    # An empty (or weights-less) directory reads as not-provisioned rather than 0 GB.
    model = tmp_path / "gpt-oss-120b"
    model.mkdir()
    _write(model / "config.json", 200)
    assert local_weights.weights_size_gb(str(tmp_path), "gpt-oss-120b") is None


def test_absent_mount_is_none() -> None:
    assert local_weights.weights_size_gb("/no/such/mount", "qwen3-vl-30b") is None


def test_dir_size_counts_partial_downloads_including_the_hf_cache(tmp_path: Path) -> None:
    # The progress reader counts EVERY file recursively, including the in-flight
    # shards huggingface streams into the `.cache/huggingface/download/*.incomplete`
    # SUBDIR before renaming them up — so the bar climbs through the whole download
    # instead of reading 0 until a ~50 GB shard finishes (the bug that left a large
    # in-progress sharded model reading 0% on the box).
    model = tmp_path / "nemotron-3-super-120b"
    model.mkdir()
    _write(model / "shard-00001-of-00003.gguf", int(1.0 * _GIB))  # one shard done, moved up
    cache = model / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    _write(cache / "shard-00002-of-00003.gguf.incomplete", int(0.5 * _GIB))  # still downloading
    assert local_weights.dir_size_gb(str(tmp_path), "nemotron-3-super-120b") == 1.5


def test_dir_size_is_zero_for_a_started_empty_dir(tmp_path: Path) -> None:
    # A just-created download dir reads as 0.0 (0%), distinct from None (not started):
    # the progress bar shows "downloading" rather than "nothing here".
    (tmp_path / "glm-4.5-air").mkdir()
    assert local_weights.dir_size_gb(str(tmp_path), "glm-4.5-air") == 0.0


def test_dir_size_missing_dir_is_none(tmp_path: Path) -> None:
    assert local_weights.dir_size_gb(str(tmp_path), "not-started") is None


# --- dropping the page-cache copy of the weights ----------------------------
#
# `--no-mmap` (a gfx1151 stability flag) makes llama.cpp READ each GGUF rather than map it,
# so the weights end up resident twice: once in GTT, once in the page cache the read filled.
# Unload frees the GTT copy and leaves the other. Measured on the box, one gpt-oss-120b load
# took `Cached` 5.2 -> 49.1 GiB with `MemFree` at 8.4 of 121, and the cache copy outlived the
# unload — the invisible half of the cost that livelocked the host for seven hours.


def _fadvised(monkeypatch) -> list[str]:  # type: ignore[no-untyped-def]
    """Record which files get POSIX_FADV_DONTNEED, by path."""
    import os as _os

    seen: list[str] = []
    real_open = _os.open

    def fake_fadvise(fd: int, offset: int, length: int, advice: int) -> None:
        assert advice == _os.POSIX_FADV_DONTNEED
        assert (offset, length) == (0, 0), "must cover the whole file"
        seen.append(_os.readlink(f"/proc/self/fd/{fd}"))

    monkeypatch.setattr(_os, "posix_fadvise", fake_fadvise)
    monkeypatch.setattr(_os, "open", real_open)
    return seen


def test_it_drops_every_shard_and_reports_the_total(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Every shard advised, and the GiB total is the sum of their sizes.

    The sizes are faked rather than written: a real fixture would have to put GiB on disk to
    move a figure reported in GiB, and a test is not worth the bytes."""
    import os as _os

    seen = _fadvised(monkeypatch)
    root = tmp_path / "m"
    root.mkdir()
    (root / "a.gguf").write_bytes(b"x")
    (root / "b.gguf").write_bytes(b"x")

    real_fstat = _os.fstat
    monkeypatch.setattr(
        _os,
        "fstat",
        lambda fd: type("_St", (), {"st_size": 16 * 1024**3})(),  # 16 GiB each
    )
    freed = local_weights.drop_weights_page_cache(str(tmp_path), "m")
    monkeypatch.setattr(_os, "fstat", real_fstat)

    assert freed == 32.0  # two 16 GiB shards
    assert sorted(p.rsplit("/", 1)[-1] for p in seen) == ["a.gguf", "b.gguf"]


def test_it_leaves_alone_anything_that_is_not_a_shard(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The staging dir holds partial downloads and the model dir holds sidecar files; only
    the GGUF the loader actually read is ours to drop."""
    seen = _fadvised(monkeypatch)
    root = tmp_path / "m"
    (root / ".cache" / "huggingface").mkdir(parents=True)
    (root / ".cache" / "huggingface" / "part.gguf").write_bytes(b"x" * 1024)
    (root / "README.md").write_text("not a shard")
    (root / "real.gguf").write_bytes(b"x" * 1024)
    local_weights.drop_weights_page_cache(str(tmp_path), "m")
    assert [p.rsplit("/", 1)[-1] for p in seen] == ["real.gguf"]


def test_a_model_that_was_never_provisioned_is_not_an_error(tmp_path: Path) -> None:
    assert local_weights.drop_weights_page_cache(str(tmp_path), "absent") is None


def test_a_directory_with_no_shards_reports_nothing_dropped(tmp_path: Path) -> None:
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "notes.txt").write_text("x")
    assert local_weights.drop_weights_page_cache(str(tmp_path), "m") is None


def test_an_unreadable_shard_does_not_break_the_sweep(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Best-effort throughout: a failure here costs memory, never correctness, so it must
    never propagate into the load that just succeeded."""
    import os as _os

    root = tmp_path / "m"
    root.mkdir()
    (root / "a.gguf").write_bytes(b"x" * 1024)

    def boom(fd: int, offset: int, length: int, advice: int) -> None:
        raise OSError("EINVAL on this filesystem")

    monkeypatch.setattr(_os, "posix_fadvise", boom)
    assert local_weights.drop_weights_page_cache(str(tmp_path), "m") is None
