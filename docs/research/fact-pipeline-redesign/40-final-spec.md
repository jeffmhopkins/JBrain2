# Fact pipeline & review redesign — FINAL SPEC (consolidated)

**Status:** converged design for sign-off (gate G_final). Consolidates v0→v2 plus the
Round-3 correctness fixes. For full per-section detail, the lineage is: `20-spec-v0.md`
(integrated synthesis) → `21-spec-v1.md` (Round-1 fixes) → `22-spec-v2.md` (Round-2 fixes,
smaller+stricter) → **this** (Round-3 correctness fixes + consolidation). This document is the
authoritative summary + the open decisions for the user; the version files hold the worked
schemas/examples.

**Red-team status (honest):** two full independent adversarial rounds (6 lenses each = 12
passes) were run to completion with full disposition (`30-redteam-r1-*`, `31-redteam-r2-*`).
Round 3 was a focused re-verification; the **correctness** lens completed and found that v2's
value-identity fix over-corrected (below); the **model** and **security** R3 lenses did not
complete (tooling), though the security partial confirmed v2's two accepted-risks are bounded.
**Recommendation:** before implementation, run one more independent model+security verification
pass against this final spec (cheap, and the responsible close). The design below folds the
R3-correctness findings; nothing in it is blocked.

---

## 1. The design in one page

- **Model proposes / deterministic committer decides.** The LLM emits structured intent with
  **no write capability**; one privileged deterministic committer validates (registry, value
  shapes, temporal soundness, firewall) and is the **sole writer**. This is simultaneously the
  reliability boundary, the audit chokepoint, and the prompt-injection defense. *schema-valid ≠
  grounded* — grounding is closed by deterministic backstops paired with an independent oracle.
- **Append-only, bitemporal store; op-log is audit + history.** Nothing is updated in place;
  every change (model- or human-authored) is a typed op + the immutable assertion rows it
  causes. Valid-time (world) and transaction-time (belief) are independent.
- **Cardinality lives in the identity key.** Functional predicates supersede; set-valued
  accumulate — decided purely by the key, never an `if functional` branch. The registry
  `functional` flag is the sole authority, snapshotted at write.
- **#7 preserved (no doctrine change).** Humans issue typed correction *operations* the
  committer validates and applies; the wiki still regenerates from facts. The one soft edge is
  `add_fact` (human-originated), gated + attributed.
- **Review is structured editing.** One parameterized card is a structured editor over a fact
  record; it submits an ordered, typed op-list (fat-read / thin-write). The kind-zoo collapses;
  the common path is a one-tap Approve / Needs-fix triage.
- **Cutover = clean rebuild (D1).** Drop the derived graph; re-ingest notes under the new
  contract. No in-place legacy migration.

---

## 2. The fact contract (`FactClaim`)

A fact is `subject —predicate[.qualifier]→ TypedValue|Ref`, plus modality, kind, domain,
confidence, provenance, temporal. Carried through three honest stage shapes
(`CandidateFact` → `FactClaim` → `StoredFact`).

- **`TypedValue` — 5 variants:** `enum`, `quantity` (value+unit), `date` (literal+grain),
  `text` (the only free-text variant, bounded), `ref` (the relationship case — carries no
  scalar). A value is **never a sentence** (the deterministic backstop rejects `text` over the
  bound for any non-`text` predicate). `boolean` folds into `enum`; `structured` (only member
  `address`) is added back on demand. 1:1 with the registry `value_shape`.
- **`Ref`:** mention surface + span retained **forever** alongside the resolved `entity_id`
  (re-resolution, audit, identity ops). At storage, `entity_id` is a **same-domain projection**.
- **Temporal (bitemporal, anti-fabrication):** valid-time endpoints each carry a **bound
  trichotomy** `closed | open | unknown` and **per-endpoint precision**. `unknown` end =
  "former without a date" — excluded from current, rendered as a word, **never a fabricated
  date or `— → 2026` glyph**. Recurrence is RFC-5545 (`rrule`/`rdates`/`exdates`/overrides),
  lazy, expanded from `now()` with a cached `next_occurrence_at`. **No Allen auto-abutment** —
  supersession marks a prior value former in valid-time without inventing an end date.
