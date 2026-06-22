"""The fish-id catalog: lookup, recommended set, selection, the setup-script
manifest, and the dest-subdir guard (a typo can't write where the service won't read)."""

import json

from jbrain.fish_id import catalog


def test_get_known_and_unknown() -> None:
    assert catalog.get("fishial-v2") is not None
    assert catalog.get("nope") is None


def test_recommended_ids_nonempty_and_known() -> None:
    ids = catalog.recommended_ids()
    assert ids
    assert all(catalog.get(i) is not None for i in ids)


def test_selected_keeps_catalog_order_and_drops_unknown() -> None:
    chosen = catalog.selected(["nope", "fishial-v2"])
    assert [m.id for m in chosen] == ["fishial-v2"]


def test_every_file_targets_a_known_subdir() -> None:
    for model in catalog.CATALOG:
        for f in model.files:
            assert f.dest_subdir in catalog.MODEL_SUBDIRS


def test_manifest_is_valid_json_with_models() -> None:
    manifest = catalog._manifest([])
    rows = json.loads(manifest)
    assert {r["id"] for r in rows} == {m.id for m in catalog.CATALOG}
    assert all("files" in r and "footprint_gb" in r for r in rows)
