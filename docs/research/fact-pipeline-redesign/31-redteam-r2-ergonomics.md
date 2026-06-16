# Red-team R2 — Over-engineering & Ergonomics

**Lens:** OVER-ENGINEERING & ERGONOMICS, round 2. Position from R1 stands: the spine is
sound, the surface is heavy. R1 forced six fixes into v1. R2's job is twofold: (1) verify
those fixes *landed and reduced net complexity* rather than relocating it again — the R1
charge — and (2) attack v1's *new* additions, which were each justified by a different
lens (correctness/security/perf) and so never faced the over-engineering lens together.

**Inputs read:** `00-framing.md` (§5 success), `21-spec-v1.md` (TARGET), `30-redteam-r1-ergonomics.md`.
Did NOT read `backend/src`. Did NOT run git.

**Severity key:** SEV-1 = would sink build/adoption/maintainability · SEV-2 = real drag,
fixable without re-architecting · SEV-3 = nit.

---

## PART A — Did the R1 wins land? (verification pass)

| R1 finding | claimed v1 fix | landed? | note |
|---|---|---|---|
| F1 fewer-kinds (god-component) | one shared op enum + one value-shape schema; dumb triage shell; `kind`/`reason` are hints; metric re-baselined to decision points | **CONFIRMED** | §4.3 + §8 + §9-F1. Editor matrix genuinely cut to `value_shape×cardinality` (7×2); the 6× and ~7× multipliers are gone in text. Re-baselining to "decision points a maintainer touches" is honest and is the right noun. |
| F2 snapshot undo (delete ~22 inverses) | snapshot-based undo; inverse defs + migration ladder deleted; no `inverse_of` column | **CONFIRMED** | §1.2, §3.4 (`-- No inverse_of, no stored inverse`), §4.5, §9-F2. The ~22 precomputed inverses and their ladder are genuinely gone. Genesis-replay also correctly dropped (§1.2). |
| F3 honest typed stage shapes (fat envelope gone) | `ExtractedClaim → ResolvedFact → ReviewCard`; `stage` is the type not a mutable enum; storage a strict projection | **CONFIRMED as framing; PARTIAL in mechanics** — see N3 below. The *naming* landed; the wire JSON is admittedly "the same JSON as v0" (§2 head) with `domain` absent and `stage` a discriminant. Compiler-enforced illegal-state-unrepresentable is asserted, not demonstrated. |
| F4 ~12-op set | shrunk to ~12; inverse-ops removed; 3 supersede-spellings → `replace_member` | **CONFIRMED with an asterisk** — the table in §4.1 lists ~16 distinct named ops once you expand the field-discriminated super-ops (`set_field`×5 fields, `set_lifecycle`×3). "~12" counts op *kinds*, not editable affordances. Honest enough; not a backslide. |
| F5 Approve/Needs-fix triage default | dumb shell defaults to Approve/Needs-fix; progressive disclosure; cardinality hidden | **CONFIRMED** | §4.3 "the 90% path is two keystrokes." |
| fewer-kinds re-baselined to decision points | success metric redefined | **CONFIRMED** | §8 explicitly. |

**Verdict on the R1 charge ("complexity relocated not reduced"):** For F1/F2/F4/F5 the
relocation charge is **answered** — these are real deletions (inverses, ladder, forks,
naming surfaces), not relocations. **But the R1 attack was scoped to the ops/review/undo
surface.** v1 paid for those wins by *adding* machinery elsewhere — a second key, a
materialized table maintained every op, a global resolution index, an inference-template
registry, a 3-way overlay diff, a blind constant-work resolver, one-way-purge move
semantics. Each was added under a non-ergonomic lens. **The relocation charge now applies
to the storage/security/migration surface that R1 never costed.** That is Part B.

---

## PART B — v1's NET complexity (the new additions, costed together)

### N1 — The materialized `fact_current` table is a second source of truth the spec spent R1 arguing it didn't need (SEV-2)