- **Modality:** `asserted | negated | hypothetical | reported | question | expected` — never
  model-trusted; cue-less irrealis → review, never auto-`asserted`.
- **Provenance: a LIST**, not a single ref (R3-6 fix) — so merge/idempotent-merge unions source
  spans without dropping any; `kind ∈ {extracted, human_correction, human_assertion, agent}`.
  (`inferred` auto-commit is **deferred** — see §6.)

---

## 3. Storage, identity & the two keys (with the Round-3 corrections)

Three layers: **entity node** (resolved identity; split/merge via `redirect_to` on a stable
`canonical_id`) · **fact assertion** (immutable append-only edge; the audit + reversibility
grain) · **fact slot** (the logical fact, a derived key — no separate materialized table).

**Two keys, via one `keys(fact)` function, over a stable `predicate_id`** (never the mutable
canonical string — R2 perf):

- **`identity_key`** = `(predicate_id, qualifier, subject, domain, value_identity)` —
  **EXCLUDES modality** (R3-4 fix). This is the lineage/history grain: an `asserted` fact and
  the `hypothetical` it was `realize`d from share one lineage, so `realize` and undo preserve
  history.
- **`live_key`** = `(predicate_id, qualifier, subject, domain, modality [, value_identity if
  set])` — **INCLUDES modality**, and the **live floor is `asserted`-only**. So a `negated`
  "not allergic" has a different `live_key` and can never collide-overwrite the `asserted`
  one, and non-asserted never reaches `current()` (the R2 negation-safety fix, now without
  breaking lineage).
- `current()` is served by the **partial unique index** `ON (live_key) WHERE tx_to IS NULL AND
  state='live' AND modality='asserted'` — an indexed lookup over the append-only assertions;
  **no separate materialized `fact_current` table** (R2 ergonomics/perf).

**`value_identity` — corrected (R3-1, the key fix):** a normalized *name* is **never** a unique
merge key (it would force-merge two distinct "Sam"s).
- `ref` values → `value_identity = the resolved object `entity_id``. "Same value" is an
  **entity-resolution** decision, made once, correctly; two distinct Sams resolve to two
  entity_ids → two members. No fact-key force-merge.
- scalar values → a **genuinely unique** natural key **only** where canonical (E.164 phone,
  lowercased email); otherwise a **minted member-id** carried forward by supersession.
- The per-`(slot, value_identity)` UNIQUE therefore applies only to genuinely-unique keys
  (entity_id / phone / email / minted id), never to names.

**Per-member history allows resumption (R3-2 fix):** a member's history is a **sequence of
validity intervals**, not one interval. Acme→Globex→Acme = the Acme member has intervals
`[2019,2021]` and `[2024,open]`; `current()` selects the member whose latest interval is live;
`history()` renders per-member interval sequences. No gap-collapse.

**Op-log:** typed ops + frozen `resolved_outputs` + the pipeline version-4-tuple per op
(history shows recorded outcomes, never re-derives through a future registry). Graph is the
live source of truth; op-log + immutable checkpoints are the history/undo substrate
(genesis-replay dropped).

---

## 4. Correction algebra & review

~12 essential ops (snapshot undo deleted the ~22 inverse ops): `set_field` (low-risk fields
only), `retime` (incl. the temporal subset), `relink_subject|object`, `mint_and_link_object`,
`unlink_object`, `add_to_set`, `replace_head`, `remove_from_set`, `split_fact`, `merge_facts`,
`add_fact`, `retract`, `supersede`, `pin`, `fix_provenance`, **`realize`**, `domain_move`, and
the identity ops (`merge_entities`, `split_entity`, `assert_distinct`).

- **`offered_ops` is arbiter-authoritative** from the registry `functional` flag — a human
  literally cannot `add_to_set` on `birthDate` or `set_field value` on a set predicate. The
  client cannot smuggle an illegal op. Ambiguous-cardinality default = **set** (additive is the
  safe failure).
