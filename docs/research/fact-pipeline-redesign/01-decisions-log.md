# Decisions log — binding constraints added during the design effort

Decisions the user has made mid-process. These are BINDING on all subsequent
synthesis, revision, and red-team work (they override anything in 00-framing.md
or a spec revision that contradicts them).

## D1 — A complete DB reset is acceptable (no in-place legacy migration)

The user does not need the existing fact corpus migrated in place. The cutover to
the new model is a **clean rebuild**: drop the derived graph (facts / entities /
review / op-log) and **re-ingest all retained notes from scratch** under the new
contract. Notes remain the sources of truth and are kept; only the derived layer
is rebuilt.

**Implications:**
- The one-time "existing-corpus migration mapping" (Round-1 migration finding M7:
  recovering cardinality intent, `value_identity`, and the `valid_to=NULL` bound
  ambiguity from today's rows) is **OUT OF SCOPE** — there is nothing to migrate;
  the new contract produces these fields natively on re-ingest.
- This **removes** a class of risk (lossy legacy mapping) and lets the storage /
  contract design be fully greenfield with no back-compat to today's `facts` table.
- **Still in scope:** FUTURE contract-version re-analysis migration AFTER launch —
  i.e. when a later contract bump re-analyzes notes, human edits (the op overlay)
  and pinned/human-touched facts must survive (Round-1 migration M2/M3). The
  human-op overlay and pinned-protection design remain required; only the *initial*
  legacy cutover is a clean wipe.
- Disposition: reclassify M7 from "must fix" to "out of scope (clean rebuild, D1)";
  keep M2/M3 in scope.

## D2 — Every frontend GUI gets 3 interactive HTML mockups first (owner chooses)

Any frontend/GUI to be built (the review card, entity views, any new screen) MUST first ship
**three interactive HTML mockups** under `docs/mocks/` for the owner to choose the design,
**before** any GUI implementation begins. The owner picks one (or directs a blend); only then
is the chosen design implemented.

**Implications:**
- Applies to **all** GUI work, not just this redesign — a standing process rule (the same
  pattern already used for the review/entity mockups in this session).
- A GUI implementation PR is **blocked** until its mockups exist and a choice is recorded.
- Mockups are interactive (toggle states, open sheets, switch approaches), match the design
  tokens, and present **three genuinely distinct** directions — not three trivial variants.
- Backend/contract/storage work (the bulk of this spec) is unaffected; this gates only the
  GUI layer.

## D3 — Incremental evolution, NOT a greenfield rebuild (supersedes D1's "clean rebuild")

After comparing the spec to the shipped system (`50-comparison-to-current.md`), the current
implementation already provides ~75–80% of the spec (bitemporal facts, modality column,
supersession history, registry + canonicalization + value-shape typing, functional-vs-set
accumulation = override-vs-array already solved at storage, entity merge/distinct/mentions,
pinned, RLS firewalls, review inbox + #7 correction notes, the per-field editing + value
recovery shipped this session). The plan is therefore:

- **Posture: incremental evolution.** Keep the existing architecture; apply the spec's genuine
  wins as targeted online migrations + small CI-green PRs. Do NOT rebuild greenfield.
- **Adopt:** (1) modality in the selection key + `current()`=asserted-only (negation safety);
  (2) structured-editing review (collapse the kind-zoo; every field editable; explicit
  add/replace/remove) — GUI gated by D2; (3) **arbitrary-order undo** via a typed-op + audit
  layer over the existing append-mostly history (selective replay); (4) stable `value_identity`
  for scalar set members (small).
- **Drop / shelf (revisit on a trigger):** per-domain entity projections — keep global tables +
  RLS (revisit only for multi-user / untrusted-agent isolation); two-stage extraction — keep
  single-stage + the deterministic backstops (revisit only if eval shows a grounding gap).
- **D1 re-scoped:** "complete DB reset" = **re-ingest notes** under the improved pipeline, NOT
  an architecture wipe. Most upgrades are online schema migrations on the existing tables.
- **Preserve shipped nuance:** `derived_from_fact_id` (materialized reciprocal edges),
  `subject_id` security separation, `is_schedule_binding`/inverse-predicate handling, the
  residual functional allowlist — must survive any change.

Sequence + PR slices: see `60-incremental-plan.md`. The greenfield spec (`40-final-spec.md`)
remains the design reference for the adopted mechanisms; its §3.5/§6 projection model and §5
two-stage extraction are shelved per this decision.

## D4 — Per-wave execution loop (binding for every implementation wave)

Each wave in `60-incremental-plan.md` runs this loop, in order:
1. **Independent design red-team** — adversarially review the wave's *approach* against the actual
   code BEFORE writing it (edge cases, interaction with supersession/pinned/derived/RLS, migration
   safety). Surface Sev-1/2; revise the wave's approach until clean.
2. **Implement** — the revised approach, on a wave branch.
3. **Red-team the implementation** — adversarially review the *diff* (correctness, firewall,
   regressions, the things the design red-team flagged); fix findings.
4. **Test** — tests in the same change (80% backend gate, security paths 100%, real Postgres via
   testcontainers, LLM faked); CI green.
5. **PR** — open it; merge on green per the usual flow.

Red-team passes prefer an independent agent for genuine independence; given background-agent
unreliability in this environment, a stalled agent is stopped and the pass is done inline rather
than blocking. GUI waves additionally honor D2 (3 mockups before any GUI implementation).

