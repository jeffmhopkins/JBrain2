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

import os

# Weights are GiB-scale; report in GiB to match the catalog's size_gb units.
_BYTES_PER_GIB = 1024**3


def weights_size_gb(models_dir: str, model_id: str) -> float | None:
    """Summed size of the `*.gguf` weights (model shards + the vision projector)
    for one provisioned model, in GiB — or None when its directory is missing or
    unreadable. scripts/local-llm-setup.sh downloads each model into
    `<models_dir>/<model_id>/`, so the directory name is the catalog id."""
    total = 0
    found = False
    try:
        with os.scandir(os.path.join(models_dir, model_id)) as entries:
            for entry in entries:
                if entry.name.endswith(".gguf") and entry.is_file():
                    total += entry.stat().st_size
                    found = True
    except OSError:
        return None
    return round(total / _BYTES_PER_GIB, 1) if found else None


def dir_size_gb(models_dir: str, model_id: str) -> float | None:
    """Summed size of EVERY file in a model's directory, in GiB — partial
    `*.incomplete` shards included — or None when the directory is absent. Drives
    the PWA's live install-progress bar: unlike weights_size_gb (final `*.gguf`
    only), this climbs smoothly through an in-flight huggingface download, so
    `dir_size_gb / catalog size_gb` is a real percentage mid-provision. Returns
    0.0 for an empty (just-created) directory so a started download reads as 0%,
    not 'not started'."""
    total = 0
    try:
        with os.scandir(os.path.join(models_dir, model_id)) as entries:
            for entry in entries:
                if entry.is_file():
                    total += entry.stat().st_size
    except OSError:
        return None
    return round(total / _BYTES_PER_GIB, 1)
