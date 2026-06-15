# JBrain2 — Entity & Schema Model

Binding reference for how entities, names, and typed attributes are **shaped**
across the knowledge graph. This is the schema layer that sits *underneath*
`docs/ANALYSIS.md`'s extraction/resolution pipeline: ANALYSIS owns *how facts
are produced and reconciled*; this doc owns *what an entity of a given kind is
made of* and *how its name and properties are spelled, rendered, and resolved*.

Produced from the owner's workflow concept plus three parallel research passes
(current-code audit, domain-standard modeling, schema-mechanism design) and one
adversarial red-team. Owner-ratified decisions are **[decided]**; choices in
this doc that await ratification are **[proposed]**. Where this doc cites an
existing ratified decision it links it as **[decided: ANALYSIS]**.

**[ratified 2026-06-11]** The owner approved this model's direction and the
three decisions in the final section. Items stay marked **[proposed]** until
they are *built* in their target phase — ratification settles the design, the
phase ships the code.

## Why this doc exists

A blank database, four capture notes, and the analysis produced an identity
graph that was visibly "all over the place":

1. **Predicate keys drifted by style** — the *same* concept stored as
   `legal_name` on one entity and `legalName` on another; "also known as"
   appearing as `alsoKnownAs` here and `nickname.from_kids` there.
2. **Canonical names were inconsistent** — the owner displayed as `Me`, the
   spouse displayed as the friend-nickname `Sammy`, with her legal name buried
   inside a fact *value* rather than driving the display name.
3. **Attribute values were inconsistent** — some clean (`name → Jeff Hopkins`),
   some entire restated sentences (`legalName → Sammy's legal name is Celine
   Kitina Hopkins.`).
4. **Entities failed to consolidate** — `Celine Hopkins`, `Celine Kitina
   Hopkins`, and `Sammy` became three things; one stayed `provisional` forever.

Root cause: **identity — the one thing a personal knowledge graph must get
right — was the least structured part of the pipeline.** Four independent
subsystems each re-derived "what is the name/value of this thing" with different
rules: predicate naming bet on LLM convergence, canonical naming froze the first
surface form, value rendering fell back to prose, and resolution compared raw
strings. Nothing made them agree.

The paradigm shift: **identity is structured, not extracted prose.** Names and
the cross-cutting property facets get a *canonical, declared* shape; the
long-tail stays schema.org-guided-free, exactly as **[decided: ANALYSIS]**.

## What this doc changes, and what it leaves alone

This doc **composes on** ANALYSIS; it does not overrule it.

| ANALYSIS decision | This doc's stance |
|---|---|
| Fact = edge `entity.predicate[.qualifier] → value/entity`; identity key `(subject, entity, predicate, qualifier)` | **Reaffirmed and leaned on hard.** Names become *more* edges, never blobs (§Names). |
| Predicate vocabulary is schema.org-guided, open, *no controlled ontology* | **Reaffirmed.** The registry supplies *preferred* spellings, never a gate (§Vocabulary). |
| `canonical_name` is a denormalized projection of current facts | **Reaffirmed and implemented** — it is currently frozen at first mention (a bug); §Names makes it a real projection. |
| Identity is mention-anchored; declared names → exact aliases; collisions → `merge_proposal`; same-name coexistence rejected | **Reaffirmed.** This doc adds **no** competing identity-key concept (§Resolution). |
| Reified inverse edges (`worksFor↔employs`); per-kind supersession; functional allowlist | **Reaffirmed and generalized** to a Role-on-edge pattern (§Relationships). |
| Recurrence = `recurrence`-kind temporal tokens; appointments are time-bound entities | **Reaffirmed.** Recurring bills/meds are tokens, not materialized rows (§Recurrence). |
| Typed records: `appointments`/`lists` (Phase 4), `lab_results` (Phase 7); structured medical docs route to typed parsers, not free-extracted | **Respected.** This doc *catalogs* their properties but marks them deferred typed records (§Vehicles). |

Three of the four observed bugs are **implementation gaps against already-ratified
design**, not new design: the canonical-name projection, the
`provisional → confirmed` promotion, and the embedding-resolution layer are all
described in ANALYSIS and simply absent from code. This doc states them as
requirements and adds the genuinely new layer — a **schema registry** — on top.

