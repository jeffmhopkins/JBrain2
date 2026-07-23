# JBrain2 — Ingest V2: LLM-Judged Disposition + Non-Destructive Supersession (sideload → cutover)

> **Status:** Proposed (not scheduled) · **Last verified:** 2026-07-23 — nothing
> built. Grounded by three scoped researchers (current-pipeline code map, review-inbox
> burden + churn, and an eight-system industry survey of RAG/KG memory ingestion) and
> owner-directed. This doc is the icebox record per `docs/DOC_LIFECYCLE.md`; promotion
> to `docs/plans/` requires the §11 open decisions ratified and a roadmap slot in
> `docs/ROADMAP.md`. Supersedes the *mechanism* (not the thesis) of
> `docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md`: that plan trimmed the predicate
> vocabulary (spine-not-encyclopedia, shipped PR #718); this plan changes *who decides
> disposition* and *what a conflict costs*, on top of that trimmed spine.

## Thesis

The graph pipeline gets the **shape** right (notes source facts, the graph arbitrates
current truth, everything is non-destructive history) but the **disposition policy**
wrong for a single-user corpus. Two structural facts, both verified in code:

1. **The default fate of any inferred fact is a review card.** `INFERRED_CEILING = 0.6`
   (`analysis/weight.py:28`) sits *below* every commit threshold — attribute 0.8,
   relationship/state/measurement/event 0.7 (`weight.py:37-44`). So `0.6 < 0.7` means a
   fact the model flags `inferred` **cannot** clear its threshold and is forced to
   `low_confidence_inference` review. The entire back half of `analysis/arbiter.py`
   (~400 lines: eight deterministic "attest anyway" backstops — `_object_named`,
   `_relationship_object_named`, `_gender_grounded`, `_date_phrase_grounded`,
   `_time_grounded`, plus `recover_dropped_fields`, `dedup_intent_facts`,
   `derive_kinship_gender`) exists **only to claw specific note-shapes back from that
   trap.** Every note shape none of the eight recognizes becomes a fresh pile of cards.
   This is the single highest-volume source of review noise, and it fires on facts the
   note *genuinely states*.

2. **Every conflict costs a human click.** `analysis/supersession.decide`
   (`supersession.py:526-796`) refuses to auto-supersede attributes/measurements/events:
   two birthdays → both to review (`:605-624`), most conflicts → `fact_conflict`. That is
   a sound default when notes disagree *by accident* across many authors. But the owner is
   the *only* author and is usually *deliberately correcting their own data* — and the
   policy cannot tell "the owner is updating this" from "two notes accidentally disagree,"
   so corrections **collide instead of winning** (the loop PR #937 fought).

The redundancy the owner senses is real: the **Integrator LLM call already sees the
current graph and already emits `supersession_proposals`/`merge_proposals`**
(`analysis/intent.py:99-133`, prompt `integrate_note.prompt:12-21` "You decide MEANING"),
but the deterministic layer **throws that judgment away** — the proposals are demoted to a
display/trace signal (`arbiter.py:625-628`, `pipeline.py:594-598`), and a conservative
deterministic re-derivation decides commit-vs-review instead. The semantic judgment
happens once, in the LLM, and is then overruled.

**The move:** let the LLM *own* the commit-vs-escalate disposition (industry-standard —
§4), default conflicts to **non-destructive supersession** instead of review (Graphiti-
standard — §4), and let structured corrections write **directly** instead of laundering
through a prose note. Keep a small deterministic **safety spine** the LLM never touches:
the domain firewall, RLS, whole-intent atomicity, pinned-override immutability, and
**determinism across re-runs** (§6 — the one hard problem the industry does not solve for
us). Build it as a **sideload** (§7) that runs in shadow against the live pipeline and is
validated on the owner's local box before any cutover.

## 0. What this plan does NOT do

- **No change to the fact grammar or the graph shape.** Facts stay
  `entity.predicate[.qualifier]` edges; supersession chains stay the full non-destructive
  revision history (`docs/reference/ANALYSIS.md` "The fact grammar"). Nothing is ever
  deleted except on note deletion. This plan changes *policy over that shape*, not the
  shape.
- **No weakening of the domain firewall.** The firewall floor/ratchet
  (`analysis/extraction.py:149-171`, applied at `pipeline.py:1902-1909`) and its RLS
  enforcement stay deterministic and untouched. The LLM may *detect* a cross-subject link
  (it already sets `cross_subject`, `intent.py:57`); the fail-safe *routing* of a
  cross-subject fact stays deterministic code (§5, invariant I2).
- **No destructive deletes.** Unlike Mem0 base (§4), a superseded fact is never removed —
  it is chained with retained history, exactly as `state` facts already are (SCD-2).
- **No big-bang cutover.** The new pipeline ships behind a flag, runs shadow-only first,
  and is diffed + graded on the local box before it writes owner-visible state (§7).
- **Notes remain the sole sources of truth; the wiki stays machine-written.** Lever C
  (§5) removes the *prose-note round-trip* for a **structured** fact correction, not the
  doctrine — a prose/wiki correction still files a correction note.
- **No new runtime dependency** (CLAUDE.md #8 goal) and no GUI surface change in the
  shadow waves (the review-inbox UI only *shrinks*; the GUI gate is only tripped by the
  Lever-C correction affordance in the final wave — §9).

## 1. The current pipeline, precisely (correcting the "three LLM calls" model)

It is **two LLM calls + a deterministic layer**, not three calls (`pipeline.py:289-452`):

| Stage | Kind | Sees | Produces |
|---|---|---|---|
| Extraction (`_extract_note`, `pipeline.py:342`) | 1 strong LLM call | paragraph chunks + capture anchor | `Extraction`: facts, mentions, temporal tokens (`note_extract.prompt` v31) |
| Integration (`Integrator.integrate`, `integrate.py:38`) | 1 strong LLM call | note text + extraction + **current graph context** (existing entities + live facts, with ids; `pipeline.py:360-367`) | `IntegrationIntent`: resolutions, facts, **supersession/merge/distinct proposals** (`intent.py:123-133`, `integrate_note.prompt` v12) |
| Repair passes | deterministic | the intent | five in-place fixes (`recover_dropped_fields`, `derive_kinship_gender`, `canonicalize_intent`, `dedup_intent_facts`) — `pipeline.py:380-395` |
| Arbiter `plan_intent` | deterministic, pure | intent + chunk texts | commit/review/reject partition (`arbiter.py:95-178`) |
| Apply `apply_intent` | deterministic | plan | writes facts/entities/chains + files review cards (`pipeline.py:454-532`) |

The review surface is decided in exactly two places:

- **`plan_intent`** (`arbiter.py:135-170`): weight-below-threshold (R1), inferred ceiling
  (R2, `weight.py:28-31`), ambiguous mention (R3), cross-subject (R4), merge/distinct (R5).
- **`supersession.decide`** (`supersession.py`): same-instant measurement clash (`:583`),
  attribute collision both-sides-held (`:605-624`), relationship contradiction (`:626`),
  irrealis-vs-asserted (`:752`), pinned-head re-flag (`:610,712,759`), low-confidence
  overwrite guard (`:721,767`).

Of these, **only R4 (cross-subject) and the firewall are safety-critical**; the rest is
heuristic conflict-handling and model-error compensation, deterministic here for
*stability, idempotency, and auditability* — not because an LLM would be unsafe. (Full map:
the current-pipeline researcher's report, archived alongside this plan's research dossier.)

## 2. The review-burden math (why it feels intrusive)

`low_confidence_inference` dominates card volume, by construction: `INFERRED_CEILING 0.6 <
COMMIT_THRESHOLDS 0.7–0.8`. The eight backstops are the maintenance tax of fighting that
gap one note-shape at a time; each docstring names a real recurring pain ("the run-to-run
flip on conjoined objects," "holding one per family member per note is pure review
noise," "the owner sees a review card for a fact already on the graph"). Second and third
offenders — `attribute_collision` and `fact_conflict` — are the deliberately-conservative
conflict policy firing on the owner's own restatements and corrections.

The load-bearing/safety cards (firewall: `domain_promotion`, `inverse_proposal`; identity:
`merge_proposal`, `ambiguous_mention`; genuine contradiction: `fact_conflict`,
`low_confidence`) are a **minority** of real volume. The bulk is policy, not safety.

## 3. Why PR #937 patched but did not cure

The correction loop (`api/analysis.py:194-239`): an owner "correct it" mints an
`owner_correction` **note** that re-runs the whole extract→integrate→force-supersede+pin
pipeline just to change one value. #937 (a) dropped `statement` from the dedup key so N
paraphrases of a `value_json`-null fact collapse (`arbiter.py:529`) and (b) routed the
correction through `owner_correction` so it force-supersedes instead of colliding. That
**breaks the loop** — but the root cause it *names* ("value trapped in prose, not
value_json") is untouched: the collapse only works when all copies share a `value_json`
(here, all null); two *different* `value_json` shapes for one value would still fragment.
And the round-trip itself — a structured fix laundered through a prose note + a full LLM
pass — is the ergonomic complaint. Lever C (§5) removes it.

## 4. How the industry does this (grounded)

Eight OSS RAG/KG memory systems surveyed (repos/papers in the research dossier). The
pattern for the exact step in question — *compare new info against the graph, decide
add/update/invalidate* — is near-universal and **not** what JBrain2 does:

| System | Calls/ingest | Entity dedup | Conflict reconciliation | Human review? | Confidence gate? |
|---|---|---|---|---|---|
| **Mem0** (arxiv 2504.19413) | 1 extract + 1 per-fact update | embed top-k=10 | **1 LLM call → ADD/UPDATE/DELETE/NOOP** over neighbors; **destructive DELETE** | **none** | none |
| **Graphiti/Zep** (arxiv 2501.13956) | multi (extract, resolve, invalidate) | embed cosine + full-text → LLM | **LLM contradiction check → invalidate old edge by timestamp**; non-destructive, bi-temporal | **none** | none (temporal validity) |
| **MS GraphRAG** | many (+ "gleanings") | exact name group | **none** — concatenate descriptions, reader LLM reconciles at query time | **none** | none |
| **LightRAG / nano-graphrag** | 1 + ≤1 gleaning | exact name | **none** — additive, coexist | **none** | none |
| **Cognee** (ECL) | ~6-stage | content-hash + LLM unify | ontology-grounded; no bi-temporal | **none** | none |
| **Letta/MemGPT** | agent self-edits | agent decides | agent overwrites memory block; **conversational** correction | **none (pull, not queue)** | none |
| **LlamaIndex PGIndex** | schema-guided extract | user-supplied | **none built-in** | **none** | none |

Three conclusions:

- **The dominant reconcile pattern is embedding-retrieve neighbors → one LLM judgment
  call** (Mem0 is the purest). **Nobody uses a deterministic weighing arbiter.** This
  validates the owner's instinct directly.
- **Nobody has a human review inbox.** But — the load-bearing insight — **they skip it not
  because the LLM is trusted, but because they made being wrong cheap:** either
  *non-destructive* (Graphiti: keep both, timestamp validity, reconcile at read) or
  *non-authoritative* (GraphRAG: concatenate, dilute in retrieval). Mem0 base is the
  exception and pays for it with silent destructive loss — acceptable for chat
  personalization, **not** for a machine-written wiki treated as truth.
- **JBrain2's inbox is noisy because it made the opposite choice on both axes:** its graph
  is *authoritative* (feeds the wiki) **and** its arbiter tries to get every fact right *at
  write time*. The cure is therefore not only "a better single call" — it is **also**
  shifting the cost of uncertainty off the human, the way Graphiti does.

The strongest published direction is the hybrid Mem0ᵍ and Graphiti are independently
converging on: **one LLM operation-decision per fact, implemented non-destructively.**
That is exactly Levers A+B.

## 5. The proposed model — three levers on one safety spine

### Lever A — the Integrator owns commit-vs-escalate (single judgment)

The integrator already sees the graph and proposes supersessions. Give it authority to
emit, **per fact**, an explicit disposition: `commit` | `supersede` | `escalate` (with a
machine-readable reason), plus the operation on any existing head. The deterministic layer
then **enacts** the disposition rather than re-deriving it — subject only to the safety
spine below. Consequences:

- **Delete `INFERRED_CEILING`/`COMMIT_THRESHOLDS` as the review gate** (`weight.py`) and
  **delete the eight backstops** (`arbiter.py`) — they exist solely to fight a gate that
  no longer exists. (Keep `dedup_intent_facts` as a cheap idempotency net; retire the
  attest-anyway rescuers.) This is the bulk of the friction *and* the bulk of the arbiter
  line count, gone together.
- The model's self-confidence stops being a floor-fighting number and becomes one input to
  *its own* escalate decision — where run-to-run noise is tolerable because a stable
  disposition is pinned (§6).

### Lever B — non-destructive supersession is the default; review is the exception

Change `supersession.decide`'s default for a conflicting head from "hold both for review"
to **"the newest asserted value becomes the active head; the prior is chained with retained
history"** — exactly what `state` facts already do (SCD-2), extended to attribute /
measurement / functional-relationship kinds. Escalate to a review card **only** when:

1. the LLM's disposition is `escalate` (it judged a genuine unresolvable contradiction — a
   hidden two-people split, a safety conflict), **or**
2. a **safety-spine** rule fires (firewall promotion, cross-subject, pinned-head overwrite,
   low-self-confidence-would-overwrite-good-data — the OCR guard `supersession.py:721`
   stays), **or**
3. a **structural** conflict the LLM can't see (e.g. two live namesakes) needs identity
   adjudication.

Everything else supersedes silently, with full history retained and reversible. The inbox
shrinks to safety + genuine ambiguity. *(Measurement "never auto-supersede" stays as a
per-kind accumulate — a new reading is a new datapoint, not a supersession; that is not a
conflict and files no card. Only genuinely contradictory same-instant readings escalate.)*

### Lever C — structured corrections write directly

`docs/reference/ANALYSIS.md` already states a correction's weight "is *implemented as*
pinning the facts it asserts." So for a **structured** fact fix from a review card, write
the pinned override **directly** (force-supersede + `pinned=true`, recording the owner
action for provenance) — no minted note, no LLM round-trip. Keep correction-*notes* for
**prose/wiki** corrections (the doctrine that governs machine-written text). This kills the
"it has to log a note as another thing of truth" round-trip for the structured case while
preserving the note-as-truth rule where it belongs. *(This is the only lever that adds a
GUI affordance — §9, GUI gate.)*

### The safety spine (deterministic, the LLM never decides these) — invariants

- **I1 — Domain firewall + RLS.** Floor/ratchet (`extraction.py:149-171`) and RLS-scoped
  writes are deterministic and unchanged. A hallucinated domain is a leak, not a nit.
- **I2 — Cross-subject routing.** The LLM may set `cross_subject`; a cross-subject fact is
  *deterministically* routed (staged/escalated), never auto-committed. (N3.)
- **I3 — Whole-intent atomicity.** A fatal structural violation rejects the whole intent;
  no partial commit (`intent.py`, `arbiter.py:116-123`).
- **I4 — Pinned-override immutability.** A human/owner-pinned head is never auto-flipped,
  only re-flagged (`supersession.py:610,712,759`). The LLM cannot overrule a human.
- **I5 — Determinism across re-runs.** Re-analysis of an unchanged note must produce the
  same graph. This is the invariant the industry does *not* preserve — §6.

## 6. The hard problem: determinism across re-runs (and its solution)

Mem0/Graphiti **never re-ingest the same episode**, so a non-deterministic LLM disposition
never bites them. JBrain2 **does** re-extract on model/prompt upgrades
(`POST /api/notes/{id}/analyze`, corpus re-runs), upserting on the structural identity key
(`docs/reference/ANALYSIS.md` "Reprocessing"). If the LLM freely decides supersession, a
re-run could non-deterministically flip a disposition — "a silent flip is the one outcome
no layer may produce" (`docs/reference/ANALYSIS.md` "Same-name coexistence").

**Solution — persist the disposition, keyed to the structural identity.** The LLM's
disposition for `(entity, predicate, qualifier, value_json-hash)` is recorded (a
`disposition` provenance row, mirroring how human decisions pin today). On re-analysis, a
matching key **reuses the stored disposition** instead of re-rolling it; only a *new* key
(genuinely new fact) draws a fresh LLM judgment. This makes re-runs idempotent by
construction while still letting the first pass be an LLM call. It is the same mechanism as
today's pinned human overrides, extended to cover machine dispositions — and it is the one
piece that cannot be copied from Mem0. **This is the load-bearing design decision of the
plan and the first thing the adversarial reviews must attack.**

*(Alternative considered — feed the prior disposition into the re-run prompt as context and
accept "usually stable": rejected as insufficient; "usually" is a silent-flip risk. The
persisted-key approach is deterministic by construction.)*

## 7. The sideload: shadow first, cut over only after local-box validation

The codebase already has the exact pattern this needs — **do not invent new plumbing.**

- **Shadow precedent.** The Phase-5 workflow engine ran a *shadow observation* alongside
  the hardcoded path and **diffed** before Wave 2 cut over (`workflow/events.py:1-14`,
  `shadow_enqueued`). Ingest V2 reuses this shape: a new registry action
  `integrate_note_v2` (`workflow/registry.py:177` is the `integrate_note` template) runs
  **read-only against a shadow store**, producing a v2 result diffed against the live v1
  graph write — never touching owner-visible state.
- **Local-box test harness precedent.** The integrate eval already drives the *real*
  prompt through an **injected router** and scores judgment + safety against golds, "the
  box driver passes a live one" (`evals/integrate_runner.py:199-235`,
  `evals/integrate_cases/`). V2 extends this: the same harness runs the single-call v2
  prompt through the **local** provider (the router's per-task override routes
  `integrate.note.v2` → `local:<model>`, `docs/reference/ANALYSIS.md` "Runtime routing
  overrides"; catalog `llm/local_catalog.py`) on the owner's Strix-Halo box, scoring the
  v2 disposition against the same golds *plus* a new "cards-filed" delta metric.
- **Shadow diff = the acceptance artifact.** For a window of real notes, run v1 (live,
  authoritative) and v2 (shadow, read-only) on the same input; record per-note: facts
  committed, dispositions, and **cards that would have fired**. The owner reviews the diff
  (a shadow report, not the live inbox). Cutover is gated on: (a) v2 files materially fewer
  cards, (b) no tier-1 recall regression vs v1 on the graded corpus
  (`tests/eval/corpus/`), (c) firewall/RLS parity (every v1 firewall action reproduced),
  (d) re-run determinism proven (§6) on the harness.
- **Cutover = flip the dispatcher**, with v1 kept behind the flag for one release as the
  rollback. No data migration (the graph shape is unchanged); the shadow store is dropped.

This is the "put it over the side, test with the local LLM, then commit" path the owner
asked for, expressed in the repo's own shadow/eval idiom.

## 8. Waves (per `docs/reference/PROCESS.md`)

| Wave | Delivers | Depends on | Size | Writes owner state? |
|---|---|---|---|---|
| V0 | **Spike/host-validation:** the single-call v2 prompt + `parse` + the integrate-eval extension, run against the **local box** model; a go/no-go on local-model judgment quality before any engine work. Blocking. | §11 decisions | M | no |
| V1 | v2 **disposition schema** + the `disposition` persistence + the deterministic *enactor* (safety spine I1–I5) — pure, unit-tested, no LLM. | V0 | M-L | no |
| V2 | `integrate_note_v2` **shadow action** + shadow store + the shadow-diff report. Read-only. | V1 | M | no (shadow) |
| V3 | **Non-destructive supersession default** (Lever B) behind the v2 enactor; corpus/harness re-tier; the cards-filed delta metric. | V1 | M-L | no (still shadow) |
| V4 | **Owner acceptance gate:** shadow-diff + local-box eval artifact (§7 a-d). Owner ratifies cutover. | V2,V3 | — | no |
| V5 | **Cutover** — dispatcher flip, v1 behind rollback flag; **Lever C** direct-correction affordance (**GUI gate: three mocks** for the changed review-card action, `PROCESS.md` GUI gate). | V4 | M | **yes** |
| V6 | Retire v1 + the eight backstops + the dead ceiling; docs reconciliation (`ANALYSIS.md`, `ENTITY_GRAPH_REFOCUS_PLAN.md`, this plan → archive). | V5 stable one release | M | — |

Conventional Commits per wave; one PR per wave; per-task + per-wave adversarial review;
security/red-team review mandatory on V1 (safety spine), V3 (supersession), V5 (cutover +
firewall parity). CI green before merge.

## 9. GUI gate

Only **V5** touches a GUI surface: the review card's "correct it" changes from
"compose a correction note" to "apply structured fix" (Lever C). Three interactive mock
HTML artifacts per `docs/reference/DESIGN.md`, owner-chosen before V5 build. The inbox
*shrinking* (fewer cards) is not a surface change and trips no gate.

## 10. Non-negotiables reconciliation (`CLAUDE.md`)

1. LLM via the adapter — the v2 call is a `router.complete` task like every other (§7). ✓
2. File I/O via storage abstraction — unchanged. ✓
3. RLS + isolation tests — I1/I2 unchanged; the shadow store and any new provenance table
   get the standard `has_domain_scope` policy + an RLS isolation test. ✓
4. Comments explain why — enforced in review. ✓
5. Tests same PR; 80%/security-100%; real Postgres; LLM faked — the shadow diff and enactor
   are testcontainer-tested; the safety spine is security-100%. ✓
6. Conventional Commits; branch+PR; CI green. ✓ (§8)
7. Wiki machine-written; humans correct via correction notes — **preserved for prose**;
   Lever C narrows the *structured* case only (§5). ✓ *(owner-ratify — §11.6)*
8. `dev-setup.sh` updated with any new tool/step — no new dep expected. ✓
9. Docs travel with code — §8 V6 reconciles. ✓

## 11. Open decisions for the owner (recommended default first)

1. **Disposition authority scope.** Default: LLM decides `commit|supersede|escalate`; the
   safety spine (I1–I5) can only *escalate* the LLM's call, never *downgrade* an escalate
   to a commit. Alternative: keep a deterministic weight as a second, conservative vote.
2. **Non-destructive default breadth.** Default: extend silent-supersede-with-history to
   attribute + state + functional-relationship; keep measurement/event as accumulate.
   Alternative: also auto-supersede within measurement series (riskier — loses the
   "contradictory reading" signal).
3. **Determinism mechanism (§6).** Default: persist disposition keyed to structural
   identity (deterministic by construction). Alternative: prompt-context stability
   (simpler, weaker). **This is the plan's crux — ratify explicitly.**
4. **Escalation floor.** With the ceiling gone, what still *forces* review beyond the
   safety spine? Default: only LLM-`escalate` + structural namesake ambiguity. Alternative:
   a small retained floor for health/finance value changes (belt-and-suspenders on the
   firewall domains).
5. **Local-model judgment quality (V0 gate).** If the local box model can't match cloud
   judgment on the integrate golds, do we (default) ship v2 on **cloud** first and move to
   local when the model lands, or block on local parity? (The privacy-routing axis already
   supports either — `docs/reference/ANALYSIS.md` "Privacy routing".)
6. **Lever C doctrine (CLAUDE.md #7).** Default: direct structured writes for review-card
   fixes; correction-*notes* retained for prose/wiki. Ratify that this does not violate
   "humans correct via correction notes."
7. **Single-call vs keep-two-calls.** Default: **keep extraction and integration as two
   calls**, move only the *disposition authority* into integration (the token saving of
   merging is minor and the long-note map-reduce + clean extraction artifact for re-runs
   argue against merging). Alternative: true single-call Mem0-style (bigger blast radius,
   re-run artifact lost).

## 12. Risks (honest)

- **Local-model judgment is unproven** — V0 is the blocking spike; §11.5 is the fallback.
- **The determinism mechanism (§6) is novel here** — if the persisted-disposition key is
  wrong (e.g. value_json-shape drift changes the key), a re-run could still flip. The key
  design must be attacked in the V1 red-team; the divergent-`value_json` fragility PR #937
  left standing (§3) is the same failure mode and must be closed here, not inherited.
- **Non-destructive default could mask a real contradiction** the old review surfaced — the
  cards-filed delta must be paired with a *recall* check (V3): did any conflict that
  *should* have escalated get silently superseded? The shadow diff is where this is caught
  before cutover.
- **Firewall parity is safety-critical** — the shadow diff must prove every v1 firewall
  action (promotion, cross-subject hold) is reproduced by v2; V5's security review
  red-teams this specifically.
- **Backstop deletion is broad** — removing ~400 lines + their tests must not drop coverage
  below the 80% gate; verify locally per `ENTITY_GRAPH_REFOCUS_PLAN.md`'s coverage-on-
  deleted-paths note.
- **Wiki leans on this** — Phase 6 treats the graph as truth; a supersede-by-default that
  silently changes a head changes what the wiki publishes. The Phase 6 plan
  (`docs/plans/PHASE6_WIKI_PLAN.md`) must be re-read against this before V5.
- **Two pipelines during shadow** double the ingest LLM cost for the shadow window —
  bounded (shadow runs on a sample or off-peak), and temporary.
