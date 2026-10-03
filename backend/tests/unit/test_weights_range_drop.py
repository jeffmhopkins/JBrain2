"""The range-aware page-cache drop for file-backed models (Flash-Next's engram table).

The engram tensor's pages are the served working set and must stay; every other byte of the
shards is residue of the read that uploaded the GPU weights. These pin the GGUF header parse
against a synthetic file, the complement arithmetic, and which ranges reach `posix_fadvise`.
"""

import os
import struct
from pathlib import Path

import pytest

from jbrain.llm import local_catalog, local_weights
from jbrain.llm.local_weights import GgufError, _tensor_ranges, drop_ranges

FLASH_ID = "qwen3.8-flash-next"


def _gguf_string(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def write_gguf(
    path: Path,
    tensors: list[tuple[str, int]],
    *,
    alignment: int | None = None,
    vocab: tuple[str, ...] = ("a", "bb", "ccc"),
) -> int:
    """A minimal GGUF v3: a string-array KV (the vocab shape that has to be walked), an
    optional `general.alignment`, then `tensors` as (name, byte size) laid out in order.
    Returns the data-section start."""
    kvs = [
        _gguf_string("general.architecture") + struct.pack("<I", 8) + _gguf_string("test"),
        _gguf_string("tokenizer.ggml.tokens")
        + struct.pack("<IIQ", 9, 8, len(vocab))
        + b"".join(_gguf_string(t) for t in vocab),
        _gguf_string("some.floats") + struct.pack("<IIQ", 9, 6, 3) + struct.pack("<3f", 1, 2, 3),
    ]
    if alignment is not None:
        kvs.append(_gguf_string("general.alignment") + struct.pack("<II", 4, alignment))
    align = alignment or 32
    infos = b""
    offset = 0
    offsets = []
    for name, size in tensors:
        offsets.append(offset)
        infos += _gguf_string(name) + struct.pack("<I", 1) + struct.pack("<Q", size)
        infos += struct.pack("<I", 0) + struct.pack("<Q", offset)
        offset += -(-size // align) * align
    header = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(kvs)) + b"".join(kvs) + infos
    data_start = -(-len(header) // align) * align
    with open(path, "wb") as handle:
        handle.write(header + b"\0" * (data_start - len(header)))
        for (_name, size), off in zip(tensors, offsets, strict=True):
            handle.seek(data_start + off)
            handle.write(b"\x01" * size)
        handle.truncate(data_start + offset)
    return data_start


# --- the header parse ----------------------------------------------------------------------


def test_tensor_ranges_reads_names_and_absolute_offsets(tmp_path: Path) -> None:
    shard = tmp_path / "m-00002-of-00003.gguf"
    start = write_gguf(shard, [("output.weight", 100), ("per_layer_token_embd.weight", 5000)])
    ranges = _tensor_ranges(str(shard))
    assert [r[0] for r in ranges] == ["output.weight", "per_layer_token_embd.weight"]
    assert ranges[0][1] == start
    # A tensor runs to the next one's offset (alignment padding included) or the file's end.
    assert ranges[0][2] == ranges[1][1] == start + 128
    assert ranges[1][2] == os.path.getsize(shard)


def test_tensor_ranges_honours_a_custom_alignment(tmp_path: Path) -> None:
    shard = tmp_path / "a.gguf"
    start = write_gguf(shard, [("x", 10), ("y", 10)], alignment=4096)
    assert start % 4096 == 0
    ranges = _tensor_ranges(str(shard))
    assert ranges[1][1] - ranges[0][1] == 4096


def test_a_metadata_only_shard_has_no_tensors(tmp_path: Path) -> None:
    shard = tmp_path / "m-00001-of-00003.gguf"
    write_gguf(shard, [])
    assert _tensor_ranges(str(shard)) == ()


def test_a_file_that_is_not_gguf_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.gguf"
    bad.write_bytes(b"NOPE" + b"\0" * 64)
    with pytest.raises(GgufError):
        _tensor_ranges(str(bad))
    truncated = tmp_path / "short.gguf"
    truncated.write_bytes(b"GGUF" + struct.pack("<I", 3))
    with pytest.raises(GgufError):
        _tensor_ranges(str(truncated))


# --- the range arithmetic ------------------------------------------------------------------


def test_drop_ranges_is_the_complement_of_what_is_kept() -> None:
    assert drop_ranges(100, []) == [(0, 100)]
    assert drop_ranges(100, [(10, 20)]) == [(0, 10), (20, 100)]
    assert drop_ranges(100, [(0, 100)]) == []
    # Overlapping and unsorted kept ranges merge; out-of-file edges are clipped.
    assert drop_ranges(100, [(50, 70), (10, 20), (15, 30), (90, 200)]) == [
        (0, 10),
        (30, 50),
        (70, 90),
    ]
    assert drop_ranges(100, [(-5, 0), (40, 40)]) == [(0, 100)]


# --- which tensors are kept ----------------------------------------------------------------


def test_the_kept_tensors_come_from_the_catalogs_cpu_override() -> None:
    flash = local_catalog.get(FLASH_ID)
    assert flash is not None
    assert local_catalog.cpu_mapped_tensor_patterns(flash) == ("per_layer_token_embd",)
    gpt = local_catalog.get("gpt-oss-120b")
    assert gpt is not None and local_catalog.cpu_mapped_tensor_patterns(gpt) == ()


def test_several_override_rules_in_one_flag_are_all_read() -> None:
    import dataclasses

    flash = local_catalog.get(FLASH_ID)
    assert flash is not None
    multi = dataclasses.replace(
        flash, extra_server_args=("--override-tensor", "a=CPU,b=Vulkan0,c\\.weight=cpu")
    )
    assert local_catalog.cpu_mapped_tensor_patterns(multi) == ("a", "c\\.weight")


# --- the drop itself -----------------------------------------------------------------------


@pytest.fixture
def advised(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Every `posix_fadvise` call as (file basename, offset, length)."""
    calls: list[tuple[str, int, int]] = []
    real_open = os.open
    names: dict[int, str] = {}

    def _open(path: str, flags: int, *a: object) -> int:
        fd = real_open(path, flags, *a)  # type: ignore[arg-type]
        names[fd] = os.path.basename(path)
        return fd

    def _fadvise(fd: int, offset: int, length: int, advice: int) -> None:
        assert advice == os.POSIX_FADV_DONTNEED
        calls.append((names.get(fd, "?"), offset, length))

    monkeypatch.setattr(local_weights.os, "open", _open)
    monkeypatch.setattr(local_weights.os, "posix_fadvise", _fadvise)
    monkeypatch.setattr(local_weights, "_cached_pages", lambda _fd: None)
    return calls


def test_the_engram_range_is_never_advised_and_everything_else_is(
    tmp_path: Path, advised: list[tuple[str, int, int]]
) -> None:
    root = tmp_path / FLASH_ID
    root.mkdir()
    shard = root / "m-00002-of-00003.gguf"
    start = write_gguf(
        shard,
        [
            ("output.weight", 300),
            ("per_layer_token_embd.weight", 9000),
            ("token_embd.weight", 200),
            ("blk.0.ffn.weight", 700),
        ],
    )
    gpu_only = root / "m-00003-of-00003.gguf"
    write_gguf(gpu_only, [("blk.1.ffn.weight", 500)])
    (root / "mmproj-F16.gguf").write_bytes(b"\0" * 64)  # not even a parseable header

    assert local_weights.drop_weights_page_cache_except_mapped(str(tmp_path), FLASH_ID) is None

    engram = next(r for r in _tensor_ranges(str(shard)) if r[0].startswith("per_layer"))
    by_file: dict[str, list[tuple[int, int]]] = {}
    for name, off, length in advised:
        by_file.setdefault(name, []).append((off, off + length))
    # The engram shard: everything before the engram and everything after it, nothing inside.
    assert by_file[shard.name] == [(0, engram[1]), (engram[2], os.path.getsize(shard))]
    assert engram[1] > start
    # A shard with no kept tensor is dropped whole.
    assert by_file[gpu_only.name] == [(0, os.path.getsize(gpu_only))]
    # A shard whose header will not parse is left alone: dropping it whole might evict engram.
    assert "mmproj-F16.gguf" not in by_file


def test_a_model_with_no_mapped_tensors_gets_the_whole_file_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        local_weights, "_drop_page_cache", lambda root, _s: seen.append(root) or 1.0
    )
    assert local_weights.drop_weights_page_cache_except_mapped(str(tmp_path), "gpt-oss-120b") == 1.0
    assert seen == [str(tmp_path / "gpt-oss-120b")]


def test_a_missing_model_directory_reports_none(tmp_path: Path) -> None:
    assert local_weights.drop_weights_page_cache_except_mapped(str(tmp_path), FLASH_ID) is None


def test_the_drop_is_measured_against_a_real_page_cache(tmp_path: Path) -> None:
    """Against the kernel, not a fake: the engram range stays cached, the rest goes."""
    root = tmp_path / FLASH_ID
    root.mkdir()
    page = os.sysconf("SC_PAGE_SIZE")
    shard = root / "m.gguf"
    write_gguf(
        shard,
        [("blk.0.weight", 16 << 20), ("per_layer_token_embd.weight", 64 * page)],
        alignment=page,
    )
    fd = os.open(shard, os.O_RDONLY)
    try:
        os.fsync(fd)
        if local_weights._cached_pages(fd) is None:
            pytest.skip("cachestat(2) unavailable (needs Linux 6.5+ outside seccomp)")
    finally:
        os.close(fd)
    with open(shard, "rb") as handle:
        while handle.read(1 << 20):
            pass
    freed = local_weights.drop_weights_page_cache_except_mapped(str(tmp_path), FLASH_ID)
    assert freed is not None and freed > 0
    fd = os.open(shard, os.O_RDONLY)
    try:
        left = local_weights._cached_pages(fd)
    finally:
        os.close(fd)
    # The engram's 64 pages survive; the 16 MiB in front of it is what was freed.
    assert left == 64 and freed == round(((16 << 20) + page) / 1024**3, 2)
