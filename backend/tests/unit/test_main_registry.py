"""The API's action registry must carry every action a seeded manual trigger can
fire. `fire_trigger` resolves a pipeline's steps via `registry.get`, which raises
ActionRegistryError on a missing action — so an Ops-fireable sweep absent here makes
"Run now" fail and the Automations surface render it as "not in the action registry".

This guards the worker/API lockstep: an in-code action added to the worker's registry
(so the worker can dispatch it) but not the API's is exactly the regression that hid
`triage_inbox` from Ops.
"""

from jbrain.main import API_ACTION_SPECS
from jbrain.workflow.registry import build_registry


def test_api_registry_carries_every_ops_fireable_sweep() -> None:
    names = {spec.name for spec in API_ACTION_SPECS}
    # Each has a migration-seeded manual trigger (0048/0064/0066/0096), so each MUST
    # resolve in the API registry or its "Run now" raises.
    required = {
        "purge_deleted_artifacts",
        "reconcile_pending_notes",
        "reconcile_pending_integration",
        "reconcile_unembedded_notes",
        "geofence_sweep",
        "entity_hygiene",
        "reembed_stale",
        "tag_consolidate",
        "wiki_refresh",
        "wiki_rebuild",
        "wiki_reindex",
        "wiki_prune",
        "wiki_lint",
        "triage_inbox",
    }
    missing = required - names
    assert not missing, f"Ops-fireable actions missing from the API registry: {sorted(missing)}"


def test_api_action_specs_build_a_valid_registry() -> None:
    # No duplicate names; the registry constructor enforces it.
    registry = build_registry(API_ACTION_SPECS)
    assert "triage_inbox" in registry
    assert len(registry) == len(API_ACTION_SPECS)