## The schema registry (soft, declared, in-process)

A small registry declares the **shape** of each entity kind: which property
facets it carries, the canonical spelling of its core predicates, how its
display name projects, and which predicates seed aliases. It is **soft**: it
informs extraction, rendering, and resolution, but it is *never* a storage gate
— making it a gate would resurrect the "controlled ontology" ANALYSIS
explicitly rejected.

**[proposed] Authoring surface: two-file YAML.** A facet library plus per-type
files, LinkML-shaped (single `extends` backbone + multiple composable `facets`
+ per-predicate fields). Layout:

```
backend/src/jbrain/schema/defs/   # co-located in the package, ships in the wheel
  _meta.yaml        # schema_version; the fact-kind enum; the value_shape enum
  facets.yaml       # the reusable mixin library (Named, Temporal, Monetary, …)
  types/
    person.yaml organization.yaml place.yaml role.yaml animal.yaml
    appointment.yaml bill.yaml lab_result.yaml vehicle.yaml medication.yaml …
```

**[proposed] Loading, not code-generation.** The YAML is read once at startup
into one in-process `SchemaRegistry`; the four consumers below are **pure
functions over that object**, not emitted/checked-in artifacts. There is no
codegen step and no `gen-schema --check` CI gate *yet* — for a single-user,
single-developer system an in-process registry has nothing to drift against, so
that machinery would police a problem the design doesn't have. Revisit when the
schema outgrows ~30 types or gains an external editor; the YAML is deliberately
LinkML-shaped so that migration stays cheap. (This is a deliberate trim of the
schema-architecture proposal after red-team: keep the ergonomic authoring
surface, drop the heavyweight pipeline.)

*Implemented:* `jbrain.schema` (`backend/src/jbrain/schema/`) — `load_registry()`
parses the YAML into a validated `SchemaRegistry`; load-time validation mirrors
`jbrain.llm.promptfile`, and the worker **eager-loads it at boot** so a
malformed registry fails loudly there, never mid-note.

**Two consumers are WIRED; the rest are deferred design, not code.** Per
CLAUDE.md #4 (lean density) the registry ships only the methods the pipeline
actually calls — no speculative projection methods sitting unconsumed.

| Consumer | Status | Uses | Hard rule |
|---|---|---|---|
| **Predicate normalization** | **WIRED** (`extraction.py`, `consolidation.py`) | `normalize_predicate` / `renamed_from` attractor | normalizes a spelling, never rejects |
| **Display projection** | **WIRED** (`canonical.py`) | `by_kind` → `display_name` precedence | recomputes `canonical_name` from name facts |

The other consumers this doc envisions — a **prompt digest** injected into
`note.extract`, **value-shape validation**, a **UI render config** — are
*deferred design*, NOT built. The data they would read (`value_shape`,
`enum_values`, `alias_seeding_predicates`, `schema_org_ref`, …) is already in
the YAML and loader-validated, so building them later is a small change. Until
then they live only here, not as dead methods. (Today the prompt's predicate
guidance is hand-written; wiring the prompt digest is the highest-value of
these, since it would stop the prompt and the YAML from drifting by hand.)

## The vocabulary invariant **[proposed]**

State it once, verbatim, so no implementer builds the gate ANALYSIS forbids:

> **Storage accepts any predicate. Shape validation may reject a malformed
> `value_json`; predicate-name validation may never reject anything.**

The registry's "canonical" predicates are *preferred*, not *closed*. They buy
two things and only two things: (a) the preferred spelling injected into the
prompt digest, and (b) `renamed_from` targets that **nightly consolidation**
normalizes drift toward — the same schema.org attractor ANALYSIS already relies
on. Convergence is achieved socially and by the consolidation sweep, **not** by
rejecting input. "Closed where it matters" was the wrong framing; the right one
is **"canonical where it matters, open everywhere, gated nowhere."**

