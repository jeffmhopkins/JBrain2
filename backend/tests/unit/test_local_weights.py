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
    # instead of reading 0 until a ~50 GB shard finishes (the bug that left a 94 GB
    # in-progress 235B reading 0% on the box).
    model = tmp_path / "qwen3-235b-a22b"
    model.mkdir()
    _write(model / "shard-00001-of-00003.gguf", int(1.0 * _GIB))  # one shard done, moved up
    cache = model / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    _write(cache / "shard-00002-of-00003.gguf.incomplete", int(0.5 * _GIB))  # still downloading
    assert local_weights.dir_size_gb(str(tmp_path), "qwen3-235b-a22b") == 1.5


def test_dir_size_is_zero_for_a_started_empty_dir(tmp_path: Path) -> None:
    # A just-created download dir reads as 0.0 (0%), distinct from None (not started):
    # the progress bar shows "downloading" rather than "nothing here".
    (tmp_path / "glm-4.5-air").mkdir()
    assert local_weights.dir_size_gb(str(tmp_path), "glm-4.5-air") == 0.0


def test_dir_size_missing_dir_is_none(tmp_path: Path) -> None:
    assert local_weights.dir_size_gb(str(tmp_path), "not-started") is None
