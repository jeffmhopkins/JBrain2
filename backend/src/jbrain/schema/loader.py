"""Parse and validate the entity-schema YAML into a `SchemaRegistry`.

Mirrors `jbrain.llm.promptfile`: load-time validation so a malformed registry
fails fast (SchemaError) rather than mid-pipeline. The definitions are the YAML
authoring surface (docs/entity.md decision 1), co-located in `defs/` so they
ship in the wheel like the `.prompt` files; `default_defs_dir()` finds them
relative to this module and tests may pass an explicit dir.
"""

from __future__ import annotations

from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from jbrain.schema.models import (
    EntityType,
    Facet,
    Meta,
    Predicate,
    SchemaError,
    SchemaRegistry,
    _norm_key,
)


def default_defs_dir() -> Path:
    """The `defs/` directory co-located in this package (ships in the wheel,
    same pattern as the `.prompt` files). Raises if it is missing."""
    defs = Path(__file__).parent / "defs"
    if not defs.is_dir():
        raise SchemaError(f"schema defs dir not found at {defs}")
    return defs


@lru_cache(maxsize=1)
def get_registry() -> SchemaRegistry:
    """The process-wide registry, loaded once from the packaged defs. Mirrors
    the prompt loader: a malformed registry fails on first use, never silently."""
    return load_registry()


def load_registry(defs_dir: Path | None = None) -> SchemaRegistry:
    """Load `_meta.yaml`, `facets.yaml`, and every `types/*.yaml` into a
    validated registry. SchemaError on any malformed or unresolved definition."""
    root = defs_dir or default_defs_dir()
    meta = _load_meta(root / "_meta.yaml")
    facets = _load_facets(root / "facets.yaml", meta)
    types = _load_types(root / "types", meta, facets)
    normalization = _build_normalization(facets, types)
    return SchemaRegistry(meta=meta, facets=facets, types=types, normalization=normalization)