- **`realize` (R3-3 fix, defined):** promotes `hypothetical|expected` → `asserted` as a
  supersede on the **same `identity_key`** (modality is not in that key) that takes the live
  floor on `live_key`; wired to the contradiction check; never throws. Nothing auto-promotes by
  wall-clock.
- **Set-predicate contradiction check (R3-5 scoping fix):** a modality-stripped check keyed on
  `(subject, predicate_id, value_identity, qualifier, domain)` — strips **only** modality (not
  qualifier/domain), so an `asserted`+`negated` pair on the same member routes to review without
  false positives and without any cross-firewall read.
- **`merge_facts`:** provenance **unioned** (the list carrier, R3-6), cross-modality merge
  **rejected**, cross-domain **rejected**, conflicting typed values → review.
- **Snapshot undo:** state-based, not per-op inverses. On un-tombstone the committer re-checks
  `live_key` occupancy (occupied → "correct instead", a forward op, never a unique-index throw),
  and **re-derives** `domain` + protection metadata from current topology (never resurrects a
  frozen `domain_code`/flag — the firewall + R2 fixes). **Honest reversibility (amends framing
  §4):** snapshot-revert with **cascade-or-block**; removing an earlier op's effect while
  keeping a later dependent op is a **new forward correction, not an undo**. Human surface:
  "undo last" / "revert to point" / "correct instead" — internals never exposed.
- **Review:** fat read projection (predicate metadata + cardinality + candidates + `ui_caps`
  firewall gates) / thin write (verdict + ordered typed op-list + `base_version`). One card
  parameterized by `(kind, value_shape, cardinality, reason)`; `kind`/`reason` are display
  hints; new shapes extend the **value-editor registry**, not the card zoo. Default path =
  Approve / Needs-fix triage with progressive disclosure.

---

## 5. Extraction & reliability

Two-stage, constrained-decode: **(1)** span-anchored verbatim candidates (high recall);
**(2)** per-candidate type+link with the registry slice + entity candidates injected (the two
worst hallucination surfaces become multiple-choice). Then a **deterministic
validate→repair→backfill→gate** pass that is the **sole authority** — terminal states are
**validated-commit** or **review-item**, never silently wrong:

