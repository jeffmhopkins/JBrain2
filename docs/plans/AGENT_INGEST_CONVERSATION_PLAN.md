# Agent-Conversation Ingestion — Build Plan

> **Status:** Plan · **Last verified:** 2026-09-08 — awaiting owner sign-off on the
> wave breakdown. Research behind it: the eighteen dossiers in
> `docs/research/agent-ingest/`, consolidated in `SYNTHESIS.md`. No code written yet.

## Thesis

A note becomes turn 0 of an agent conversation. The agent decides **what** the note
means and what should be written; deterministic code decides **how it lands**. High
confidence commits; low confidence becomes a question in a silent queue. The owner's
replies build and correct the graph.

This is not "agent instead of pipeline" and not "pipeline instead of agent". The model
supplies meaning; the engine supplies mechanics. Eighteen dossiers reached that split
from unrelated starting points.

## Decisions ratified

**By the owner:**

| # | Decision |
| --- | --- |
| D1 | Auto first pass on owner notes; confidence-split governs writes (high → commit, low → approval). `ASSISTANT.md` #10 is amended to exempt the owner's own captured notes. |
| D2 | Questions expire on a **ladder** — 7d long-tail, 30d registry predicates, 60d-then-conservative for merges — decaying to a *marked assumption*. **Never** for health, finance or location. |
| D3 | Non-note paths (EMR import, intake, correction notes, `save_place`) are ported onto the new path rather than left on the old pipeline. |
| D4 | Phase 6 (wiki) Wave D is decided after the spike, not before. |

**Delegated to me, decided:**

| # | Decision | Reasoning |
| --- | --- | --- |
| D5 | **Spike first, then B.** | The spike deletes nothing and turns the fleet's one real disagreement into a number. |
| D6 | **Serving stack folded into the spike.** | Measure llama.cpp as it stands; migrate to vLLM/SGLang + XGrammar only if the numbers fail *and* constrained decoding is the identified cause. |
| D7 | **Re-derivability stays binding; owner answers mint owner-authored notes.** | Solves the `wiki_citations.chunk_id NOT NULL` blocker for free — an answer-note is chunked, so there is a real chunk to cite. No conversation-chunk hack, and rebuild-from-notes still reproduces the graph. |
| D8 | **GUI variant C**, with F2's queue placement (launcher tile, not home) and B's reply-composer pill. | C is the only variant whose queue satisfies "no nagging badge" as written, and the only one where a note thread doesn't dead-end. Home stays for capture. |

**Standing interpretations** (flagged, not separately ratified):

- D3 means *one commit path*, not a model in front of lab results. EMR import stays
  deterministic and routes through the same `commit_facts` primitive the agent's tools
  call. No LLM between an EMR file and the medical record.
- Facts sourced **solely** from an OCR'd attachment are ineligible for auto-commit
  regardless of claimed confidence. A photographed letter is foreign content; that is
  the real trust boundary inside a note the owner captured.

## Binding constraints

Baked in from the start, not discovered in wave 3:

1. `wiki_citations.chunk_id` is `NOT NULL` (`0046:159`) behind a trigger tying it to the
   chunk's note, and `wiki/builder.py:520-530` INNER JOINs chunks. A fact with no chunk
   is **rejected by Postgres**. (Satisfied by D7.)
2. The ingest job must run `narrowed_context(owner_scoped=True)` at the note's domain.
   `SYSTEM_CTX` is `owner_scoped=False` (`db/session.py:20-31`, `queue.py:24`) — safe
   while the writer is code, fatal once a tool loop holds it.
3. No destructive verb in the model's vocabulary. `jbrain_app` holds `DELETE` on
   facts/entities/review_items (`0009:34-39`); revoke it and purge via `SECURITY DEFINER`.
4. `supersession.decide()` stays the implementation of the write tool, never a
   model-facing verb. This is what keeps 75 scenario files alive.
5. The `entity_mentions` write is **un-gated** by confidence — it is the co-mention spine
   `neighborhood()` traverses, and it would degrade silently over months.
