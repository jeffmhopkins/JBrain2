"""Real on-disk footprint of provisioned local-model weights.

The settings drawer reports each model's measured size from the GGUF files an
operator actually downloaded, rather than the catalog's hand-entered estimate.
Those weights are host/infra files on the read-only weights mount — not
application blobs — so (like host_metrics' /proc read) this sits OUTSIDE the
storage abstraction.

Best-effort: returns None when hosting is off, the mount is absent, or the
model's directory hasn't been provisioned, and the caller falls back to the
catalog's nominal size.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import platform
import re
import shutil
import struct
from typing import BinaryIO

import structlog

from jbrain.llm import local_catalog

log = structlog.get_logger()

# Weights are GiB-scale; report in GiB to match the catalog's size_gb units.
_BYTES_PER_GIB = 1024**3
_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def weights_size_gb(models_dir: str, model_id: str) -> float | None:
    """Summed size of the `*.gguf` weights (model shards + the vision projector)
    for one provisioned model, in GiB — or None when its directory is missing or
    unreadable. scripts/local-llm-setup.sh downloads each model into
    `<models_dir>/<model_id>/`, so the directory name is the catalog id."""
    # Recursive: some repos (Unsloth's UD-Q*) nest the shards in a quant subdir, so
    # the weights live under <id>/<quant>/. Skip hf's .cache/ download staging.
    root = os.path.join(models_dir, model_id)
    if not os.path.isdir(root):
        return None
    total = 0
    found = False
    for dirpath, _dirs, files in os.walk(root):
        if ".cache" in os.path.relpath(dirpath, root).split(os.sep):
            continue
        for name in files:
            if name.endswith(".gguf"):
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(dirpath, name))
                    found = True
    return round(total / _BYTES_PER_GIB, 1) if found else None


def free_gb(models_dir: str) -> float | None:
    """Free space on the weights volume, in GiB — or None when no weights directory is
    configured or present (nothing to guard). A directory that exists but cannot be measured
    reads as 0.0: "unknown" must refuse an install rather than wave a ~90 GB download onto a
    volume that may be full, on a box whose owner cannot clean one up (CLAUDE.md #10)."""
    if not models_dir or not os.path.isdir(models_dir):
        return None
    try:
        return round(shutil.disk_usage(models_dir).free / _BYTES_PER_GIB, 1)
    except OSError:
        return 0.0


def dir_size_gb(models_dir: str, model_id: str) -> float | None:
    """Summed size of EVERY file in a model's directory, in GiB — partial
    `*.incomplete` shards included — or None when the directory is absent. Drives
    the PWA's live install-progress bar: unlike weights_size_gb (final `*.gguf`
    only), this climbs smoothly through an in-flight huggingface download, so
    `dir_size_gb / catalog size_gb` is a real percentage mid-provision. Returns
    0.0 for an empty (just-created) directory so a started download reads as 0%,
    not 'not started'.

    Recursive on purpose: huggingface streams in-flight shards into a
    `<dir>/.cache/huggingface/download/*.incomplete` SUBDIRECTORY and only moves
    each up to the top level when it completes. A non-recursive scan would read 0
    through the whole download (every byte sits in .cache until a ~50 GB shard
    finishes), so the bar would never move — walk the tree and count every file."""
    root = os.path.join(models_dir, model_id)
    if not os.path.isdir(root):
        return None
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            # A file vanishing mid-walk (a shard being renamed out of .cache) is fine.
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(dirpath, name))
    return round(total / _BYTES_PER_GIB, 1)


def serves_file_backed(model_id: str) -> bool:
    """Whether the engine serves part of `model_id`'s weights memory-mapped from disk
    (`LocalModel.file_backed_gb`) — Flash-Next's engram table, read through the page cache on
    every token. That table's page cache is the working set, not a residue; the rest of the
    file's cache is residue (`drop_weights_page_cache_except_mapped`)."""
    model = local_catalog.get(model_id)
    return model is not None and model.file_backed_gb > 0


