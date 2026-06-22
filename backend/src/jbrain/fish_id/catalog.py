"""Curated catalog of self-hostable fish-identification models.

Single source of truth for the OPT-IN fishial feature (docs/FISH_ID_PLAN.md): it
names the models JBrain can run on-box through the `fish-id` compose profile (ROCm),
records the weight files scripts/fish-id-setup.sh must download and where the service
expects each, and carries the footprint estimate the shared RAM meter reserves while
a model is loaded (Wave F4/F5).

The models are MIT-licensed (fishial/fish-identification). Tuned for an AMD Strix
Halo box (gfx1151, large unified memory): the classifier loads on the iGPU and is
load → use → unload per call (freed after each identification), so its footprint is
~the load cost, time-shared with the LLM and the diffusion model.

Two consumers read this:
  - the app surfaces enabled models in settings (Wave F4/F5);
  - scripts/fish-id-setup.sh reads `python -m jbrain.fish_id.catalog <ids>`
    for the JSON download manifest.

Structurally wired but on-box-validated by the F0 spike: the `fishial-v2` entry's
weights/repo path await the spike's download+run, so it ships recommended once
confirmed.
"""

import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

# Service model subdirs each weight file must land in (relative to the models mount).
# The setup script places files by these names; the catalog is validated against this
# set so a typo can't write to a directory the service never reads.
MODEL_SUBDIRS: frozenset[str] = frozenset({"classifier", "detector", "segmenter", "database"})


@dataclass(frozen=True)
class FishFile:
    """One weight/data file and the service models subdir it belongs in.

    `repo_path` is the file's path inside the source repo/release; the setup script
    downloads it and places its basename under `dest_subdir`."""

    source: str  # HF repo or release tag the file comes from
    repo_path: str
    dest_subdir: str


@dataclass(frozen=True)
class FishModel:
    """One self-hostable fish-identification model and how to provision + drive it."""

    id: str  # stable settings-choice id
    label: str  # human label for the settings screen
    arch: str  # e.g. "DINOv2+ViT" — shown on the result card caption
    files: tuple[FishFile, ...]  # everything the service needs to run it
    size_gb: float  # total on-disk download
    # Resident unified-memory footprint ESTIMATE (not a measurement) the RAM meter
    # reserves while loaded; refined by the F0 spike's on-box measurement.
    footprint_gb: float
    species_count: int  # classes the embedding database covers (card caption)
    recommended: bool
    note: str = ""


# The fishial v2 release: the DINOv2+ViT classifier head + the per-species embedding
# database (nearest-neighbor over 866 classes), plus the detection/segmentation
# stages that crop the fish before embedding. Repo paths are the spike's to confirm.
_FISHIAL = "fishial/fish-identification"

_CLASSIFIER = FishFile(
    source=_FISHIAL, repo_path="models/classification_v2.ts", dest_subdir="classifier"
)
_DATABASE = FishFile(source=_FISHIAL, repo_path="models/embeddings_v2.pt", dest_subdir="database")
_DETECTOR = FishFile(source=_FISHIAL, repo_path="models/detector_v2.ts", dest_subdir="detector")
_SEGMENTER = FishFile(
    source=_FISHIAL, repo_path="models/segmentation_v2.ts", dest_subdir="segmenter"
)


# Order is the order the settings screen and setup prompt present them.
CATALOG: tuple[FishModel, ...] = (
    FishModel(
        id="fishial-v2",
        label="Fishial · classify (DINOv2+ViT)",
        arch="DINOv2+ViT",
        files=(_CLASSIFIER, _DATABASE, _DETECTOR, _SEGMENTER),
        size_gb=2.0,
        # A ViT classifier + embedding DB is small next to the diffusion/LLM weights;
        # the F0 spike replaces this estimate with the measured resident footprint.
        footprint_gb=4.0,
        species_count=866,
        recommended=True,
        note="MIT-licensed fishial models (segment → detect → embed → nearest of 866 "
        "species). Loaded per identification and freed right after. Repo file paths + "
        "the measured footprint await the F0 on-box spike.",
    ),
)

_BY_ID = {m.id: m for m in CATALOG}


def get(model_id: str) -> FishModel | None:
    return _BY_ID.get(model_id)


def recommended_ids() -> tuple[str, ...]:
    """The default-provisioned set the setup script offers when none are named."""
    return tuple(m.id for m in CATALOG if m.recommended)


def selected(ids: Sequence[str]) -> tuple[FishModel, ...]:
    """Catalog entries for the given ids, in catalog order; unknown ids dropped."""
    wanted = set(ids)
    return tuple(m for m in CATALOG if m.id in wanted)


def _manifest(ids: Sequence[str]) -> str:
    """JSON download manifest for the setup script (one object per model)."""
    models = selected(ids) if ids else CATALOG
    return json.dumps([asdict(m) for m in models], indent=2)


if __name__ == "__main__":  # scripts/fish-id-setup.sh reads this
    print(_manifest(sys.argv[1:]))
