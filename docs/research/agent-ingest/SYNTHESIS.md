# Agent-Conversation Ingestion — Research Synthesis

> **Status:** Research · **Last verified:** 2026-09-08 — consolidated read of the
> eighteen dossiers in this directory. **The owner ratified on 2026-09-08 and the
> decisions moved:** see `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` for what was
> chosen. The "Open owner decisions" section below is preserved as the record of what
> was asked, not as live questions — several were answered differently from the
> recommendation here (notably: option B directly with no gate spike, and the review
> inbox retired rather than kept).

## The ask

Replace the deterministic note-analysis pipeline (extract → Integrator → arbiter →
apply) with a note-rooted agent conversation: the agent reads the note, decides what
it means, writes the entity/predicate graph through tools where confident, and asks
the owner where it isn't. Owner replies build and correct the graph.

Owner constraints, ratified up front: confidence-split write authority; the DB is
disposable; scope is owner notes + attachments/OCR/media; questions go to a **silent**
queue with no push; **inference is local-only** (`gpt-oss-120b`, `qwen3.8-27b`);
the owner operates the box from a phone with no terminal.

## What the fleet converged on

Six dossiers reached the same conclusion from six unrelated starting points, which is
the strongest signal in the set:

| Dossier | Route to the conclusion |
| --- | --- |
| X7 (adversarial) | `ENTITY_GRAPH_INGEST_V2_PLAN.md` §16 already evaluated and rejected this at owner request, on a 121-case on-box battery |
| L1 (local models) | llama.cpp cannot combine a user grammar with tool calling; choosing tools forfeits `strict`, `maxItems` and the router re-ask |
| L2 (confidence) | the model's self-report cannot be trusted to gate writes; the server must own the ceiling |
| B3 (tool surface) | `supersession.decide()` is the hardest-won decision in the repo and must not become a model-facing verb |
| X3 (prior art) | mem0 shipped LLM-decided reconciliation and removed it; Letta split conversation from memory maintenance |
| X5 (evals) | keeping `decide()` is what determines whether 75 scenario files survive |

**The convergent answer: the agent should decide _what_ to write; deterministic code
should decide _how it lands_.** Not "agent instead of pipeline", and not "pipeline
instead of agent" — the model supplies meaning and the engine supplies mechanics.

## The counter-argument, which is good

X6 rebuts §16 head-on and the rebuttal holds. §16's decisive line is *"the model lacked
restraint, not information — a lookup tool answers a question the context already
answers."* That is an argument against giving the model **retrieval** tools. This
proposal's core new affordance is not retrieval; it is `ask` — a way to *spend*
restraint. And the battery's dominant failure was precisely a detection-to-abstention
gap: on the sensitive net the model set `inferred:true` + `domain:health` correctly on
**7/7** facts and then proposed `commit` anyway (§15). It perceived correctly and
abstained wrongly. `ask` targets that gap directly.

So §16 does not refute the owner's instinct. It refutes a *different, larger* design
than the one that instinct actually requires.

## The three options

**A — Full replacement (as originally scoped).** Delete the pipeline; agent tools are
the writer. Contradicted by §16, by mem0's and Letta's retreats, by the measured 39%
degradation / +112% unreliability of multi-turn instruction sharding, and by the
llama.cpp grammar-vs-tools constraint. No dossier recommends it.

**B — Agent writes *through* the deterministic floors.** The agent produces the intent
through tools, but `supersession.decide()`, the domain floor/ratchet, the same-name
gate and server-side span attestation remain non-negotiable preconditions inside the
tool implementations. The conversation is real and the graph keeps its guarantees.
B3, L2, X5, X4 and X6 independently converged on this shape.

**C — Conversable ingest.** The pipeline stays the writer of record; the agent becomes
the conversation and correction surface over its output, plus `ask`. Smallest change,
ships fastest, retains every guarantee, and delivers most of the felt experience.

## Recommendation

**Run the gate spike first; ship C's conversation surface in parallel; go to B only if
the spike passes.** The spike deletes nothing and is the cheapest way to convert the
central disagreement into a number. C is valuable under every outcome, so it is not
wasted work if the spike fails.