def drop_weights_page_cache(models_dir: str, model_id: str) -> float | None:
    """Drop the kernel page-cache copy of `model_id`'s weights. Returns the GiB whose
    cache was released, or None when the model's directory is missing.

    MEASURED on the box, and the reason this exists: the gateway serves every model with
    `--no-mmap` (a gfx1151 stability flag, jbrain.llm.llama_swap_config). Without mmap
    llama.cpp `read()`s the whole GGUF into buffers it hands to the GPU driver, so the
    kernel caches every block it reads and the weights end up resident TWICE — once in
    GTT, once in the page cache. Loading gpt-oss-120b took GTT to 67.6 GiB and `Cached`
    from 5.2 to 49.1 GiB, leaving `MemFree` at 8.4 GiB of a 121 GiB box. Unloading freed
    the GTT completely and left the 39.4 GiB cache copy behind for good.

    That second copy is what killed the host on 2026-08-19: `MemAvailable` counts it as
    free, so the residency budget saw ~36 GiB of headroom over ~8 GiB of actually-free
    pages, admitted another model, and the reclaim-under-GTT-pressure that followed
    livelocked the box for seven hours. Dropping the cache the moment the load finishes
    removes the second copy at its source — the load has already read what it needs, and
    nothing else wants those bytes until the next load reads them again.

    `POSIX_FADV_DONTNEED` only drops CLEAN pages, and weights are read-only, so this can
    never lose a write. It is advisory: the kernel may decline, and pages another process
    still has mapped stay. Best-effort throughout — a failure here costs memory, never
    correctness, so it degrades to the prior behaviour rather than failing a load.

    Deliberately targeted rather than the global `drop_caches` the update path uses
    (deploy/update-inner.sh): this touches only the weights just read, so Postgres's
    working set and the rest of the box's cache survive.

    Everything above assumes `--no-mmap`. A model with a file-backed share
    (`serves_file_backed`) is the exception WHILE IT IS LOADING OR RESIDENT: part of its page
    cache is the engram table being served, so the caller (`LocalGatewayClient`, which knows the
    resident set) uses `drop_weights_page_cache_except_mapped` then. Once the model is unloaded
    its cache is residue like any other — up to ~88 GiB that `host_metrics.read_memory_gb`
    counts as used — and this drops all of it."""
    return _drop_page_cache(os.path.join(models_dir, model_id), (".gguf",))


def drop_weights_page_cache_except_mapped(models_dir: str, model_id: str) -> float | None:
    """`drop_weights_page_cache` for a file-backed model that may be loading or serving: drop
    every byte range of its GGUF shards EXCEPT the tensors the engine serves from the file
    mapping. Returns GiB released (measured, as `_drop_page_cache`), or None when the model's
    directory is missing or the drop cannot be measured.

    Why ranges: Flash-Next's page cache is two different things in one file. The engram table
    (`per_layer_token_embd`, 26.8 GiB, CPU-pinned and lazily mapped) is the working set — evict
    it and the next tokens read it from disk again. Everything else is residue of the read that
    uploaded ~60 GiB of weights to Vulkan, and nothing reads it again. Skipping the whole model
    (what the gateway used to do, on the assumption that all of it was engram) left that residue
    in `Cached`, which `host_metrics.read_memory_gb` counts as used — the 1M-pool loads that
    tripped the guard's 6 GB host floor on 2026-10-03. Counting the cache as free instead is
    the 2026-08-19 livelock (`MemAvailable` saw ~39 GiB of clean cache as headroom), so the
    residue has to actually go.

    The kept ranges come from each shard's GGUF header (`_tensor_ranges`), matched against the
    catalog's `-ot <regex>=CPU` rules (`local_catalog.cpu_mapped_tensor_patterns`); a shard
    with no kept tensor (the projector, a metadata-only first shard) is dropped whole. A shard
    whose header cannot be parsed is LEFT ALONE — dropping it whole might evict the engram, and
    a parse failure must cost memory, never the served model's latency. `POSIX_FADV_DONTNEED`
    rounds inward to whole pages, so a kept tensor's edge pages are never touched."""
    model = local_catalog.get(model_id)
    patterns = local_catalog.cpu_mapped_tensor_patterns(model) if model is not None else ()
    root = os.path.join(models_dir, model_id)
    if not patterns:
        return _drop_page_cache(root, (".gguf",))
    if not os.path.isdir(root):
        return None
    keep = [re.compile(p) for p in patterns]
    freed_pages = 0
    measured = False
    for path in _weight_files(root, (".gguf",)):
        # Anything at all going wrong with one shard leaves THAT shard's cache alone: it costs
        # memory, and evicting the engram by mistake would cost every token a disk read.
        try:
            got = _drop_shard_except(path, keep)
        except _KnownUnparseable:
            continue  # already logged when it first failed
        except Exception as exc:  # noqa: BLE001 — see above; logged, never raised
            log.warning("local_weights.shard_left_alone", path=path, error=str(exc))
            continue
        if got is not None:
            measured = True
            freed_pages += got
    if not measured:
        return None
    return round(freed_pages * _PAGE_SIZE / _BYTES_PER_GIB, 2)


