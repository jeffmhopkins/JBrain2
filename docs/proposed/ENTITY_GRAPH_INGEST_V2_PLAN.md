# JBrain2 — Ingest V2: Flip the Disposition Default (fewer cards, same safety)

> **Status:** Proposed (not scheduled) · **Waves:** V0◻️ V1◻️ V2◻️ V3◻️ V4◻️ V5◻️ · **Last verified:** 2026-07-24 — on-box gpt-oss-120b validation done (§15): ingest gap is prompt+schema, not architecture; agentic/multi-tier ingestion evaluated and rejected (§16).

The **icebox record** per `docs/DOC_LIFECYCLE.md` — nothing built. Promotion to
`docs/plans/` requires the §11 open decisions ratified and a `docs/ROADMAP.md`
slot. This plan **corrects-in-place** (not supersedes) two Living docs when it
builds: `docs/reference/ANALYSIS.md` (the per-kind conflict policy and the
correction-note doctrine change) and `docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md`
(whose §6 rationale leans on the `INFERRED_CEILING` gate this plan removes). Both
stay Living and CLAUDE.md-cited; neither is archived. Grounded by three
current-system researchers + four independent adversarial reviews (safety/RLS,
architecture/feasibility, industry-grounding, process/scope); this is post-review
**v0.2** — §6 and the sideload were redesigned after the safety and feasibility
reviews broke the v0.1 mechanism (§13 records what changed and why).

## Thesis

The pipeline gets the **shape** right (notes source facts; the graph arbitrates
current truth; supersession chains are full non-destructive history) but the
**disposition default** is tuned for a multi-author corpus, not a single owner.
Two verified structural facts:

1. **The default fate of an inferred fact is a review card.** `INFERRED_CEILING =
   0.6` (`analysis/weight.py:28`) is below every commit threshold — attribute 0.8,
   relationship/state/measurement/event 0.7 (`weight.py:37-44`). So a fact the model
   flags `inferred` cannot clear its threshold and is forced to
   `low_confidence_inference`. The back half of `analysis/arbiter.py` (~400 lines:
   eight deterministic "attest anyway" backstops — `_object_named:181`,
   `_relationship_object_named:204`, `_gender_grounded:300`, `_date_phrase_grounded:568`,
   `_time_grounded:584`, `recover_dropped_fields:396`, `dedup_intent_facts:487`,
   `derive_kinship_gender:338`) exists **only to claw specific note-shapes back from
   that trap.** Any shape none of the eight recognizes becomes a pile of cards, on
   facts the note *genuinely states*. This is the dominant noise source.