That sweep is implemented as the **`consolidate_predicates` action**
(`jbrain.analysis.consolidation`): it rewrites a stored drift spelling onto its
canonical address in place (same row, citations intact) when the canonical key
is free, and counts a collision — never auto-merging two supersession chains —
when it is occupied. It runs as a boot self-heal today; recurring and on-demand
("emergency") triggering lands with the Phase-5 workflow engine
(docs/ROADMAP.md "Scheduled-task migration").

`allow_open_predicates` (default `true` for Person/Organization/Place; `false`
for tightly-structured records) only changes the *prompt digest's tone* —
"prefer these exact names" vs. "these names are the schema; coin schema.org-style
`snake_case` only if truly needed" — never what storage accepts. A predicate the
registry has never seen is still stored; consolidation may later promote a
frequently-seen open predicate into the declared set in a new `schema_version`.

## The meta-schema **[proposed]**

Fields a `types/<type>.yaml` may declare. (Trimmed after red-team: no
`identity_keys`, no `domain_floor`, no `cardinality`, no `is_subject_type` — see
§Rejected.)

```
id:                  # stable machine id, snake_case, never reused (migration anchor)
name:                # schema.org type name where one fits, else snake_case  (= entities.kind)
extends:             # 0..1 parent type id  (single backbone)
facets:              # 0..n facet ids to mix in
schema_org_ref:      # advisory, e.g. "schema:Person"
description:         # why this type exists; can seed the prompt
vehicle:             # graph | typed_record(phase N)   — see §Vehicles
default_fact_kind:   # maps to the fixed fact-kind enum; per-predicate kind overrides it
allow_open_predicates: true|false      # tone of the prompt digest only (never a gate)

predicates:
  - canonical_name:  # the predicate string (preferred spelling)
    qualifier_vocab: # 0..1 named vocab for predicate families (e.g. name.<kind>.<audience>)
    value_shape:     # scalar | text | enum | quantity | date | ref(<type>) | structured(<shape>)
    kind:            # 0..1 override of default_fact_kind
    functional:      # bool — supersede-on-change (joins the ANALYSIS functional allowlist)
    schema_org_ref:  # advisory, per-predicate
    enum_values:     # for value_shape: enum
    range_type:      # for value_shape: ref(...) — target type id
    renamed_from:    # 0..n prior spellings → consolidation/normalization targets
    description:     # one line (would seed a prompt digest)

alias_seeding_predicates: # ordered predicates whose asserted values register as exact aliases
display_name:             # ordered predicate paths → canonical_name projection
```

**`value_shape` rule (the BLOCKER-1 fix):** `structured(<shape>)` is reserved
for genuinely **atomic** compounds — a `quantity` is `{value, unit}`, an address
is one shape. **If the sub-parts can independently change, disagree, or be cited
separately, they are separate edges, not a `value_json` blob.** This is what
keeps per-property history, supersession, and citation intact (§Names is the
canonical application).

## Cross-cutting facets **[proposed]**

Reusable property bundles, attached to types rather than redefined per category.
Canonical names align to schema.org / vCard / FHIR so the graph maps cleanly to
those standards and the LLM already knows the spellings.

| Facet | Canonical predicates | Standard | Notes |
|---|---|---|---|
| **Named** | `name` (display), `name.legal`, `name.given`, `name.family`, `name.additional`, `name.prefix`, `name.suffix`, `name.preferred`, `name.nickname.<audience>`, `name.maiden`, `name.former`, `name.aka` | schema.org name props, vCard `N`/`FN` | every variant is its **own edge** (§Names); `name` is the projected display string |
| **Temporal** | `startDate`, `endDate`, `effectiveDate`, `validInterval` (→ temporal token) | ISO 8601, FHIR `effective[x]` | absent `endDate` ⇒ ongoing; values reference temporal tokens **[decided: ANALYSIS]** |
| **Recurrence** | `recurrence` (→ `recurrence`-kind temporal token: RRULE/RDATE/EXDATE) | iCalendar RFC 5545 | a token, **never** materialized instance rows (§Recurrence) |
| **Located** | `address` (`structured(postal_address)`), `geo` (`structured(geo)`) | schema.org `PostalAddress`/`GeoCoordinates`, ISO 3166 | 🔒 `location` domain |
| **Monetary** | `amount` (`quantity` value+ISO-4217 currency) | schema.org `MonetaryAmount`, ISO 4217 | 🔒 `finance`; amount always paired with currency |
| **ExternalIdentified** | `identifier.<scheme>` (VIN, IMEI, RxNorm, LOINC, EIN, microchip…) | schema.org `identifier`, FHIR `identifier` | many 🔒; strong resolution signals |
| **Lifecycle** | `status` (enum per type, with transition history) | FHIR `status`, schema.org `*StatusType` | `status` is **functional** (supersede), keep the chain |
| **Contactable** | `email`, `telephone`, `url` | schema.org / vCard | 🔒 general; accumulate |
| **Related** | a reified **Role** edge: `roleName`, `startDate`, `endDate`, source, target | schema.org `Role`/`OrganizationRole` | employment, ownership, residence, prescriber, vet, account-holder (§Relationships) |

