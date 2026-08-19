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
import os
import platform

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
    working set and the rest of the box's cache survive."""
    return _drop_page_cache(os.path.join(models_dir, model_id), (".gguf",))


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