6. **No JSON-Schema `enum` anywhere** — it segfaults llama.cpp's harmony grammar
   (`STRIX_HALO_SETUP.md:592-601`). Values go in descriptions, validated in handlers.
7. The server owns the auto-commit ceiling. The model's self-report may only ever
   *lower* it, never raise it, and a forced-ask gate overrides both.
8. No question may be filed without a declared `default_action` and `default_at`.
9. `entity_mentions.chunk_id` NOT NULL (`0006:89-104`) must accept an answer-turn anchor.
10. Note delete is a **soft** delete (`notes/repo.py:174-194`) — FK cascades do not fire;
    conversation purge is explicit in `analysis/purge.py`.

## Wave 0 — the gate spike (deletes nothing)

Two independent tracks.

**0a — Measure the bet.** Can `gpt-oss-120b` drive write tools well enough to be the
writer? Pre-registered kill numbers, published before the run:

| Metric | Kill threshold |
| --- | --- |
| Well-formed tool output | < 95% |
| Judgment gain over today's `INTENT_SCHEMA` | < 5 points |
| Safety flag-strip regression | any |
| Median latency vs today | > 5× |

Any one of those fails → B is off, C ships alone, and the plan re-scopes. Prerequisites
found by X5: a `converse-async` debug endpoint (`DebugRouter` implements only
`complete`), and `FixtureLlmClient`'s `TypeError` behind the router. Corpus already
exists — 56 outcome-shaped cases plus 325 extraction cases.

**0b — Live defects, worth fixing regardless of the spike's outcome.**

- The capture-race settle clause compares against `notes.created_at`, the *client's*
  capture time. An offline-flushed note with a promised attachment is eligible for
  body-only integration immediately — defeating the gate in exactly the case it exists
  for.
- The live vision route is an abliterated checkpoint whose GGUF template hard-codes a
  "never refuse, no pushback" system prompt **above** ours, with no API switch
  (`llm/local_catalog.py:869-878`). It structurally inverts the data/instruction boundary.
- `merge_entity_pair` repoints facts with unfiltered `UPDATE`s — a cross-domain merge
  half-completes under a narrowed session.

## Waves 1+ (shape, pending spike)

- **W1 — `commit_facts` primitive.** Extract the deterministic commit core from `_apply`
  so old callers and new tools share one writer. Unblocks D3 and is reusable under every
  outcome. Includes the four typed projections and provisional→confirmed promotion.
- **W2 — Conversation surface (C).** `note_conversations`, turn/tool-call/question
  tables, RLS with the domain ratchet, the launcher-tile queue. Ships value with or
  without B.
- **W3 — Write tools** (only if 0a passes): `resolve_entity`, `assert_fact`,
  `note_mentions`, `merge_entities`, `ask_owner`. Server-side forced-ask gate.
- **W4 — Cutover + the rebuild sweep.** There is currently **no way to rebuild the graph
  while keeping the notes** — the only no-terminal full re-derive destroys them. That
  sweep is the acceptance instrument, the cutover tool and the rollback lever at once,
  and must be PWA-operable.
- **W5 — Teardown.** Delete the old chain only once the new one is proven. Never split
  deletion from replacement (coverage gate).

## Rollback

C standing alone is a coherent product. If B fails at any point, the retreat is to stop
at W2 with the pipeline still writing — which is why W1 and W2 come before any deletion.

## Docs to reconcile at merge

`ANALYSIS.md` (largest), `ASSISTANT.md` (#10 amendment), `PREDICATE_CANONICALIZATION.md`,
`ENTITY_GRAPH_REFOCUS_PLAN.md`, `entity.md`, `ARCHITECTURE.md`, `ROADMAP.md` (Phase 6
status appears to understate what shipped), `ENTITY_GRAPH_INGEST_V2_PLAN.md` §16,
`backend/evals/README.md` (documents a table migration 0092 dropped), `DESIGN.md`
(three-lane inbox vs. two shipped).