def _build_normalization(facets: dict[str, Facet], types: dict[str, EntityType]) -> dict[str, str]:
    """The `renamed_from` attractor: every declared drift spelling maps to its
    canonical predicate. A spelling claimed by two different canonicals is an
    authoring bug and fails the load."""
    mapping: dict[str, str] = {}
    sources: list[Predicate] = [p for f in facets.values() for p in f.predicates]
    sources += [p for t in types.values() for p in t.own_predicates]
    for pred in sources:
        for alias in pred.renamed_from:
            key = _norm_key(alias)
            prior = mapping.get(key)
            if prior is not None and prior != pred.canonical_name:
                raise SchemaError(
                    f"renamed_from {alias!r} maps to both {prior!r} and {pred.canonical_name!r}"
                )
            mapping[key] = pred.canonical_name
    return mapping


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SchemaError(f"missing schema file {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SchemaError(f"{path}: top level must be a mapping")
    return data


def _str_list(raw: Any, *, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise SchemaError(f"{where}: expected a list of strings")
    return tuple(raw)


def _load_meta(path: Path) -> Meta:
    data = _read_yaml(path)
    fact_kinds = frozenset(_str_list(data.get("fact_kinds"), where=f"{path} fact_kinds"))
    value_shapes = frozenset(_str_list(data.get("value_shapes"), where=f"{path} value_shapes"))
    if not fact_kinds or not value_shapes:
        raise SchemaError(f"{path}: fact_kinds and value_shapes are required")

    shapes_raw = data.get("shapes") or {}
    if not isinstance(shapes_raw, dict):
        raise SchemaError(f"{path}: shapes must be a mapping")
    shapes = {str(name): dict(fields) for name, fields in shapes_raw.items()}

    # Every other top-level list[str] (name_audiences, id_scheme, …) is a vocab
    # a predicate's `qualifier_vocab` may reference.
    reserved = {"schema_version", "fact_kinds", "value_shapes", "shapes"}
    vocabs = {
        key: tuple(val)
        for key, val in data.items()
        if key not in reserved and isinstance(val, list) and all(isinstance(x, str) for x in val)
    }
    return Meta(
        schema_version=int(data.get("schema_version", 0)),
        fact_kinds=fact_kinds,
        value_shapes=value_shapes,
        shapes=shapes,
        vocabs=vocabs,
    )


def _predicate(raw: Any, meta: Meta, *, where: str, default_kind: str | None) -> Predicate:
    if not isinstance(raw, dict):
        raise SchemaError(f"{where}: predicate must be a mapping")
    name = raw.get("canonical_name")
    if not name:
        raise SchemaError(f"{where}: predicate missing canonical_name")
    shape = raw.get("value_shape")
    if shape not in meta.value_shapes:
        raise SchemaError(f"{where} {name}: unknown value_shape {shape!r}")

    kind = raw.get("kind") or default_kind
    if kind is None:
        raise SchemaError(f"{where} {name}: missing kind (no default available)")
    if kind not in meta.fact_kinds:
        raise SchemaError(f"{where} {name}: unknown kind {kind!r}")

    qvocab = raw.get("qualifier_vocab")
    if qvocab is not None and qvocab not in meta.vocabs:
        raise SchemaError(f"{where} {name}: unknown qualifier_vocab {qvocab!r}")

    pshape = raw.get("shape")
    if pshape is not None and pshape not in meta.shapes:
        raise SchemaError(f"{where} {name}: unknown shape {pshape!r}")

    return Predicate(
        canonical_name=str(name),
        value_shape=str(shape),
        kind=str(kind),
        functional=bool(raw.get("functional", False)),
        qualifier_vocab=qvocab,
        enum_values=_str_list(raw.get("enum_values"), where=f"{where} {name} enum_values"),
        range_type=raw.get("range_type"),
        shape=pshape,
        renamed_from=_str_list(raw.get("renamed_from"), where=f"{where} {name} renamed_from"),
        schema_org_ref=raw.get("schema_org_ref"),
        advisory_required=bool(raw.get("advisory_required", False)),
        description=str(raw.get("description", "")),
    )


def _load_facets(path: Path, meta: Meta) -> dict[str, Facet]:
    data = _read_yaml(path)
    raw_facets = data.get("facets") or {}
    if not isinstance(raw_facets, dict):
        raise SchemaError(f"{path}: facets must be a mapping")
    facets: dict[str, Facet] = {}
    for fname, body in raw_facets.items():
        preds = tuple(
            # Facet predicates must name their own kind — there is no type default here.
            _predicate(p, meta, where=f"facet {fname}", default_kind=None)
            for p in (body.get("predicates") or [])
        )
        facets[str(fname)] = Facet(
            name=str(fname),
            description=str(body.get("description", "")),
            predicates=preds,
        )
    return facets


def _load_types(types_dir: Path, meta: Meta, facets: dict[str, Facet]) -> dict[str, EntityType]:
    if not types_dir.is_dir():
        raise SchemaError(f"missing types dir {types_dir}")

    raws: dict[str, dict[str, Any]] = {}
    for path in sorted(types_dir.glob("*.yaml")):
        data = _read_yaml(path)
        tid = data.get("id")
        if not tid:
            raise SchemaError(f"{path}: missing id")
        if tid in raws:
            raise SchemaError(f"duplicate type id {tid!r} ({path})")
        data["__path__"] = str(path)
        raws[str(tid)] = data

    type_ids = set(raws)
    built: dict[str, EntityType] = {}

    def build(tid: str, stack: tuple[str, ...]) -> EntityType:
        if tid in built:
            return built[tid]
        if tid in stack:
            raise SchemaError(f"extends cycle: {' -> '.join((*stack, tid))}")
        data = raws[tid]
        where = data["__path__"]

        for fname in data.get("facets") or []:
            if fname not in facets:
                raise SchemaError(f"{where}: unknown facet {fname!r}")
        extends = data.get("extends")
        if extends is not None and extends not in type_ids:
            raise SchemaError(f"{where}: unknown extends {extends!r}")
        default_kind = data.get("default_fact_kind")
        if default_kind is not None and default_kind not in meta.fact_kinds:
            raise SchemaError(f"{where}: unknown default_fact_kind {default_kind!r}")

        own = tuple(
            _predicate(p, meta, where=where, default_kind=default_kind)
            for p in (data.get("predicates") or [])
        )
        parent_eff = build(str(extends), (*stack, tid)).effective_predicates if extends else ()
        status_values = _str_list(data.get("status_values"), where=f"{where} status_values")
        effective = _roll_down(
            tid, where, facets, parent_eff, data.get("facets") or [], own, status_values
        )

        _validate_effective(tid, where, effective, type_ids, meta)
        alias_seed = _str_list(
            data.get("alias_seeding_predicates"), where=f"{where} alias_seeding_predicates"
        )
        eff_names = {p.canonical_name for p in effective}
        for seed in alias_seed:
            if seed not in eff_names:
                raise SchemaError(f"{where}: alias_seeding predicate {seed!r} is not a property")
        display = _str_list(data.get("display_name"), where=f"{where} display_name")
        if not display:
            raise SchemaError(f"{where}: display_name is required")

        et = EntityType(
            id=tid,
            name=str(data.get("name", tid)),
            vehicle=str(data.get("vehicle", "graph")),
            default_fact_kind=str(default_kind or "attribute"),
            allow_open_predicates=bool(data.get("allow_open_predicates", True)),
            facets=tuple(str(f) for f in (data.get("facets") or [])),
            extends=str(extends) if extends else None,
            own_predicates=own,
            effective_predicates=effective,
            alias_seeding_predicates=alias_seed,
            display_name=display,
            schema_org_ref=data.get("schema_org_ref"),
            description=str(data.get("description", "")),
        )
        built[tid] = et
        return et

    for tid in raws:
        build(tid, ())
    return built


def _roll_down(
    tid: str,
    where: str,
    facets: dict[str, Facet],
    parent_eff: tuple[Predicate, ...],
    own_facets: list[Any],
    own_preds: tuple[Predicate, ...],
    status_values: tuple[str, ...],
) -> tuple[Predicate, ...]:
    """type = parent + facets + own. A cross-facet collision on the same name
    with a different shape/kind/functional fails loudly (red-team); own
    predicates override silently (intended specialization)."""
    merged: dict[str, tuple[Predicate, str]] = {
        p.canonical_name: (p, "extends") for p in parent_eff
    }
    for fname in own_facets:
        for p in facets[str(fname)].predicates:
            existing = merged.get(p.canonical_name)
            if existing is not None:
                prev, src = existing
                if (prev.value_shape, prev.kind, prev.functional) != (
                    p.value_shape,
                    p.kind,
                    p.functional,
                ):
                    raise SchemaError(
                        f"{where}: predicate {p.canonical_name!r} conflicts "
                        f"between {src} and facet {fname}"
                    )
            else:
                merged[p.canonical_name] = (p, f"facet {fname}")
    for p in own_preds:
        merged[p.canonical_name] = (p, "own")

    effective = {name: pred for name, (pred, _src) in merged.items()}
    if status_values and "status" in effective:
        # The Lifecycle facet declares `status` shapeless; the type fills its enum.
        effective["status"] = replace(effective["status"], enum_values=status_values)
    return tuple(effective.values())


def _validate_effective(
    tid: str,
    where: str,
    effective: tuple[Predicate, ...],
    type_ids: set[str],
    meta: Meta,
) -> None:
    for p in effective:
        if p.value_shape == "enum" and not p.enum_values:
            raise SchemaError(f"{where}: enum predicate {p.canonical_name!r} has no enum_values")
        if p.value_shape == "ref" and p.range_type is not None and p.range_type not in type_ids:
            raise SchemaError(
                f"{where}: predicate {p.canonical_name!r} ref to unknown type {p.range_type!r}"
            )
