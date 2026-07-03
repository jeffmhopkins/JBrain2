# EMR Import — Build Plan

> **Status:** In progress · **Last verified:** 2026-07-03 · **Waves:** W0✅ W1✅ W2◻️ W3◻️ W4◻️ W5◻️

**An in-progress build plan** (per `docs/DOC_LIFECYCLE.md`): red-teamed, on the roadmap. Wave 0
(gates + fixtures) and Wave 1 (storage bedrock — schema defs, the `fhir_status`/supersession
exception, the Layer-2 firewall guard, migrations 0115–0118, RLS isolation tests; see §12) are
complete; waves W2–W5 in §10 open. Synthesized against the shipped graph (`app.entities`/`app.facts`,
migration `0006`), the projection pattern (`analysis/appointment_projection.py`), the attachment
dispatcher and analysis pipeline (`docs/reference/ANALYSIS.md`), the predicate registry
(`docs/reference/PREDICATE_CANONICALIZATION.md` + `schema/defs/**`, including the
already-catalogued but deferred `lab_result.yaml`), and the firewall/RLS precedent (migration
`0006`; `docs/archive/PHASE7_LOCATION_PLAN.md`). The migration numbers below are a snapshot as
of the `Last verified` date; the source of truth is `backend/migrations/versions/` — re-derive
the head before building. All examples are synthetic — no real patient name, MRN, or lab value
appears here.

This plan imports one patient's ~350-page, three-EMR-system corpus into JBrain's health-domain
graph as cited, firewalled, queryable facts, then exposes them through two projection
read-models, two typed agent tools, and health-scoped search. It leads with the storage model
(because it determines what retrieval and safety are possible), then the ingest pipeline,
retrieval surface, migrations, tests, and rollout.

---

## 1. Goal & scope

