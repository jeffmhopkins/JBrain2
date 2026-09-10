"""The fold logic in migration 0187 (archivist memory consolidation).

The migration repairs a box whose owner key was rotated: the archivist's notes stay in
`app.archivist_memory` under the revoked principal, unaddressed, while the persona reads
an empty memory. The SQL round-trip is covered by the integration test; this pins the
decision — what leads, what is stripped, and when it declines to touch anything.
"""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

BACKEND_ROOT = Path(__file__).resolve().parents[2]
_PATH = BACKEND_ROOT / "migrations" / "versions" / "0187_archivist_memory_consolidate.py"


def _module() -> ModuleType:
    """Load the migration by path — `migrations/versions` is not an importable package."""
    spec = importlib.util.spec_from_file_location("migration_0187", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = _module()
ACTIVE = "active-principal"
OLD = "revoked-principal"


def _at(day: int) -> datetime:
    return datetime(2026, 6, day, tzinfo=UTC)


def _clarifications(rule: str) -> str:
    return f"=== TRIAGE CLARIFICATIONS ===\n- {rule}\n=== END TRIAGE CLARIFICATIONS ==="


def test_nothing_to_fold_leaves_the_document_alone() -> None:
    rows = [(ACTIVE, "taxonomy: Finance/Chase", _at(27))]
    assert M._consolidate(ACTIVE, rows) is None


def test_blank_superseded_rows_are_not_folded() -> None:
    rows = [(ACTIVE, "taxonomy", _at(27)), (OLD, "   \n ", _at(20))]
    assert M._consolidate(ACTIVE, rows) is None


def test_current_document_leads_and_the_old_one_is_recovered() -> None:
    rows = [
        (ACTIVE, _clarifications("additionfinancial.com → high"), _at(27)),
        (OLD, "taxonomy: Finance/Chase", _at(20)),
    ]
    merged = M._consolidate(ACTIVE, rows)
    assert merged is not None
    assert merged.startswith("=== TRIAGE CLARIFICATIONS ===")
    assert "taxonomy: Finance/Chase" in merged
    assert "RECOVERED NOTES (saved 2026-06-20, under a previous owner key)" in merged


def test_a_stale_clarifications_section_is_stripped_from_recovered_notes() -> None:
    """The sweep reads the FIRST corrections section. A recovered one resurfacing as the
    live rule set is exactly the misfiling this whole area exists to prevent."""
    rows = [
        (ACTIVE, _clarifications("acme.com → high"), _at(27)),
        (OLD, "taxonomy\n" + _clarifications("acme.com → spam"), _at(20)),
    ]
    merged = M._consolidate(ACTIVE, rows)
    assert merged is not None
    assert merged.count("=== TRIAGE CLARIFICATIONS ===") == 1
    assert "acme.com → spam" not in merged
    assert "taxonomy" in merged


def test_superseded_documents_fold_newest_first() -> None:
    rows = [
        (ACTIVE, "current", _at(27)),
        ("older", "the older note", _at(10)),
        ("newer", "the newer note", _at(20)),
    ]
    merged = M._consolidate(ACTIVE, rows)
    assert merged is not None
    assert merged.index("the newer note") < merged.index("the older note")


def test_an_absent_current_document_is_replaced_by_the_newest_row() -> None:
    rows = [(OLD, "taxonomy: Finance/Chase", _at(20))]
    assert M._consolidate(ACTIVE, rows) == "taxonomy: Finance/Chase"


def test_folding_twice_does_not_duplicate() -> None:
    rows = [
        (ACTIVE, _clarifications("acme.com → high"), _at(27)),
        (OLD, "taxonomy: Finance/Chase", _at(20)),
    ]
    once = M._consolidate(ACTIVE, rows)
    assert once is not None
    twice = M._consolidate(
        ACTIVE, [(ACTIVE, once, _at(28)), (OLD, "taxonomy: Finance/Chase", _at(20))]
    )
    assert twice is None


def test_the_result_stays_under_the_write_tools_ceiling() -> None:
    """`archivist_memory_write` refuses content over 20k chars — a fold that blew past it
    would hand the agent a document it could never save back."""
    rows = [(ACTIVE, "current", _at(27))]
    rows += [(f"old-{i}", "x" * 9_000, _at(20 - i)) for i in range(3)]
    merged = M._consolidate(ACTIVE, rows)
    assert merged is not None
    assert len(merged) <= M._MAX_CHARS
    assert "1 older memory document(s) did not fit" in merged
