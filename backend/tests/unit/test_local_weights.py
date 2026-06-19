"""The on-disk weights reader: real GGUF footprint, best-effort to None."""

from pathlib import Path

from jbrain.llm import local_weights

_GIB = 1024**3


def _write(path: Path, size_bytes: int) -> None:
    path.write_bytes(b"\0" * size_bytes)


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
