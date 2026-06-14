"""Unit test for the low_confidence_inference review-kind migration (A1b-ii-2).

Pure (no DB): the migration chains from head and the new kind is in the widened
allowlist. The CHECK behavior itself is exercised by the integration suite.
"""

import importlib.util
from pathlib import Path


def _load_migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0030_low_confidence_inference_review_kind.py"
    )
    spec = importlib.util.spec_from_file_location("mig0030", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_chains_from_0029():
    mod = _load_migration()
    assert mod.revision == "0030"
    assert mod.down_revision == "0029"


def test_new_kind_added_and_removed_across_up_down():
    mod = _load_migration()
    assert "low_confidence_inference" in mod._KINDS_WITH
    assert "low_confidence_inference" not in mod._KINDS_WITHOUT
    # The base set is preserved (e.g. an existing kind still admitted).
    assert "extraction_truncated" in mod._KINDS_WITHOUT