**Goal.** Turn four messy real EMR exports into **notes → facts → entities** in the `health`
domain so every lab value, admission, provider, and diagnosis is a cited graph fact that stays
the single source of truth the rest of the system already trusts (#7), and expose them safely
to the assistant.

**In scope:** encrypted-PDF intake; text-layer + vision-OCR extraction; per-source parsers
(Epic / OneContent / athena / ARIA); a FHIR/LOINC/UCUM-shaped lab-observation storage model
(`measurement` facts); an encounter/admission model; providers as `Person`, diagnoses as
`MedicalCondition`, facilities/labs as `Organization`; one **new status-aware
measurement-supersession exception** in the arbiter for amended/corrected/withdrawn results;
cross-source dedup; two projections (`lab_results`, `encounters`); two agent tools
(`read_labs`, `read_encounters`) + health-scoped hybrid search over the pathology narrative;
the migrations + tests to the coverage and RLS-isolation gates; a phased rollout.

**Out of scope (named follow-ons):** medication reconciliation (no med list in the corpus;
`medication.yaml` exists but stays unused); imaging/radiology; live FHIR/HL7 feeds; **microbiology
culture/sensitivity and serologic titers** (non-scalar — structured route-to-review only this
phase); **multi-component scalar results (e.g. vital-signs BP)** — the fact-layer *vocabulary*
exists (the shipped `lab_result.yaml` carries a `component` measurement predicate, §3.2), but the
**projection does not support them this phase** and no such data is in the corpus; activating them
is a specified, bounded projection delta (§3.2, §4.1), not a claimed no-op; discharge-summary/H&P/
progress-note NLP (absent); clinical decision support of any kind; **multi-patient / subject-scoped
RLS** (the corpus is a single patient — a Phase-7 guided-intake follow-on, §5).

**The safety frame (binding, not a footer):** JBrain is *personal record-keeping, not medical
advice*. The tools and wiki return **what the record says**, cited; they never synthesize a
diagnosis, never present an inference as fact, and never recommend action.

---

## 2. The source corpus — what is / isn't present

One patient, ~350 pages, three source EMR systems, four **AES-256-encrypted PDFs** (password
known; **not committed** — they stay in the scratchpad and enter only as content-addressed blobs
behind the storage abstraction, #2).

| # | Source | Pages | Text layer | Grouping spine | Contents |
|---|---|---|---|---|---|
| 1 | Epic "EMR Report" | 216 | structured | encounter-header lines + page banners (`Adm/DC`) | labs; **2 inpatient stays with a facility transfer** (MICU → regional A3); 8 outpatient lab visits; a bone-marrow **surgical-pathology narrative** |
| 2 | OneContent (Cape Canaveral) | 82 | fixed-width columns | **ACCOUNT numbers** (e.g. `Cnnnnnnnnnn`), *not* Adm/DC | cumulative lab reports spanning 2020/2021/2022/2025 admissions |
| 3 | ARIA | 40 | **none — scanned images → OCR required** | portal print headers | patient-portal lab printouts, largely a **duplicate of the 2021 OneContent labs** |
| 4 | athena | 12 | structured | per-result header block | 2024 outpatient lab panels |

**Present:** LABS (dominant — CBC/CMP/coag/UA/blood-bank/micro/immunology), HOSPITAL ADMISSIONS
(dates, unit, facility, transfers, LOS), PROVIDERS (22+, roles ordering/authorizing/attending/
pathologist/collecting-RN), DIAGNOSES (ICD-10), TRANSFUSIONS/BLOOD-BANK (ABO/Rh, crossmatch,
FFP/platelet/RBC orders with indications), one PATHOLOGY NARRATIVE (Final Diagnosis / Gross /
Microscopic / Flow Cytometry / Addendum).

**Notably absent — do not hallucinate them:** discharge summaries, H&P, progress notes,
**medication lists**, imaging/radiology, **vital signs**. The importer must not invent them; the
tools report only what the facts say.

**Two structural traps the parsers must survive** (both observed in the `reconstruct_admissions.py`
prototype):

1. **Banner bleed (Epic).** Page banners print *above* the encounter header, and encounters are
   reverse-chronological, so a first-page `Adm/DC` banner bleeds into the *previous* encounter.
   Grouping takes the **mode of banners per encounter**; inpatient-vs-outpatient is decided by
   whether `Adm/DC` banners outnumber `Visit date` banners (`reconstruct_admissions.py:97`).
2. **A hospitalization can span facilities.** The MICU stay transfers to a regional unit; these
   are two `Encounter` entities of one episode, linked (§3.4), reconstructed by continuity of
   admit/discharge dates across facilities. OneContent, separately, is **account-keyed, not
   date-keyed** — one cumulative table spans four admissions years apart, so each *row's*
   `collected_at`, not the file's grouping, sets `valid_from`.

---

## 3. Storage model (entities / facts / value_json / predicates)

Everything lands in the **shipped** `app.entities` + `app.facts` graph (migration 0006) — no
parallel medical store. All rows carry `domain_code='health'` (the strictest floor; ANALYSIS.md:
"misclassifying *into* health is cheap; *out of* it is a leak"). The design's leverage is
choosing the right `kind`, predicate, `value_json` shape, temporal binding, and **identity key**
so the shipped machinery (measurement accumulation, supersession, entity resolution, re-extraction
upsert) does the work — with exactly **one** deliberate, tested extension (§3.5).

> **Vehicle deviation, declared up front.** `lab_result.yaml` carries `vehicle: typed_record(P7)`
> (its original intent was a separate typed table). This build instead materializes every type as
> **graph entities + facts and re-derives a projection** (§4), buying the arbiter's firewall,
> supersession, citations, and review inbox for free. `vehicle` is not runtime-enforced (only
> `schema/models.py` carries the field), so nothing breaks; the four defs keep `typed_record` with
> this note recording why they are stored as graph facts. Flipping them to `vehicle: graph` is an
> acceptable alternative.

### 3.1 Entities

| Concept | `entities.kind` | Notes |
|---|---|---|
| An analyte / observable | `Observation` (FHIR; `lab_result.yaml` `name: Observation`) | **one entity per analyte** (e.g. "Platelet count"), so its `value` measurement facts form a native time-series for trend queries |
| A hospitalization / lab-visit | `encounter` (**new** `encounter.yaml`, FHIR Encounter) | one per admit-facility-unit segment; a facility transfer = two entities linked `partOfEncounter` |
| A provider | `Person` | **health-domain on resolution** (§3.6); name + credential + a `role` via the encounter edge |
| A performing lab / facility | `Organization` | the target of the registry's organization-ranged `performer` |
| A diagnosis | `MedicalCondition` (**new** `medical_condition.yaml`) | schema.org `MedicalCondition`; ICD-10 as `identifier.icd10` |
| The pathology report | prose note (health domain) | narrative stays prose (chunks + a few high-confidence facts), never shredded (§6.5) |
| **The patient** | the existing **"Me" entity** | carries **timeless patient-level attributes** (`bloodType`, `birthDate`) so cross-source disagreements surface (§3.7) |

> **Central decision — entity-per-analyte, `value` as `measurement` (not per-draw `state`).**
> Verified against the shipped def, `value` is `kind: measurement` / `value_shape: quantity` and
> `default_fact_kind: measurement`; `_meta.yaml` defines `measurement` as "instant + value_json;
> time-series, **accumulate**." Modeling one entity per analyte with draws as accumulating
> measurement facts is therefore the **native** policy and matches the agreed direction ("store
> lab observations as measurement facts"). The competing entity-per-draw model would force `value`
> to `state`, contradict the def and the agreed direction, mint thousands of near-identical
> entities, and require excluding them from embedding resolution. Rejected. The time-series is a
> read-time query across an analyte's `value` facts, materialized by the `lab_results` projection
> (§4.1). Corrections are a small, tested arbiter exception (§3.5), not a change of fact kind.

### 3.2 A lab draw is a small fan of sibling facts — one per canonical predicate

`lab_result.yaml` is `allow_open_predicates: false` and already declares most of the exact
vocabulary; the parser honors each predicate's own `value_shape`, validated at integration
(`pipeline.py` value-shape check). **Activating the def makes two vocabulary edits, both declared
in §3.8:** it **drops the composed `Lifecycle` facet** (whose `status` predicate is `functional`
and one-per-*entity* — see below) and **renames its `category` predicate to `observationCategory`**
(a real global-registry collision with `product.category`, §3.4). Crucially, it introduces **no
per-draw report-status fact** — the earlier draft's `reportStatus` attribute is removed as
unbuildable (§3.5); report status is a projection derivation from the value fact's own lifecycle.
The fan splits into two qualifier classes.

**(a) Per-draw facts — keyed with the timestamped qualifier `<collected_iso>|<specimen_or_empty>`
(§3.3):**

```jsonc
// Observation "Platelet count" — one DRAW of 2026-02-01T06:14 (SYNTHETIC)
// entity.predicate[.qualifier] → value_json                         fact kind / value_shape
value            → {"value": 9, "unit": "10*3/uL"}                   // measurement / quantity — STRICTLY {value,unit}
referenceRange   → {"low": {"value":150,"unit":"10*3/uL"},
                    "high":{"value":400,"unit":"10*3/uL"}}           // attribute / structured(reference_range)
interpretation   → "critical"                                        // attribute / enum
effectiveDate    → <temporal_token>  valid_from=2026-02-01T06:14  precision=instant   // event / date
specimen         → "H8202188-8"                                      // attribute / text
performer (edge) → object_entity_id → Organization "Gateway Lab"     // relationship / ref(range=organization) — the LAB
```

The reading's FHIR **report status** (`final`/`corrected`/`preliminary`/…) is **not** a fact field
of its own. It rides the draw as the in-flight `fhir_status` signal (§3.5) that *drives the value
fact's lifecycle transition*, and its durable record IS the value fact's resulting lifecycle
(`active`/`superseded`/`pending_review`/`retracted`) + supersession chain — from which §4.1 derives
the projection's `report_status` column. There is no collision-prone sibling status fact.

> **Why `Lifecycle.status` is dropped, not reused.** The composed `Lifecycle` facet ships `status`
> as `functional: true`, `kind: state` — **one current status per *entity***. Under entity-per-analyte
> (§3.1) an analyte entity spans thousands of draws, so a single functional entity-level status would
> be *clobbered on every draw* and could never say "the 2026-02-01 potassium was corrected while the
> 2025 one was final." The functional entity-level status is therefore genuinely inapplicable to this
> model. We drop `Lifecycle` from the def's facet list (the acknowledged def edit) and, instead of
> replacing it with a sibling status fact, derive per-draw report status in the projection from the
> **value fact's own lifecycle + supersession chain** (§3.5, §4.1) — an enforced, arbiter-owned
> signal, storing no status fact at all. The def retains the `status_values` list only as
> documentation of the incoming FHIR status vocabulary the parser maps from into `fhir_status`.

**Deterministic temporal token for `effectiveDate` (no LLM).** `effectiveDate` is `value_shape: date`,
which per `_meta.yaml` "references a temporal token (resolved absolute)" — a `facts.temporal_token_id`
pointing at an `app.temporal_tokens` row. The parsers are pure deterministic Python, so the importer
**mints the token deterministically**, one per draw: `kind='point'`, `resolved_start = collected_at`,
`temporal_precision = 'instant'` (falling back to `'day'`/`'month'` when the source prints only a
coarser date), `capture_anchor = collected_at`, `surface_phrase =` the printed date string,
`note_id`/`chunk_id =` the source page-chunk, `domain_code='health'`. No anchor resolution and no LLM
are involved — the absolute instant is already in the record. Encounter admit/discharge instants need
**no** token: `period` is a `state` (§3.4) carrying them on the bi-temporal `valid_from`/`valid_to`
columns directly (only `value_shape: date` predicates reference tokens). Parser unit tests pin the
minted token's `kind`/`precision`/`capture_anchor` (§9).

**(b) Analyte-constant facts — keyed with a *constant* qualifier on the analyte entity (one
functional fact per analyte, NOT per draw):**

| predicate | kind | value_shape | value_json | qualifier |
|---|---|---|---|---|
| `identifier` | attribute | `scalar` | `"777-3"` | `loinc` (the `id_scheme` member — constant scheme tag, not a timestamp) |
| `observationCategory` | attribute | `enum` | `"laboratory"` | `""` |

The LOINC and category of "Platelet count" are the same on every draw; keying them per-draw would
mint an identical fact for every reading and fragment one functional attribute across N addresses.
Keying them with a constant qualifier makes each exactly one functional fact on the entity; a
re-parse upserts in place. (`observationCategory` is the renamed-from-`category` predicate; see the
§3.4 collision audit for why the rename is mandatory.)

**Multi-component scalar results (BP etc.) — vocabulary present, projection out of scope this phase.**
The shipped `lab_result.yaml` ships a `component` predicate (`value_shape: quantity`,
`kind: measurement`, comment: "multi-part result (BP systolic/diastolic); qualifier = component
LOINC"), so a component-bearing result *stores* with no vocab change: one `Observation` entity (e.g.
"Blood pressure") carrying two `component` measurement facts per draw, addressed with a **composite
qualifier `<collected_iso>|<specimen_or_empty>|<component_LOINC>`** so systolic and diastolic occupy
distinct addresses and each accumulates its own series. **The `lab_results` projection, however, does
NOT support them this phase** — it groups by draw and sources `value_num` from the `value` fact, and a
BP draw has *two* `component` facts and *no* `value` fact, which (a) would leave `value_num` unpopulated
and (b) would collide two current rows on the partial unique key (§4.1). This is **not** claimed as a
tested no-op. Activation is a **bounded, specified projection delta** (§4.1): add a `component_code`
discriminator to the projection's unique key and partial unique index, and teach the projector to
read `component` facts (one row per component). The corpus contains no vital signs, so that delta is
deferred; the fact layer is ready the day such a result is parsed, but the read-model must be extended
first — it is not free.

**What is deliberately *not* a fact field.** There is no `loinc`, `accession`, `source_system`, or
`report_status` key smuggled into `value_json` (which would fail `value_shape: quantity`): LOINC lives
on `identifier`, the accession on `specimen` + the qualifier, the source system is provenance on the
synthetic note, and the FHIR report status is **derived in the projection from the value fact's
lifecycle** (§4.1) — never a token in free-text `statement`, never a shape-unvalidated sibling fact.
**Non-scalar results** — blood-bank ABO/Rh (→ `bloodType` attribute on "Me", §3.7), microbiology, and
titers — never land in `value`: micro/titers route deterministically to review (§6.6) and are a named
follow-on.

### 3.3 The qualifier: `<collected_iso>|<specimen_or_empty>` (per-draw facts) — the record-integrity spine

The shipped structural identity key is `(entity_id, predicate, qualifier)` (`facts_identity_idx`,
`qualifier NOT NULL DEFAULT ''`; index is **non-unique** so an active fact and its superseded
predecessors share the key). Every per-draw fact uses
`qualifier = "<collected_at_iso>|<specimen_id_or_empty>"`, e.g. `"2026-02-01T06:14:00-05:00|H8202188-8"`.

- **The timestamp guarantees distinct draws never collide.** Two draws of one analyte with no
  readable specimen (the OCR path, or any source lacking one) still differ in `collected_at`, so
  they occupy different addresses and both survive as time-series points. Encoding *only* the
  specimen id would give two specimen-less draws the same empty qualifier → a
  `(entity_id,'value','')` collision that silently destroys a time-series point.
- **The specimen id inside the qualifier makes a *reconciled* same-draw match idempotent.** Once
  the candidate stage (§6.4) has established that the 2021 ARIA and OneContent rows are the same
  physical draw and forced their `collected_at` (and specimen, where present) equal, the identical
  qualifier lets the arbiter upsert in place: one fact, dual-cited. The qualifier is the
  *idempotency* mechanism — **not** itself the cross-source matcher (§6.4).
- **The projection's uniqueness mirrors this qualifier** (§4.1), so fact identity and projection
  identity can never disagree on which readings belong to which draw.

`valid_from` carries the collected instant (bi-temporal); `effectiveDate` restates it as a
first-class fact (with its deterministic point token, §3.2); the qualifier is the *addressing* copy.
Supersession compares **validity time, never capture time** — a 2020 OneContent value can never
displace a 2025 value even when both import the same day. Because "same draw ⇒ same qualifier ⇒ same
`valid_from`", the §3.5 correction transition supersedes precisely the prior reading of *that* draw.

### 3.4 Encounter facts, the lab↔encounter edge, and the predicate-collision audit

`encounter.yaml` is a **new schema-def** (FHIR Encounter, `allow_open_predicates: false`). **Every
predicate name — reused or new — was audited against the shipped registry**, because
`registry_seed_rows()` (`predicates.py:73`) dedupes canonicals *globally* to one `(value_shape, kind)`
chosen as the **lexicographically-first** `(value_shape, kind)` across all types declaring the name.
A name reused with a divergent shape is silently resolved to the *other* type's shape and then
rejected by value-shape enforcement at integration. The audit ran over **every** name the activated
`lab_result.yaml`, new `encounter.yaml`, and new `medical_condition.yaml` actually seed:

- **Real collisions found (four, not three):**
  - `category` — `product.category` is `text`; `lab_result.category` is `enum`; `'enum' < 'text'`, so
    the global seed would force *product's* category to the enum shape and then reject product values
    like `"appliance"`. **Resolved by renaming lab_result's predicate to `observationCategory`** (§3.2).
  - `location` — a `ref`→`place` canonical (appointment/organization/product). Encounter avoids it
    (`serviceProvider`, `careUnit`).
  - `partOf` — a `ref`→`project` canonical (task). Encounter uses `partOfEncounter`.
  - `reasonCode` — a canonical in medication. Encounter uses `encounterDiagnosis`.
- **Verified safe (no divergent-shape peer):** `identifier` (`scalar`/`attribute` in both lab_result
  and medication — identical), `effectiveDate`/`interpretation`/`specimen`/`performer`/`component`/
  `value`/`referenceRange` (declared only in lab_result), and every new encounter name
  (`period`, `class`, `careUnit`, `serviceProvider`, `attender`, `encounterDiagnosis`, `transfusion`,
  `partOfEncounter`, `disposition`, `hasObservation`) — none currently exist in any other def, so each
  seeds as its own row.

The encounter vocabulary:

| predicate | kind | value_json / edge |
|---|---|---|
| `period` | **state** (SCD-2 interval) | `valid_from`=admit, `valid_to`=discharge; imported **closed** (both dates present); `{"class":"inpatient"}` restated below |
| `class` | attribute (enum) | `inpatient\|emergency\|ambulatory\|observation` |
| `careUnit` | attribute (text) | `"MICU"` — the clinical ward; **health, NOT a Located value** (see §3.6) |
| `serviceProvider` (edge) | relationship | `object_entity_id` → the facility `Organization` (**name only**, no address — §3.6) |
| `attender` (edge, qual=role) | relationship | `object_entity_id` → health-domain `Person`; qualifier `attending\|ordering\|pathologist\|collecting_rn` |
| `encounterDiagnosis` (edge, qual=icd10) | relationship | `object_entity_id` → `MedicalCondition`; qualifier e.g. `D69.3` *(distinct name — avoids the `reasonCode` collision)* |
| `transfusion` (event, qual=orderId) | event | `{"product":"FFP","units":13,"indication":"…"}` — one event per order; distinct order-id qualifiers coexist (events accumulate) |
| `partOfEncounter` (edge) | relationship | `object_entity_id` → the episode's first `Encounter` (facility-transfer linkage) *(distinct name — avoids the `partOf` collision)* |
| `disposition` | attribute (text) | discharge disposition, e.g. `"transferred"` |
| **`hasObservation`** (edge, qual=`<collected_iso>\|<specimen>`) | relationship | `object_entity_id` → the analyte `Observation`; **one edge per draw-in-encounter.** This is the lab↔encounter join, owned **here** because the frozen `lab_result.yaml` reading vocab has no encounter predicate. |

Length-of-stay is **derived** (`discharge − admit`), never stored, so it cannot drift.

> **Ordering provider & the frozen lab vocab (reconciliation note).** `lab_result.yaml` is
> `allow_open_predicates: false` with no person-ranged predicate, and we do **not** add one to its
> reading vocabulary. The ordering/collecting provider is therefore reached **through the enclosing
> Encounter** (`hasObservation` → Encounter → `attender[ordering]`), and `lab_results.orderer` is
> projected from that path. To keep athena's explicit per-panel ordering provider (and Epic
> outpatient / OneContent-account orderers), **each outpatient lab visit is modeled as an ambulatory
> `Encounter`** (`class:"ambulatory"`) that carries the orderer via `attender[ordering]`. Only
> genuinely orphan portal reprints (ARIA, no orderer printed) are encounter-less: their
> `encounter_id` is NULL and the tool states there is no ordering provider on record rather than
> inventing one.

### 3.5 Supersession — a new, status-aware measurement exception (100% security path)

The default per-kind policy stays: **`measurement` facts never auto-supersede** — two platelet
counts from two draws are both true and accumulate (verified: `supersession.decide()`'s
`event/measurement` branch at line 453 inserts a new row and, on a same-`valid_from` value clash,
files `fact_conflict` — it never supersedes today). The one exception below is **new deterministic
code in `supersession.decide()`** — a security-critical component under the 100%-coverage gate,
scheduled as its own wave (§10).

**The in-flight signal — a NEW per-fact field, `fhir_status` (not a mirror of `correction`).** The
parser tags each measurement candidate with the incoming **FHIR report status** (`registered|
preliminary|final|amended|corrected|cancelled|entered-in-error`). This is a **genuinely new field**;
it is **not** copied from a `correction` field on `IntentFact`, because — verified — `IntentFact`
(`intent.py`) has no `correction` field. `correction` originates *later*, as a per-fact flag that
`plan_intent()` computes (gated on `surface_attested`) into `PlannedFact` (`arbiter.py:70`), then
carries onto `ExtractedFact` (`extraction.py:96`) and `Candidate`. `fhir_status` therefore
**originates on `IntentFact`** (a new field) and, from `PlannedFact` onward, travels the *exact* links
`correction` already travels. Default `None` everywhere ⇒ every non-lab caller is byte-for-byte
unaffected:

1. `IntentFact` (`analysis/intent.py`) — add `fhir_status: str | None = None` **(new field; there is
   no `correction` field here to mirror — the parallel with `correction` begins at the next hop).**
2. `plan_intent` / `PlannedFact` (`analysis/arbiter.py:62/70`) — carry `fact.fhir_status` onto
   `PlannedFact` **beside the computed `correction` flag**; no weighing on it (lifecycle metadata, not
   a confidence signal).
3. `_to_extracted(fact, weight, *, correction, fhir_status=fact.fhir_status)` (`arbiter.py:580`, called
   at 657) — pass it onto the rebuilt `ExtractedFact`, exactly as `correction=` is passed today.
4. `ExtractedFact` (`analysis/extraction.py:96`, beside `correction: bool = False`) — add
   `fhir_status: str | None = None`.
5. The `Candidate(...)` construction in `_apply` (`pipeline.py:~2008`, beside `correction=fact.correction`)
   — add `fhir_status=fact.fhir_status`.
6. `Candidate` (`supersession.py:232`) — add `fhir_status: str | None = None`.
7. `decide()` (`supersession.py:403`) — the new transition helper, invoked **before** the
   idempotency-refresh short-circuit (below).

The `EmrImporter` (§6.6) is the only populator of `fhir_status`; the LLM Integrator never sets it (it
stays `None`, hitting the unchanged accumulate/`fact_conflict` path).

**The transition runs BEFORE the idempotency-refresh short-circuit.** Verified: `decide()`'s
idempotency refresh (`supersession.py:418–429`) returns `refresh_id` for an identical-value,
same-validity **accumulating** fact *before* the `event/measurement` branch at 453 — so a same-value
correction (status `final`→`corrected`, value unchanged) would short-circuit and never transition. The
status transition is therefore a helper `_lab_status_transition(candidate, live)` invoked right after
`_interval_close` and **before line 418**:

```
_lab_status_transition(candidate, live) -> Decision | None
  # inert for every existing caller:
  if candidate.kind != "measurement" or candidate.fhir_status is None: return None
  peers = [e for e in live if e.status in ("active","pending_review")
                          and e.valid_from == candidate.valid_from]   # same draw ⇒ same qualifier ⇒ same valid_from
  ...branch on candidate.fhir_status...
```

| Incoming `fhir_status` | prior head at same `valid_from` | Decision on the `value` fact |
|---|---|---|
| `corrected`/`amended` | active / pending peer(s) | **insert active; supersede active peers, hold pending peers — even when the value is identical** (runs before idempotency, so a status-only correction still transitions) |
| `corrected`/`amended` | none | insert active (behaves as a first `final`) |
| `preliminary` | any | insert `pending_review` (not yet a citable current value) |
| `final` | a `pending_review` preliminary peer | **insert active; supersede the preliminary** (finalization) |
| `final` / `None` | new draw (distinct `valid_from`) | helper returns `None` → **unchanged** path → insert active, **accumulate** |
| `final` / `None` | same qualifier, identical value | helper returns `None` → **unchanged** idempotent upsert-in-place |
| `final` / `None` | same qualifier, different value, prior `final` | helper returns `None` → **unchanged** same-`valid_from` clash → **`pending_review` `fact_conflict`** |
| `cancelled`/`entered-in-error` | peer(s) | insert `retracted`; supersede active peers, hold pending — the reading is **withdrawn** |
| `registered` | any | helper returns `None` (dormant; no such data in corpus) |

The helper reuses only the **shipped** `Decision` fields (`insert`, `insert_status`, `supersede_ids`,
`hold_ids` — as the existing correction path at 443–451 does). Because "same draw ⇒ same qualifier ⇒
same `valid_from`," a correction supersedes precisely the prior reading of *that* draw; a genuinely new
draw has a different `valid_from`, never enters the peer set, and accumulates unchanged.

**Report status is a PROJECTION DERIVATION, not a stored sibling fact.** There is deliberately **no**
separate `reportStatus` fact. A sibling attribute fact would be resolved by its **own** identity key
through `decide()`'s attribute branch (`supersession.py:475–494`) → `attribute_collision`, holding both
old and new status in `pending_review`; the single measurement-branch transition **cannot** touch a
sibling attribute-kind fact, so no "lockstep" is possible. Instead the **value fact's own lifecycle IS
the durable, shape-enforced, arbiter-owned record of status**, and §4.1 derives the projection's
`report_status` from it:

- active `value` fact, **no** superseded predecessor at its qualifier → `final`
- active `value` fact that is the **head of a supersession chain** (≥1 superseded predecessor at the
  same qualifier) → `corrected` — the *only* way a `value` fact acquires a superseded predecessor on
  this pipeline is the corrected/amended transition above, so the equivalence is exact. (FHIR
  `corrected` and `amended` are **collapsed to `corrected`** — a documented, safety-neutral
  simplification: the safety-relevant fact is "this value was revised, see current.")
- superseded predecessor → projected as a **second row**, `is_current=false`, `superseded_by_id` set
  (its own `report_status` stays `final` — what it was reported as; the reader sees it was replaced)
- `pending_review` → `preliminary`, `is_current=false` (a same-instant `fact_conflict` reading is
  likewise not-current and fires the currency flag; the review inbox distinguishes the two — the
  projection label is secondary to `is_current`)
- `retracted` (cancelled / entered-in-error) → the row is **dropped**

The in-flight `fhir_status` field is used **only** at decision time to select the branch; it is never
persisted, because the **result** of the decision (the value fact's lifecycle `status` + the
`superseded_by` chain) durably and shape-safely encodes it. `report_status` is thus re-derived from
**enforced** signals (the 4-state lifecycle enum + the chain), never from free text and never from a
collision-prone sibling — the idempotency invariant of §4.1.

A corrected potassium must never be quoted as current — this transition (active head = the correction;
predecessor superseded) is the tooth behind that guarantee (§7.3), and the §9 tests pin every
transition: corrected-**same-value**-still-transitions (proves the branch runs before the idempotency
short-circuit); corrected-different-value → supersede; new-draw → accumulate; same-qualifier
disagreeing-`final` → `pending_review` `fact_conflict`; `preliminary` → `pending_review` then `final`
→ promote; `cancelled` → `retracted`/dropped; **`None`-status → byte-for-byte unchanged** — plus the
projection's lifecycle-derived `report_status` re-derivation.

### 3.6 Provider entity resolution + the location firewall (defense in depth, not a single floor)

**Providers resolve under a health-only RLS scope.** The shipped resolver keys on
`lower(canonical_name)` + `summary_embedding`, **not on domain**, so name-matching a provider onto
an existing *general*-domain `Person` (a doctor who is also a personal contact) is the default —
and that would leak "the owner is this doctor's patient" into a general-scope reader. The EMR
pipeline therefore resolves/mints provider entities inside a session whose
`domain_scopes = {health}`: RLS hides every general-domain `Person`, so the resolver **cannot see**
the general contact and is forced to mint (or re-match) a **health-domain `Person`**. Domain-local
minting is a consequence of the scope, not an assertion. Optional owner-invoked unification is a
**health-stamped `sameAs`** fact (written only behind a future health-scoped merge tool, never by
the parser) — invisible under a general-only scope, so the patient relationship stays firewalled.
This is a conscious isolation-over-deduplication tradeoff.

**The location firewall — and why the domain floor is NOT the backstop here.** Two *independent*
shipped mechanisms exist: `extraction._DOMAIN_BY_PREDICATE` floors only *precise geo*
(`geocoordinates`, `latitude`, `longitude`, `gpscoordinates`) to `location`; separately, the
`Located` facet marks its predicates 🔒 location — **the `address` predicate (structured postal
address) and the `geo` predicate (`shape: geo`)** — and `appointment_projection` splits `address`
to a sidecar *because of that facet mark*. `person.yaml` composes `Located`, so a provider **can**
carry an `address` *or* `geo` fact, and EMR headers routinely print facility/provider postal
addresses.

**Critically, the `domain_floor` does not ratchet a health note.** Verified at `pipeline.py:220` /
`extraction.py:190`, `domain_floor` only ratchets when `note_domain == 'general'` (general →
restricted); on a **health** note the computed domain is `note_domain` itself, so an `address`/geo
fact **stays `health`** rather than being pushed to `location`. There is therefore **no floor
backstop** on this pipeline — the firewall is instead enforced by two deliberate layers so parser
stripping is never a single point of failure:

- **Layer 1 — parser strips postal addresses and geo.** Facility and provider *names* ride the
  health row (`serviceProvider`/`attender` edges, `facility`/`provider_name` projection columns —
  names are not whereabouts). Any postal *address* or geo coordinate is dropped at parse time; the
  parser never emits an `address` or `geo` fact. The ward is `careUnit` (plain health text), never
  the `location` canonical, so it can never bind the Located floor or trigger a cross-domain split.
- **Layer 2 — an integration-time firewall guard (belt-and-suspenders).** Before `EmrImporter`
  lowers a candidate to an `IntegrationIntent`, a deterministic assertion **rejects/holds any fact
  whose predicate is in the location-lock set** — **the union of the `Located` facet predicates
  `{address, geo}` and the floor dict `{geocoordinates, latitude, longitude, gpscoordinates}`** —
  **when its subject entity kind is a health EMR entity** (`Observation`/`encounter`/`Person`/
  `Organization`/`MedicalCondition`). Because such a fact should never exist on this path, the guard
  routes it to a `low_confidence` review card (`subkind=firewall_address`) anchored to the chunk and
  **never commits it**. Building the set as that explicit union closes the earlier draft's gap (a
  stray `geo` fact, whose predicate is *not* in the floor dict, would otherwise have slipped the
  guard). A single parser miss thus cannot silently plant location-domain whereabouts in the health
  domain — the guard catches it, and (should a facility address ever legitimately be needed) it is
  added deliberately as a location-domain `Place` reference / sidecar on the `appointment_projection`
  split pattern, never as a health-entity fact.

A §9 test asserts a parsed facility/provider address never lands as a fact on a health entity, that
the Layer-2 guard alone (parser stripping disabled) still holds an `address` fact out, **and that a
`geo` fact on a health entity is caught by the same guard**.

### 3.7 Patient-level attributes live on the "Me" entity

`bloodType → {"abo":"O","rh":"positive"}` and `birthDate` are **attribute** facts on the stable
"Me" entity, not per-encounter. Asserting them per-encounter would fragment them across distinct
entities so that a cross-source disagreement (OneContent `O+` vs a mis-OCR'd ARIA `A+`) could never
collide. On "Me", both land on `(Me,'bloodType','')` and surface as a real `attribute_collision`
review card, held `pending_review`, never auto-superseded — exactly the transfusion-safety property
the corpus needs.

### 3.8 Type-def changes (YAML edits + `sync_predicates`, NOT migrations)

Schema-registry additions load from YAML at startup and seed `app.canonical_predicates` via the
runtime **`sync_predicates` job** (`PredicateEmbedder.sync_predicates`), which inserts+embeds seed
rows (`origin='seed'`) through the owner-gated INSERT policy and the adapter's `EmbedClient` — the
only legitimate place embeddings are computed (#1). **There is no predicate-seed migration** (a
migration has no app context or adapter and could not embed without violating #1):

1. **Activate `lab_result.yaml`** (`[proposed]` → built) with **two declared vocabulary edits:**
   (a) **remove `Lifecycle` from the composed facet list** — its `status` is `functional`,
   one-per-entity, and inapplicable to entity-per-analyte (§3.2); report status is derived in the
   projection from the value fact's lifecycle, so **no replacement status predicate is added**;
   (b) **rename `category` → `observationCategory`** to resolve the real `product.category` (text)
   global-seed collision (§3.4). The *reading* vocabulary (`value`, `referenceRange`,
   `interpretation`, `specimen`, `effectiveDate`, `identifier`, `observationCategory`, `performer`,
   `component`) is otherwise unchanged; encounter linkage and orderer are owned by `encounter.yaml`
   (§3.4). The already-shipped `component` predicate is the BP/vital-signs hook, whose *storage* is
   ready but whose *projection* is out of scope this phase (§3.2, §4.1). `status_values` is retained
   only as the documented FHIR-status vocabulary the parser maps into `fhir_status` (§3.5).
2. **New `encounter.yaml`** with the §3.4 predicates (collision-audited names).
3. **New `medical_condition.yaml`** (`facets: [Named, ExternalIdentified, Temporal, Related]`),
   `identifier` using `qualifier_vocab: id_scheme` so `identifier.icd10` reads as a diagnosis code.
   (`identifier` is `scalar`/`attribute` in every def that declares it — no shape collision.)
4. **`_meta.yaml` — OPTIONAL/cosmetic only.** Verified: `_meta.yaml`'s INVARIANT is "storage never
   gates a qualifier … open schemes are accepted," and `id_scheme` already lists 18 schemes.
   `identifier.icd10` on a `MedicalCondition` therefore **validates today with no edit**. Adding
   `icd10` to the `id_scheme` list is a *prompt-digest nicety* (it seeds the vocabulary hint the LLM
   sees), **not** a correctness requirement — do it if convenient, skip it with no functional impact.

**Arbiter code change (Wave 1/2, with its own tests):** the status-aware measurement-supersession
helper `_lab_status_transition` invoked before the idempotency short-circuit in `supersession.decide()`
(§3.5), gated on the new `fhir_status` intent field so every non-lab caller is byte-for-byte unaffected.

---

## 4. Projection read-models (with DDL)

Two read-models re-derived from **active facts**, idempotent on re-analysis, built exactly like
`appointment_projection.py`: after a note's facts settle, the projector re-derives each touched
entity's state and upsert-or-deletes, **inside the caller's RLS-scoped transaction** (atomic with
the graph write). They are **not** sources of truth (#7). Because the whole corpus is `health`, no
cross-domain sidecar split is needed for the medical fields — but the projector **copies
`domain_code` from the fact, never hardcodes `'health'`** (defensive, matching the appointment
pattern), and the one leak-prone field (provider/facility address) is kept off the row by §3.6.

**No projection-to-projection FK (the ordering-hazard fix).** A projection is re-derived per
note-apply, and the plan commits **one `IntegrationIntent` per grouping unit in its own transaction**,
so a lab-results row can be built before — or in a different committed batch than — the encounters
projection row for its stay. The appointment projector deliberately references `app.entities`, never
a sibling projection row, to avoid exactly this. This plan follows suit: **`lab_results.encounter_id`
and `encounters.part_of_id` reference `app.entities(id)`** (the encounter *entity* always exists in
the graph before any projection row, because the fact write precedes projection in the same apply),
**not** a sibling projection PK. The tool resolves the encounter projection *row* softly by
`entity_id` at read time. This makes both cross-batch inserts and `is_current` re-projection churn
(delete/reinsert of encounter rows) safe — a dependent lab row can never dangle against a transiently
absent projection PK.

**Projector ordering within a call (belt-and-suspenders).** `project_emr` materializes **all
`encounter`-kind rows before any `observation`-kind rows** in every invocation, and on re-projection
deletes observation rows before re-deriving them, so even the soft `entity_id` references resolve
against freshly-present entities and the sidecars (which *do* reference `encounters(entity_id)`, their
same-call parent) are never orphaned.

**Cost guard on the shared apply path (mirrors the appointment projector).** Verified,
`project_appointments` is called inside `_apply` (`pipeline.py:1011`) for **every** note and guards
itself with a single up-front `func.lower(Entity.kind).in_(_APPOINTMENT_KINDS)` filter, so a
non-appointment note costs one empty `SELECT`. The new `project_emr` projector is wired into `_apply`
the same way and **must open with the same kind-filter** — `func.lower(Entity.kind).in_({'observation','encounter'})`
— so every non-EMR note ingested system-wide pays exactly one empty kind-filtered `SELECT`, not a
pair of `lab_results`/`encounters` scans. This is a required part of the wiring, not an optimization.

### 4.1 `app.lab_results` — migration **0115** (one row per *reading*)

The projector groups the per-draw facts by `(entity_id, qualifier)` — i.e. by draw — assembling a
row from the fan (`value`→`value_num`/`unit`, `referenceRange`→bounds, `interpretation`→flag,
`specimen`→id), joins the analyte-constant `identifier`/`observationCategory` off the same
`entity_id`, resolves `performer` to the performing-lab name, reaches the orderer + `encounter_id`
via the `hasObservation` edge from the enclosing Encounter (NULL for orphan portal labs), and
**derives `report_status` from the `value` fact's lifecycle + supersession chain** (§3.5).

**One row per `value` *fact*, not per draw** — a corrected draw has two `value` facts at the same
qualifier (a `superseded` predecessor and its `active` head, §3.5), and §7.3 renders **both**. The
unique key therefore includes `source_fact_id`; a **partial unique index** re-imposes "exactly one
current reading per draw."

```sql
CREATE TABLE app.lab_results (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id        uuid NOT NULL REFERENCES app.entities(id) ON DELETE CASCADE,  -- the analyte entity
    analyte          text NOT NULL,                                -- entity canonical_name (display)
    loinc            text,                                         -- analyte-constant `identifier` fact; NULL when unmapped
    value_num        double precision,                             -- `value`; NULL for non-numeric
    value_text       text,                                         -- verbatim reading ("POSITIVE", "few")
    unit             text,                                         -- UCUM, from `value`
    ref_low          double precision,
    ref_high         double precision,
    ref_text         text,
    interpretation   text CHECK (interpretation IN
        ('normal','high','low','abnormal','critical','borderline','indeterminate')),
    collected_at     timestamptz NOT NULL,
    specimen_id      text NOT NULL DEFAULT '',                     -- accession; '' when OCR-unreadable (closes the NULL-distinctness hole)
    performing_lab   text,                                         -- performer Organization NAME only (no address; §3.6)
    orderer          text,                                         -- ordering provider name via hasObservation→Encounter→attender[ordering]; NULL when none
    encounter_id     uuid REFERENCES app.entities(id),             -- the encounter ENTITY (not a projection row); NULLABLE for orphan portal labs
    report_status    text NOT NULL DEFAULT 'final'                 -- DERIVED from the value fact's lifecycle + supersession chain (§3.5); never a stored fact
        CHECK (report_status IN
            ('registered','preliminary','final','amended','corrected','cancelled','entered-in-error')),
    is_current       boolean NOT NULL DEFAULT true,                -- false when the backing `value` fact is superseded/pending
    superseded_by_id uuid REFERENCES app.lab_results(id),          -- the reading that replaced this one
    source_note_id   uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
    source_fact_id   uuid NOT NULL REFERENCES app.facts(id),       -- the `value` fact this row projects
    domain_code      text NOT NULL DEFAULT 'health' REFERENCES app.domains(code),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz,
    UNIQUE (entity_id, collected_at, specimen_id, source_fact_id)  -- one row per READING of a draw
);
CREATE UNIQUE INDEX lab_results_current_draw_idx                   -- exactly one CURRENT reading per draw
    ON app.lab_results (entity_id, collected_at, specimen_id) WHERE is_current;
CREATE INDEX lab_results_series_idx    ON app.lab_results (entity_id, collected_at DESC);
CREATE INDEX lab_results_abnormal_idx  ON app.lab_results (collected_at DESC)
    WHERE interpretation IN ('critical','high','low','abnormal');

ALTER TABLE app.lab_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.lab_results FORCE  ROW LEVEL SECURITY;
CREATE POLICY lab_results_domain ON app.lab_results
    USING (app.has_domain_scope(domain_code)) WITH CHECK (app.has_domain_scope(domain_code));
GRANT SELECT, INSERT, UPDATE, DELETE ON app.lab_results TO jbrain_app;   -- projections re-derive, so DELETE is granted
```

> **Multi-component activation delta (deferred, §3.2).** To support BP-style results the projection
> would add a `component_code text NOT NULL DEFAULT ''` column **inside both the `UNIQUE (…)` key and
> the `lab_results_current_draw_idx` partial unique index**, and the projector would read `component`
> facts (one row per component, `value_num` sourced from the component fact). Until that delta lands,
> a component-bearing entity is intentionally **not** projected — the code path is out of scope, not a
> silent no-op.

`report_status` is **re-derived purely from the `value` fact's enforced lifecycle + supersession
chain** (the idempotency invariant — never parsed from a string, never a sibling attribute fact): the
current row projects `final` for a lone active fact or `corrected` for an active chain-head; a
superseded predecessor projects a **second row** (`is_current=false`, `superseded_by_id` set,
`report_status='final'` — what it was reported as); a `pending_review` fact projects `preliminary`
(`is_current=false`); a `retracted` (cancelled/entered-in-error) fact's row is **dropped**. Accumulating
distinct draws each get their own current row. The projector produces the documented subset
`{final, corrected, preliminary}` (the CHECK is a superset — retracted rows never materialize). It is
**never** guessed from the 4-state lifecycle enum alone in isolation — the chain distinguishes
`final` from `corrected`. `specimen_id NOT NULL DEFAULT ''` closes the NULL-distinctness hole so
OCR-unreadable draws collide-or-not purely on `collected_at`, identical to the fact qualifier.

### 4.2 `app.encounters` (+ two sidecars) — migration **0116**

```sql
CREATE TABLE app.encounters (
    entity_id       uuid PRIMARY KEY REFERENCES app.entities(id) ON DELETE CASCADE,
    class           text,                                          -- inpatient|emergency|ambulatory|observation
    facility        text,                                          -- serviceProvider Organization NAME (no address; §3.6)
    care_unit       text,                                          -- careUnit (ward) — health, not location
    admitted_at     timestamptz,                                   -- period valid_from
    discharged_at   timestamptz,                                   -- period valid_to (NULL = ongoing)
    los_days        integer,                                       -- projector-computed whole-day floor; NULL while ongoing
    disposition     text,
    part_of_id      uuid REFERENCES app.entities(id),              -- partOfEncounter: the ENTITY of the episode's first encounter (not a projection row)
    source_note_id  uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
    domain_code     text NOT NULL DEFAULT 'health' REFERENCES app.domains(code),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz
);
CREATE TABLE app.encounter_providers (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    encounter_id  uuid NOT NULL REFERENCES app.encounters(entity_id) ON DELETE CASCADE,  -- same-call parent (§4)
    provider_id   uuid NOT NULL REFERENCES app.entities(id),       -- the health-domain Person
    provider_name text NOT NULL,                                   -- name ONLY (no contact PII; §3.6)
    role          text,                                            -- attending|ordering|pathologist|collecting_rn
    domain_code   text NOT NULL DEFAULT 'health' REFERENCES app.domains(code),
    UNIQUE (encounter_id, provider_id, role)
);
CREATE TABLE app.encounter_diagnoses (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    encounter_id  uuid NOT NULL REFERENCES app.encounters(entity_id) ON DELETE CASCADE,  -- same-call parent (§4)
    condition_id  uuid NOT NULL REFERENCES app.entities(id),       -- the MedicalCondition
    icd10         text,
    label         text NOT NULL,
    domain_code   text NOT NULL DEFAULT 'health' REFERENCES app.domains(code),
    UNIQUE (encounter_id, condition_id)
);
-- Each carries the standard RLS quartet (ENABLE+FORCE, has_domain_scope USING/WITH CHECK, grants).
```

`part_of_id` points at `app.entities(id)` (the sibling encounter *entity*), not at `encounters`, so a
transfer's second segment never dangles against a not-yet-projected first segment; the tool joins to
the projection row softly. The sidecars reference `encounters(entity_id)` because `project_emr`
materializes the parent encounter row **before** its providers/diagnoses within the same call (§4).
`los_days` is computed in the projector (Python) for clean NULL-handling while ongoing. Transfusions
read from the encounter's `transfusion` events into an ephemeral read cache when `read_encounters`
expands one; a facility transfer reads as one continuous hospitalization via `part_of_id`.

---

## 5. Security & RLS

Medical data ⇒ `domain_code='health'` on every entity, fact, mention, derived chunk, and
projection row (#3). Enforcement is Postgres RLS on the domain-scope GUC. Every new
**domain-scoped** table (`lab_results`, `encounters`, `encounter_providers`,
`encounter_diagnoses`) ships `ENABLE`+`FORCE ROW LEVEL SECURITY`, the shipped
`has_domain_scope(domain_code)` policy with a `WITH CHECK` mirror, grants including DELETE
(projections re-derive), and an isolation test — the migration-0006 pattern verbatim.

**`app.canonical_predicates` is NOT one of these tables.** It is global cross-domain vocabulary
(no `domain_code`, `USING (true)` read, owner-gated write), seeded by `sync_predicates` (§3.8) —
**no predicate-seed migration, no domain isolation test.** This is *why* the §3.4 collision audit
matters: global seed-dedup silently resolves a shape-divergent name collision against the
lexicographically-first `(value_shape, kind)` — the exact `category`/`observationCategory` hazard.

**Subject scope is provenance, not enforcement — and the plan says so.** The policies are
`has_domain_scope(domain_code)` only; there is **no** subject predicate or subject-scope GUC on
these tables. The corpus is a single patient with a single owner-principal, so — unlike the
Phase-7 location plan, which needed a subject-pin for non-owner device-key writers — the domain
firewall alone is the tooth here. Cross-subject isolation is a **named Phase-7 follow-on** (revisit
if guided intake ever ingests another subject's records) and is **not** asserted as a shipped
guarantee.

**RLS isolation test per domain-scoped table (security path → 100% coverage):**
1. owner-health-scoped session sees seeded rows;
2. a session scoped to `general`/`finance`/`location` (no health) sees **zero** and cannot
   read/insert/update them;
3. an unscoped session sees zero;
4. `WITH CHECK` blocks a cross-firewall insert/update;
5. for the sidecars, an EXISTS-join test that a scoped session cannot read a provider/diagnosis row
   whose parent encounter is out of scope.

**The retrieval consequence (the point of the firewall):** a session whose scopes omit `health`
retrieves **nothing medical** — `read_labs`/`read_encounters` return **empty**, and the health
search legs return zero (the enforced guarantee is empty-under-scope, not that the tool name
vanishes from the manifest). This is what lets a general agent answer "what's on my calendar"
without ever touching the medical record.

**PHI egress posture (honest).** The health firewall is a **storage/retrieval** boundary, not a
network one. Per ANALYSIS.md, dev-time cloud-LLM egress is a **recorded owner opt-in**, not an
accident; the adapter is the sole egress point (#1). The plan does **not** over-claim local-only.
**Recommended hardening (owner Wave-0 decision, not asserted as shipped):** pin the three
PHI-touching LLM calls (ARIA vision-OCR, provider disambiguation, pathology-narrative extraction)
to **local** task routes once local models are served, behind a **fail-closed egress guard** that
runs as the *first pipeline action* and **raises** (driving the run to an owner-visible `error`
row) rather than an `ActionSpec.precondition` (which would *defer* forever — the opposite of
fail-closed). Correct mechanics if elected: dedicated `emr.*` `TASK_DEFAULTS` keys pinned to
catalog-served local models (`qwen3-vl-30b-a3b` for vision, `gpt-oss-120b` for reasoning);
task-parametrize `OcrPipeline` (its shipped call hardcodes `vision.ocr`) so the local route binds
at the actual `router.complete` call; resolve the guard via the override-aware `effective_spec`.
Liveness (the local backend being resident) is separately handled by the shipped
`model_already_loaded` **precondition**, which correctly *defers*. **Egress raises; liveness
defers.** The parsers themselves are deterministic Python — zero LLM, zero egress — so the only
egress surface is these three guarded calls.

---

## 6. Ingest pipeline

The corpus enters through the **existing attachment dispatcher** (ANALYSIS.md: "structured
medical/financial documents are *detected and routed* rather than free-extracted"). This importer
**is** that typed-parser route, expressed as a **workflow-engine pipeline** (events → triggers →
pipelines → runs): **intake (one note + the encrypted zip + an inline password, §6.1) → unzip +
decrypt → extract → parse → normalize → dedup → integrate → project**, with idempotent re-run and
review-inbox routing for the messy tail.

### 6.0 Trigger — how a note enters the importer (detection ≠ parser fingerprint)

Two detections that the reader must not conflate:

- **The trigger (this section)** decides *whether a note runs the importer at all.* It fires on the
  **raw note, before anything is decrypted**, so it can key only on what's visible pre-decryption.
- **The parser fingerprint (§6.3)** decides *which per-source parser handles each PDF* (Epic /
  OneContent / athena / ARIA), on the **already-extracted pages** — a separate, post-decryption
  concern. It cannot gate the trigger, because those pages are locked inside the encrypted archive
  until the pipeline has already started and pulled the password.

**The trigger is an explicit, user-chosen marker — not a body-text guess.** JBrain's capture UI files
a Medical note to a **`destination`** (the `notes/medical/` mode's subcategory selector, shipped:
`frontend/src/notes/modes.ts`; Medical mode → `domain='health'`, options `Records`/`Labs`/
`Medications`/`Appointments`), persisted to the note's `destination` column. The `emr_import` trigger
(seeded `0118`) is a workflow-engine trigger on the note-created/attachment event whose condition is:

```
domain_code = 'health'
AND destination = 'notes/medical/Records'
AND the note carries an archive (application/zip) attachment
```

All three are readable on the raw note. This is deliberate over any implicit "a zip appeared"
heuristic: the import is **destructive** (§6.1 deletes the archive), so a stray encrypted zip attached
anywhere else — or a non-archive note under Records — must never trip it. The firewall is automatic
(Medical ⇒ `health`). *Reuse of the existing `Records` subcategory is chosen over a dedicated import
type: a zip under Records is already unambiguous, and it adds no UI; a dedicated type is a cheap
follow-on if non-import zips ever routinely land in Records.* A note that matches the domain +
destination but has **no** archive attachment is a normal Medical note and flows through ordinary
ingestion untouched.

### 6.1 Intake — normalize the note in place: decrypt, attach raw files, drop the zip + password

**The front door is a note, not server config.** The owner writes **one health-domain note** filed
under **Medical → Records**, attaches the **encrypted ZIP**, and states the password in plain language
— *"here is my zip of health records, password `xyz`."* The **`emr_import` trigger** (§6.0) fires on
it and the importer pipeline runs; nothing is configured out of band. The pipeline **normalizes the
note in place** — decrypts the archive, attaches the raw files, and removes both the secret and the
encrypted zip — so the end state is **one note carrying the unencrypted source files, no password, no
archive**.

- **Password extraction (held in memory only).** A deterministic matcher pulls the password from the
  note body (`password[:\s]+…`, `pw …`, `password is …`; multiple candidates are each tried at
  decrypt, §6.6). The raw secret lives **only** in the job's in-memory scope for the decrypt step —
  **never** written to a `chunk`, **never** embedded, **never** logged, **never** in `app.settings`.
  If no password parses, the run **fails closed** and files a review card (keeping the zip — see
  delete-last below).
- **Unzip — a new, hardened dependency.** The archive is AES-encrypted; Python's stdlib `zipfile`
  handles only legacy ZipCrypto, so extraction uses **`pyzipper`** (a new dependency → added to
  `scripts/dev-setup.sh` in the same PR, #8). Extraction is hardened against hostile archives —
  per-entry and total **uncompressed-size caps** (zip-bomb guard), **path-traversal rejection** (no
  absolute paths, no `..`), and a members-are-regular-files check — and runs in memory / through the
  storage abstraction, never onto a raw filesystem path (#2). A member that fails a guard is carded; a
  wholly-unreadable archive fails closed.
- **Attach the decrypted raw files to the same note.** For each contained PDF, **PyMuPDF** (already
  shipped) opens it and `doc.authenticate(password)`; the decrypted bytes are content-addressed by
  `BlobStore` and attached to **the same note** as an `app.attachments` row (health domain). No
  `pikepdf`/`ocrmypdf`/Tesseract is added — decryption and OCR ride shipped components; only
  `pyzipper` is new. **One note ends up carrying N decrypted files** — there is no per-file note
  explosion.
- **The record text is cited from the attachments, not the note body (#7).** Extraction (§6.2) chunks
  each decrypted attachment's text into `app.chunks` anchored by **`attachment_id` + `source_kind`
  (`text-layer`/`ocr`) + `source_anchor` (page)** — the shipped chunk schema already carries
  attachment-anchored chunks, so no note-body verbatim rendering is needed. Every fact cites one of
  those chunks, resolving back to the page of the actual decrypted record a human can open. The note
  **body** stays the owner's sentence (password scrubbed) and carries **no** extracted health facts.
- **Delete last, fail-closed (the safety-critical ordering).** Only **after** every PDF has decrypted,
  attached, and chunked successfully does the pipeline **(a)** delete the encrypted-zip attachment
  (`app.attachments` delete grant, `0005`/`0009`) and **(b)** scrub the password span from the
  persisted note body (→ `password [redacted]`) **before** that body is chunked/embedded, so the
  secret never reaches an index or the LLM. If any step fails, the run **keeps the zip and the
  original body, writes no facts, and files a review card** — an intake must never destroy the only
  copy on a failed run, and an empty import must never look like success. (This delete-and-scrub path
  is a security path ⇒ 100% coverage, §9.) Keeping the encrypted original is an **opt-in** for anyone
  who wants a decryption audit trail; the default removes it.
- **Idempotent re-feeding.** Each decrypted attachment is content-addressed on its **blob sha256**, so
  re-adding the same zip note (or re-running before the zip is dropped) upserts the same attachments
  and re-derives byte-identical facts rather than duplicating (§6.6).
- **Grouping units are NOT note or attachment boundaries.** The encounter/account grouping the parsers
  reconstruct (§6.3) drives *chunk anchoring* (chunk-per-page, §6.6) and *`IntegrationIntent` batching*
  (one intent per grouping unit), **not** the granularity of notes or attachments. A 216-page Epic
  file is one attachment with hundreds of page-chunks, and its facts cite the specific page-chunk they
  came from.

> **Rejected alternatives.** (1) *Env-backed password* — an earlier draft read the decrypt password
> from an env-backed `Settings` field (`emr_decrypt_password`); dropped because it forces out-of-band
> server config for a note-driven system and can't express "these files, this password" in one
> gesture. (2) *Explode into per-file child notes, keep the zip, redact the password in place* — a
> second draft minted one verbatim-body note per contained PDF and retained the encrypted archive;
> superseded by the in-place normalization above, which cites record text from the decrypted
> **attachments** (the shipped chunk schema anchors chunks by `attachment_id`), so it needs no
> per-file note and can delete the zip and the secret outright rather than merely redacting them.

### 6.2 Extraction — text layer + vision-OCR

Registered on the dispatcher (`ingest/extract.py`), returning provenanced `Segment`s:

- **Text-layer PDFs (Epic, athena):** the shipped `PdfTextLayerExtractor` (PyMuPDF
  `get_text("text")`, verified `ingest/extract.py:70`) yields reading-order text, sufficient for
  Epic's line-oriented encounter headers and athena's label→value blocks.
- **OneContent's fixed-width tables — do NOT assume `text` mode preserves column offsets.**
  `get_text("text")` returns reading-order text with **reflowed/normalized whitespace** and does
  **not** guarantee that fixed-width columns land at stable character offsets. The 82-page account-keyed
  cumulative tables are the hardest parse in the corpus, and ruler-derived *character-offset* slicing
  over `text` output is likely to misalign. **Wave 0/3 validates `get_text("text")` column fidelity
  against real OneContent pages first.** If columns are not offset-stable (the expected outcome), the
  extractor is **extended to expose word x-coordinates** (`get_text("words")` — still PyMuPDF, no new
  dependency) and the OneContent parser **slices columns by x-geometry, not character offset.** The
  parser interface is written to accept either a character-ruler or a geometry-ruler so the OneContent
  path can switch to geometry without touching Epic/athena. This is an explicit go/no-go gate, not an
  assumption.
- **Scanned PDF (ARIA — zero text layer):** each page renders to an image
  (`ingest/imageprep.py`) → the **vision-LLM OCR job** via the adapter (ANALYSIS.md: "Vision-LLM is
  the first OCR backend; Tesseract remains a later config option"). The OCR row lands in
  `app.attachment_extracts` (`kind='ocr'`, `confidence = OCR_CONFIDENCE = 0.7` — load-bearing for
  dedup, §6.4); the analysis gate already waits for outstanding OCR before integrating. Vision-OCR
  yields linear text (no word boxes), so the ARIA parser is **line-oriented** and leans on the
  text-layer OneContent file as the authoritative column structure (ARIA is a reprint of those same
  tables). The OCR text lands as the ARIA attachment's `ocr` extract, chunked and cited by
  `attachment_id` (§6.1) — not a note body.

### 6.3 Per-source parsers (pure Python, no LLM)

A parser is selected by a **fingerprint** on the first extracted pages (fail-closed: no confident
match → the whole file routes to review, never free-extracted). Each is a pure function of the
decrypted text/segments emitting typed candidates (`CandidateObservation`/`CandidateEncounter`) with
**page/line provenance anchors into its source file's attachment** (§6.1) — parsers do *not* mint
separate notes; they anchor facts to the `chunk_id`s of that decrypted attachment's chunks. Each candidate
carries a `source_system`, a `fidelity` rank (text-layer > OCR), the FHIR `status` (→ the in-flight
`fhir_status`, §3.5), its deterministically-minted `effectiveDate` temporal token (§3.2), and the
§3.3 dedup address. **Parsers strip postal addresses and geo** (§3.6, Layer 1) and never emit an
`address`/`geo` fact. The candidate output may be cached as an `attachment_extracts` row of
`kind='emr_parse'` (migration 0114) for re-runnable stages.

| Parser | Fingerprint | Grouping spine | Key challenge |
|---|---|---|---|
| **Epic** | `\d{2}/\d{2}/\d{4} - .+ in .+` headers | encounter header + **mode of `Adm/DC` banners** | banner-bleed; `inpatient = Σ(adm_dc) > Σ(visit)`; component value/flag columns; transfusion orders → `transfusion` events keyed by order id; the pathology narrative kept as prose (§6.5) |
| **OneContent** | `KEY FOR ABNORMAL COLUMN`, `Account:` | **ACCOUNT number** (`C…`) | **x-geometry column slicing** (§6.2, not char-offset); each row's `collected_at` sets `valid_from`; abnormal-flag legend → `interpretation` |
| **athena** | `Specimen/Accession ID` blocks | per-analyte block | label→value pairs; `Ordering Provider` → ambulatory Encounter's `attender[ordering]`; a cancelled `RESULT NOTE` sets `status=cancelled` and suppresses the value |
| **ARIA** | (post-OCR) portal-print header | OCR'd result blocks | line-oriented; every fact `confidence=0.7`; expected to dedup against 2021 OneContent (§6.4) |

Providers/diagnoses/facilities are minted under the **health-scoped resolution session** (§3.6) and
bound by deterministic edge linking (`link_relationship_objects`) — a header name binds to the
`Person` the parser already named, never a hallucinated one.

**Analyte canonicalization (load-bearing for dedup).** Different systems print the same test under
different labels (`WBC`/`Leukocytes`/`White Blood Cell Count`), and OCR adds noise, so the raw
label is not a stable key. Normalization resolves each analyte to a **canonical analyte code** via
(1) a bundled curated **LOINC subset** for the common CBC/CMP/coag/UA panel (unmapped ⇒ `loinc=null`,
never a guessed code), then (2) a small curated **synonym map**. The canonical code — not the label
— is what the entity identity and §6.4 key on; an analyte resolving to neither is keyed by its slug
**but flagged** so it can't silently masquerade as dedup-eligible.

### 6.4 Cross-source dedup — candidate-stage reconciliation is the mechanism; the qualifier makes it idempotent

The 2021 labs recur across OneContent and ARIA. The dedup **enforcer is the candidate-stage
reconciliation, a pre-graph pure-Python step**; the §3.3 qualifier is *not* itself the cross-source
matcher — the same physical draw renders as a precise datetime + accession in OneContent and a
date-only, specimen-less OCR read in ARIA, i.e. as **different** raw qualifiers. They converge only
because reconciliation forces their `collected_at` (and specimen, where the OCR read supplies one)
equal *first*. So all real cross-source dedup rests on this reconciliation; the qualifier then makes
the reconciled match idempotent at the graph.

1. **Candidate-stage reconciliation (pre-graph, pure Python) — the enforcer, with a strengthened
   contract.** A date-only OCR candidate is matched to a precise candidate only when **all** hold:
   (a) **same canonical analyte code** (§6.3, not raw label); (b) the OCR `collected_at` date falls
   within a **widened same-day window** of the precise draw (calendar day of the precise draw, ±1
   day to absorb midnight/timezone drift on a portal reprint); and (c) **values agree within a
   per-analyte tolerance** (exact for integer counts; a small relative epsilon for float chemistries)
   — a value that disagrees beyond tolerance is treated as a *different or misread* reading, never
   silently merged. On a confirmed match, reconciliation **adopts the precise source's timestamp and
   specimen** (the OCR read corroborates and dual-cites) before minting the §3.3 qualifier; the
   winner at equal precision is chosen by `fidelity` (text-layer > OCR). If **no** matching precise
   draw exists in the window, or the **OCR timestamp itself is unreadable or is readable-but-wrong**
   (parses to a day with no compatible precise draw and no in-tolerance value), the read **parks in
   `pending_review` behind a `low_confidence` card** rather than guessing an address or minting a
   spurious time-series point. A readable-but-wrong OCR timestamp therefore cannot silently duplicate
   a draw or force a false supersession — it fails the tolerance/window match and routes to review.
2. **Graph identity (the qualifier) — the idempotency layer, not the matcher.** The *reconciled*
   candidate keys on `(entity_id, predicate, qualifier="<collected_iso>|<specimen>")`; the identical
   reconciled draw → identical qualifier → the arbiter **upserts in place**, one fact, dual-cited.
   Two **genuinely distinct** draws — even specimen-less ones — differ in `collected_at`, occupy
   different addresses, and both persist (§3.3). A readable-but-conflicting value at a matching
   qualifier routes to `fact_conflict` (§3.5). The naive `(analyte, collected_at, value)` heuristic
   is *not* the DB identity key and does not by itself prevent collisions — reconciliation upstream
   and the timestamped qualifier downstream jointly do.

### 6.5 The pathology narrative is prose, not facts

The bone-marrow surgical-pathology report is **not** field-parsed into hundreds of facts (the
ANALYSIS.md guard). It is part of the Epic file's health-domain note, chunked at section/paragraph
granularity, embedded, and FTS-indexed — searchable, cited, retrievable, but structurally inert. A
*small, high-confidence* set of facts is extracted from its "Final Diagnosis" line (a diagnosis edge
to a `MedicalCondition`), gated by model self-confidence; "rule-out" language stays `hypothetical`
(never a diagnosis fact). This one free-text extraction is the only LLM touch on the structured
path.

### 6.6 Integration, idempotency, batching, failure handling

- **Deterministic integration (no LLM for structured data).** Candidates do not write the graph
  directly: a new `EmrImporter` lowers them into `IntegrationIntent`s — the same object the LLM
  Integrator emits — populating the new `IntentFact.fhir_status` (§3.5) and running the Layer-2
  firewall guard (§3.6) before it drives the shipped deterministic core `plan_intent`
  (validates/weighs/partitions commit/review/reject) → `apply_intent` (resolves
  Person/MedicalCondition/Organization via `analysis/entities.py`; per-kind supersession incl. the
  §3.5 `_lab_status_transition` branch; RLS-scoped writes; then the kind-guarded projection hook, §4).
  `predicate_canonicalization` + value-shape validation run on this shared path, so `WBC`/`platelets`/`PLT`
  collapse to the canonical address. **Note the domain-floor caveat (§3.6): on a health note the floor
  does *not* push an address/geo fact to `location`; the firewall relies on parser stripping + the
  Layer-2 guard, not the floor.** The LLM extractor and Integrator are bypassed for structured
  candidates (nothing to infer); the arbiter is fully reused.
- **Batching & chunk anchoring (the 216-page-scale answer).** Each structured **page (or table
  block) is one chunk** within its source-file note; every fact from that region cites *that*
  chunk_id — hundreds of chunks anchoring thousands of facts, no chunk-per-row explosion. One
  `IntegrationIntent` **per grouping unit** (per inpatient Encounter, per outpatient visit /
  OneContent account) commits in **its own transaction**; a worker crash re-runs only unfinished
  intents. Because `encounter_id`/`part_of_id` reference `app.entities` (never a sibling projection
  row, §4), an out-of-order commit of a lab intent before its encounter intent cannot FK-fault — the
  encounter *entity* is written before its projection row within its own apply. Supersession lookups
  key on the `(entity, predicate, qualifier)` index — no O(n²) blowup (a load-shaped test guards
  this, §9).
- **Idempotency.** Each decrypted attachment is content-addressed on `(decrypted-blob sha256,
  parser-version)`; re-running produces the same attachment → the same identity keys → upserts, never
  duplicates. The
  deterministic `effectiveDate` temporal token (§3.2) re-mints to the same absolute instant, and
  `report_status` re-derives from the same value-fact lifecycle + chain, so re-projection is
  byte-identical. Citations survive; owner pins survive (auto-supersession may only re-flag a pinned
  fact). A parser-version bump is a planned, budgeted re-run.
- **Failure handling (nothing silently dropped).** A parse failure or a non-scalar micro/titer
  result files a page-anchored review card and does **not** fail the batch — other intents commit.
  A decrypt failure hard-fails and writes nothing. An OCR job that exhausts retries falls back to
  the shipped body-only path.
- **Malformed lab value (the shape-mismatch case).** The value-shape check runs on the shared
  integration path. Its documented default (`value_shape_enforce`) is *not* to destroy the fact —
  it nulls the structured `value_json` while the fact **survives on its `statement`**, so it is the
  projection's `value_num` that would be absent. For a possibly-critical lab that is unacceptable, so
  lab integration instead **raises a new `shape_mismatch` review card and holds the reading
  `pending_review` with its `value_json` intact** for owner correction — never degrading a critical
  number to a statement-only fact the projection can't chart.
- **Review-inbox mapping (minimize new kinds).** Every degraded outcome is a review card, mapped to
  an **existing** kind with a `subkind` payload discriminator, anchored to a durable row id (note /
  chunk / `attachment_extract` id — never page text, per the 0006 payload rule): unrecognized
  source / parse gap / dedup near-miss / OCR disagreement / stripped-address-or-geo firewall catch →
  `low_confidence` (`subkind=firewall_address`); two `final` disagreeing → `fact_conflict`; two blood
  types / birthdays → `attribute_collision`; ambiguous provider → `ambiguous_mention`/`merge_proposal`.
  The **only** genuinely new kind is `shape_mismatch` (migration 0117).

---

## 7. Retrieval & AI use

### 7.1 The two typed tools (markdown + YAML frontmatter, health-scoped, read-only)

Both read the **projections** on the RLS-scoped session (so a non-health scope returns nothing by
construction) and reconcile against the graph for currency — never quoting note prose as current
truth.

`backend/src/jbrain/agent/tools/read_labs.tool`:

```yaml
---
name: read_labs
version: 1
permission: read
params:
  type: object
  properties:
    analyte:
      type: string
      description: The lab test to read (e.g. "platelet count", "hemoglobin"). Omit to list recent results across all analytes.
    since:
      type: string
      description: ISO date; only results collected on or after it. Optional.
    until:
      type: string
      description: ISO date; only results collected on or before it. Optional.
    abnormal_only:
      type: boolean
      description: Only results flagged high, low, abnormal, or critical. Defaults to false.
    trend:
      type: boolean
      description: For a single analyte, return the full time-series oldest-to-newest to describe how a value moved. Defaults to false (most-recent first).
    limit:
      type: integer
      description: Maximum results (default 20).
  required: []
---
List the owner's lab results from their imported medical records — analyte, value with unit,
reference range, the abnormal/critical flag, collection date, the performing lab, and the id of
the encounter each draw belongs to — for one analyte or across all. Set trend:true with an analyte
to get the ordered time-series.

The ordering provider is NOT on the individual result; it belongs to the encounter the draw was
part of — pass encounter_id to read_encounters to see who ordered it. Many patient-portal labs
have NO enclosing encounter: encounter_id is then empty and there is simply no ordering provider on
record — say so rather than inventing one.

These are the readings the record CONTAINS, not a diagnosis or an assessment of what they mean.
Report the numbers, their reference ranges, and their flags; do not infer a cause, a condition, or
a recommendation — that is not what this record is for. Each result cites its source note id (pass
it to read_note for the full report).

A corrected result supersedes the earlier value for the same draw: superseded readings are marked
"corrected — see current" and carry the id of the reading that replaced them. When a result is
superseded, still pending review, or preliminary, say so plainly rather than presenting the number
as current. Only results the current session is scoped to see are returned; under a non-health scope
this tool returns nothing.
```

`read_encounters.tool` (mirrors `read_appointments`, `permission: read`): lists or expands hospital
admissions and clinical visits — class, facility, care unit, admit/discharge, derived length of
stay, disposition, the providers and their roles, the ICD-coded diagnoses, transfusion orders, and
the transfer chain (a stay that moved facilities reads as one continuous hospitalization); each
citing its source note id; read-only; empty under a non-health scope. Its prose likewise forbids
clinical interpretation: "the record of what happened and when — dates, places, people, coded
diagnoses as they appear in the source… leave any medical meaning to the owner and their
clinicians."

### 7.2 Search, currency, wiki, safety

- **Search:** hybrid dense (pgvector) + FTS fused with RRF, **always health-scoped**, over the
  narrative chunks (chiefly the pathology report and transfusion-indication prose). Structured labs
  live in the projection (a single indexed `(entity_id, collected_at)` scan answers a trend, not a
  350-page model read); the source-note chunks remain searchable and cite back to the graph. Under
  a non-health scope these chunks are invisible.
- **Currency flag (the safety-critical retrieval property) — broadened.** A search hit on a note
  whose prose quotes an analyte value carries the ⚠ currency flag (naming the analyte entity)
  **whenever the backing fact for that value is not the current, citable reading** — that is, whenever
  it is **`superseded`** (a `corrected`/`amended` result replaced it) **OR `pending_review`** (a
  same-instant `fact_conflict` between two disagreeing finals, or a `preliminary`-status reading not
  yet final). All three carry the same staleness/contested risk, so all three fire the flag; the agent
  then confirms current values via `read_entity`/`read_labs` before relying on note prose. `read_labs`
  renders **two rows** for a corrected draw (the "corrected — see current" predecessor with
  `superseded_by_id` set, plus the current head — distinct rows because the projection key includes
  `source_fact_id`), and marks a `pending_review`/`preliminary` reading as not-current. Ordinary
  time-series growth (two independent draws) does **not** fire the flag — the distinction is exactly
  what §3.5's status-aware transition and §3.3's qualifier mechanics enforce, keeping the flag meaningful.
- **Wiki (Phase-6-gated):** each `MedicalCondition` clearing the notability gate anchors a
  machine-written, fully-cited **health-domain section** on how that condition appears *in the
  owner's own record* — never a textbook definition presented as the owner's fact. The Phase-6
  grounding gate is the anti-synthesis firewall (every clause entailed by its cited same-domain
  health chunk; the entity graph wins on conflict; contested/`pending_review`/`preliminary` results
  held out). The health section is hidden from any out-of-scope principal. Humans correct via
  correction notes, never direct edits (#7).
- **Safety posture (binding):** record-keeping, not advice; diagnoses stored **only** as they
  appear (ICD-coded edges), never minted by the agent/wiki; critical results are *reported* as facts
  ("flagged critical on that date"), never alarmed into a clinical recommendation; a malformed value
  is held for review with its value intact, never silently nulled (§6.6); the firewall is a
  storage/retrieval boundary (not a network one until local models land — §5); provenance-or-silence
  (no citable health chunk ⇒ not emitted; an unknown analyte returns empty, not a guess).

---

## 8. Migrations

Type-def changes (§3.8) are YAML edits seeded by `sync_predicates`, **not** migrations.

| Migration | `down_revision` | Object | Notes |
|---|---|---|---|
| **0114_attachment_extract_emr_parse_kind** | 0113 | drop + re-add `attachment_extracts_kind_check` to admit `'emr_parse'` | **Verified: the CHECK is currently the four-value set `('ocr','caption','transcript','video_analysis')` — 0084 already widened it past the 0011/0079 baseline.** `up` re-adds `CHECK (kind IN ('ocr','caption','transcript','video_analysis','emr_parse'))`; `down` restores the **0084 four-value set** (`'ocr','caption','transcript','video_analysis'`) after deleting `emr_parse` rows — it MUST NOT narrow to the 0079 three-value set (that would drop `video_analysis` and break the next video ingest). Same PR: the new `pyzipper` import into `dev-setup.sh` (#8). **One new dependency — `pyzipper`** for AES-zip extraction; decryption + OCR still ride shipped PyMuPDF + vision-OCR. |
| **0115_lab_results_projection** | 0114 | `app.lab_results` + indexes + RLS quartet + isolation test | `specimen_id NOT NULL DEFAULT ''`; nullable `encounter_id` **→ `app.entities(id)`, not `app.encounters`**; `source_fact_id` in the unique key + the `is_current` partial unique index; `report_status` a DERIVED column (no backing status fact). |
| **0116_encounters_projection** | 0115 | `app.encounters`, `app.encounter_providers`, `app.encounter_diagnoses` + RLS quartet | projector-computed `los_days`; `part_of_id → app.entities(id)`; sidecars EXISTS-join isolation-tested. |
| **0117_lab_review_kinds** | 0116 | `ALTER … review_items` CHECK | **DROP and re-ADD from the CURRENT twelve-kind list (through 0034's `_BASE`) PLUS `'shape_mismatch'`** — following the `_BASE`/`_KINDS_WITH`/`_KINDS_WITHOUT` pattern; `down` re-narrows to twelve. **Must not rebuild from the 0006 baseline** (would silently drop `inverse_proposal, extraction_truncated, low_confidence_inference, new_predicate, confirm_entity`). Lands in Wave 1, before any code references the kind. |
| **0118_seed_emr_import_workflow** | 0117 | the `emr_import` **trigger** (§6.0: `domain='health'` AND `destination='notes/medical/Records'` AND an `application/zip` attachment) + pipeline + `manual` schedule for the EMR ActionSpecs | seeded **in the same PR** as the registered `workflow/registry.py` actions (else `DispatchResolutionError`); trigger-condition + pipeline-resolves tests required. |

No new fact/entity storage — labs and encounters land in the existing `app.facts`/`app.entities`.
`down`-migrations drop the projection tables (facts unaffected — re-projection rebuilds them).

---

## 9. Testing plan (meets the 80% / security-100% / real-Postgres / LLM-faked gates)

Real Postgres via testcontainers; **LLM/vision-OCR/embed calls faked**; clocks injected. The
encrypted PDFs are **never** in the test corpus — fixtures are hand-built **synthetic** Epic/
OneContent/athena text blocks + canned OCR text reproducing the layout hazards (banner bleed,
account grouping, fixed-width columns, OCR noise) without real PHI. 80% backend line coverage;
security paths (RLS + decryption + address/geo-stripping + supersession + PHI-egress if elected) at
**100%**.

- **Parser unit tests:** Epic banner-mode picks the correct `Adm/DC` when the first banner bleeds
  from the prior encounter; inpatient-vs-outpatient decision; the MICU→A3 **transfer** links two
  encounters `partOfEncounter`; OneContent account grouping + **x-geometry column slicing** (with a
  fixture whose `text`-mode offsets are deliberately reflowed to prove char-offset slicing would fail
  and geometry slicing succeeds) + abnormal-flag legend; athena suppresses a **cancelled** value and
  emits `attender[ordering]`; ARIA line-parse stamps `confidence=0.7`; **address AND geo stripping
  (Layer 1)**; each emitted `value_json` validates against its `value_shape`; the per-draw fan carries
  the timestamped qualifier while `identifier`/`observationCategory` carry the constant qualifier;
  **each draw mints a deterministic `effectiveDate` point token (`kind='point'`, `precision='instant'`,
  `capture_anchor=collected_at`) with no LLM.**
- **Note/attachment-model test:** a zip note yields **one** note carrying the N decrypted files as
  attachments; a fact's `chunk_id` resolves to an **attachment-anchored** page-chunk (`attachment_id`
  set, `source_kind IN ('text-layer','ocr')`) holding real record text; the encrypted zip attachment
  is gone and the body reads `password [redacted]`; a 216-page fixture is one attachment's chunks, not
  one-note-per-encounter.
- **Analyte canonicalization:** `WBC`/`Leukocytes`/`White Blood Cell Count` (+ an OCR-noisy variant)
  resolve to one canonical code + one entity; an unmapped analyte is flagged, not silently keyed.
- **Identity / dedup (record-integrity core):** the same 2021 draw from OneContent (precise
  datetime + accession) and ARIA (date-only, no accession) **reconciles at the candidate stage**
  (same canonical analyte + in-tolerance value + within the widened same-day window) and resolves to
  **one** fact on `(entity,'value',qualifier)`, dual-cited; a **readable-but-wrong OCR timestamp**
  (parses cleanly but to a day with no in-tolerance precise draw) **parks `low_confidence` rather
  than duplicating a point or forcing a false supersession**; a value disagreeing beyond tolerance
  at a same-day match is NOT merged; **two specimen-less draws at different collected times remain
  TWO facts** (the §3.3 hole-closing test); an analyte drawn N times has **one** `identifier`/LOINC
  and **one** `observationCategory` fact, not N; a same-qualifier different-value `final` pair →
  `fact_conflict`; value is **not** in the fact identity key (a correction supersedes, not duplicates).
- **Supersession / currency (safety core, 100%):**
  - a `corrected` result at a matching qualifier with a **different** value supersedes the prior
    `final` (predecessor `status='superseded'`, `superseded_by` set; correction active);
  - **a `corrected` result with an IDENTICAL value STILL transitions** (predecessor superseded, not
    idempotently refreshed) — the regression proving `_lab_status_transition` runs **before** the
    idempotency short-circuit at `supersession.py:418`;
  - the projection writes **two rows** (predecessor `is_current=false` + current head) and re-derives
    `report_status='corrected'` on the head **from the value-fact lifecycle + chain, never a string
    and never a sibling fact**; the partial unique index permits exactly one current row;
  - a note citing the old value surfaces with the currency flag;
  - two distinct draws **accumulate** (no supersession, no false flag);
  - `preliminary`→`pending_review`→`report_status='preliminary'` **and fires the currency flag**,
    then a subsequent `final` at the same `valid_from` **promotes** it (preliminary superseded, final
    active);
  - a same-instant `fact_conflict` reading **also fires the currency flag**;
  - `entered-in-error`/`cancelled`→`retracted` and the projection row **drops**;
  - `read_labs(trend:true)` returns the ordered series with the superseded point marked;
  - **a `None`-`fhir_status` (non-lab) candidate is routed through `decide()`'s unchanged
    `event/measurement` path byte-for-byte (regression guard — proves `_lab_status_transition`
    returns `None` immediately and the new code is inert for every existing caller).**
- **`fhir_status` thread-through:** a unit test that a **new** `IntentFact.fhir_status='corrected'`
  survives `plan_intent` → `PlannedFact` → `_to_extracted` → `ExtractedFact` → the `Candidate`
  `_apply` builds, arriving at `decide()` intact (the parallel with `correction` begins at
  `PlannedFact`); and that an intent with no status leaves every downstream object's field `None`.
- **Report-status re-derivation (idempotency invariant):** re-projecting the same facts reproduces
  byte-identical `report_status` and row-set for every lifecycle state — the projector derives status
  from each `value` fact's lifecycle + `superseded_by` chain (`final` for a lone active, `corrected`
  for an active chain-head, `preliminary` for pending, dropped for retracted), never parses a string,
  never stores a sibling status fact.
- **Attribute collision:** two blood types across systems both land on `(Me,'bloodType','')` and
  surface as one `attribute_collision` card.
- **Provider isolation (the §3.6 wiring):** a provider whose name equals an existing **general**-domain
  contact resolves, *under the health-scoped ingest session*, to a **distinct health-domain `Person`**,
  never the general one; the medical association lands only on health facts; a general-only scope sees
  neither the health `Person` nor any `sameAs`.
- **Firewall / address+geo stripping (defense in depth):** a synthetic header with facility + provider
  postal addresses imports the **names** onto the health row and asserts **no `address` fact** on any
  health-domain entity; **with parser stripping disabled, the Layer-2 integration guard alone holds an
  `address` fact out**; **and a `geo` fact planted on a health entity is caught by the same guard**
  (proving the guard's location-lock set is the `{address, geo}` ∪ `{geocoordinates, latitude,
  longitude, gpscoordinates}` union, not just the floor dict) — each routed to a `low_confidence`
  `firewall_address` card, never committed. Proves stripping is not a single point of failure.
- **RLS isolation** per new table (the §5 five-part matrix incl. sidecar EXISTS-join denial) + a
  firewall-shaping test that `read_labs`/`read_encounters` return empty and health search legs return
  zero under a non-health scope. (No `canonical_predicates` isolation test — it is intentionally global.)
- **Predicate-registry / collision audit (the §3.4 core):** `encounter.yaml` loads and
  `sync_predicates` seeds `origin='seed'` idempotently (skip on matching `embedding_model`);
  **`observationCategory` seeds as its own `enum` row and does NOT collapse `product.category`'s `text`
  shape — and a `product.category='appliance'` fact still passes value-shape enforcement after the seed**
  (the regression proving the rename fixed the silent-collapse); **`careUnit`/`encounterDiagnosis`/
  `partOfEncounter`/`hasObservation` seed as their own rows and do NOT collapse into the existing
  `location`/`partOf`/`reasonCode` canonicals**; `identifier` seeds once as `scalar`/`attribute` (lab
  and medication agree); an encounter fact's `value_json` passes validation (not silently re-shaped by
  global dedup); `identifier.icd10` on a `MedicalCondition` validates with no `_meta` edit (open-scheme
  invariant); **`lab_result.yaml` loads with `Lifecycle` removed and `category`→`observationCategory`
  renamed, and the drop leaves no orphaned `status`/`category` reference.** Embeds faked.
- **Review-kind migration:** after 0117 the CHECK admits all thirteen kinds (the twelve + `shape_mismatch`);
  a probe insert of each pre-0117 kind still succeeds (proving no post-0006 kind was dropped); `down`
  re-narrows and rejects `shape_mismatch`.
- **Migration 0114 (the CHECK-widening trap):** after `up`, an insert of **each** pre-0114 kind —
  `'ocr'`, `'caption'`, `'transcript'`, **and `'video_analysis'`** — into `app.attachment_extracts`
  succeeds (proving 0084's four-value set was preserved, not narrowed to 0079's three), plus a new
  `'emr_parse'` insert succeeds; after `down`, `'emr_parse'` is rejected while `'video_analysis'`
  still inserts.
- **Shape-mismatch (new wiring, safety):** a malformed lab `value_json` is **held + carded** with its
  `value_json` intact, **not** reduced to a statement-only fact; the graph is not corrupted.
- **Cross-projection FK safety (the ordering-hazard fix):** a lab intent committed **before** its
  encounter intent inserts its `lab_results` row with `encounter_id →` the encounter **entity** with
  **no FK fault** (the entity exists though its `encounters` projection row does not yet); a subsequent
  `is_current` re-projection that deletes+reinserts the encounter row does not orphan the dependent lab
  row; the tool still resolves the encounter softly by `entity_id`.
- **Projections idempotency + cost guard:** re-analysis yields byte-identical rows (incl. the
  `specimen_id=''` OCR path under the partial unique index); deleting the source note purges facts and
  the projection rows follow (`ON DELETE CASCADE` + re-projection); `encounter_id`/`orderer` populate
  from the `hasObservation` path and are NULL when absent; `project_emr` materializes encounter rows
  before observation rows within a call; **a non-EMR note (e.g. an appointment or a plain text note)
  drives `project_emr` to exactly one empty kind-filtered `SELECT` and writes zero
  `lab_results`/`encounters` rows** (the §4 cost-guard test); **a component-bearing Observation is NOT
  projected this phase** (asserting the deferred multi-component decision, not a silent partial row).
- **Load-shaped integration:** a synthetic multi-hundred-page fixture drives the full importer →
  per-unit intents → arbiter → projections, asserting per-intent transaction boundaries,
  chunk-per-page anchoring, and bounded runtime (no O(n²) supersession blowup).
- **Trigger detection (§6.0):** the `emr_import` trigger fires **only** on `domain='health'` +
  `destination='notes/medical/Records'` + an `application/zip` attachment; it does **not** fire on the
  same note without the archive (normal Medical note), on a zip under a different `destination`, or on
  a zip in another domain — so a stray encrypted attachment never trips the destructive import.
- **Intake / secret handling (security path ⇒ 100%):** a synthetic note carrying an AES-zip
  attachment + an inline password drives the full in-place normalization; on success the **encrypted
  zip attachment is deleted**, the decrypted files are attached, and the body reads `password
  [redacted]`; the password never lands in a `chunk`, an embedding, a log, or `app.settings`.
  **Delete-last / fail-closed:** a decrypt/extract failure **keeps the zip and the original body,
  writes no facts, and cards** — the only copy is never destroyed on a failed run; a wrong password
  across all candidates writes nothing; and the zip guards reject a path-traversal member and a
  size-cap (zip-bomb) breach. The audit-trail opt-in **retains** the zip while still scrubbing the
  password.
- **Config/deploy edits carry a guarding check (PR-template rule).** The `pyzipper` addition to
  `scripts/dev-setup.sh` and any worker-image/compose change ship with a check that fails when the
  dependency or setup step is missing (a bootstrap/import smoke test), run locally before push — so a
  config edit can't silently break the build the way an untested compose change once reddened main.
- **PHI-egress (only if local-pinning elected):** the actual EMR OCR `router.complete` resolves to a
  `local` provider; the first-action guard **raises** and drives the run to `error` when any `emr.*`
  route is overridden to a cloud provider via a live per-task override; the `model_already_loaded`
  precondition **defers** (not errors) when the backend is not resident.
- **Tools:** frontmatter/param-schema validity; scoped reads only; `read_labs` exposes `encounter_id`
  (NULL for orphan labs) and no per-result provider column; the safety-prose contract exercised via a
  scenario nudging the model toward a diagnosis where the tool output contains none.

---

## 10. Phased rollout (PROCESS.md: worktrees, per-task + per-wave adversarial review, one PR/wave, CI green)

- **Wave 0 — gates (no code).** GUI mocks for the labs time-series + encounter views; owner sign-off
  on: entity-per-analyte + `value`-as-measurement + the new status-aware supersession exception
  (§3.5) + **dropping the inapplicable functional `Lifecycle.status` and deriving report status in
  the projection from the value fact's lifecycle** (§3.2/§4.1) + the `category`→`observationCategory`
  rename (§3.4 collision audit); the qualifier design (§3.3); the closed-encounter + transfer model;
  the `encounter.yaml` vocabulary and collision-audited names; the health-scoped provider-isolation
  rule + the two-layer address/geo firewall (§3.6); the note-inline password + scrub-before-index
  secret rule and the `pyzipper` AES-zip handling + archive-safety guards (§6.1); the analyte
  canonicalization sources + the strengthened day-reconciliation tolerance/window
  (§6.4); **the OneContent `get_text('text')` column-fidelity validation and the geometry-slicing
  fallback decision (§6.2)**; **the PHI-egress decision** (accept the recorded cloud opt-in vs. elect
  local-pinning + enable local models); Phase-6/7 sequencing; confirmation no subject-pin is needed for
  the single-owner corpus (Phase-7 caveat recorded); build the synthetic fixture corpus. DoD: decisions
  + fixtures + the OneContent fidelity finding recorded.
- **Wave 1 — storage bedrock (security).** Activate `lab_result.yaml` (drop `Lifecycle`, rename
  `category`→`observationCategory`); new `encounter.yaml` + `medical_condition.yaml`; (optional)
  `_meta.yaml` `id_scheme += icd10`; `sync_predicates` seed with the **collision-audit assertions
  (incl. the `product.category='appliance'` regression proving the rename fixed the silent collapse)**;
  the **`fhir_status` thread-through + the `_lab_status_transition` branch placed before the
  idempotency short-circuit in `supersession.decide()`** with its unit tests (incl. the corrected-
  same-value-still-transitions test and the non-lab byte-for-byte regression guard); the Layer-2
  firewall guard (the `{address, geo}` ∪ floor-dict lock set); migrations 0114–0117 (0114 with the
  video_analysis-preserving CHECK test; projections with `entity_id`-referencing FKs + RLS **isolation
  tests** + the review-kind rebuild **before** anything references `shape_mismatch`). *Red-team
  mandatory — domain floor + supersession kinds + the new branch's blast radius + the branch-ordering
  vs. idempotency + the 0114 CHECK set + the Lifecycle drop + the category rename.* Proven-empty tables,
  no parser.
- **Wave 2 — intake + decrypt + Epic + deterministic integration.** The `emr_import` intake stage —
  `pyzipper` AES-unzip (archive-safety guards) + note-inline password extraction and
  scrub-before-index (§6.1) + PyMuPDF per-PDF decrypt — **normalizing the note in place**: attach the
  decrypted files, chunk from the attachments, then delete-last (zip + password) fail-closed; the
  deterministic `effectiveDate` token minting;
  the `EpicParser` (banner-mode promoted from `reconstruct_admissions.py`, transfer linkage,
  `hasObservation` edges, transfusion events, address/geo stripping, micro/titer routing); the
  `EmrImporter` lowering to per-unit `IntegrationIntent`s (populating `fhir_status`, running the
  Layer-2 guard) → `plan_intent`/`apply_intent`; the kind-guarded `project_emr` projector
  (encounter-before-observation ordering, `entity_id`-FK safety, lifecycle-derived `report_status`)
  wired into apply; the pathology-narrative prose extraction; `read_encounters` + `read_labs` proven
  on Epic; the amendment / retraction / firewall (both layers, incl. geo) / provider-isolation /
  shape-mismatch / cost-guard / cross-projection-FK / load-shaped tests. *Red-team — idempotency +
  supersession + batching + firewall + FK ordering.*
- **Wave 3 — OneContent + athena + ARIA OCR + cross-source dedup.** The account-number parser (on the
  **validated column strategy from Wave 0** — geometry slicing if `text`-mode offsets proved unstable)
  and the panel parser; the ARIA vision-OCR path (adapter, `confidence=0.7`) + OCR guard; the
  **candidate-stage reconciliation (the dedup enforcer)** + qualifier idempotency; the
  divergent-rendering dedup + readable-but-wrong-timestamp-parks-in-review + two-specimen-less-draws +
  one-LOINC-per-analyte tests. *Red-team on the OCR-confidence guard, the reconciliation contract, and
  the OneContent column strategy — the three highest-risk changes.*
- **Wave 4 — retrieval polish + workflow.** The seeded EMR import pipeline (0118, actions +
  `manual` trigger); the **broadened currency ⚠ flag** (superseded OR pending_review/preliminary) on
  corrected/contested results; health-scoped search over the pathology narrative; tool + currency +
  safety-prose tests; (if elected) the PHI-egress guard action + local task routes + `OcrPipeline`
  task-parametrization + egress security-path tests.
- **Wave 5 — wiki (gated on Phase 6, follow-on).** Health-domain condition articles over the imported
  facts (asserted/active-only, contested/pending/preliminary held out); the grounding gate exercised
  against the medical corpus; the health-section firewall isolation test.

**Critical path:** W0 → W1 → W2 → W3 → W4; W5 trails Phase 6. The supersession exception sits in W1/W2
(security-critical arbiter code), red-teamed on its own; OCR + dedup + the OneContent column strategy
are the W3 gate.

---

## 11. Risks & open questions

- **The measurement-supersession exception is subtle and NEW.** Getting "corrected supersedes / new
  draw accumulates / same-draw disagreement pends" wrong either buries a correction (unsafe) or
  false-flags every new lab (noise). It threads a new `fhir_status` field — originating on `IntentFact`
  (a genuinely new field, *not* a mirror of `correction`, which does not exist on `IntentFact`) and,
  from `PlannedFact` onward, traveling the exact links `correction` already travels — defaults `None`
  so non-lab callers are byte-for-byte unaffected (§3.5), and runs `_lab_status_transition` **before**
  `decide()`'s idempotency short-circuit so a same-value correction still transitions. It is pinned by
  the §9 thread-through + currency + two-row-projection + corrected-same-value + non-lab regression
  tests, red-teamed early. The primary safety target of the plan.
- **FHIR report status is a PROJECTION DERIVATION, not a stored fact.** The earlier draft stored status
  as a per-draw `reportStatus` attribute fact and claimed it "supersedes in lockstep" with the value
  fact — unbuildable: `decide()` resolves each fact by its own identity key, so a sibling attribute
  fact lands in `attribute_collision`, never lockstep, and a same-value correction short-circuits at
  the idempotency refresh before any measurement branch. This revision removes the sibling fact
  entirely: report status is the **result** of the value fact's own lifecycle transition (the enforced,
  arbiter-owned signal), and §4.1 derives the `report_status` column from that lifecycle + the
  `superseded_by` chain. Documented simplification: FHIR `corrected`/`amended` collapse to `corrected`
  (the safety-relevant fact is "revised, see current"). Open: none — verified against
  `supersession.py:403–494`.
- **OneContent fixed-width column recovery is the hardest parse and does NOT get a free ride from
  `text` mode.** `get_text('text')` reflows whitespace and does not guarantee stable char offsets;
  char-offset ruler slicing is likely to misalign on the 82-page tables. Mitigation: Wave 0/3 validates
  fidelity against real pages and falls back to **word x-coordinate (`get_text('words')`) geometry
  slicing** — still PyMuPDF, no new dependency. Open: does geometry slicing fully resolve the account
  tables, or is a deterministic local OCR/table engine warranted? Decide on the Wave-0 finding.
- **OCR fidelity on ARIA (a scanned duplicate).** A misread digit could fork a draw; a misread
  *timestamp* is worse (the qualifier is timestamp-keyed). Mitigation lives at the candidate stage
  (§6.4), which is the actual dedup enforcer: reconciliation to a same-day precise read gated on
  analyte + value tolerance + a widened same-day window; unreadable *or readable-but-wrong* timestamps
  park in review (never guess an address), and a 0.7-confidence read never supersedes a structured
  prior. Open: is line-oriented vision-OCR accurate enough, or is a deterministic local OCR engine (a
  `dev-setup.sh` dependency) warranted? Decide on a sampled accuracy check in Wave 3.
- **No projection-to-projection FK (ordering-hazard fix).** `lab_results.encounter_id` and
  `encounters.part_of_id` reference `app.entities(id)`, never a sibling projection PK, so out-of-order
  per-unit commits and `is_current` re-projection churn cannot FK-fault a dependent row; `project_emr`
  additionally materializes encounter rows before observation rows within each call. The tool resolves
  the encounter projection *row* softly by `entity_id`. Open: none — this matches the appointment
  precedent.
- **Analyte identity across systems.** Trends require `WBC`/`PLT`/`Platelets` to resolve to one
  entity; LOINC anchors where present, canonicalization + resolution carry the rest. Open: seed a
  larger bundled LOINC subset vs. lean on the synonym map; calibrate on the corpus.
- **Entity-per-analyte is the native measurement model** (§3.1); the projection carries multiple rows
  per entity (one per reading), a deliberate departure from appointment's one-row-per-entity, keyed by
  the §3.3 qualifier + `source_fact_id`. Open: revisit only if a future feature prefers a different
  grain.
- **Provider fragmentation vs leakage (§3.6).** Health-scoped resolution avoids leaking the patient
  relationship but can double a provider who is also a general contact; the tradeoff favors isolation.
  Owner-visible unification is the health-scoped `sameAs` behind a merge tool, never the parser. Expect
  review volume on 22+ providers at first import.
- **The location firewall has no domain-floor backstop on this path.** Verified: `domain_floor` only
  ratchets `general`→restricted, so on a health note an address/geo fact would *stay health* rather than
  be pushed to `location`. The firewall therefore rests on **two deliberate layers** (parser stripping +
  the integration-time guard whose lock set is the `{address, geo}` ∪ floor-dict union, §3.6), not the
  floor. The §9 tests exercise the guard *without* stripping — for both `address` and `geo` — to prove
  it is not a single point of failure.
- **Registry name collisions are silent by construction.** Global seed-dedup resolves a reused name
  with a divergent shape to the lexicographically-first `(value_shape, kind)`; the §9 collision-audit
  guards **four** known cases (`category`→`observationCategory`, plus the avoided `location`/`partOf`/
  `reasonCode`), asserts `product.category` still enforces its `text` shape after seeding, and every
  new predicate must re-run the audit before it is declared.
- **PHI cloud egress during dev** is a recorded ANALYSIS.md opt-in, not an accident — the firewall is
  a storage/retrieval boundary, not a network one, until local models land. Any second-subject intake
  (Phase 7) re-raises consent explicitly. Open: elect local-pinning now (with the fail-closed egress
  guard) vs. defer to the local-model config flip.
- **The password lives in a note (§6.1) — a secret in indexable text.** The whole safety of the
  note-inline model rests on the extract-redact-then-index ordering: redaction must complete **before**
  chunk/embed, the raw secret must never touch a `chunk`/embedding/log, and any doubt must fail closed.
  This is the plan's sharpest new security seam; it is a 100%-coverage path (§9), and a redaction miss
  is a credential leak into hybrid search, not a cosmetic bug. Residual: a password typed in an unusual
  phrasing the matcher misses fails *safe* (no decrypt) but annoys — the review card must make the
  expected phrasing obvious rather than silently dropping the import.
- **Non-scalar and multi-component results.** ABO/Rh is typed (`bloodType` on "Me"); microbiology +
  serologic titers route to review and are a named follow-on — never shredded into scalar facts.
  **Multi-component scalar results (BP): the fact-layer `component` vocabulary is ready, but the
  `lab_results` projection is NOT — supporting them is a specified projection delta (add a
  `component_code` discriminator to the unique key + partial index and teach the projector to read
  `component` facts, §4.1), deferred because the corpus has none.** This is explicitly *not* claimed as
  a tested no-op.
- **Vehicle deviation (typed_record vs graph).** The FHIR types keep `vehicle: typed_record` while
  stored as graph facts + a projection; not runtime-enforced, so a legibility choice (§3). Open: flip
  to `vehicle: graph` if the typed-record intent is formally retired.
- **Transfer & transfusion modeling.** Two encounters + `partOfEncounter` (an ambiguous admit/discharge
  overlap routes to review, not a guess); transfusions are `transfusion` events qualified by order id
  (a dedicated `procedure`/`MedicationAdministration` entity is a follow-on if the corpus grows).
- **Sparse condition articles.** With no meds/notes, a condition article is labs + encounters + coded
  diagnoses only; acceptable (it honestly reflects the record), but the Phase-6 notability gate may
  need tuning so thin conditions stay link-target-only rather than stub articles.
- **Subject-scoped RLS (deferred, Phase-7).** `subject_id` is provenance only, not policy-enforced;
  multi-patient isolation is built only when guided multi-patient intake lands.

---

## 12. Wave 0 — sign-off record, decisions, and grounding corrections

**DoD (§10):** decisions recorded · synthetic fixtures built · OneContent fidelity finding recorded.
Wave 0 added no production code. Its artifacts are this section, the synthetic fixture corpus under
`backend/tests/fixtures/emr/`, and the doc reconciliation (status → In progress, W0 ✅).

### 12.1 Migration numbering re-derived (the §8 snapshot was stale)

The head is **`0114`** (`0114_facts_object_entity_idx.py`), not the `0113` the §8 table assumed, so the
EMR migrations renumber **+1**:

| §8 name | Planned | **Actual** |
|---|---|---|
| attachment_extracts `emr_parse` kind | 0114 | **0115** |
| `app.lab_results` projection | 0115 | **0116** |
| `app.encounters` (+ sidecars) | 0116 | **0117** |
| review_items `shape_mismatch` kind | 0117 | **0118** |
| seed `emr_import` workflow | 0118 | **0119** |

The `down_revision` chain and the seed-order dependency (review-kind before code; trigger seed in the
same PR as the registered actions) are unchanged — only the numbers shift.

### 12.2 Grounding corrections to the plan (verified against the working tree)

Four §-level claims did not survive grounding; the resolution is recorded here and the affected §s are
read **with this section as the correction of record** (the body prose is left intact for its rationale):

1. **The `product.category` collision is fictional (§3.2/§3.4/§3.8/§9 collision-audit).** Only
   `lab_result` declares a `category` predicate in the current defs; `product.yaml` names "category"
   only in a prose comment, so there is **no global-seed collision**. **Decision: keep `category`
   as-is (no rename to `observationCategory`).** It is FHIR-accurate (`Observation.category`) and seeds
   cleanly as its own `enum`/`attribute` row. The rename and its "`product.category='appliance'` still
   enforces text" regression test are **dropped**. The collision audit stays for the **real** cases
   (`location` ref→place, `partOf` ref→project, `reasonCode` medication text) — Encounter still uses
   `serviceProvider`/`careUnit`, `partOfEncounter`, and `encounterDiagnosis` to avoid those. *(If
   `product` ever adds a shape-divergent `category`, the rename becomes a one-line follow-on.)*
2. **`allow_open_predicates` is not a real flag (§3.2, §3.4).** The schema subsystem has no such key;
   the invariant is soft — storage accepts any predicate name and never rejects one; only **value-shape**
   is validated at integration (`pipeline._shape_check`). The defs simply omit the flag. The collision
   audit still matters because `registry_seed_rows` collapses a reused name to the lexicographically-first
   `(value_shape, kind)` — the audit's purpose is intact; only the "rejected because closed" framing is
   dropped.
3. **`destination` is stored as the bare option string `'Records'`, not `'notes/medical/Records'`
   (§6.0, §8, §9).** The `notes/medical/` prefix is a display-only omnibox label. **Decision: the
   `emr_import` trigger condition is `domain_code='health' AND destination='Records' AND (a zip
   attachment is present)`.**
4. **The `note.created`/`note.ingested` event payload carries only `{note_id}` (§6.0).** A
   `TriggerFilter` can match `domains:['health']` but cannot see `destination` or attachment
   content-type, which are not in the payload. **Decision: widen the emitted event payload** (in
   `api/notes.py`) to include `destination` and `has_zip_attachment`, so the trigger stays a precise,
   pre-decryption, user-chosen marker via `payload_equals` (the plan's §6.0 intent, now with a real
   mechanism). A new `application/zip` extractor is registered in `ingest/extract.py` (W2).

Two further build facts, not contradictions:

- **No PDF page→image renderer exists yet** (`PdfTextLayerExtractor` renders text only; scanned pages
  extract to nothing). The ARIA OCR path (W3) builds page rasterization with **PyMuPDF** (already a
  dependency — **no new dep** beyond `pyzipper`), feeding the shipped `vision.ocr` route.
- **`project_emr` is wired into both `pipeline._apply` *and* `analysis/purge.py`** (the appointment
  projector is called from both; note-deletion re-derivation must not orphan projection rows). §4's
  "wired into `_apply`" is extended to include the purge caller.

### 12.3 Owner sign-off decisions

Recorded per §10's Wave-0 gate. Items the plan already argues through are **accepted as written**;
the two genuinely-open forks are resolved with the recommended default (revertible):

- **entity-per-analyte + `value`-as-`measurement`** — accepted (§3.1).
- **status-aware measurement-supersession exception** (`_lab_status_transition` before the idempotency
  short-circuit) — accepted (§3.5); it is the W1/W2 security-critical, 100%-coverage change.
- **drop the inapplicable functional `Lifecycle.status`; derive report status in the projection from the
  value fact's lifecycle** — accepted (§3.2/§4.1).
- **`category` rename** — **rejected** (see §12.2.1: the collision is fictional; keep `category`).
- **qualifier design** `<collected_iso>|<specimen_or_empty>` — accepted (§3.3).
- **closed-encounter + facility-transfer model** (`partOfEncounter`) — accepted (§3.4).
- **`encounter.yaml` vocabulary + collision-audited names** — accepted (§3.4).
- **health-scoped provider isolation + two-layer address/geo firewall** — accepted (§3.6).
- **note-inline password + scrub-before-index + `pyzipper` AES-zip + archive-safety guards** — accepted
  (§6.1); the sharpest new security seam, 100%-coverage.
- **analyte canonicalization sources + strengthened day-reconciliation tolerance/window** — accepted
  (§6.4).
- **OneContent column-fidelity go/no-go** — see §12.4.
- **PHI-egress posture** — **Decision: accept the recorded cloud opt-in** (the shipped ANALYSIS.md
  posture; the firewall is a storage/retrieval boundary, the adapter is the sole egress point). Local
  PHI-pinning (fail-closed egress guard + `emr.*` local task routes + `OcrPipeline`
  task-parametrization) remains a **specified, electable W4 hardening** once local models are served —
  not built this pass. The §5/§7/§9 "if elected" branches stay dormant.
- **no subject-pin for the single-owner corpus** — accepted; cross-subject isolation is the named
  Phase-7 follow-on (§5).
- **GUI mocks (labs time-series + encounter views)** — **deferred.** Waves W1–W4 build backend storage,
  the deterministic importer, the projections, and the two **agent-facing** `.tool` read-models
  (`read_labs`/`read_encounters`); none adds a rendered GUI surface, so the `PROCESS.md` GUI gate does
  not fire in this build. A dedicated frontend labs/encounter view is a follow-on and, when built,
  triggers the three-mock owner-choice gate then.

### 12.4 OneContent column-fidelity finding (§6.2 go/no-go)

Empirical validation against **real** OneContent pages is impossible in-repo (the pages are PHI and are
never committed). Wave 0 therefore records the **framing and the strategy**, and builds a synthetic
fixture pair that lets W3 prove the parser mechanics without PHI:

- `onecontent_account.txt` — the reading-order `get_text('text')` rendering, with **deliberately
  reflowed inter-column whitespace** across rows (so a fixed **character-offset** ruler misaligns).
- `onecontent_words.json` — the `get_text('words')` word-box view of the same region, with **stable
  column x-bands** (so **x-geometry** slicing recovers the columns, and multi-word analyte names like
  "White Blood Cell Count" join by geometry, not a fixed char window).

**Decision (go/no-go): the OneContent parser slices columns by word x-geometry, not character offset.**
The extractor is extended in W3 to expose word x-coordinates via `get_text('words')` (still PyMuPDF,
**no new dependency**); the parser interface accepts either a character-ruler or a geometry-ruler so the
Epic/athena paths are untouched. The residual open question — whether geometry slicing fully resolves
the real 82-page account tables or a deterministic local table/OCR engine is warranted — is re-checked
against **real** pages during W3 on the owner's machine (a sampled fidelity check), per §11.

### 12.5 Synthetic fixture corpus (built)

`backend/tests/fixtures/emr/` (see its `README.md`) — Epic (banner bleed, MICU→A3 transfer, transfusion
orders, pathology narrative), OneContent (account grouping, abnormal legend, the fixed-width hazard +
its word-geometry pair), athena (accession blocks, ordering provider, a cancelled result), and ARIA
(canned line-oriented OCR duplicating the 2021 OneContent labs, plus one readable-but-wrong timestamp
that must park in review). **All synthetic — no real PHI.**

### 12.6 Wave 1 — shipped surface (storage bedrock)

Proven-empty tables, no parser (§10). Landed:

- **Schema defs.** `lab_result.yaml` activated (Lifecycle dropped, `category` kept — §12.2.1);
  new `encounter.yaml` (collision-audited names) + `medical_condition.yaml`; `_meta.yaml`
  `id_scheme += icd10`, new `encounter_role` vocab + `transfusion_order` shape. A unit test pins
  activation + the global collision audit (`category` seeds as its own enum row; the encounter names
  don't collapse into `location`/`partOf`/`reasonCode`; `identifier` seeds once).
- **The status-aware supersession exception.** `fhir_status` originates on `IntentFact` and threads
  `PlannedFact → _to_extracted → ExtractedFact → Candidate` (defaults None → non-lab callers
  byte-for-byte unchanged, proven by the full unit suite still green). `_lab_status_transition` runs
  before the idempotency short-circuit in `decide()`; 20 pure tests cover the §3.5 matrix incl. the
  corrected-same-value regression, re-run idempotency, and the None-status inert guard.
  - **Red-team hardening (§3.5 deviation of record).** An adversarial review found the transition was
    not idempotent when the first application left no `superseded` marker. Resolved: a transition that
    changes a value always leaves a durable marker (a superseded predecessor or a retracted row), so
    re-runs refresh in place; **a `corrected`/`amended` reading with NO original to revise is held in
    review (`low_confidence` / `correction_without_original`) instead of minted as a bare active value**
    — a safety-positive change from §3.5's "insert active (behaves as a first final)" for the none row
    (a bare active correction is indistinguishable from a plain final on re-run). `final`/`preliminary`
    defer to the unchanged path when an active reading already exists, so two current rows can never
    collide on the `is_current` partial unique index.
- **Layer-2 firewall guard** (`ingest/emr/firewall.py`): the `{address, geo}` ∪ floor-dict lock set,
  with a drift-guard test proving the union is necessary (geo is not floored) and sufficient.
- **Migrations 0115–0118** (renumbered per §12.1) with RLS quartets + isolation tests, integration-
  verified against real Postgres (RLS firewall for all four projection tables, the WITH CHECK block,
  the sidecar EXISTS-join denial, the `is_current` partial unique index, and both CHECK-widening
  safety tests). `pyzipper` added (pyproject + lock + dev-setup note + import smoke test).

Deferred to their planned waves: the `EmrImporter` wiring of the firewall guard + `fhir_status`
population (W2), the `project_emr` projector (W2), and the parsers (W2/W3).