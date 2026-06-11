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

## Reading order

1. `_meta.yaml` — the value vocabulary every type draws on.
2. `facets.yaml` — the composable property bundles.
3. `types/person.yaml` — the richest example (structured names + projection).
4. `types/role.yaml` — reified relationship edges (employment, ownership, …).
5. `types/bill.yaml` / `appointment.yaml` — recurrence-as-token, not rows.
6. `types/lab_result.yaml` — a deferred Phase-7 typed record's catalog.

Not yet present (cataloged in `docs/entity.md`, scaffold on demand): `place`,
`financial_account`, `document`, `subscription`, `device`.