def drop_ranges(size: int, kept: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The `[lo, hi)` byte ranges of a `size`-byte file that are NOT in any `kept` range —
    the complement, merged and clipped, in file order. Pure, so the arithmetic that decides
    what the kernel may evict is testable without a page cache."""
    out: list[tuple[int, int]] = []
    pos = 0
    for lo, hi in sorted((max(0, lo), min(size, hi)) for lo, hi in kept):
        if hi <= lo:
            continue
        if lo > pos:
            out.append((pos, lo))
        pos = max(pos, hi)
    if pos < size:
        out.append((pos, size))
    return out


def _drop_shard_except(path: str, keep: list[re.Pattern[str]]) -> int | None:
    """Drop one shard's cache outside the tensors matching `keep`; pages freed, or None when
    unmeasurable. Raises on a header it cannot read (the caller leaves the shard alone)."""
    size = os.path.getsize(path)
    ranges = _tensor_ranges(path)
    kept = [(lo, hi) for name, lo, hi in ranges if any(k.search(name) for k in keep)]
    fd = os.open(path, os.O_RDONLY)
    try:
        before = _cached_pages(fd)
        for lo, hi in drop_ranges(size, kept):
            os.posix_fadvise(fd, lo, hi - lo, os.POSIX_FADV_DONTNEED)
        after = _cached_pages(fd)
    finally:
        os.close(fd)
    if before is None or after is None:
        return None
    return max(0, before - after)


class GgufError(ValueError):
    """A GGUF header this parser cannot read (bad magic, unknown type, truncated, or a size
    past the parser's bounds)."""


# GGUF metadata value types (ggml/include/gguf.h `enum gguf_type`) -> fixed byte width; STRING
# (8) and ARRAY (9) are variable and handled in `_skip_value`.
_GGUF_FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
_GGUF_STRING, _GGUF_ARRAY, _GGUF_UINT32 = 8, 9, 4
_GGUF_DEFAULT_ALIGNMENT = 32

# Bounds on what a header may claim, so a corrupt or hostile file is refused instead of driving
# a huge read or a near-endless loop. Generous against real files: llama.cpp's own keys and
# tensor names are tens of bytes; the largest real VALUE strings are chat templates (~10-20 KB),
# which are skipped by seek, never read, so they get a looser bound than the names that are.
_GGUF_MAX_NAME = 64 * 1024
_GGUF_MAX_VALUE_STRING = 16 * 1024 * 1024
_GGUF_MAX_DEPTH = 8
_GGUF_MAX_COUNT = 1_000_000


class _Reader:
    """Header reads that can never run past the file: every length is checked against the
    bytes actually left before anything is read or skipped."""

    def __init__(self, handle: BinaryIO, size: int) -> None:
        self._h = handle
        self._size = size

    def _left(self) -> int:
        return self._size - self._h.tell()

    def take(self, n: int) -> bytes:
        if n < 0 or n > self._left():
            raise GgufError("truncated header")
        data = self._h.read(n)
        if len(data) != n:
            raise GgufError("truncated header")
        return data

    def u32(self) -> int:
        return int(struct.unpack("<I", self.take(4))[0])

    def u64(self) -> int:
        return int(struct.unpack("<Q", self.take(8))[0])

    def name(self) -> str:
        """A key or tensor name: read, so bounded tightly."""
        n = self.u64()
        if n > _GGUF_MAX_NAME:
            raise GgufError(f"a {n}-byte name is past the {_GGUF_MAX_NAME}-byte bound")
        return self.take(n).decode("utf-8", "replace")

    def skip(self, n: int) -> None:
        if n < 0 or n > self._left():
            raise GgufError("truncated header")
        self._h.seek(n, os.SEEK_CUR)

    def tell(self) -> int:
        return self._h.tell()

    def left(self) -> int:
        return self._left()


def _skip_value(r: _Reader, kind: int, depth: int = 0) -> None:
    if depth > _GGUF_MAX_DEPTH:
        raise GgufError(f"metadata arrays nested past depth {_GGUF_MAX_DEPTH}")
    if kind in _GGUF_FIXED:
        r.skip(_GGUF_FIXED[kind])
    elif kind == _GGUF_STRING:
        n = r.u64()
        if n > _GGUF_MAX_VALUE_STRING:
            raise GgufError(f"a {n}-byte string is past the {_GGUF_MAX_VALUE_STRING}-byte bound")
        r.skip(n)
    elif kind == _GGUF_ARRAY:
        inner, count = r.u32(), r.u64()
        if inner in _GGUF_FIXED:
            r.skip(_GGUF_FIXED[inner] * count)
        else:
            # Strings (the tokenizer vocab) or nested arrays: walked one by one, with seeks. A
            # count the remaining bytes could not hold (8 per string length, 12 per nested
            # array header) is refused before the loop rather than discovered at its end.
            if count * 8 > r.left():
                raise GgufError(f"an array of {count} elements cannot fit the file")
            for _ in range(count):
                _skip_value(r, inner, depth + 1)
    else:
        raise GgufError(f"unknown metadata type {kind}")


def _parse_tensor_ranges(path: str) -> tuple[tuple[str, int, int], ...]:
    file_size = os.path.getsize(path)
    with open(path, "rb") as handle:
        r = _Reader(handle, file_size)
        if r.take(4) != b"GGUF":
            raise GgufError("not a GGUF file")
        version = r.u32()
        if version not in (2, 3):
            raise GgufError(f"GGUF v{version} is not supported")
        n_tensors, n_kv = r.u64(), r.u64()
        if n_tensors > _GGUF_MAX_COUNT or n_kv > _GGUF_MAX_COUNT:
            raise GgufError(f"{n_tensors} tensors / {n_kv} keys is past the parser's bound")
        alignment = _GGUF_DEFAULT_ALIGNMENT
        for _ in range(n_kv):
            key, kind = r.name(), r.u32()
            if key == "general.alignment" and kind == _GGUF_UINT32:
                alignment = r.u32() or _GGUF_DEFAULT_ALIGNMENT
            else:
                _skip_value(r, kind)
        infos: list[tuple[str, int]] = []
        for _ in range(n_tensors):
            name = r.name()
            n_dims = r.u32()
            if n_dims > _GGUF_MAX_DEPTH:
                raise GgufError(f"tensor {name!r} claims {n_dims} dimensions")
            r.skip(8 * n_dims)
            r.skip(4)  # the ggml type
            infos.append((name, r.u64()))
        data_start = -(-r.tell() // alignment) * alignment
    # A tensor's extent is up to the next tensor's offset (or the end of the file): exact up to
    # alignment padding, and it needs no table of ggml block sizes that new quant types outrun.
    infos.sort(key=lambda t: t[1])
    out: list[tuple[str, int, int]] = []
    for i, (name, offset) in enumerate(infos):
        end = infos[i + 1][1] + data_start if i + 1 < len(infos) else file_size
        out.append((name, data_start + offset, end))
    return tuple(out)


class _KnownUnparseable(GgufError):
    """A shard already remembered as unparseable: refused again without a re-parse or a log."""


# Shards whose header failed to parse, by (path, size, mtime) -> the reason, so the in-load
# sweep (every couple of seconds) does not re-parse and re-log a bad shard each time. A
# re-downloaded shard has a new size or mtime and gets a fresh attempt.
_UNPARSEABLE: dict[tuple[str, int, int], str] = {}


@functools.lru_cache(maxsize=32)
def _tensor_ranges_cached(path: str, size: int, mtime_ns: int) -> tuple[tuple[str, int, int], ...]:
    del size, mtime_ns  # part of the cache key only: a re-downloaded shard re-parses
    return _parse_tensor_ranges(path)


def _tensor_ranges(path: str) -> tuple[tuple[str, int, int], ...]:
    """(tensor name, first byte, end byte) of every tensor in one GGUF shard, absolute file
    offsets, in file order. Header-only read (a few MB on a vocab-carrying first shard), cached
    by path+size+mtime since a shard does not change under a running engine — and a failure is
    remembered by the same key. Raises `GgufError` on anything it cannot read."""
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    if key in _UNPARSEABLE:
        raise _KnownUnparseable(_UNPARSEABLE[key])
    try:
        return _tensor_ranges_cached(*key)
    except (GgufError, struct.error, UnicodeError, OverflowError, MemoryError) as exc:
        if len(_UNPARSEABLE) > 256:
            _UNPARSEABLE.clear()
        _UNPARSEABLE[key] = str(exc) or type(exc).__name__
        raise GgufError(_UNPARSEABLE[key]) from exc


def _weight_files(root: str, suffixes: tuple[str, ...]) -> list[str]:
    """Every file under `root` ending in `suffixes`, skipping hf's `.cache` download staging
    (partial shards nothing has read into a model)."""
    out: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        if ".cache" in os.path.relpath(dirpath, root).split(os.sep):
            continue
        out.extend(os.path.join(dirpath, name) for name in files if name.endswith(suffixes))
    return out


# The weight formats ComfyUI reads. Same double-residency problem, different loader: a render
# streams tens of GB of diffusion weights off disk and `image_gen.render` frees the model
# straight afterwards, so EVERY render re-reads them cold and leaves another cache copy.
_IMAGE_WEIGHT_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".pth", ".sft", ".gguf")


def drop_image_model_page_cache(models_dir: str) -> float | None:
    """The `drop_weights_page_cache` twin for the on-box image models. Returns GiB dropped.

    A render reads ~58 GB of diffusion weights and then unloads the model on purpose
    (`image_gen.render._free_comfyui_model`) so the pool goes back to the LLMs — but the
    page-cache copy of those weights stays, and since the budget now counts page cache as
    used (`host_metrics.read_memory_gb`), that residue would read as a box with no room and
    block the end-of-turn LLM restore the render just made space for.

    Whole-tree rather than per-model: ComfyUI resolves its own checkpoints/VAE/text-encoders
    across the mount and we do not model which files a given render touched."""
    return _drop_page_cache(models_dir, _IMAGE_WEIGHT_SUFFIXES)


class _CachestatRange(ctypes.Structure):
    _fields_ = [("off", ctypes.c_uint64), ("len", ctypes.c_uint64)]


class _Cachestat(ctypes.Structure):
    _fields_ = [
        ("nr_cache", ctypes.c_uint64),
        ("nr_dirty", ctypes.c_uint64),
        ("nr_writeback", ctypes.c_uint64),
        ("nr_evicted", ctypes.c_uint64),
        ("nr_recently_evicted", ctypes.c_uint64),
    ]


# cachestat(2), Linux 6.5+. No glibc wrapper, so it goes through syscall(2) directly.
_SYS_CACHESTAT = {"x86_64": 451, "aarch64": 451}.get(platform.machine())


def _cached_pages(fd: int) -> int | None:
    """Pages of THIS file currently in the page cache, or None if unavailable.

    The reason this exists: `posix_fadvise` returns 0 unconditionally — the kernel
    discards the count from `invalidate_mapping_pages()` and reports success whether it
    evicted every page or none. This function was previously written to add
    `fstat().st_size` after that call, so its "GiB dropped" was the total size of every
    matching file, evicted or not. It could not distinguish a working drop from a total
    no-op, which is exactly the failure mode the drop is prone to (see the caveats in
    `drop_weights_page_cache`)."""
    if _SYS_CACHESTAT is None:
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    span, out = _CachestatRange(0, 0), _Cachestat()  # (0, 0) = the whole file
    if libc.syscall(_SYS_CACHESTAT, fd, ctypes.byref(span), ctypes.byref(out), 0) != 0:
        return None
    return int(out.nr_cache)


def _drop_page_cache(root: str, suffixes: tuple[str, ...]) -> float | None:
    """Drop clean page cache for every matching file under `root`, returning the GiB
    ACTUALLY released — measured, not assumed.

    Advisory throughout: the kernel skips folios that are dirty, under writeback, locked
    for in-flight I/O, or mapped by anyone. Weights are read-only so nothing can be lost,
    but a partial drop is normal and a total no-op is possible, which is why the return
    value is a measurement rather than a total of file sizes. On a kernel without
    `cachestat(2)` there is nothing to measure with, and the function reports None rather
    than inventing a number."""
    if not os.path.isdir(root):
        return None
    freed_pages = 0
    measured = False
    for dirpath, _dirs, files in os.walk(root):
        # hf's download staging holds partial shards nothing has read into a model.
        if ".cache" in os.path.relpath(dirpath, root).split(os.sep):
            continue
        for name in files:
            if not name.endswith(suffixes):
                continue
            path = os.path.join(dirpath, name)
            with contextlib.suppress(OSError, AttributeError):
                fd = os.open(path, os.O_RDONLY)
                try:
                    before = _cached_pages(fd)
                    # (0, 0) means "the whole file" to posix_fadvise.
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    after = _cached_pages(fd)
                    if before is not None and after is not None:
                        measured = True
                        freed_pages += max(0, before - after)
                finally:
                    os.close(fd)
    if not measured:
        return None
    # Two decimals, not one: at one decimal an 8 MiB drop and a total no-op both render
    # as 0.0, which reinstates exactly the ambiguity this function was rewritten to remove.
    return round(freed_pages * _PAGE_SIZE / _BYTES_PER_GIB, 2)