🔒 = privacy-sensitive: domain-scoped, redactable, never cited into a `general`
wiki article. Domain is carried **per fact** by the classifier **[decided:
ANALYSIS]** — the registry does **not** store a per-predicate `domain_floor`
(it would be a third, leak-prone domain source; a health-suggestive predicate is
a *classifier hint*, and the prompt digest is domain-scoped like any query).

## Names — the structured identity facet **[proposed]**

Names are where the paradigm earns its keep. **Each name variant is its own
edge**, addressed `name[.kind[.audience]]`, with its own supersession chain,
its own alias registration, and its own collision→merge behavior — exactly the
edge model ANALYSIS already built (and exactly what the running app's
`nickname.from_kids` already was; the only bug was that `alsoKnownAs` wasn't
normalized into the same family).

The `name.*` family, with `name.nickname` taking `qualifier_vocab: name_audiences`:

| Edge | Meaning | Kind | Functional |
|---|---|---|---|
| `name` | the projected display string (see below) | attribute | derived, not stored directly |
| `name.legal` | registered legal name | state | yes — a legal change supersedes, with history |
| `name.given` / `name.family` / `name.additional` | structured components (vCard `N`) | attribute | per-component |
| `name.prefix` / `name.suffix` | honorific prefix/suffix | attribute | accumulate |
| `name.preferred` | what they go by | preference | yes |
| `name.nickname.<audience>` | audience-scoped nickname; `audience` preferred `{kids, family, friends, work, public}`, open values allowed 🔒 | attribute | accumulate (one per audience) |
| `name.maiden` / `name.former` | prior names, with `validInterval` token | state | accumulate |
| `name.aka` | catch-all alternate | attribute | accumulate |

**Display name is a projection, not a stored override** **[decided: ANALYSIS;
currently unimplemented].** `canonical_name` is recomputed on every name-fact
write by the type's `display_name` precedence — for Person:
`[name.preferred, name.given+name.family, name.legal, first surface form]`.
This fixes the frozen-`Sammy` bug directly: "Sammy" becomes a
`name.nickname.friends` edge; the display projects to her given+family or
preferred name; "Me" remains an explicit owner override because the owner entity
is the deliberate center of the graph **[decided: ANALYSIS]**.

**Internationalization rules (bake in — "falsehoods about names"):**

- A person may have **exactly one** name component (mononym). Require **nothing**
  but the projected `name`; never require `name.given` + `name.family`.
- Name **order is not** given-then-family. Store components structurally; render
  per locale; never reconstruct identity by concatenation.
- Names **change** (marriage, transition, legal change) → interval-valid
  variants; supersede the *preferred/legal* edge, keep history.
- Multiple **scripts** are not transliterations of each other → parallel
  `script`/`language`-tagged edges; don't collapse.