**Attack.** R1-perf promoted `fact_current` from v0's "optional" to "MATERIALIZED,
authoritative-cache, maintained by the committer in the SAME op transaction" (§3.1, §3.3,
perf S1-1). Read that against spine #2's whole thesis: "append-only `fact_assertion` is
the source of truth; it already holds every prior state." v1 now maintains a *derived
current-value table on every single op*, in-transaction, and calls it "authoritative-
cache." That phrase is doing heavy lifting — a cache that is written in the same
transaction as the truth, that every read path trusts, and that must be kept consistent
through supersession / tombstone / un-tombstone / modality-gating / valid-time-window /
the two-key live-selection, is not a cache. It is a **second materialized model of
current-state that must agree with the snapshot-derived answer on every one of those
axes**, forever. The undo path (§1.2) tombstones assertions *and* must re-maintain
`fact_current`; the cascade path must too. That is exactly the dual-write bug surface
event-sourcing-with-a-read-model is famous for — and it was added for performance on a
**single-user** corpus.

**Simpler alternative.** Drop the materialized table; serve `current()` from an indexed
view/query over `fact_assertion` (the indexes in §3.1 — `fa_subject_live`, `fa_asof`,
`valid_from_sortkey` — already exist and already make this cheap). A single owner's live
fact set is small (thousands, not millions); the three-gate filter on a partial index is
microseconds. If a hot path ever proves slow, add a materialized view refreshed
*asynchronously* (eventually-consistent read cache, no in-transaction dual write, rebuild
on drift) — which is what a cache actually is.