2. **Every conflict costs a human click.** `analysis/supersession.decide`
   (`supersession.py:526-796`) refuses to auto-supersede state/attribute/measurement:
   two birthdays → both held (`:605-624`), most conflicts → `fact_conflict`. Sound when
   notes disagree *by accident* across authors; wrong when the owner is the *only*
   author *deliberately correcting their own data* — corrections collide instead of
   winning (the loop PR #937 fought).

The redundancy the owner senses is real: the Integrator already sees the graph and
emits `supersession_proposals` (`intent.py:99-133`, `integrate_note.prompt` "You
decide MEANING"), but the deterministic layer discards them — advisory only
(`arbiter.py:625-629`, `pipeline.py:594-598`) — and re-derives conservatively.

**The move — smaller and safer than v0.1 framed it.** Do **not** move
safety-critical disposition into a non-deterministic LLM (the safety and feasibility
reviews showed why — §13). Instead:

- **Lever A — remove the inferred-ceiling *review* trap** so a model-asserted fact
  commits by default, retiring the eight backstops that only existed to fight it —
  while **keeping a narrow escalation net for sensitive-domain inferred facts** (§5).
- **Lever B — flip the deterministic conflict *default* from flag-to-review to
  supersede-with-retained-history** for the kinds that already do newest-wins
  (`state`, functional `relationship`), **by validity time**, while **keeping**
  attribute-collision and the other identity/safety floors as review (§5).
- **Lever C — let a structured review-card correction write its pinned override
  directly** (re-running the pipeline's shape/firewall/scope checks), dropping the
  prose-note round-trip for the *structured* case only (§5).

Re-run determinism stays exactly where it is today: the deterministic enactor
**recomputes** conflict disposition over the current graph (idempotent) — there is
**no cached LLM verdict** (§6). The LLM's authority grows only in the *soft*
direction: it can *raise* an escalate signal for genuine ambiguity; it can never
*lower* a safety floor. Built as a **corpus-snapshot sideload** validated on the
owner's local box before cutover (§7).

## 0. What this plan does NOT do

- **No change to the fact/graph shape.** Facts stay `entity.predicate[.qualifier]`
  edges; chains stay non-destructive history. Policy over the shape changes, not the
  shape. No graph-shape migration.
- **No cached LLM disposition.** v0.1's "persist the disposition keyed to structural
  identity" is **dropped** — the safety review showed a disposition depends on current
  graph state, so a cached verdict is stale-*wrong*, and `value_json`-null collapses
  distinct values onto one key (§13, F1/F2). Determinism comes from deterministic
  recomputation, as today.
- **No blanket attribute auto-supersede.** Lever B is scoped to `state`/functional-
  `relationship`; `attribute` conflicts (two birthdays) **stay** review — they are the
  hidden-two-people-merge identity signal (`supersession.py:605-624`; §5, I6).
- **No weakening of the firewall.** `domain_floor` (`extraction.py:177`) /
  `ratchet_domain` (`:183`) and their RLS enforcement are unchanged; the sensitive-
  predicate escalation net (§5, I5) replaces the ceiling's incidental firewall coverage.
- **No destructive deletes** (unlike Mem0 base — §4).
- **No big-bang cutover.** v2 ships behind a flag, is diffed vs v1 on a corpus
  snapshot on the box, and is graded before it writes owner-visible state (§7).
- **Notes remain the sole sources of truth; the wiki stays machine-written.** Lever C
  removes the *note round-trip* for a structured fix; a prose/wiki correction still
  files a correction note (§5, §11.6).

## 1. The current pipeline, precisely

Two LLM calls + a deterministic layer, not three calls (`pipeline.py:289-452`):

| Stage | Kind | Sees | Produces |
|---|---|---|---|
| Extraction (`_extract_note` def `pipeline.py:226`, called `:342`) | 1 strong call | paragraph chunks + capture anchor | `Extraction` (`note_extract.prompt` v31) |
| Integration (`Integrator.integrate`, `integrate.py:38`) | 1 strong call | note text + extraction + **current graph context** (`pipeline.py:360-367`) | `IntegrationIntent` incl. advisory supersession/merge proposals (`intent.py:99-133`) |
| Repair passes (defined in `arbiter.py`, called `pipeline.py:380-395`) | deterministic | the intent | `recover_dropped_fields`, `derive_kinship_gender`, `canonicalize_intent`, `dedup_intent_facts` |
| Arbiter `plan_intent` | deterministic, pure | intent + chunk texts | commit/review/reject (`arbiter.py:95-178`) |
| Apply `apply_intent` | deterministic | plan | writes facts/entities/chains + files cards (`pipeline.py:454-532`) |

Review is decided in `plan_intent` (weight/ambiguous/cross-subject) and
`supersession.decide` (the conflict rules). Only **cross-subject (`arbiter.py:131-132`)
and the firewall** are safety-critical; the rest is heuristic conflict-handling and
model-error compensation, deterministic for stability/idempotency/auditability.

## 2. The review-burden math

`low_confidence_inference` dominates by construction (`0.6 < 0.7`). The eight backstops
are the tax of fighting that gap note-shape by note-shape. Second/third offenders,
`attribute_collision` + `fact_conflict`, are the conservative conflict policy firing on
the owner's own restatements. Load-bearing/safety cards (firewall: `domain_promotion`,
`inverse_proposal`; identity: `merge_proposal`, `ambiguous_mention`; genuine
contradiction) are a **minority** of real volume.

## 3. Why PR #937 patched but did not cure

The correction loop (`api/analysis.py:194-239`): "correct it" mints an
`owner_correction` **note** re-running the whole pipeline to change one value. #937 (a)
dropped `statement` from the dedup key (`arbiter.py:529`) and (b) routed the correction
through `owner_correction` so it force-supersedes. It broke the *loop* but the root
cause it *names* ("value trapped in prose, not `value_json`") is untouched: the collapse
only works when all copies share a `value_json` (here, all null); divergent `value_json`
shapes still fragment. Lever C removes the round-trip; the divergent-shape fragility is
now explicitly in scope to close (§12), not inherit.

## 4. How the industry does this (grounded, corrected)

Eight OSS RAG/KG memory systems surveyed (repos/papers in the research dossier):

| System | Calls/ingest | Entity dedup | Conflict reconciliation | Human review? |
|---|---|---|---|---|
| **Mem0** (arXiv 2504.19413) | 1 extract + 1 per-fact update | embed top-k=10 | **1 LLM call → ADD/UPDATE/DELETE/NOOP**; base is destructive DELETE¹ | none |
| **Graphiti/Zep** (arXiv 2501.13956) | multi | embed cosine + full-text → LLM | LLM contradiction check → **invalidate old edge by timestamp** (bi-temporal, non-destructive) | none |
| **MS GraphRAG** | many (+ gleanings) | exact name group | no fact-level arbitration — duplicate descriptions **LLM-summarized at *index* time**²; contradictions coexist, dilute in retrieval | none |
| **LightRAG / nano-graphrag** | 1 + ≤1 gleaning | exact name | none — additive, coexist | none |
| **Cognee** (ECL) | ~6-stage | content-hash + LLM unify | ontology-grounded; no bi-temporal | none |
| **Letta/MemGPT** | agent self-edits | agent decides | agent overwrites block; conversational correction | none (pull) |
| **LlamaIndex PGIndex** | schema-guided extract | user-supplied | none built-in | none |

¹ *Mem0's published paper is destructively two-pass; its production code has since
drifted toward non-destructive ADD-only, and Mem0ᵍ (graph) marks edges invalid rather
than deleting — the industry is converging on Graphiti's non-destructive stance, which
strengthens Lever B.*
² *Corrected from v0.1's "reconcile at query time": GraphRAG runs an index-time LLM
description-merge; it does no truth-arbitration of conflicting facts.*

Three conclusions:

- **The dominant reconcile pattern is embedding-retrieve neighbors → one LLM judgment
  call** (Mem0 purest). **No *streaming agent-memory* system in this class ships a review
  inbox.** *Scoped honestly:* human-in-the-loop KG-construction tools **do** (e.g.
  **ExtracTable**, arXiv 2506.03221; the KG-construction survey arXiv 2510.20345 documents
  confidence-gated staging) — but for *multi-author, authoritative* corpora, a different
  regime. JBrain2 is single-author but authoritative (feeds the wiki), so it sits between.
- **They skip the inbox by making being wrong cheap** — non-destructive versioning
  (Graphiti) or non-authoritative additive merge (GraphRAG) — not by trusting the LLM.
- **JBrain2's inbox is noisy because it is authoritative *and* arbitrates at write time.**
  The cure is to shift the cost of uncertainty (Lever B, non-destructive) — *not* to hand
  safety-critical disposition to the model.

**The risk the industry evidence adds** (industry review's strongest finding): the
single-LLM-call disposition Mem0 uses is **documented ~15–20 points lossy on
supersession** — the "Supersede" memory-update-gap study (arXiv 2606.27472) names Mem0's
op-choice "a heuristic, not a learned policy" and measures accuracy dropping 92→77%
(and 82→63% on a smaller model) at exactly *keep-current / retire-superseded*. This is
**why v0.2 keeps disposition deterministic** and gives the LLM only a *soft escalate
signal* — and why the V0 gate measures **supersession correctness**, not just fewer cards
(§7, §12). (See also A-Mem, arXiv 2502.12110; surveys arXiv 2510.20345 / 2512.13564.)

## 5. The proposed model — three levers on an expanded safety spine

### Lever A — remove the inferred-ceiling *review* trap; keep a sensitive net

Delete `INFERRED_CEILING`/`COMMIT_THRESHOLDS` as the *review gate* (`weight.py`) and
retire the eight attest-anyway backstops (`arbiter.py`; keep `dedup_intent_facts` as an
idempotency net). A model-asserted fact commits by default. **But** — because the ceiling
today incidentally forces *non-floored* sensitive inferred facts to review (the firewall
allowlist is deliberately partial: weight/temperature and unknown predicates fall back to
the model, `extraction.py:148-151,171-174`), Lever A adds a **narrow deterministic
escalation net (I5):** an `inferred` fact whose predicate OR resolved domain is
health/finance/location — and is not already floored — escalates (a `domain_promotion`-
class review), so a mis-domained sensitive inference can't commit silently. Non-sensitive
inferred facts commit. This removes the volume without reopening a firewall gap.

### Lever B — non-destructive supersede as the default for state/relationship, by validity time

For the kinds that already do newest-wins-eagerly (`state`; functional `relationship` —
`supersession.py:110,113`), change the default from "supersede **and flag review**" to
"supersede **with retained history**, no card." **Newest = latest *validity* time
(`valid_from`, tie-broken by `reported_at`), never capture time** (I4 —
`supersession.py:3-6`): a retrospective note lands as closed history, never as the new
head. Escalate to review **only** when a retained floor fires (I4–I9 below) or the LLM
raises `escalate`.

**Explicitly out of Lever B — stay review (I6):** `attribute` conflicts (two birthdays,
two genders) remain `attribute_collision` review. This is a **deliberate reversal-guard**:
`ANALYSIS.md:110-111` says attributes "hold `pending_review`, never auto-supersede — two
birthdays is a bug, not news," because a collision is the primary signal that two real
people were wrongly merged into one entity — and the LLM is the component that produced
the bad merge, so it cannot be trusted to escalate it. Owner attribute *corrections* route
through Lever C, not through silent supersession. `measurement`/`event` stay accumulate
(a new reading is a datapoint, not a conflict; genuinely contradictory same-instant
readings still escalate). **This narrows Lever B vs v0.1 and removes the direct
`ANALYSIS.md:111` contradiction** — but the state/relationship half is still a per-kind
policy change under a Living doc, surfaced for owner ratification (§11.2) and reconciled
in-wave.

### Lever C — structured corrections write directly, re-running the pipeline's checks

`ANALYSIS.md:359-362` already sanctions this: *"structured pipeline outputs … are
corrected directly in the review inbox. A correction note's elevated weight is implemented
as pinning."* So a review-card structured fix writes the pinned, force-superseding override
**directly** — but it **must re-run the enforcement the note→extract path owns** before
pinning (I8): `_shape_check` (`pipeline.py:1932-1945`), `domain_floor` + `ratchet_domain`
(`:1906-1909`), and entity-scope validation (`_resolve_from_intent`, `:548-563`). Skipping
them would let an owner correction commit a malformed/mis-domained value that is then
*pinned* (immutable) — a human-triggered firewall/corruption bypass (§13, F6). Correction-
*notes* stay the path for **prose/wiki** corrections. `ANALYSIS.md:359-362` is edited
in-wave (the mechanism changes from "note" to "direct write").

### The safety spine — deterministic, the LLM never lowers these (I1–I9)

| # | Invariant | Where it lives today |
|---|---|---|
| I1 | Domain firewall floor/ratchet + RLS; **every commit routes through `_upsert_fact`** | `extraction.py:177,183`; `pipeline.py:1906-1909` |
| I2 | Cross-subject facts deterministically routed (LLM only *detects*) | `arbiter.py:131-132`; `intent.py:57` |
| I3 | Whole-intent atomicity — fatal violation rejects the whole intent | `arbiter.py:116-123`; `intent.py:283` |
| I4 | Supersession compares **validity** time, never capture time | `supersession.py:3-6,662-796` |
| I5 | **Sensitive-domain inferred facts escalate** (new — replaces the ceiling's firewall role) | new (Lever A) |
| I6 | Attribute collision stays review (hidden-merge identity signal) | `supersession.py:605-624` |
| I7 | Pinned-override immutability; irrealis never displaces asserted; asserted-vs-negated held | `supersession.py:610,712,759,752,638-657` |
| I8 | **Direct corrections re-run shape/floor/scope checks before pinning** (new — Lever C) | new (Lever C) |
| I9 | Derived edge never supersedes a primary head; low-self-confidence never overwrites more-confident (OCR guard) | `pipeline.py:2418-2430`; `supersession.py:721,767` |

The v2 enactor must re-implement I1–I9, not just `decide()`'s happy path (I9's derived-
defers-primary lives in the apply layer, *outside* `decide()` — the safety review's F9).

## 6. Re-run determinism — by recomputation, not caching

The one hard requirement the industry does not solve for us: re-analysis of a note (model/
prompt upgrade, corpus re-run — `POST /api/notes/{id}/analyze`) must produce the same
graph; "a silent flip is the one outcome no layer may produce" (`ANALYSIS.md`).

**v0.2 keeps the source of determinism exactly where it is today: the deterministic
enactor recomputes conflict disposition over the *current* graph.** `supersession.decide`
is already a pure, idempotent function of `(candidate, current heads)`; re-running it
yields the same result given the same graph. Lever B changes its *default outcome* (silent
supersede vs flag) but not its determinism. **There is no cached LLM verdict** — which is
what makes this safe: a disposition depends on graph *state* the structural-identity key
does not capture, so caching it (v0.1's §6) was stale-*wrong*, not just stable (§13,
F1/F2/F7/F8). The LLM's contribution stays *advisory* (resolutions, facts, and a new
`escalate` hint the enactor may honor to *raise* a review, never to *suppress* one), so
its run-to-run noise can only add a card, never silently flip a head. Because nothing is
cached, there is **no disposition-backfill problem** for the existing corpus (§13 dissolves
process #7/#8).

## 7. The sideload — corpus-snapshot eval on the box, not a production shadow store

v0.1 claimed the Phase-5 "shadow" pattern as precedent; the feasibility review showed that
pattern is only an **enqueue-string diff** (`workflow/events.py`, `dispatcher.py:232-275`)
— no shadow *write* store exists, and a faithful in-production v1-vs-v2 diff hits an
ordering problem (v1 commits first; v2 then reads post-v1 heads and decides differently).
**v0.2 drops the production shadow-write substrate** in favor of a precedent that *does*
exist:

- **The DB-mode eval runner already does the whole thing.** `tests/eval/runner.py`
  `run_case_db` runs `plan_intent → apply_intent → COMMIT` against a real (testcontainer/
  ephemeral) Postgres and reads back the committed facts **and the filed review cards**
  (`runner.py:245+`) — exactly the v1-vs-v2 comparison surface, against a throwaway DB, no
  production shadow store, no ordering dance.
- **The sideload = run v1 and v2 through `run_case_db` over a snapshot of the owner's real
  corpus on the local box**, with the router's per-task override sending the v2 integrate
  task to the **local** provider (`llm/router.py` overrides; `llm/local_catalog.py`). The
  new task `integrate.note.v2` must be registered in `TASK_DEFAULTS` first
  (`router.py:62,226-228` raises on an unregistered task). The judgment/safety scoring
  reuses `evals/integrate_runner.py` (in-memory); the **cards-filed + supersession-
  correctness** delta comes from the DB-mode runner — two harnesses, both extant, both
  cited correctly now.
- **Hold the model constant (the 100%-local decision, §11.5).** Today every task defaults
  to `xai:grok-4.3` (`router.py:45-62`); the ratified target is `local:gpt-oss-120b`. So the
  v1-vs-v2 diff must run **both arms on gpt-oss-120b** — otherwise it conflates the *policy*
  change (Levers A/B/C) with a cloud→local *model* swing and measures nothing clean. V0
  therefore first establishes a **v1-on-`gpt-oss-120b` baseline**, then measures v2 against
  it on the same model. (Grading needs no cloud judge: the integrate eval scores against
  per-case golds, `integrate_runner.py:_score`, not an LLM oracle — consistent with no cloud
  inference at all.) Ratified: the owner's box already runs every task on gpt-oss-120b, so
  both arms are already on the same local model and V2 owns no cloud→local migration (§11.5a).
- **On-box interactive validation via the debug console (owner-driven, read-only + live
  model).** The debug-token surface (`api/debug.py`, `docs/runbooks/DEBUG_ACCESS.md`) is
  read-only for data but **runs live LLM calls through the adapter to gpt-oss-120b** — which
  makes it a first-class V0 tool, not just an inspector:
  - **`/tool-probe`** sends a **JSON schema** to the model with *no handler running* — the
    exact shape the Integrator uses (`json_schema=INTENT_SCHEMA`). So the v2 prompt **and**
    its extended `IntegrationIntent` schema (the new soft `escalate` field) can be exercised
    against the real gpt-oss-120b, on-box, before any pipeline wiring — the fastest possible
    prompt/schema iteration loop, and the honest measure of whether the local model produces
    a well-formed, well-judged intent (the V0 make-or-break, §11.5).
  - **`/complete`** probes free-form judgment on a hand-picked hard note; the **routing
    inspector** confirms the task is actually on `local:gpt-oss-120b`.
  - **`sql.read`** (read-only, owner RLS) reads back `facts`/`entities`/fact-chains/
    `review_items` after a batch of test notes runs through v2 behind the flag — the owner
    sees exactly what committed vs. carded, live, without waiting on the eval report.
  This channel is no-cloud and cannot escalate (no write route), so it is safe to lean on
  throughout V0–V4. *(Writing the test notes themselves is a separate owner-session write —
  `POST /api/notes` — or the DB-mode fixture loader; the debug token drives the model and
  reads the result, it does not author notes.)*
- **The "huge swap of test notes" = a reusable ingest test-corpus** (V0/V2 deliverable):
  a large, versioned batch of representative + adversarial notes — seeded from the 74 harness
  scenarios (`tests/harness/scenarios/`), the graded corpus (`tests/eval/corpus/`), and a
  snapshot of the owner's real notes — that BOTH the automated v1-vs-v2 diff and the
  interactive debug-console loop draw from, so a scenario is repeatable and a regression is
  attributable. Batch-ingest → inspect-via-debug-console → iterate is the owner's manual
  test loop alongside the CI eval.
- **Acceptance artifact (owner gate, V4):** on the corpus snapshot, v2 vs v1: (a)
  materially fewer cards, (b) **no tier-1 recall regression** on the graded corpus
  (`tests/eval/corpus/`), (c) **firewall/RLS parity** (every v1 floor/ratchet/cross-subject
  action reproduced), (d) **supersession correctness** ≥ v1 (the §4 lossiness risk — did v2
  supersede the *right* head, by validity time?), (e) re-run idempotency proven (run the
  snapshot twice, identical graph).
- **Cutover (V5) is a pipeline-pointer flip** to `integrate_note_v2`, v1 behind a flag for
  one release. Two non-obvious tasks the feasibility review surfaced: the note-dedup guard
  (`queue.has_active_analysis`, hardcoded `kind='integrate_note'`, `queue.py:274`) and the
  integration reconciler (`dispatcher.py:342-370`) must be extended to the v2 kind or E4
  double-process protection lapses. **Rollback caveat:** Lever B will have already
  rewritten owner-visible heads non-destructively; reverting the code path does not revert
  that state — but because every superseded head is retained history (never deleted), the
  prior heads are recoverable. Stated, not hand-waved.

No new production table is required by this design (the disposition cache is gone; the eval
runs on a throwaway DB), so **no new-table RLS migration** — a direct consequence of the §6
redesign.

## 8. Waves (per `docs/reference/PROCESS.md`)

Promote this doc to `Scheduled`/`plans/` **before** V0 (a `Proposed` doc means nothing
built — DOC_LIFECYCLE). V4 is an **owner acceptance gate**, not a code wave.

| Wave | Delivers | Depends on | Size | Owner state? |
|---|---|---|---|---|
| V0 | **Local-box judgment spike:** `integrate.note.v2` prompt (soft `escalate` hint) + `TASK_DEFAULTS` reg + the DB-mode-runner extension for the cards + **supersession-correctness** metric. Iterate the prompt + intent schema **live against gpt-oss-120b via the debug console `/tool-probe`** (`api/debug.py`) before pipeline wiring; inspect results with read-only `sql.read`. Go/no-go on local judgment quality (§11.5). | §11 decisions | M-L | no |
| V1 | **The deterministic v2 enactor + expanded safety spine I1–I9** (Lever A ceiling removal + sensitive net I5; Lever B state/relationship default + validity-time; I6–I9 re-asserted). Pure, unit-tested, LLM faked. **Security red-team (firewall/validity/identity floors).** | V0 | L | no |
| V2 | `integrate_note_v2` action (`registry.py` ActionSpec) + the corpus-snapshot diff harness over `run_case_db`; the v1-vs-v2 report. | V1 | M-L | no |
| V3 | Corpus/harness **re-tier** (invert the ceiling-trap cases; `absent_review_cards` sweeps; the `ANALYSIS.md:111` attribute case stays green; validity-time + sensitive-net cases). | V1 | M | no |
| V4 | **Owner acceptance gate:** the §7 (a–e) artifact from the local box. Owner ratifies cutover + the §11 doctrine changes. | V2,V3 | — | no |
| V5 | **Cutover** — pipeline-pointer flip, dedup-guard + reconciler extended to v2 kind, v1 behind rollback flag; **Lever C** direct-correction (I8); **frontend review-block registry** edits for the removed card kinds (**GUI gate: three mocks**); **in-PR doc reconciliation** of `ANALYSIS.md` (per-kind policy + correction doctrine) and `ENTITY_GRAPH_REFOCUS_PLAN.md` (ceiling rationale) — corrected in place, not archived. | V4 | M-L | **yes** |
| — | (follow-up, after V5 stable one release) retire v1 + the backstops + dead ceiling code. | V5 | S | — |

One PR per wave; per-task + per-wave adversarial review; **security red-team on V1 and V5**
(firewall/RLS/identity). CI green before merge. Conventional Commits.

## 9. GUI gate

- **V5 trips it:** (a) the review card's "correct it" → structured direct-apply (Lever C),
  and (b) the **review-block registry / card renderers** change when Lever A removes card
  kinds (the refocus plan flagged this same surface). Three interactive mock HTML artifacts
  per `docs/reference/DESIGN.md`, owner-chosen before V5 build.
- **Undecided (§11.7):** whether the V2 shadow-diff **report** and any v2 **routing
  setting** (§11.5) are owner-facing surfaces needing their own mocks. Defaulted to
  dev-only (no gate); ratify.

## 10. Non-negotiables reconciliation (`CLAUDE.md`)

1. LLM via adapter — v2 is a `router.complete` task. ✓
2. Storage abstraction — unchanged. ✓
3. RLS + isolation tests — **no new table** (the disposition cache is gone; eval runs on a
   throwaway DB), so no new RLS surface. I1/I2 unchanged. ✓ *(if any decision reintroduces a
   table, its RLS isolation test is in-wave)*
4. Comments explain why — review-enforced. ✓
5. Tests same PR; 80%/security-100%; real Postgres; LLM faked — the enactor + diff harness
   are testcontainer-tested; the safety spine is security-100%; **backstop deletion must not
   drop coverage below 80%** (verify locally, §12). ✓
6. Conventional Commits; branch+PR; CI green. ✓ (§8)
7. Wiki machine-written; humans correct via correction notes — **preserved for prose.**
   Lever C narrows the *structured* case, sanctioned by `ANALYSIS.md:359-362` (§11.6). ✓
8. `dev-setup.sh` — no new dep expected. ✓
9. Docs travel with code — **reconciled in the V5 PR** (the behaviour-change wave), not
   deferred (§8). ✓

## 11. Open decisions for the owner (recommended default first)

1. **Escalate-signal authority.** Default: the LLM's `escalate` hint may only *raise* a
   review, never suppress a floor or a commit. Alternative: also let a high-confidence LLM
   `commit` override I6 attribute-collision (rejected by default — F3).
2. **Lever B breadth / `ANALYSIS.md` change.** Default: silent supersede-with-history for
   `state` + functional `relationship` only; `attribute` stays review (I6). Ratify the
   per-kind policy edit to `ANALYSIS.md:110-111`. Alternative: include attribute (reopens
   the hidden-merge risk — not recommended).
3. **Sensitive net threshold (I5).** Default: any `inferred` fact on a health/finance/
   location predicate-or-domain not already floored → escalate. Alternative: also escalate
   *asserted* sensitive facts on non-floored predicates (belt-and-suspenders; more cards).
4. **Escalation floor overall.** With the ceiling gone, what still forces review beyond the
   spine? Default: I5–I9 + LLM-`escalate` + structural namesake ambiguity. Ratify.
5. **Local-model judgment quality (V0 gate) — RATIFIED 2026-07-23: 100% local, no cloud.**
   The system runs entirely on `local:gpt-oss-120b` (text reasoning, `local_catalog.py:166`);
   **no cloud inference and no cloud fallback, ever.** V0 is therefore a **hard blocking
   gate**: if gpt-oss-120b can't clear the integrate + supersession-correctness bar, the
   response is to *narrow what the LLM decides* (lean harder on the deterministic spine) or
   defer — never fall back to cloud. This makes the v0.2 design choice (disposition stays
   deterministic; the LLM only *raises* a soft escalate hint, never lowers a floor)
   **load-bearing, not optional** — a 120B OSS model is exactly where the §4 supersession-
   lossiness result (arXiv 2606.27472) bites hardest, so keeping the safety-critical
   decisions off the model is the whole reason the design survives on local hardware.
5a. **Local-cutover sequencing — RATIFIED 2026-07-23: already local; V2 owns no migration.**
   The owner's deployment already runs every task on `local:gpt-oss-120b` via task overrides
   (DB override > env `JBRAIN_LLM_TASKS` > code default — so the `xai:grok-4.3` defaults in
   `router.py:45-62` are irrelevant to this box and are **not** changed by this plan). V0
   therefore runs on the already-local baseline; both v1 and v2 diff arms are on
   gpt-oss-120b automatically (the §7 model-constant requirement is satisfied for free).
   Ingest V2 is a **pure policy change** on top of an already-local pipeline.
6. **Lever C doctrine.** Default: direct structured writes for review-card fixes (per
   `ANALYSIS.md:359-362`), editing that line's mechanism in-wave; correction-*notes* kept
   for prose/wiki. Confirm this is a mechanism edit, not a CLAUDE.md #7 violation.
7. **GUI surfaces (§9).** Default: V2 diff report + v2 routing setting are dev-only (no
   mock gate); only the card renderer + Lever C trip the gate. Ratify.
8. **Two calls vs one.** Default: **keep two calls** (extraction + integration); move only
   the soft escalate authority into integration. The token saving of merging is minor and
   the long-note map-reduce + clean extraction artifact argue against it. (v0.1's pervasive
   "single-call" language is retired — §13.)

## 12. Risks (honest)

- **Local-model judgment is unproven** — V0 blocking spike; §11.5 fallback.
- **Supersession is a documented LLM weak spot** (§4, arXiv 2606.27472, ~15–20 pts) — v0.2
  keeps disposition deterministic precisely for this; the residual risk is the LLM's
  *escalate* hint being noisy (only adds cards) and the *default* policy mis-superseding by
  validity-time edge cases — the V0/V4 supersession-correctness metric is the gate.
- **Lever B could mask a real contradiction** the old review surfaced — the cards-filed
  delta is paired with a **recall check** (V4 (d)): did any conflict that *should* have
  escalated get silently superseded? Caught on the snapshot before cutover.
- **The `ANALYSIS.md:111` attribute doctrine** is deliberately *not* changed (I6); if the
  owner wants attribute corrections frictionless, that rides Lever C, not Lever B.
- **Divergent-`value_json`-shape fragility** (the #937 residue) must be *closed* here (a
  normalized value fingerprint for dedup/idempotency), not inherited (§3, §13 F2).
- **Firewall parity is safety-critical** — V4 (c) proves every v1 floor/ratchet/cross-
  subject action is reproduced; V1 + V5 security reviews red-team it.
- **Backstop deletion is broad** (~400 lines + tests) — coverage must stay ≥80% (CLAUDE.md
  #5); verify locally per the refocus plan's coverage-on-deleted-paths note.
- **Wiki leans on this** — a supersede-by-default changes what the wiki publishes; re-read
  `docs/plans/PHASE6_WIKI_PLAN.md` against this before V5.
- **Rollback reverts code, not state** — mitigated by non-destructive retained history
  (§7); the prior heads are recoverable.
- **Cutover guards** — the hardcoded `integrate_note` dedup guard + reconciler must extend
  to the v2 kind or double-process protection lapses (§7).

## 13. What changed from v0.1 (post-review changelog)

- **§6 rewritten.** v0.1 persisted the LLM disposition keyed to structural identity; the
  safety review proved this stale-*wrong* (disposition depends on graph state; `value_json`-
  null collapses distinct values — F1/F2/F7/F8). v0.2 keeps deterministic recomputation
  (idempotent, as today); the LLM gets only a soft *raise-only* escalate signal. Dissolves
  the disposition table, the backfill wave, and the "no owner state in shadow" tension.
- **§7 rewritten.** v0.1's production shadow-write store had no real precedent (the Phase-5
  shadow is an enqueue-string diff) and an unsolved read-ordering problem (feasibility F1).
  v0.2 uses the extant DB-mode eval runner over a corpus snapshot on the box.
- **Lever A narrowed** — keeps a sensitive-domain escalation net (I5) so removing the
  ceiling doesn't reopen the firewall gap it incidentally covered (safety F5).
- **Lever B narrowed** — scoped to state/relationship + validity-time; `attribute` stays
  review as the hidden-merge identity signal (safety F3/F4, process #2 — the direct
  `ANALYSIS.md:111` contradiction is removed, the remaining change is surfaced for
  ratification).
- **Lever C hardened** — re-runs shape/floor/scope checks before pinning (safety F6);
  re-pointed to its real authority `ANALYSIS.md:359-362`, not CLAUDE.md #7 (process #10).
- **Safety spine expanded** I1→I9 to name the floors v0.1 dropped (validity-time, attribute-
  identity, irrealis, asserted-vs-negated, derived-defers-primary, OCR guard — safety
  F4/F9/F10/F11).
- **Industry §4 corrected** — GraphRAG index-time (not query-time) merge; "no review inbox"
  scoped to streaming agent memory (HITL KG tools excepted); the Supersede lossiness result
  added and wired into the V0 metric; Mem0 non-destructive drift footnoted.
- **Lifecycle fixes** — "correct-in-place," not "supersede," the cited refocus doc; in-wave
  reconciliation; README index entry; `Waves:` header; V4 relabeled a gate; buried
  decisions (attribute doctrine, sensitive net, GUI surfaces) surfaced to §11.
- **Citations fixed** — `_extract_note` def `pipeline.py:226`; `domain_floor:177` /
  `ratchet_domain:183` (not the dict at 149-171); single-call language retired for the
  ratified two-call design.

## 14. V0 preliminary probe — on-box gpt-oss-120b (2026-07-23)

Two live probes through the owner's debug console (`/api/debug/complete`, `task=integrate.note`,
`provider=local model=gpt-oss-120b`, `reasoning_effort=medium`) exercised a first-cut v2
integrate prompt + a constrained intent schema carrying the `disposition ∈
{commit,supersede,escalate}` field. Hand-fed graph context + extraction; schema-valid JSON
returned both times (~950 output tokens each). The model's dispositions matched the design on
every case tried, including the two most likely to break:

| Case | Correct v2 behavior | gpt-oss-120b |
|---|---|---|
| Address change vs current head | supersede, bind the right head | ✅ `supersede`, `supersedes=f-100` |
| Birthday conflict on an attribute | escalate (I6 hidden-merge signal) | ✅ `escalate`, cites "timeless attribute … human arbitration" |
| New fact, no head (Mom's birthday) | commit | ✅ `commit` |
| **Retrospective address (2010–2014) vs current head** | **commit as history, NOT supersede** (I4 validity-time) | ✅ `commit`, `supersedes=None` |
| **Inferred gender from "sister"** (Lever A) | commit quietly, not escalate | ✅ `commit`, `inferred=true` |

**Read honestly:** two probes are a *signal*, not the V0 eval — the real gate runs the graded
corpus with the supersession-correctness metric ≥3× for reasoning-model variance (§7 a–e), and
extraction quality on gpt-oss-120b is a separate variable not tested here. But the core V0
hypothesis — *the local model can produce well-formed, well-judged dispositions* — has strong
first support, and notably the model AGREED with the deterministic validity-time floor (I4)
rather than fighting it, which lowers the escalate-noise risk. I4 stays deterministic anyway
(one good probe is not a guarantee across phrasings — belt and suspenders). Minor: the probe
schema let the model overload `new_kind`/`new_name` on existing-entity resolutions — a schema
nit to fix when the real `IntegrationIntent` v2 shape is authored in V0.

## 15. On-box gpt-oss-120b validation — the ingest judgment is prompt+schema, not architecture (2026-07-24)

A 121-case adversarial battery (5 lanes: temporal/supersession, cross-subject+firewall,
identity/resolution, assertion+inference+salience, adversarial+determinism — authored by
five scoped researchers, grounded in the I1–I9 invariants) was run **on the owner's box
against `local:gpt-oss-120b`** via the debug console (`/api/debug/complete`, §7). Then four
independent researchers audited results-vs-intent and gpt-oss-120b prompt-craft. The
question under test: is the ingest-quality gap an **architecture** problem (needs an agent,
tools, or multi-tier decomposition) or a **prompt/schema** problem?

**Finding: overwhelmingly prompt/schema.** The raw 34.7% failure rate collapsed to a
**genuine model-error rate of 8.3% (10/121)** once three noise sources were removed: scorer
brittleness (12), a deliberately thin probe prompt lacking rules the **production**
`integrate_note.prompt` v12 already carries (14), and researcher expectations *stricter than
the plan's own invariants* (6 — e.g. expecting the model to self-escalate an *asserted*
sensitive fact when I5 only escalates *inferred* ones). Crucially, the two genuine
error clusters — inferred-sensitive escalation and same-name ambiguity — are the exact
places the plan **already** makes enforcement deterministic (I5 sensitive net; the
`resolve_entity` 2+→`ambiguous_mention` gate; I2 cross-subject routing). The model fails
where the plan already put a deterministic floor, which **validates the spine rather than
exposing a hole.**

**gpt-oss-120b is capable at the meaning task.** Cross-subject *attribution* is a strength
(it tagged the correct non-owner entity + `cross_subject` every time); assertion status is
handled well ("rule out diabetes"→hypothetical, "wondering if"→question, "quit"→negated);
injection resistance is real; runs are near-deterministic (1 flip in 5 rerun-x3 cases, on a
genuinely borderline self-correction). The dominant failure was a **detection-to-abstention
gap** (P3): on the sensitive net the model set `inferred:true`+`domain:health` correctly on
7/7 facts, then still proposed `commit` — a disposition-*selection* miss, not a perception
miss, and exactly what the deterministic I5 net exists to catch.

**The one class that genuinely matters — flag-stripping (safety).** Because the engine can
only *add* review, never remove it, over-escalation is mere noise but a **stripped flag
silently defeats a safety floor**. Two cases did this (an inferred mood tagged
`inferred:false`; a *future* job start tagged `asserted`+`supersede`, flipping the current
employer). This is the top-priority fix — and it is prompt-fixable.

**Confirming A/B (improved prompt + split schema, re-run on-box):** synthesizing the
production rules + the researchers' blocks and **splitting the disposition** (below) fixed
**8/10 genuine errors and BOTH flag-strips** — the future-job now commits as `expected`
(no employer flip), the inferred mood now carries `inferred:true` (net fires), the two
same-name guesses now return `ambiguous`, and the negation-transitions now `supersede`
instead of escalating. The 2 residuals are sensitive-net edge cases the deterministic net
still backstops via the `domain` flag.

**Implementation deltas this pins for Wave 2 (prompts/schema — no architecture change):**
- **Split the model's disposition** into `disposition ∈ {commit, supersede}` **plus a
  separate `needs_review` flag** (the 3-way enum that folded safety-routing into graph-
  mechanics invited commit-by-default; this also matches the production separation of
  `supersession_proposals[]` from engine-side review).
- **Add `expected`** to the assertion enum (future starts must not supersede now — the
  flag I7/Lever-B key on).
- **Lock the safety-critical flags in the prompt:** a mood/symptom/condition read from
  behaviour is `inferred:true` even about the owner; a future start is `expected`. These
  are the flags the deterministic nets key on.
- **Assertion discipline:** `reported` is third-party-relayed ONLY; the owner's own hedged
  statement is `asserted`+`inferred` (uncertainty rides `inferred`/`self_confidence`, never
  a downgraded assertion) — fixes the systematic `asserted→reported` over-hedge.
- **Negation is a transition:** quit/stopped/moved-out → `negated`+`supersede` (close the
  head); escalate only a flat contradiction of the head's whole validity or a
  non-functional opposite-polarity edge.
- **Identity:** keep the production "mint sparingly / never mint a name / a vague ref is
  ambiguous" rules and reframe same-name as a mechanical **count** ("2+ live matches →
  ambiguous, FORBIDDEN to break the tie by context"); keep the `INFER GENDER` block.
- **Keep `reasoning_effort` at production High**; the fixes above are the leverage, not
  more thinking (high alone barely moved the number and risks fresh over-escalation).
- **Deterministic nets stay exactly as specified** (I5 sensitive, namesake ambiguity, I2
  cross-subject) — the model supplies flags, the engine escalates.

## 16. Evaluated and rejected: agentic / multi-tier ingestion (with evidence)

Three heavier architectures were seriously explored at owner request and **rejected on
evidence** — recorded here so they are not re-litigated:

- **Full agentic ingestion** (an agent instance with note-query tools + budget, Letta/
  Mem0-style). Rejected: (1) it breaks re-run determinism (§6) — an agent that chooses what
  to read produces a different graph each run, and JBrain re-extracts on model/prompt
  upgrades; (2) it widens the prompt-injection surface (the agent reasons freely over more
  untrusted note text); (3) the literature does **not** show agentic memory is more
  *accurate*, only more flexible, and the single-call disposition it would rely on is the
  documented weak spot (§4, arXiv 2606.27472). The battery showed gpt-oss-120b already
  handles the meaning task well *without* tools — in every genuine identity failure the
  colliding entities were already in the injected context; the model lacked restraint, not
  information, so a lookup tool answers a question the context already answers.
- **Multi-scope decomposition** (many specialist calls — salience / who-is-it-about / … —
  + a reviewer). Rejected as over-engineering: the single integration call is already
  strong at temporal (96%), assertion, injection, salience, and determinism; splitting those
  adds cost + error-compounding for near-zero gain. Only two sub-tasks genuinely fail, and
  both are already deterministic gates, not model tiers.
- **A single identity/attribution specialist tier with an entity-lookup tool.** Rejected:
  R3's genuine failures reduce to same-name-ambiguity (2 cases), which the existing
  deterministic `resolve_entity` gate catches and the "count, don't guess" prompt rule
  fixed in the A/B; cross-subject attribution is already a model strength. No tier or tool
  is warranted.

**Net:** keep the two-call + deterministic-spine architecture; the ingest-quality work is
prompt + schema (Wave 2) on top of the Levers A/B/C already in this plan. The wiki-noise
reduction the owner wanted comes from Lever A/B, not from a heavier ingest engine.