- Names are not unique, not stable, not ASCII, have no max length, and
  capitalization is not normalizable → **never key identity on a name** (that is
  the resolver's job, §Resolution).

## Relationships are reified Role edges **[proposed]**

Employer/employee, owner/pet, resident/place, prescriber/patient,
account-holder/account are **roles on an edge, not entity types and not endpoint
attributes.** This generalizes the inverse-edge pattern ANALYSIS already ships
(`worksFor↔employs`). A Role carries `roleName`, `startDate`, `endDate`, and the
two endpoints; it is an interval `state`/`relationship`, functional only where
the predicate is on the ANALYSIS allowlist (`employer`, `spouse`, `residence`).

This prevents type explosion: there is no `Employee` type and no `Employer`
type — there is a `Person`, an `Organization`, and an employment Role between
them. "Sammy is married to Me" is a `spouse` Role; "Jeff works for Acme" is an
employment Role with `jobTitle`.

## Recurrence is a token, never a materialized row **[proposed]**

Recurring bills, subscriptions, medication schedules, and repeating appointments
are stored as a single `recurrence`-kind **temporal token** (RRULE + EXDATE/RDATE)
**[decided: ANALYSIS]** — *not* as a template that pre-generates dated instance
rows. Pre-generating would manufacture rows no note ever produced, breaking the
"notes are the sole source of truth / every datetime traces to words" invariant
and leaving rule-spawned future rows un-purgeable on note deletion.

Instances are **computed at read time** for the ICS feed and queries. A specific
occurrence becomes a real row **only when a note instantiates it** ("paid Sept
rent"), at which point it is an ordinary note-derived fact tracing to words. A
repeating appointment is one entity with a `recurrence` token binding; a
reschedule supersedes the binding, exactly as ANALYSIS specifies.

## Resolution & identity — no new key system **[proposed]**

Identity stays **mention-anchored and resolver-owned** **[decided: ANALYSIS]**.
This doc adds **no** `identity_keys` / uniqueness-constraint concept — that would
be a second, weaker source of truth that disagrees with the resolver (two people
legally named "James Smith" must route to a `merge_proposal`, not silently
collide). (Under the shipped `integrate_note` path the Integrator *agent* now
proposes each mention's coreference as `IntegrationIntent.entity_resolutions`,
which the arbiter validates — existing must be in scope, new mints a provisional,
ambiguous routes to review — with the deterministic resolver as the fallback for
any ref the agent left unresolved. The structural machinery below — alias
seeding, merge proposals, no second key system — is unchanged.)

The registry contributes exactly one resolution input: **`alias_seeding_predicates`**
— the predicates whose *asserted* values register as exact aliases on their
entity, feeding the existing declared-name→alias machinery. For Person that is
`[name.legal, name.preferred, name.aka, name.maiden]`. A seeded alias that
collides with a different live entity files a `merge_proposal` (the more-anchored
side wins) — unchanged from ANALYSIS. The structured `name.given`/`name.family`
edges give the resolver token-level overlap signals so `Celine Hopkins` /
`Celine Kitina Hopkins` / `Sammy` surface as **one merge proposal** instead of
forking three provisional entities — but the *decision* still routes through the
review inbox, never an auto-link.

**Implementation requirements (gaps against ratified design):**

- **`canonical_name` is now the live projection** above (`jbrain.analysis.canonical`,
  wired into the pipeline after each note settles) — no longer frozen at first
  mention. The owner "Me" keeps its explicit override.
- **Declared-name near-duplicate merge proposals are now wired**
  (`entities.near_duplicate_entity`): when a self-declared name strongly embeds
  to a *different* same-kind entity (`Celine Kitina Hopkins` ~ `Celine Hopkins`),
  the pipeline files a `merge_proposal` — the exact-alias collision check could
  not see it. Embedder-gated (skipped without one, so the harness is unaffected)
  and proposal-only: a near match is never an auto-link. The embedding
  resolution layer itself was already active in production (the worker passes
  the embed client); it stays conservative — a single strong, non-subject
  candidate auto-links, everything else degrades to review.
- `provisional → confirmed` promotion — **deferred, needs a ratified signal.** The
  obvious "corroborated by ≥2 notes" rule contradicts the current golden harness,
  which asserts entities stay `provisional` across multiple notes; auto-promotion
  is a behaviour change the owner must define (and the goldens be updated for),
  not a silent fix. Until then only the hard-coded "Me" is `confirmed`.

## Entity vehicles: graph vs. typed record **[proposed]**

The registry catalogs **every** category's property model, but a category's
`vehicle` says where its data physically lives — honoring the ROADMAP's typed
tables rather than prematurely folding them into the graph.

| Vehicle | Meaning | Categories |
|---|---|---|
| **graph** | entity rows + facts now (Phase 3) | Person, Organization, Place, Role, Animal/Pet, Appointment*, Vehicle, Document, Device |
| **typed_record (P4)** | catalog now; **typed table** + ICS/agent tools in Phase 4 | `lists`/`list_items`, `appointments`** |
| **typed_record (P7)** | catalog now; **typed parser + table** in Phase 7 | `lab_results`, and structured medical/financial documents |
| **graph, 🔒-scoped** | graph entity within a firewall now; may gain a typed projection later | Bill/Invoice, FinancialAccount, Medication, Subscription |

\* An appointment is a **graph entity** (a time-bound entity) from Phase 3;
Phase 4 adds the typed `appointments` projection for the ICS feed and agent
tools. \*\* Listed twice deliberately: the entity exists in the graph now, the
typed record arrives in Phase 4.

The shared **facets are the unification**: `Monetary`, `Temporal`, `Located`,
`ExternalIdentified` are reused by both vehicles, so a Phase-7 `lab_results`
table and a Phase-3 graph entity speak the same property vocabulary. **Every
category that becomes a typed table ships its own RLS isolation test in the
phase that creates it** (CLAUDE.md #3) — this doc does not create tables.

ANALYSIS's guard holds: structured medical/financial documents are **detected and
routed to typed parsers, not free-extracted into hundreds of facts.** entity.md
defines *what those parsers populate*, not a graph-fact encoding that would
contradict the guard.

## Per-category catalog **[proposed]**

Compact catalog; full property sets live in `backend/src/jbrain/schema/defs/types/*.yaml`. Standards in
parentheses. 🔒 marks the firewall a category floors into.

- **Person** *(graph; schema.org Person, vCard)* — facets: Named, Contactable,
  Located, Temporal, Related. Core: `name.*` (§Names), `birthDate`/`deathDate`
  (event, functional), `gender`/`pronouns` (state 🔒). Display:
  `[name.preferred, name.given+family, name.legal]`. Alias-seed:
  `[name.legal, name.preferred, name.aka, name.maiden]`. May be a security
  subject (set per-entity, not per-type). Domain: general.

- **Organization** *(graph; schema.org Organization)* — facets: Named,
  Contactable, ExternalIdentified, Related. Core: `name`, `name.legal`
  (functional), `identifier.ein`/`.duns`/`.lei` 🔒, `parentOrganization`
  (ref). Display: `[name, name.legal]`. Locations live on `Place` refs, not
  inlined. Domain: general (tax id finance-adjacent).

- **Place** *(graph; schema.org Place/PostalAddress)* — facets: Named, Located,
  ExternalIdentified. Core: `name`, `address` (`structured(postal_address)`),
  `geo` 🔒, `geofence` 🔒. Display: `[name, rendered address]`. Domain:
  **location** 🔒. The place is timeless; *who is associated with it* is a Role
  interval.

- **Role** *(graph edge; schema.org Role)* — facets: Temporal, Lifecycle,
  Monetary(opt). Core: `roleName` (enum), `jobTitle`, `worksFor`/endpoints
  (ref), `startDate`/`endDate`, `baseSalary` 🔒 finance. Identity:
  `(source, target, roleName, startDate)`. The reified-edge engine for all
  relationships.

- **Animal / Pet** *(graph; custom, schema.org has no fit)* — facets: Named,
  ExternalIdentified, Temporal, Related. Core: `species` (the `kind`, never
  "pet"), `breed`, `sex`, `birthDate`, `identifier.microchip` 🔒 (ISO
  11784/11785), `owner`/`veterinarian` (Role refs). Pet *health* records ride
  the health firewall. Domain: general.

- **Appointment / Event** *(graph entity now; typed `appointments` P4;
  iCalendar VEVENT)* — facets: Named, Temporal, Recurrence, Located, Lifecycle,
  Related. Core: `name`, `scheduledTime` (token binding; schedule supersession
  **[decided: ANALYSIS]**), `recurrence` token, `attendee` (refs), `status`
  (`tentative|confirmed|cancelled` + occurred flag). Identity: the entity +
  `(uid, recurrenceId)` for an occurrence. Domain: general; clinical reason may
  ratchet to health.

- **Bill / Invoice** *(graph 🔒-scoped; schema.org Invoice + RRULE)* — facets:
  Named, Monetary, Temporal, Recurrence, Related, Lifecycle. Core: `payee`
  (Org ref), `amount` 🔒, `dueDate`, `billingPeriod`, `status`
  (`PaymentDue|PaymentComplete|PaymentPastDue|…`), `account` (ref 🔒),
  `recurrence` token for recurring bills (not materialized). Display:
  `[payee — period — amount]`. Domain: **finance** 🔒.

- **Lab result / Observation** *(typed_record P7; FHIR Observation, LOINC,
  UCUM)* — facets: ExternalIdentified, Temporal, Related. Core:
  `identifier.loinc` (CodeableConcept), `value` (`quantity` + UCUM),
  `referenceRange` (`structured`), `interpretation`, `status`
  (`final|amended|corrected|…`), `effectiveDate` (event), `performer`/`subject`
  refs, `specimen`. Domain: **health** 🔒🔒 — strictest. **Populated by the
  Phase-7 typed parser, not free-extracted.**

- **Vehicle** *(graph; schema.org Vehicle)* — facets: Named,
  ExternalIdentified, Related, Lifecycle. Core: `manufacturer`/`model`,
  `modelDate` (year), `identifier.vin` 🔒, `licensePlate` 🔒
  (`{value, jurisdiction}`), `mileage` (accumulating readings), `owner` (Role).
  Display: `[name, modelDate make model]`. Identity signal: VIN. Domain:
  general (VIN/plate 🔒).

- **Financial account** *(graph 🔒-scoped; PCI-aware)* — facets: Named,
  ExternalIdentified, Related, Lifecycle. Core: `institution` (Org ref),
  `accountType` (enum), `identifier.last4` 🔒 (**last four only — never the
  full PAN/CVV/credentials**), `currency`, `accountHolder` (Role). Domain:
  **finance** 🔒🔒.

- **Medication** *(graph 🔒-scoped; typed projection possible; FHIR
  MedicationStatement, RxNorm)* — facets: ExternalIdentified, Temporal,
  Related, Lifecycle. Core: `identifier.rxnorm`, `dosage` (`structured`:
  dose+UCUM, route, timing), `effectiveInterval` (interval state),
  `prescriber` (Role ref), `reasonCode` 🔒 (reveals diagnosis), `status`
  (`active|stopped|…`). Domain: **health** 🔒🔒.

- **Document** *(graph; schema.org DigitalDocument)* — facets: Named,
  Temporal, Related, ExternalIdentified. Core: `name`, `documentType` (enum),
  `dateIssued`, `issuer` (ref), `about`/`mentions` (refs — links to the
  bill/lab/person it concerns), `contentUrl` 🔒 (**via storage abstraction,
  never a raw path** — DEVELOPMENT.md #2), `encodingFormat`,
  `identifier.sha256` (dedup). Domain: **inherits its content's** firewall.

- **Subscription** *(graph 🔒-scoped; Service + RRULE)* — facets: Named,
  Monetary, Temporal, Recurrence, Related, Lifecycle. Core: `provider` (ref),
  `plan`, `amount` 🔒, `recurrence`/`renewalDate`, `status`
  (`active|paused|cancelled`), `paymentMethod` (account ref 🔒). The agreement
  template; its charges are Bills. Domain: **finance** 🔒.

- **Device** *(graph; custom / FHIR Device)* — facets: Named,
  ExternalIdentified, Related, Lifecycle. Core: `deviceType` (enum),
  `identifier.imei`/`.serial`/`.mac` 🔒, `manufacturer`/`model`, `owner` (Role),
  `lastLocation` 🔒🔒 (live location — among the most sensitive fields),
  `status`. Domain: **location** 🔒🔒 for tracking devices.

## How this fixes the four observed inconsistencies

1. **Predicate drift** → the `Named` facet and the cross-cutting facets give the
   high-traffic predicates one *preferred* spelling; nightly consolidation
   normalizes `legalName`/`legal_name`/`alsoKnownAs` toward
   `name.legal`/`name.aka`. No gate, no rejection — just an attractor with teeth.
2. **Inconsistent canonical names** → `canonical_name` becomes the live
   `display_name` projection; "Sammy" is a `name.nickname.friends` edge, not the
   identity. (Implementation gap, now a requirement.)
3. **Inconsistent values** → every predicate declares a `value_shape`; the UI
   renders that shape and **never** falls back to the whole statement sentence.
   `statement` stays prose for embedding/citation only.
4. **Failed consolidation** → structured `name.given`/`name.family` edges give
   the resolver token overlap; `alias_seeding_predicates` feed the
   declared-name→merge path so the three "Celine/Sammy" fragments surface as one
   merge proposal; plus the `provisional→confirmed` and embedding-layer
   implementation gaps are closed.

## Versioning & migration **[proposed]**

- `schema_version` is one integer in `_meta.yaml`, bumped on a breaking change.
  Facts are stamped with `prompt_version` already; schema version governs the
  registry, not per-fact rows. Git history of `types/*.yaml` is the fine log.
- **Predicate rename = an alias map, not a data migration.** Add
  `renamed_from: [oldName]`; the loader builds an old→new normalization table;
  re-extraction upserts rewrite in place (citations survive) and nightly
  consolidation applies it to untouched facts. **Never** rename by delete +
  re-coin — that orphans the supersession chain and reads as a retraction.
- **No identity-key migration apparatus** — there is no identity key to migrate
  (identity is mention-anchored). Renames touch *spellings*, not identity.
- Adding predicates/facets is **non-breaking** (new optional edges). Removing a
  predicate from the registry never deletes stored facts; they become legacy/open
  predicates that consolidation can flag.

## Rejected / deferred (adversarial-review log)

For transparency, the design choices the red-team cut from the first sketch:

- **Names as a `value_json` blob** — rejected: opaque to the identity key, kills
  per-variant supersession and the alias/merge machinery. Names are edges.
- **`identity_keys` as an identity determinant** — rejected: a second, weaker
  source of truth that contradicts the mention-anchored resolver and the
  rejected "same-name coexistence." Kept only as `alias_seeding_predicates`.
- **Folding Bill/Lab/Appointment typed records into entity+facts** — rejected:
  overrules ROADMAP Phase 4/7 typed tables and the "route structured docs to
  typed parsers" guard. Replaced with the `vehicle` marker.
- **"Closed" vocabulary** — rejected as a misnomer/gate risk. Renamed to
  *canonical/preferred*; the one invariant is stated verbatim above.
- **Codegen + `gen-schema --check` CI gate + dev-setup wiring** — deferred:
  over-engineered for a single-user system; an in-process registry has nothing
  to drift against. Revisit past ~30 types.
- **`domain_floor` on predicates** — rejected: a third, leak-prone domain
  source; domain is per-fact via the classifier, prompt digest domain-scoped.
- **`is_subject_type`, `cardinality`** — dropped: subjecthood is per-entity not
  per-kind; `cardinality` overlaps `functional`.

## Ratified decisions **[decided 2026-06-11]**

1. **Authoring surface is the two-file YAML registry.** The red-team's leaner
   alternative (a typed Python structure) was considered and not taken; the YAML
   is LinkML-shaped, so lifting to a Python dict — or up to LinkML proper past
   ~30 types — stays cheap if scale ever argues for it. The codegen/CI-gate/
   dev-setup machinery stays deferred until it earns its keep.
2. **The 🔒-scoped graph categories (Bill, FinancialAccount, Medication,
   Subscription) stay graph entities for now;** a typed projection is deferred to
   the phase that needs structured querying over them, not built speculatively.
3. **`name.nickname.<audience>` is a preferred-but-open enum** —
   `{kids, family, friends, work, public}` seed the prompt digest, but open
   audiences (e.g. `gym`, `bandmates`) are accepted. This is the one vocabulary
   invariant applied to a qualifier: preferred where it matters, gated nowhere.

All fourteen categories in the catalog are now scaffolded under
`backend/src/jbrain/schema/defs/`
(`person, organization, place, role, animal, appointment, bill, lab_result,
vehicle, medication, financial_account, document, subscription, device`).