**Tradeoff.** You lose a guaranteed O(1) point-read of current-value and accept an index
scan (bounded by one owner's slot count). For a personal system that is invisible. You
*gain* deletion of the dual-write consistency obligation across undo/cascade/modality/
valid-time — the exact maintainability tax R1 was trying to cut.

**Essential vs accidental.** Indexed live-selection over append-only assertions =
**essential**. The in-transaction materialized `fact_current` as *authoritative* =
**accidental** — it re-encodes current-state a second time and must be kept in lockstep
through every lifecycle transition, for a corpus small enough not to need it.

---

### N2 — The TWO-key scheme is correct but under-explained for the one job a human does (SEV-2)

**Attack.** §3.2 ships `identity_key` (includes value+modality) and `live_key` (excludes
value for functional, includes modality), plus `value_identity` (minted, value-decoupled,
carried by supersession). This is the right *correctness* answer to functional-over-time
(S1-4) — I do not contest the storage mechanics. The ergonomic problem is the **blast
radius of the second key into everything downstream**: every op must compute *both* keys;
`merge_entities` must re-key both (§3.2); migration must re-key both (S3-1); the unique
index is on `live_key WHERE modality='asserted'` while history groups on `identity_key`;
`replace_member` mints a `value_identity` "decoupled from the value's natural key" and a
later re-extraction "matches via the member's recorded natural-key map." A maintainer now
holds **three identity concepts** (`identity_key`, `live_key`, `value_identity`) plus a
natural-key map, to answer the human's one question: "is this the same fact or a new
one?" The spec never gives the *human-facing* projection of this — the review card hides
cardinality (good, F5) but the underlying three-key model is the thing a maintainer
debugs when undo cascades surprise the owner (N5).

**Simpler alternative.** Keep the two keys (they earn their place for functional-over-
time), but **make `value_identity` the single member-identity primitive and derive both
keys from it deterministically**, documented as ONE function `keys(fact) → (identity_key,
live_key)` with the value-include/exclude switch as its only branch. Forbid any op from
constructing keys by hand. Collapse "natural-key map" into `value_identity` minting (the
map *is* how you mint a stable id). One function, one test, one place to reason — instead
of three keys threaded through ~16 ops, merge, migration, and undo.

**Tradeoff.** None structural; this is a packaging/discipline fix. Cost is writing the
one function and the rule "ops never hand-build keys." Cheap.

**Essential vs accidental.** Two keys for functional-over-time = **essential** (proven
by S1-4). Three independently-reasoned-about identity concepts spread across every op =
**accidental** — collapse to one derivation.

---

### N3 — "Honest typed stage shapes" is a naming win, not yet a typing win (SEV-3, escalates N-cluster)

**Attack.** F3 was graded FIXED, but §2's own head concedes "the wire shapes below are
the same JSON as v0; only the *typing discipline* changed." The single committed example
(§2.1) still shows one envelope with `stage:"resolved"` as a *field*, `entity_id` nullable,
`canonical` nullable, `slot identity absent` at extraction. That is the v0 fat-optional
envelope with a discriminant field bolted on — the exact thing R1-F3 said pushes a runtime
state-machine invariant into every reader. "The type IS the stage" is asserted in prose
(§2 head, §2.1 comment) but the schema shown is one object with a `stage` enum and
everything-nullable. **Illegal-state-unrepresentable requires two distinct types (no shared
nullable envelope); the spec shows one envelope with a tag.** This is the *same* false-
economy R1 named, re-labelled "FIXED."

**Simpler alternative.** Either (a) actually ship two non-overlapping record types
(`ExtractedClaim` has *no* `entity_id`/`canonical`/`slot` fields at all — not nullable,
absent) with one `resolve(ExtractedClaim) → ResolvedFact` function, and let storage be its
third strict shape; or (b) downgrade the F3 disposition from FIXED to "framing clarified,
mechanics deferred to implementation" and stop claiming compiler-enforced illegal-state-
unrepresentable. Pick one; do not claim the strong property while shipping the weak shape.

**Tradeoff.** (a) costs the two mapping functions R1 already priced as cheap. (b) costs
honesty in the disposition table — also cheap, and the spec elsewhere is admirably honest.

**Essential vs accidental.** Two real types = essential to the claim. The single tagged
envelope = accidental, and the FIXED label over-claims.

---

### N4 — The inference-template registry is a speculative sub-system gating a thin slice of value (SEV-2)

**Attack.** §5.2/§7d add a **closed set of typed inference templates** (`age_to_birthyear`,
`ordinal_to_count`, `relative_date`, unit-literal conversions), **each with a deterministic
verifier** that recomputes the value from cited literal spans, plus a per-note inference
cap, plus a dedicated injection-abuse eval slice with a zero-tolerance gate, plus an
`inference_template` provenance field, plus the `kind=inferred` admissibility rule. This
is a **second mini-language with its own registry, verifiers, caps, and eval slice** —
built to admit a handful of derivations (birth-year from age, count from ordinal). For a
single-user system, every one of those derivations is *also* expressible as: store the
literal the note actually stated (`stated_age` / `ordinal`) and compute the derived form
at *read/render* time, with no stored inferred fact, no template registry, no verifier, no
injection surface. The spec even concedes inference is "capped as a fraction of a note" and
that "anything outside the set is `add_fact`/review" — i.e. the feature is deliberately
tiny. A tiny feature with a registry, a verifier-per-template, a cap, a provenance variant,
and a zero-tolerance eval slice is over-built.

**Simpler alternative.** **Don't store inferred facts at all in v1.** Extract and store
only literally-stated values. Where a derived form is needed (birth-year for a timeline),
compute it on read from the stored literal + anchor — a pure function, no persistence, no
template registry, no injection surface (there is nothing to forge because nothing is
written). Defer the inference-template machinery to a later phase *if* a real need for
*persisted* derived facts appears.

**Tradeoff.** You can't query "people born in 1984" without a read-time derivation step
over `stated_age@captured_at`. For a personal corpus that query is rare and the derivation
is one line. You also lose `relative_date` materialization — but relative dates ("next
Tuesday") are *already* resolved deterministically at extraction against `captured_at`;
that is span-grounded parsing, not "inference," and stays.

**Essential vs accidental.** Span-grounded date/unit *parsing* at extraction = essential
(it's already in B2). A persisted-inferred-fact subsystem with a template registry +
per-template verifiers + cap + eval slice = **accidental/speculative** for v1 — it builds
infrastructure for a feature the spec itself scopes to near-nothing.

---

### N5 — Snapshot-undo "cascade-or-block" is correct but NOT comprehensible to a human reviewer (SEV-2)

**Attack.** §1.2: undo of op *k* is legal "only when no later live op depends on *k*'s
outputs (same `slot_key`/`value_identity`/entity); otherwise it **cascades** (undo
dependents first, shown as a preview) or is **blocked with an explicit dependency error**.
Selective mid-history 'remove k's delta but keep k+1' is a **new forward correction**, not
an undo." This is a *correct* model and a good R1 fix. It is also the single least
human-comprehensible thing in the spec. The owner's mental model of undo is Ctrl-Z: "take
back the last thing." v1's undo can, on the owner pressing undo on a 3-edits-ago relink:
(a) silently cascade-tombstone two later edits they thought were independent, behind a
"preview" they must read and reason about a dependency graph to trust; or (b) refuse with
a "dependency error" naming `value_identity`/`slot_key` internals (N2's three keys leak
to the surface here); or (c) tell them "that's not an undo, author a forward correction"
— forcing them to *manually reconstruct* the inverse the system just refused to compute.
For a personal knowledge tool, an undo that sometimes refuses and tells you to hand-build
the correction is an adoption hazard: the owner stops trusting undo, which means they stop
making corrections boldly, which means the wiki rots.

**Simpler alternative.** Two human-facing primitives, both always-available, neither
exposing the dependency graph:
1. **Undo last op** (the literal last applied op for this owner) — always a clean
   tombstone+un-tombstone because nothing later depends on it *by construction* (it's
   last). This covers the fat-finger case, which is ~all real undo demand.
2. For anything older, present it as **"revert to this point"** = re-apply the snapshot
   diff forward (cascade) with a *plain-language* preview ("this will also undo: [2 later
   edits, named in human terms]"), or a one-click **"correct instead"** that pre-fills a
   forward `set_field`/`retime` to the desired end-state. Never surface "dependency error"
   or `value_identity` to the human; never ask them to hand-build the inverse.

Keep the dependency-graph machinery internally; just don't make the human operate it.

**Tradeoff.** "Undo arbitrary mid-history op atomically without touching later ops" is not
offered as a one-click — but the spec already says that case *is* a forward correction, so
this only changes the *framing* the human sees, not the capability. Net: same power,
comprehensible surface.

**Essential vs accidental.** Dependency-gated correctness of history edits = **essential**
(you cannot let undo silently break later facts). Exposing cascade/block/dependency-error
as the human's undo UX = **accidental** and adoption-hostile.

---

### N6 — One-way move + purge is safe but the asymmetry is a usability cliff (SEV-3, accept-risk-adjacent)

**Attack.** §6.4/§7f: domain downgrade is one-way, copy-forward; **undo PURGES the general
row (tombstone), re-protection requires authoring a NEW fact**, and `retime`/`unretract`/
`supersede` are *forbidden* on any `lineage_op_kind='move_domain'` row. This is the correct
*security* answer (it kills the move→undo→retime launder). The ergonomic cost: the owner
who moves a fact health→general by mistake cannot undo it like any other op — undo here
means *destroy the general copy and re-author from scratch in health*. Every other op in
the system undoes uniformly (snapshot); this one op has bespoke, irreversible, "author a
new fact" semantics. That non-uniformity is a thing the owner must *know in advance* or be
surprised by at the worst moment (they moved a sensitive fact wrongly and now can't simply
take it back).

**Simpler alternative.** This is genuinely security-constrained, so the simplification is
**not** to weaken it but to (a) make the *confirmation* (already non-batchable, §6.4c) state
plainly "this cannot be undone normally — to reverse it you will re-author the fact in
[health]," so the asymmetry is disclosed before, not discovered after; and (b) ensure the
"re-author" path is a **one-click pre-filled** new-fact draft seeded from the purged
value's audit row (owner-scoped all-domains view already holds it, §6.4), so re-protection
is one confirmation, not manual re-entry.

**Tradeoff.** None — security model unchanged; only disclosure + a pre-fill added.

**Essential vs accidental.** One-way irreversible downgrade = **essential** (security
S2/M4 proved the launder). The *silent* asymmetry and manual re-author = **accidental**
friction, cheaply removed by disclosure + pre-fill.

---

### N7 — The blind constant-work resolver is gold-plating relative to the single-user threat model (SEV-2)

**Attack.** §6.3 specs the cross-domain resolver as a **separately-privileged, audited,
rate-limited, constant-time/constant-work, decoy-padded, dual-domain-audited** service that
"always runs the full candidate set, decoy-padded, no early-exit on a protected match" to
kill a *timing oracle*. A timing-side-channel-hardened, decoy-padded resolver is a
**nation-state-grade defense**. The threat it defends: a single human owner inferring, from
the *latency* of their own ingest pipeline, whether one of their own entities also exists
in their own health domain. The owner already has the health domain — they can just look.
The realistic adversary here is **prompt-injection content in an ingested note**, and that
is already handled by (i) the committer being the sole writer that re-derives domain and
fails closed, (ii) the resolver returning only opaque match/no-match with no attribute and
no rank into a general row, and (iii) the agent/extractor allowlist barring cross-canonical
links. Constant-work + decoy padding adds real CPU on the **ingest hot path** (the spec's
own perf concern) to close a timing channel that requires an attacker who *is the owner
measuring their own latency* — a non-threat for a personal system.

**Simpler alternative.** Keep attribute-blindness, opaque match/no-match, no-rank-into-
general, rate-limiting, and dual-domain audit (all cheap, all real). **Drop constant-work
+ decoy-padding** as v1 scope; record it as an explicit ACCEPTED-RISK ("local timing
channel observable only to the owner, who already holds both domains") to be revisited
*if* the system ever becomes multi-tenant or hosts an untrusted co-user. The firewall's
job is to stop *content* (injected notes, rendered attributes) crossing — which the
attribute-blind opaque interface already does — not to stop the owner timing their own box.

**Tradeoff.** A multi-tenant future would need the timing defense added back. The spec
already declares single-user; gating this on the multi-tenant transition is exactly the
right time to pay for it. Net: removes per-resolve hot-path CPU now.

**Essential vs accidental.** Attribute-blind opaque resolver + audit + rate-limit =
**essential** (real injection defense). Constant-work + decoy padding for a timing oracle =
**accidental gold-plating** under the stated single-user threat model.

---

### N8 — The 3-way overlay diff is essential to re-analysis but is being built before re-analysis exists (SEV-3)

**Attack.** §5.3 specs a **3-way diff** `(old machine facts ⊕ human-op overlay) vs new
machine facts` with retraction-suppression, human-touched freezing, split-lineage overlap
routing, per-pin shape-lift-or-blocker, incremental blast-radius scoping, and no-op
suppression. This is correct and even elegant *for re-analysis*. But D1 (the decision log)
says the **initial cutover is a clean rebuild — no in-place migration**, and §5.5 is marked
OUT OF SCOPE. So the 3-way overlay machinery defends *future contract-version re-analysis*
that does not exist in v1 and whose first instance is gated behind a major-version bump.
Building the full overlay reconciliation now — six sub-rules, each a code+test path — is
infrastructure ahead of its first user.

**Simpler alternative.** v1 ships **only** the two rules that bite on day one of *normal
re-ingest* (not contract re-analysis): retraction-suppression (M2 — a re-ingested note
must not resurrect a fact the owner retracted) and human-touched freezing (M3 — don't let
re-ingest clobber a human edit). Those two are needed the first time the owner edits a note
they already ingested. **Defer** split-lineage-overlap, per-pin shape-lift, and
incremental-blast-radius to the phase that actually introduces a major contract migration.

**Tradeoff.** When the first major contract bump lands, that phase must build the deferred
three rules — but it can build them *against a concrete migration*, not speculatively. Net:
smaller v1, same eventual capability, built when its requirements are real.

**Essential vs accidental.** Retraction-suppression + human-touched freezing = **essential
to v1** (they fire on ordinary note re-ingest). The full per-pin/shape-lift/blast-radius
overlay = **essential to re-analysis, accidental to v1** — defer.

---

## PART C — Adjudications requested

### F7 verdict: ship 5 TypedValue variants, not 7

I adjudicate F7 (left open in §7.l and §9 as a "judgement call"): **ship 5 now —
`enum, quantity, date, text, ref` — and defer `boolean` and `structured`.**

- **`boolean`** is strictly `enum` over `{true,false}`. The spec keeps it only to avoid
  `{"type":"enum","code":"true"}`. That is one fewer variant, one fewer editor widget, one
  fewer parser branch, one fewer row in the value_shape↔registry 1:1 map, for zero lost
  capability. Fold it into `enum` with a conventional `{true,false}` domain.
- **`structured`** is a HARD CLOSED SET today (§2.2) — meaning its *only* current member is
  `address`. A discriminated-union member whose population is one shape, carrying a
  `propose_shape` review fast-path and a "never model-coined" guard, is a
  schema-registry-inside-a-union built for a population of one. For a personal system,
  model `address` as the thing it is in practice — a small set of `text`/`enum` facts
  (line, city, region, postal) — keeping every value scalar and every editor flat, and add
  the `structured` variant **when a second real struct appears**. This also deletes the
  `propose_shape` fast-path and the closed-set-membership guard from v1.

**Why this overrides the spec's "retain 7."** The spec's defense (§2.2, §9) is that 7 map
1:1 onto `value_shape` so "contract↔registry can't drift." That property is *preserved* at
5 — it's still 1:1, just a shorter list. Retaining `boolean` and a one-member `structured`
buys nothing the registry doesn't already give, and each variant is a full vertical
(producer + parser + validator + editor + registry enum). The R1 grading of F7 as SEV-3
stands — this is not load-bearing — but the open question resolves cleanly toward 5.

**Counter-acknowledgement:** if a concrete second `structured` shape (phone-with-extension,
geo-point) is *already* in the corpus, the calculus flips and `structured` ships now. The
spec gives no such second shape, so: 5.

---

## Summary table

| ID | Title | Sev | Simpler alternative | Tradeoff |
|---|---|---|---|---|
| N1 | Materialized `fact_current` is a 2nd source of truth, dual-written every op | 2 | Indexed query over append-only assertions; async matview only if proven slow | Lose O(1) point-read; gain no dual-write consistency tax |
| N2 | Two-key + value_identity = 3 identity concepts threaded everywhere | 2 | One `keys(fact)` derivation function; `value_identity` the sole member primitive | None structural; packaging discipline |
| N3 | "Honest typed stages" is a naming win, not a typing win | 3 | Two non-overlapping types OR downgrade F3 from FIXED | 2 mapping fns, or disposition honesty |
| N4 | Inference-template registry over-built for a deliberately-tiny feature | 2 | Don't persist inferred facts in v1; derive at read from stored literals | Rare read-time derivation step; defer persisted-inference |
| N5 | Cascade-or-block undo is correct but not human-comprehensible | 2 | "Undo last" (always clean) + "revert to point"/"correct instead"; hide the graph | Mid-history atomic undo reframed as forward correction (already the spec's stance) |
| N6 | One-way move + purge: safe but a silent usability cliff | 3 | Disclose irreversibility up front + one-click pre-filled re-author | None; security unchanged |
| N7 | Blind constant-work decoy-padded resolver is gold-plating for single-user | 2 | Keep attribute-blind opaque resolver; drop constant-work/decoy as ACCEPTED-RISK until multi-tenant | Re-add timing defense at multi-tenant transition |
| N8 | 3-way overlay diff built before re-analysis exists | 3 | Ship only retraction-suppression + human-touched freezing in v1; defer the rest | Build deferred rules against the real migration later |

## Positions on the requested questions

- **R1 wins CONFIRMED:** F2 (snapshot undo, ~22 inverses deleted), F1 (~12-op set + shared
  enums + dumb triage shell), F5 (Approve/Needs-fix default), fewer-kinds re-baselined to
  decision points. **PARTIAL/over-claimed:** F3 — naming landed, the "compiler-enforced
  illegal-state-unrepresentable" property did not (still one tagged nullable envelope) (N3).
- **NET complexity verdict:** the *ops/review/undo* surface is genuinely simpler than v0.
  The *storage/security/migration* surface got **heavier** — `fact_current` (N1), the
  third identity concept (N2), the inference registry (N4), the decoy resolver (N7), the
  full overlay diff (N8). Each was justified by a single non-ergonomic lens and never
  costed against simplicity together. For a single-user system, N1+N4+N7 are the clearest
  cases where a much simpler model serves the same goal.
- **Cheapest 80%-value cut:** **N1 — delete the materialized `fact_current` as an
  authoritative dual-write and serve current-value from indexed queries over the
  append-only assertions you already keep.** It removes the highest-frequency consistency
  obligation in the system (every op, plus every undo/cascade) on a corpus far too small to
  need it, while keeping every binding invariant — the same shape of win F2 was in R1, now
  on the read path.
- **Comprehensibility of cascade-or-block undo:** **No** — it leaks the dependency graph
  and the three-key internals to a human whose model is Ctrl-Z, and sometimes refuses and
  demands a hand-built correction (N5). Needs-fix progressive disclosure (F5) *is* low-load
  and lands; the *undo* surface does not.
- **F7:** ship **5** (`enum, quantity, date, text, ref`); fold `boolean` into `enum`,
  build `structured` on demand. The 1:1 registry property is preserved at 5.

---

*End R2 ergonomics. New findings: 2× borderline-SEV-1 (N1, N5 — graded SEV-2 because each
is fixable without re-architecting, but both edge SEV-1 on maintainability/adoption
respectively); 4× SEV-2; 2× SEV-3. No finding breaks a binding invariant — these are
over-engineering and ergonomics, not correctness or security regressions.*
