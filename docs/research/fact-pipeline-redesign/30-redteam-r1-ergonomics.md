# Red-team R1 — Over-engineering & Ergonomics

**Lens:** OVER-ENGINEERING & ERGONOMICS. Position: the design is, in several places,
heavier than a single-user personal knowledge system can carry or use. The spine is
sound; the *surface area* is not. This log argues where simpler wins, what the simpler
thing costs, and which complexity is essential vs accidental.

**Inputs read:** `00-framing.md` (§5 success, §7 lenses), `20-spec-v0.md` (target),
briefs A (fact IR), C (corrections), E (review). Did NOT read `backend/src`.

**Severity key:** SEV-1 = would sink adoption or long-term maintainability ·
SEV-2 = real drag, fixable without re-architecting · SEV-3 = nit.

Findings are ordered by severity. Each carries: the attack, the simpler alternative,
the tradeoff, and an essential-vs-accidental call.

---

## F1 — The "fewer kinds" success criterion is NOT met; complexity was relocated, not removed (SEV-1)

**Attack.** Framing §5 makes "fewer bespoke review kinds / one-off override prompts" a
*gating* success criterion. The spec claims victory: "the kind-zoo collapses to one
parameterized card" (E §4, spec §7 closing). This is an accounting trick. Count the
total *distinct things a builder must implement and a maintainer must reason about*,
not the number of card React components:

- **1 card shell**, but it is parameterized by `(kind ∈ 6, value_shape ∈ 7,
  cardinality ∈ 2, reason ∈ ~7)`. That is a `6×7×2` sub-editor cross-product gated by
  conditional rendering. E itself names the risk: "an over-configurable god component"
  (E §6.1). The zoo did not disappear; it moved inside one file as branching.
- **~22 correction ops** (C §2.3), each with a mutation, an audit shape, and a
  *precomputed inverse*. That is ~22 code paths × undo paths × RLS-revalidation paths.
- **7 TypedValue variants** (A §2.3), each needing a producer, a deterministic
  re-deriving parser (D B2), a validator, and an editor widget.
- **2 asymmetric review contracts** (fat-read + thin-write, E §2) that "duplicate
  value-shape definitions" and must be kept in sync by codegen (E risk 7).
- **~13 op_kind values** in the storage `fact_op` enum (spec §3.2) that must *map* to
  C's 22 ops and E's submission op names — a third naming surface the spec itself
  flags ("hold E's submission JSON and C's op schema side by side," spec §4.3).

Old design: N bespoke cards, each trivially understandable in isolation, each with one
override. New design: one card + 22 ops + 7 value types + 3 op-naming surfaces +
codegen. **The cyclomatic complexity went UP; only the component *count* went down.**
"Fewer kinds" measured the wrong noun. A god-component with a 6×7×2 conditional matrix
is harder to test, harder to onboard into, and harder to change safely than five flat
cards — precisely the maintainability failure the criterion was meant to prevent.

**Simpler alternative.** Re-baseline the success metric on *total decision points a
maintainer touches to add a capability*, then attack that number directly:
1. Collapse the three op-naming surfaces (C ops / E submission ops / storage op_kind)
   into **one shared enum generated from one schema** — non-negotiable, not codegen-as-
   mitigation-footnote. If E and C cannot share *literally one* enum, the "one algebra"
   claim is false.
2. Ship the card as **value_shape editors only** (7 small, independently testable
   widgets) over a *dumb* shell that does NOT branch on `kind` or `reason`. `kind` and
   `reason` become display hints (a chip, a tint), never structural forks. That kills
   the 6× and ~7× multipliers, leaving a 7×2 surface.

**Tradeoff.** You lose `kind`-driven affordances (e.g. measurement auto-forcing
quantity+instant). Cost: the human can momentarily put a text value on a measurement
predicate — but the deterministic value_shape gate (registry) already rejects that at
commit, so the *structural* fork buys nothing the validator doesn't already provide. Net:
pure simplification.

**Essential vs accidental.** The `value_shape × cardinality` matrix (7×2) is
**essential** — those genuinely need different editors and different ops. The
`kind`-fork and `reason`-fork and the triple op-naming are **accidental**. The
success criterion as written (count components) is itself accidental complexity smuggled
into the gate.

