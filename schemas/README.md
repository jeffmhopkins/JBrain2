# schemas/ — the entity schema registry

Declarative shape of each entity kind: the facets it carries, the canonical
spelling of its core predicates, how its display name projects, and which
predicates seed aliases. Binding rationale and the full model live in
[`docs/entity.md`](../docs/entity.md). **Status: [proposed] — awaiting owner
ratification; not yet wired into the pipeline.**

```
_meta.yaml      schema_version, the fact-kind enum, value_shapes, shared shapes
facets.yaml     the reusable mixin library (Named, Temporal, Monetary, …)
types/*.yaml    per-type definitions composing facets + type-specific predicates
```

## The one invariant

> Storage accepts any predicate. Shape validation may reject a malformed
> `value_json`; predicate-name validation may never reject anything.

The registry is **soft**: it supplies (a) preferred predicate spellings for the
`note.extract` prompt digest and (b) `renamed_from` targets that nightly
consolidation normalizes drift toward. It is never a storage gate — that would
resurrect the controlled ontology `docs/ANALYSIS.md` rejects.

## Runtime

`backend/src/jbrain/schema/` loads these files into an in-process
`SchemaRegistry` (`load_registry()`), validating them at load (unknown
facet/kind/value_shape, cross-facet collisions, unresolved refs/vocabs/shapes,
enum-without-values all fail fast). It exposes the four consumers — `prompt_digest`,
`render_config`, `resolution_config`, `validate_value`. Not yet wired into the
pipeline. Tests: `backend/tests/unit/test_schema_registry.py`.

## Reading order

1. `_meta.yaml` — the value vocabulary every type draws on.
2. `facets.yaml` — the composable property bundles.
3. `types/person.yaml` — the richest example (structured names + projection).
4. `types/role.yaml` — reified relationship edges (employment, ownership, …).
5. `types/bill.yaml` / `appointment.yaml` — recurrence-as-token, not rows.
6. `types/lab_result.yaml` — a deferred Phase-7 typed record's catalog.

All fourteen catalog types are scaffolded: `person, organization, place, role,
animal, appointment, bill, lab_result, vehicle, medication, financial_account,
document, subscription, device`.