- **Grounding:** span verification (value is a substring of the cited span); **typed-value
  re-derivation** from the span, with two guards so "model+parser agree on a wrong span" is
  catchable — a **mandatory registry plausibility range** (no range → review, not commit) and
  **subject+predicate+value co-location** (catches cross-subject capture "Sam's A1c 5.4, mine
  12.8"); modality cue cross-check (never auto-flip).
- **Vocabulary:** enum coercion; predicate canonicalization owns the coined-slug dedup
  threshold (weak → `new_predicate` review); **cardinality stamped from the registry**.
- **Link/firewall (100% tested):** entity existence in current RLS scope; cross-firewall link →
  review with consequence surfaced.
- **Completeness guard:** required sub-objects present-or-review, independent of `finish_reason`.
- **Repair:** structured re-ask capped N=2 (feedback stripped of note text — no injection
  amplifier), then degrade to review.
- **Versioning:** SemVer'd contract; the 4-tuple process-provenance per fact; future re-analysis
  is a budgeted, shadow-diffed, run-logged migration that **overlays the human-op layer**
  (retracted facts never resurrect; human-touched slots protected — not just the literal pin).
- **Eval:** frozen golden set with negatives; per-field semantic metrics; **zero-tolerance gates
  on negated/hypothetical→asserted and hallucinated links**; backstop-ablation; adversarial
  prompt-injection slice (no cross-firewall links).

---

## 6. Security (RLS / firewalls)

- **Sole privileged committer**; the app/LLM/UI role has **no direct DML**. The committer
  **re-derives `domain` from operands**, ignoring any model/payload-claimed domain.
- **Per-domain entity projections + an attribute-free global canonical resolution index** (no
  cross-domain FK — kills the Postgres FK covert channel; relink chooses same-domain projections
  only). Firewall enforced at **value materialization**, not row visibility alone.
- **`domain_move` = PUBLISH:** owner-only, LLM-cannot-emit, non-batchable, explicit-confirmation,
  copy-forward, **one-way**, audited in both bands (protected metadata redacted from the general
  side). Undo = **tombstone** the general copy (not destroy); it cannot un-publish derivations —
  the owner is told a publish is **irreversible** (accepted as inherent to publishing).
- **`add_fact`:** real cited note (committer derives domain from it) OR forced correction-note
  round-trip; **non-droppable** `human_assertion` attribution; location-domain link objects
  cannot be `add_fact`'d (no movement-pattern oracle).
- **Inferred-fact auto-commit deferred** (§2): derived facts route to human `add_fact`/review;
  the premise-verification hole is dissolved (nothing ungrounded auto-commits). *Note: ordinary
  relative-date resolution ("last Tuesday" → instant) is a normal extraction step, not an
  "inferred fact," and is unaffected — verify in the follow-up model pass.*
- Every new table (`fact_assertion`, `fact_op`, `fact_audit`, `entity_projection`,
  `entity_identity`, provenance) ships its RLS isolation test in the same PR.

**Two documented bounded ACCEPTED-RISKs** (single-user system): (a) the attribute-free global
canonical index leaks ≤1 bit/query of cross-domain existence/co-membership (constant-work gate;
binary-search amplification over many queries is the residual to watch); (b) a `domain_move`
publish is irreversible in the security sense (derivations of a published value stay public).
Both are surfaced to the owner, not silent. **The follow-up security verification pass should
press both.**

---

## 7. Phased rollout (no code in this effort — implementation plan)

1. **Schema + committer + registry + RLS** (the storage spine, two keys, projections,
   isolation tests). 2. **Extraction v-next + deterministic backstops + eval harness** (the
   reliability gate) behind a flag, shadow-diffed. 3. **Op algebra + snapshot undo + audit.**
   4. **Review card** (read-only triage first, then editors). 5. **Clean-rebuild cutover (D1):**
   drop the derived graph, re-ingest all notes under the new contract; the old graph is retained
   read-only for one cycle, then dropped. Each stage is its own branch+PR, CI-green, tests with
   code (CLAUDE.md rules).

---

## 8. Open decisions for sign-off (the gate)

1. **Cross-domain resolver + global canonical index** — accept the bounded 1-bit residual
   (recommended, firewall-first), or take the simpler global-entity-table + attribute-RLS with
   its FK-channel mitigation cost?
2. **`add_fact` strictness** — direct only with a cited note, else forced note round-trip
   (recommended), vs always-direct (more ergonomic) vs always-note (stricter)?
3. **Honest undo promise** — accept the bounded "snapshot + cascade-or-block; non-tail removal =
   forward correction" framing (it is the honest maximum; v0 over-promised)?
4. **Inferred facts deferred** — confirm derived facts (age→birth_year) are out of the first
   build and route to human add_fact?
5. **TypedValue at 5** — ship 5 (`boolean`→`enum`, `structured` on demand), or keep 7?

## 9. Residual / before-build

- **Run the follow-up model + security verification pass** (R3 model/security didn't complete):
  press the typing-guard review-volume question, relative-date-vs-inferred boundary, the publish
  irreversibility, and the 1-bit index amplification.
- **R3 correctness SEV-2 residuals folded above** (`realize` wiring, contradiction scoping,
  multi-provenance carrier) should get a confirming correctness pass once written as code.
- Everything else (R1+R2) is dispositioned in the version files' tables.

---

**Bottom line:** the redesign delivers the §2-framing wishlist — every field of a fact
(predicate, value, subject/object links, dates/temporal, modality, domain, kind) is editable;
set-valued predicates make **add vs. replace vs. remove explicit** via cardinality-in-the-key;
the review kind-zoo collapses to one structured editor; and the contract is reliably emittable,
deterministically gated, versioned, firewall-safe, and reversible (in the honest bounded sense).
It is ready for sign-off on the §8 decisions, with one model+security verification pass
recommended before the first build PR.