---

## F2 — The op-log / audit / precomputed-inverse machinery is over-built for a single-user system (SEV-1)

**Attack.** The spec mandates, for *every* mutation by *any* actor: an append to a typed
`fact_op` log, a transactional immutable `fact_audit` row with before/after snapshots,
**and a precomputed stored inverse op** (spine #2, C §2.5, spec §3.2). This is full
event-sourcing-lite. For a *single human owner* correcting *their own* notes, this is a
bank-grade audit trail bolted onto a personal wiki. Concretely the cost:

- **Every** op type must define and maintain a *correct* inverse — and compound ops
  (`mint_and_link_object`, `merge_facts`, `split_fact`) have *compound* inverses with
  ref-count preconditions (C §2.3). C's own open-Q 8 admits undo-composition of
  structure+identity ops "may need an explicit dependency graph." That is an unbounded
  rabbit hole: inverse correctness is a *proof obligation per op*, and 22 of them.
- The inverse is stored, then "re-validated, not blindly applied, on undo" across schema
  versions (C risk 6). So you maintain a migration ladder *for the inverses too*.
- Spine #2 says the op-log "*is* the change feed, audit trail, and undo stack" — but C
  §5 then quietly retreats to "graph is live store, op-log is authoritative history, we
  don't *require* replay." So it is **not** actually event-sourced; it is a parallel
  bookkeeping system that the spec elsewhere claims is the single source of truth. That
  is the worst of both: replay-shaped obligations (stored inverses, append-only audit,
  schema-versioned ops) without the replay guarantee that would justify them.

The honest question framing §7 poses — "is the op-log/audit/inverse machinery worth it
for a single-user personal system?" — is answered by the spec with "yes, uniformly,"
and that is wrong. The owner who fat-fingers a relink wants Ctrl-Z, not a forensic
inverse-op ledger with a migration ladder.

**Simpler alternative.** **Snapshot-based undo, not inverse-op undo.** Keep the
append-only `fact_assertion` history you already have (storage is append-only and
bitemporal *anyway* — spine, B). Undo = "tombstone the assertions written by op X and
un-tombstone the ones X superseded," read straight off `created_by_op` + `supersedes`
columns that already exist in the schema (spec §3.1). No precomputed inverse, no
inverse-migration-ladder, no per-op inverse proof. The audit row keeps before/after for
*display*; it does not need to carry an executable inverse, because the assertion
history already contains the prior state.

**Tradeoff.** You lose "replay from genesis" (which C §5 already says you don't need)
and "what will undo do" previews computed from the stored inverse (recomputable from the
snapshot diff on demand). You keep full audit + reversibility — the binding invariant —
because append-only assertions + op attribution already deliver it. The ~22 inverse
definitions and their migration ladder evaporate.

**Essential vs accidental.** Append-only assertion history + op attribution +
domain-scoped op log = **essential** (audit invariant, RLS). The *precomputed,
stored, schema-versioned, per-op inverse* is **accidental** — it re-derives what the
append-only store already holds. This is the single highest-value simplification in this
log: it removes ~22 proof obligations and a whole migration ladder while keeping every
binding invariant.

---

## F3 — The single fat `FactClaim` envelope is a false economy vs honest stage shapes (SEV-2)

**Attack.** A §1 sells "one shape everywhere; stages differ only by which optional
sub-objects are populated and by `resolution`." The spec then *immediately contradicts
the premise* in two places:
- Storage: "the persisted `fact_assertion` row is *narrower and stricter* than this
  envelope" (spec §2.1 note, A's claim "deliberately diverged at the storage boundary,"
  spec §7(b)). So it is **not** one shape at the boundary that matters most.
- Validation: required-fields are keyed off `resolution` (spec §2.1) — i.e. the envelope
  has *four different validity contracts* (`mention`/`resolved`/`held`/`committed`)
  enforced by mode-switching validators. That is four shapes wearing one trench coat.

A fat optional-everything envelope means **every consumer must defensively null-check
fields that "shouldn't" be populated yet** (`entity_id` MUST be null at `mention`;
`canonical` null at extraction; `value_identity` null until set-member). The type system
cannot help — every field is optional, so the compiler permits the illegal states the
prose forbids. The "one shape" saves writing 2-3 mapping functions at the cost of pushing
a runtime state-machine invariant ("which fields are legal at which `resolution`") into
*every* reader. For a 3-stage pipeline, that is a bad trade.

**Simpler alternative.** Two honest types with one explicit mapping:
`ExtractedClaim` (mention refs, no ids, no canonical, no slot identity) →
`ResolvedFact` (ids filled, canonical, slot key). The review payload is a *projection of
`ResolvedFact`* (E already builds a separate fat-read projection anyway). The storage row
stays its own strict shape (the spec already concedes this). Each transition is one total
function the compiler checks — illegal-state-unrepresentable instead of
illegal-state-prose-forbidden.

**Tradeoff.** You write ~2 mapping functions and lose the "claim_id stable across
enrichment" convenience (recover it by carrying the ULID as a field, not a shared
identity). A's worry — "multiplies contracts, re-litigates version/migrate three times"
— is overstated: the spec *already* maintains a separate storage shape and a separate
review projection, so you are at 3 shapes regardless; honest types just *name* them and
make the compiler enforce the transitions.

