"""Unit tests for the note integration_state lifecycle column (W0.2).

Pure (no DB): the column's shape and the model↔migration consistency. The
DB-level behavior (CHECK constraint, RLS via the notes table's domain_code) is
exercised by the integration suite in CI.
"""

import importlib.util
from pathlib import Path

from jbrain.models.notes import INTEGRATION_STATES, Note


def _load_migration():
    path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "0029_note_integration_state.py"
    )
    spec = importlib.util.spec_from_file_location("mig0029", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_states_are_the_expected_set():
    assert {
        "pending_integration",
        "integrating",
        "integrated",
        "stale",
        "skipped",
    } == INTEGRATION_STATES


def test_column_exists_and_defaults_to_pending_integration():
    col = Note.__table__.c.integration_state
    assert not col.nullable
    assert col.default.arg == "pending_integration"  # python-side default
    assert col.server_default is not None  # DB-side default for backfill
    # Pin the server-default VALUE so a future typo can't let the python and DB
    # defaults drift apart unnoticed.
    assert str(col.server_default.arg) == "pending_integration"


def test_migration_chains_from_head():
    mod = _load_migration()
    assert mod.revision == "0029"
    assert mod.down_revision == "0028"


def test_migration_check_states_match_model():
    # The migration's CHECK constraint and the model constant must not drift.
    mod = _load_migration()
    assert set(mod._STATES) == INTEGRATION_STATES