Pre-register the kill numbers before running it (L1's proposal): well-formed tool
output < 95%, no ≥5-point judgment gain over today's `INTENT_SCHEMA`, any safety
flag-strip regression, or median latency > 5× — any one of those kills B.

## Findings that bind whichever option is chosen

1. **`ASSISTANT.md` #10 is inverted.** *"Untrusted-origin content never triggers a
   background job… never on note bodies."* Every option that auto-runs on capture
   violates this verbatim. Owner escalation under `PROCESS.md`.
2. **The chunk FK is enforced in Postgres.** `wiki_citations.chunk_id` is `NOT NULL`
   (`0046:159`) with a trigger tying it to the chunk's note, and `wiki/builder.py:520-530`
   INNER JOINs chunks. A fact with no chunk is rejected, not merely unpublished.
   Conversation turns must be mintable as chunks.
3. **`SYSTEM_CTX` is `owner_scoped=False`** (`db/session.py:20-31`, `queue.py:24`).
   Safe while the writer is code; hand it to a tool loop and the domain firewall
   evaporates with RLS still reporting success.
4. **`jbrain_app` holds `DELETE`** on facts/entities/review_items (`0009:34-39`).
   No destructive verb belongs in a model's vocabulary.
5. **The abliterated vision model injects its own "never refuse" system prompt above
   ours**, with no API switch (`llm/local_catalog.py:869-878`). It is the live vision
   route today. Fix regardless of this redesign.
6. **Non-note paths borrow the pipeline.** EMR import, intake, correction notes and
   `save_place` all route through `integrate_note` / `plan_intent` / `apply_intent`.
   "Leave them alone" is not achievable; extract a shared deterministic `commit_facts`
   primitive first.
7. **Four projections + provisional→confirmed promotion live inside `_apply`.**
   Unhooked, appointments/labs/geofences freeze silently and the nightly hygiene sweep
   hard-deletes never-promoted provisional entities.
8. **The mentions spine degrades silently.** An agent that writes only when confident
   stops populating `entity_mentions`, which is what `neighborhood()` traverses. The
   mention write must be un-gated.
9. **No JSON-Schema `enum` anywhere** — it segfaults llama.cpp's harmony grammar
   (`STRIX_HALO_SETUP.md:592-601`).
10. **There is no way to rebuild the graph while keeping the notes.** The only
    no-terminal full re-derive available today destroys the notes. That sweep is the
    acceptance instrument, the cutover tool and the rollback lever at once.

## Pre-existing defects surfaced (independent of this redesign)

- The capture-race settle clause compares against `notes.created_at`, which is the
  *client's* capture time — an offline-flushed note with a promised attachment is
  eligible for body-only integration immediately, defeating the gate in exactly the
  case it was built for.
- `merge_entity_pair` repoints facts with unfiltered `UPDATE`s; under a narrowed
  session a cross-domain merge half-completes.
- `FixtureLlmClient` (record/replay, built for this) raises `TypeError` behind the
  router.
- `backend/evals/README.md` documents a nightly `eval_run` writing `app.eval_runs`;
  migration `0092` dropped that table.
- `DESIGN.md` specifies a three-lane review inbox; two shipped, and the `deferred`
  status sits unused.
- OCR/VLM agreement scores and RapidOCR per-line boxes are computed and discarded —
  the line boxes are the missing image-citation primitive.

## Open owner decisions

Collected from all eighteen dossiers, deduplicated, in the order they block work:

1. **The fork** — A, B, or C-then-B.
2. **Serving stack** — stay on llama.cpp (grammar XOR tools) or evaluate vLLM/SGLang
   with XGrammar, where constrained decoding composes with tool calling.
3. **Is re-run determinism still binding?** §6 of the Ingest V2 plan says the graph must
   be re-derivable from notes. If yes, owner answers must become owner-authored notes,
   not agent state.
4. **`ASSISTANT.md` #10** — amend, or gate first passes behind an owner action.
5. **GUI variant** — three mocks are in `docs/mocks/agent-ingest/`; F1 recommends C,
   F2 dissents on where the queue lives.
6. **Question expiry** — decay to a marked assumption on a ladder, never for
   health/finance/location?
7. **Non-note paths** — extract `commit_facts` and keep EMR/intake/correction working?
8. **Phase 6** — X4 says this doesn't delay the wiki; X8 says pause Wave D. Unresolved.

## The dossiers

`B1` teardown · `B2` conversation model · `B3` graph tools · `B4` predicate system ·
`B5` orchestration · `L1` local tool-calling · `L2` confidence · `F1` GUI mocks ·
`F2` silent queue · `F3` frontend teardown · `X1` security · `X2` attachments/vision ·
`X3` prior art · `X4` waves/cutover · `X5` evals · `X6` prompt design · `X7` adversarial ·
`X8` downstream consumers.