**Essential vs accidental.** A typed value (not-a-sentence) and a stable claim id are
**essential**. The "single monotone envelope" framing is **accidental** — it is asserted
as a simplification but the spec's own concessions (§2.1 note, §7(b)) show three shapes
already exist; the envelope just hides two of them behind optionality.

---

## F4 — The ~22-op algebra is neither minimal nor demonstrably closed; ~12 ops cover the wishlist (SEV-2)

**Attack.** C claims the 22 ops form a "closed algebra" but never proves closure (no
demonstration that every wishlist edit decomposes into the set, nor that the set is
irredundant). It is asserted ("a fixed, closed set of ~22"). Several ops are ceremony or
sugar:
- `replace_head` is defined as "sugar for supersede *this* member" (C §2.3) — sugar is by
  definition not minimal.
- `unmerge_entities`, `unretract`, `unpin`, `split_entity`-as-inverse-of-merge: half the
  algebra exists to be *inverses of the other half*. If undo is snapshot-based (F2), the
  explicit inverse ops are not needed as *first-class human ops* — the human merges; undo
  un-merges via history. That is ~5-6 ops gone.
- `supersede` (functional) and `replace_head` (set) and `set_field value` (functional)
  are three spellings of "the value is now X, keep history." Cardinality already routes
  this deterministically (spine #3); it does not need three op names.
- `set_field` is *already* a super-op merging 6 fields, which the spec then partially
  un-merges in §7(g) (pulling `domain` back out). So the algebra is simultaneously
  too-merged (firewall risk, F6 below) and too-split (redundant inverses).

22 ops to express ~15 wishlist items is a poor ratio for a closed algebra; closure was
*assumed*, minimality was *not pursued*.

**Simpler alternative.** Target ~12 ops:
`set_field` (intra-domain fields), `move_domain` (gated, per §7(g)), `retime`,
`relink` (subject|object, discriminated), `mint_and_link`, `add_to_set`,
`remove_from_set`, `replace_member` (subsumes `replace_head`+`supersede`),
`split_fact`, `merge_facts`, `add_fact`, `pin`. Identity ops (`merge_entities`,
`split_entity`, `assert_distinct`) stay because they target entities not facts — but
their *inverses* are snapshot-undo, not separate ops. Lifecycle toggles
(`retract`/`pin`/`set_confidence`) become a single `set_lifecycle` field-discriminated
op mirroring `set_field`.

**Tradeoff.** Fewer op names means the *audit display* must derive intent from
(field, cardinality) rather than read it off the op type — slightly less self-describing
logs. Worth it: ~10 fewer code+test paths.

**Essential vs accidental.** The *cardinality-routed* value ops (`add_to_set` /
`remove_from_set` / `replace_member`) and the structure ops are **essential** — they are
the actual fix for the override-vs-array bug. The inverse-ops-as-first-class-ops and the
three-spellings-of-supersede are **accidental**.

---

## F5 — Cognitive load: the card invites over-editing and decision fatigue; offered_ops is a footgun surface (SEV-2)

**Attack.** "The card IS the editor; every field is an in-place editor" (E §1.1, §2) is
the stated *anti*-pattern for high-throughput review. The common case for a single-user
system is "this note's 6 facts are fine." Exposing predicate / value / subject / object /
temporal / modality / domain / kind / confidence / pin / provenance as editable on every
card maximizes the per-item decision surface. E §6.2 admits this ("invites over-editing,
slows triage") and offers progressive disclosure as mitigation — but the *default* is
still "everything is editable," which is the load-maximizing default. Decision fatigue in
a personal system means the owner stops reviewing — adoption death (SEV-2, edging SEV-1
on adoption).

Separately, `offered_ops` (C §3.2) is computed arbiter-side and shipped to the card to
gate which buttons render. That is correct for *security* but creates an ergonomics
trap: the set of legal ops is *data-dependent per predicate*, so the UI is non-uniform
across cards — the owner re-learns the affordances per predicate ("why no 'add another'
here?"). The cardinality label (E §3.2) is the mitigation, but it puts a
data-modeling concept ("functional vs set-valued") in front of a human who just wants to
add a phone number.

**Simpler alternative.** **Default the card to read-only triage** with two primary
actions: `Approve` and `Needs fix`. Only `Needs fix` opens the full editor. This makes
the 90% path two keystrokes and confines the 6×7×2 surface to the 10% that need it. Hide
`cardinality` entirely; render "+ add another" *iff* set-valued and just omit it
otherwise — never explain the model to the human.

**Tradeoff.** A power-user who wants to fix a field inline on an approved card takes one
extra click (Needs fix → field). Acceptable; the throughput case dominates.

**Essential vs accidental.** Structured editing *capability* is **essential** (the
wishlist demands it). Structured editing as the *default surface on every card* is
**accidental** — it is an IA choice that maximizes load, mitigated only by a drawer.

---

## F6 — Position on §7(g): `set_field` super-op including `domain` — SPLIT IT (SEV-2)

**Attack & position.** The spec's provisional pick (ii) — pull `domain` out of
`set_field` into a dedicated `move_domain` op — is **correct, and I strengthen it**. C's
"fewer kinds" instinct merged `domain` into `set_field`; F's entire firewall model gates
by `op_kind`. Burying the single highest-blast-radius action (health→general downgrade)
as a *discriminator value inside a permitted op* turns the capability allowlist into a
field-level check, which must be perfect on every code path that constructs a `set_field`.
A field-level allowlist is strictly harder to get right and to test than an op-type
allowlist: you must enumerate forbidden *(op, field)* pairs instead of forbidden *ops*.

This is not a fewer-kinds-vs-safety trade; it is **pure downside to merging**. `domain`
is unlike the other `set_field` fields: it re-keys the slot (spec §3.1 — domain is in the
slot key), it requires a copy-forward not a relabel (§7(f)), it is owner-only and
non-batchable, and the LLM may never emit it. A field that breaks *every* assumption the
other `set_field` fields share does not belong in `set_field`. **Split it.** The
burden-of-proof for re-merging (spec §7(g) "what would flip it") should never be met,
because the field-level allowlist "must re-derive domain and re-check RLS regardless" —
so merging saves *nothing* on the security path and only adds a way to get the gate wrong.

**Tradeoff.** One more op_kind (`move_domain`). Trivially worth it.

**Essential vs accidental.** A dedicated `move_domain` op is **essential complexity** —
it reflects a genuine difference in blast radius and reversibility. The merged super-op
is **accidental** — it optimizes a vanity metric (kind count) against a binding invariant.

---

## F7 — The 7-variant TypedValue could be 5; `boolean` and `structured` earn scrutiny (SEV-3)

**Attack.** Seven variants (`enum | quantity | date | boolean | text | structured |
ref`) is defensible but two are weak:
- `boolean` is `enum` with domain `{true,false}` — a strict subset. It exists only to
  avoid writing `{"type":"enum","code":"true"}`. One fewer variant, one fewer editor,
  one fewer parser branch.
- `structured` (A open-Q 7, spec §7(k)) is the open-ended one — "registry-declared closed
  shapes" today, but it is the variant most likely to grow (address, then phone-with-ext,
  then…). It is a mini-schema-registry inside the value union. For a personal system, an
  `address` is realistically the *only* struct that matters; everything else is `text`
  until proven otherwise. Carrying the general `structured` machinery up front is
  speculative generality.

**Simpler alternative.** Ship 5: `enum` (absorbs boolean), `quantity`, `date`, `text`,
`ref`. Add `structured` *when* the second concrete struct shape appears, not before — and
even then consider modeling address as three `text`/`enum` facts (line, city, region)
rather than a nested struct, keeping every value scalar and every value editor flat.

**Tradeoff.** `{"type":"enum","code":"true"}` is uglier than `{"type":"boolean"}`. A
future struct need triggers a (small, additive — patch-level per D §4) contract bump
instead of being pre-built. Both are cheap.

**Essential vs accidental.** `enum/quantity/date/text/ref` are **essential** — they are
the actual not-a-sentence typing. `boolean` is **accidental** (enum subset);
`structured` is **speculative** (build-on-demand). This is a genuine nit (SEV-3) — fewer
variants is nice-to-have, not load-bearing.

---

## F8 — Two asymmetric review contracts (fat-read/thin-write) with codegen-mitigated drift (SEV-3)

**Attack.** E §2 ships two contracts whose value-shapes must match, "mitigated" by shared
codegen (E risk 7). Mitigating a self-inflicted duplication with a build-step is a smell:
the simplest fix is to not duplicate. The thin-write ops already reference the read
payload's stable ids (`value_id`, `fact_id`); the value shapes in the write are a *subset*
of the read shapes. Two hand-maintained schemas that "must derive from one" is one schema
plus discipline away from being one schema.

**Simpler alternative.** One value-shape schema (Track A's), imported by both directions;
the write contract is *only* `{verdict, base_version, ops[]}` where op payloads reuse the
A value types by reference. No codegen step, no drift surface.

**Tradeoff.** The read payload is fatter (carries candidates, enum domains, ui_capabilities
the write doesn't need) — fine, asymmetry of *enrichment* is healthy; asymmetry of
*value-shape definition* is not.

**Essential vs accidental.** Fat-read / thin-write asymmetry is **essential** (read needs
render metadata write doesn't). Two *separately-defined* value-shape vocabularies is
**accidental**.

---

## Summary table

| ID | Title | Sev | Essential core kept | Accidental complexity removed |
|---|---|---|---|---|
| F1 | "Fewer kinds" not met; complexity relocated to god-component | 1 | `value_shape×cardinality` editors | `kind`/`reason` forks; triple op-naming |
| F2 | Op-log/inverse machinery over-built for single user | 1 | append-only history + op attribution | per-op stored inverses + inverse migration ladder |
| F3 | Fat `FactClaim` envelope is false economy | 2 | typed value, stable id | "one monotone shape" optionality |
| F4 | ~22-op algebra not minimal/proven-closed | 2 | cardinality-routed value + structure ops | inverse-ops-as-ops; 3 spellings of supersede |
| F5 | Card over-edits; offered_ops a load surface | 2 | structured-edit capability | edit-everything default; exposed cardinality concept |
| F6 | §7(g): SPLIT `domain` out of `set_field` | 2 | dedicated `move_domain` gated by op_kind | firewall field buried in a generic op |
| F7 | 7-variant TypedValue → 5 | 3 | enum/quantity/date/text/ref | `boolean` (enum subset); pre-built `structured` |
| F8 | Two value-shape contracts w/ codegen drift | 3 | fat-read/thin-write enrichment asymmetry | duplicated value-shape vocabularies |

## Positions on framing's open questions
- **§7(g) set_field super-op:** SPLIT (F6). Strengthen the spec's pick (ii) to a binding
  decision; the re-merge burden-of-proof can never be met because the field-level gate
  saves nothing on the security path.
- **"Fewer kinds" success criterion (framing §5):** NOT met (F1). It counted components,
  not decision points; total maintainer-facing complexity went *up*. The criterion must
  be re-baselined or the spec fails its own gate.

## Highest-value single simplification
**F2 — replace per-op precomputed inverses with snapshot-based undo read off the
already-append-only assertion history.** It deletes ~22 inverse definitions, their
proof obligations, and a whole inverse-migration ladder, while preserving every binding
invariant (audit, reversibility, RLS) for free — because the append-only bitemporal
store already holds the prior state the inverses laboriously re-encode.
