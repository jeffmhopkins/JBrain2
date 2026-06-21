"""Real on-disk footprint of provisioned image-model weights.

The image sibling of jbrain.llm.local_weights: report each model's measured size
from the files an operator actually downloaded, rather than the catalog estimate.
These are host/infra files on the read-only weights mount — not application blobs
— so (like host_metrics' /proc read) this sits OUTSIDE the storage abstraction.

Best-effort: returns None when nothing for the model is on this box, and the
caller falls back to the catalog's nominal size. Files shared between models (the
text encoder + VAE) are counted toward each model's own footprint — this is "what
this model needs on disk", not a deduplicated total.
"""

from __future__ import annotations

import os

from jbrain.image_gen.catalog import ImageModel

# Weights are GiB-scale; report in GiB to match the catalog's size_gb units.
_BYTES_PER_GIB = 1024**3


def weights_size_gb(models_dir: str, model: ImageModel) -> float | None:
    """Summed size of `model`'s present weight files, in GiB — or None when none of
    them are on this box. scripts/comfyui-setup.sh places each file at
    `<models_dir>/<dest_subdir>/<basename>`."""
    total = 0
    found = False
    for f in model.files:
        path = os.path.join(models_dir, f.dest_subdir, os.path.basename(f.repo_path))
        try:
            total += os.stat(path).st_size
            found = True
        except OSError:
            continue
    return round(total / _BYTES_PER_GIB, 1) if found else None
