"""The action registry validates the six shipped handlers as data and fails the
boot (ActionRegistryError) on any action/handler mismatch — the W0.1 boot gate that
moves an unknown-kind failure from job run time to startup (E3)."""

from __future__ import annotations

from typing import Any

import pytest

from jbrain.workflow.registry import (
    ACTION_SPECS,
    ActionRegistry,
    ActionRegistryError,
    ActionSpec,
    build_registry,
)


async def _noop(payload: dict[str, Any]) -> None:  # a stand-in handler
    return None


SHIPPED_KINDS = {
    "ingest_note",
    "embed_note",
    "integrate_note",
    "ocr_attachment",
    "consolidate_predicates",
    "sync_predicates",
}


def _handlers(names: set[str]) -> dict[str, Any]:
    return {name: _noop for name in names}


def test_shipped_registry_covers_the_six_handlers() -> None:
    registry = build_registry()
    assert registry.names() == SHIPPED_KINDS
    # Every shipped spec maps its name straight to the existing job kind, so the
    # dispatch table is identical to the old hardcoded dict.
    assert {spec.handler for spec in ACTION_SPECS} == SHIPPED_KINDS


def test_dispatch_table_matches_the_handlers_for_known_kinds() -> None:
    registry = build_registry()
    impls = _handlers(SHIPPED_KINDS)
    table = registry.dispatch_table(impls)
    assert set(table) == SHIPPED_KINDS
    for kind in SHIPPED_KINDS:
        assert table[kind] is impls[kind]


def test_validate_passes_when_actions_and_handlers_agree() -> None:
    build_registry().validate(_handlers(SHIPPED_KINDS))  # no raise


def test_boot_fails_when_an_action_has_no_handler() -> None:
    # The schema registry precedent: a missing handler is config drift caught at
    # boot, not a runtime job failure.
    missing = SHIPPED_KINDS - {"sync_predicates"}
    with pytest.raises(ActionRegistryError, match="actions without handlers.*sync_predicates"):
        build_registry().validate(_handlers(missing))


def test_boot_fails_when_a_handler_has_no_action() -> None:
    extra = SHIPPED_KINDS | {"rogue_handler"}
    with pytest.raises(ActionRegistryError, match="handlers without actions.*rogue_handler"):
        build_registry().validate(_handlers(extra))


def test_dispatch_table_enforces_validation_before_building() -> None:
    with pytest.raises(ActionRegistryError):
        build_registry().dispatch_table(_handlers(SHIPPED_KINDS - {"embed_note"}))


def test_duplicate_action_name_rejected() -> None:
    dup = ActionSpec(name="ingest_note", version=1, handler="ingest_note")
    with pytest.raises(ActionRegistryError, match="duplicate action name"):
        ActionRegistry([dup, dup])


def test_get_unknown_action_raises() -> None:
    with pytest.raises(ActionRegistryError, match="unknown action"):
        build_registry().get("does_not_exist")


def test_registry_basics() -> None:
    registry = build_registry()
    assert len(registry) == len(SHIPPED_KINDS)
    assert "ingest_note" in registry
    assert "nope" not in registry
    spec = registry.get("integrate_note")
    assert spec.version == 1
    assert spec.cost_class == "expensive"
    assert spec.mutating is True
    assert spec.dedup_key_expr == "note_id"
