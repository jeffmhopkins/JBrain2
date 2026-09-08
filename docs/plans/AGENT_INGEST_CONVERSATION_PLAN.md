# Agent-Conversation Ingestion — Build Plan

> **Status:** Scheduled · **Last verified:** 2026-09-08 · **Waves:** W1◻️ W2◻️ W3◻️ W4◻️ W5◻️

Owner-ratified 2026-09-08. Research behind it: the eighteen dossiers in
`docs/research/agent-ingest/`, consolidated in `SYNTHESIS.md`. No code written yet.

## Thesis

A note is turn 0 of a conversation, and that conversation is **the ordinary agent
conversation** — not a second, hidden ingest path. The agent reads the note, writes
what it means through tools, and shows you what it did. You correct it by talking to
it. Deterministic code still owns *how* a write lands: `supersession.decide()`, the
domain floors, span attestation, the projections.

The model supplies meaning; the engine supplies mechanics. Eighteen dossiers reached
that split from unrelated starting points. What changed at ratification is the
*posture*: the agent does not hold facts back for approval. It commits its reading and
makes the write legible, and disagreement is a reply, not a queue.

## Decisions ratified

| # | Decision |
| --- | --- |
| D1 | **One agent, one conversation type.** A note conversation is the same agent, loop, memory and session as chat. No separate ingest agent, no hidden path. |
| D2 | **Clear facts commit; the agent asks only when it cannot proceed.** No confidence threshold, server-side or model-side. `ask_owner` is for genuine ambiguity (two equally-good Daves), not for caution. |
| D3 | **Every tool call is visible as an "entity modified" chip**, expandable to what was added or changed. Correction is conversational: you disagree in the thread and the agent fixes it. |
| D4 | **No review inbox.** Conversations are the only surface. The inbox redirects to the relevant thread in W2 and is deleted in W5. |
| D5 | **Questions live in the note's thread only** — no queue, no launcher tile, no badge, no expiry ladder. An unanswered question just sits in its thread. |
| D6 | **A note keeps its original body frozen** and gains appended, timestamped clarification blocks as you answer. Re-analysis reads the whole thing. |
| D7 | **Re-derivability stays binding.** The clarification blocks are chunks of the same note, so the graph re-derives from notes alone and `wiki_citations` has a real chunk to cite. |
| D8 | **Unattended, the first pass gets graph tools only.** No outward-facing tool runs while you are asleep. The full tool surface unlocks the moment you reply in the thread. |
| D9 | **EMR import goes through the agent conversation, like a note** — reversing this plan's earlier "no model between a lab result and the record". Large imports chunk across several turns. |
| D10 | **Intake commits like a note, unrestricted.** `ASSISTANT.md` #10 is retired, not amended. Risk accepted below. |
| D11 | **Correction notes are retired.** The conversation replaces them; `correction=True` survives as what the conversational-correction tool sets, so force-supersede + pin keep their semantics. |
| D12 | **Attachment-sourced facts commit**, marked as attachment-sourced on the chip. No hold. |
| D13 | **No gate spike.** Build straight through. W1 and W2 land before anything is deleted, so stopping at W2 remains the retreat. |
| D14 | **Throughput accepted as-is** — a note costs a multi-turn conversation instead of two calls, on a serial single GPU. No fast path, no quiet-hours mode. |
| D15 | **`owner_prefs`** — see below. |

### `owner_prefs`

A single capped Markdown document of your standing instructions ("how to handle recipe
notes", "stop splitting ingredients", "never infer a mood"), modelled directly on the
archivist's cross-session memory (`agent/archivisttools.py`, `models/archivist.py`): an
owner-only table, a read tool and a write tool, no separate Settings editor — you see
changes as tool-call chips like any other write.

- **Injected into every note conversation's prompt**, ahead of the note.
- **`prefs_write` fires only when you ask for it** ("remember that", "stop doing that").
  It is never a tool the agent reaches for on its own initiative, and it never proposes
  a rule unprompted. This is the one gated write in the design, because it is the thing
  that steers every other one.
- **New rules apply forward only.** When one lands, the agent reports how many existing
  notes it would change and offers to re-run them — the W4 rebuild machinery, scoped.

## What ratification removed

Recorded so the research trail stays readable against the dossiers:

- **Wave 0, the gate spike, and its pre-registered kill numbers** (was D5/D6). The
  design no longer rests on the agent's commit-vs-hold judgment, so three of the four
  thresholds measured something nothing depends on. llama.cpp tool-calling reliability
  is now discovered in W3; the serving-stack question (llama.cpp's grammar-XOR-tools
  constraint vs. vLLM/SGLang + XGrammar) is decided there, on observed behaviour.
- **The question expiry ladder** (was D2) — 7d/30d/60d decay to a marked assumption.
  Nothing blocks on an answer any more, so there is nothing to expire.
- **The I5 sensitive hold.** An inferred fact on a deterministically floored sensitive
  predicate used to wait for review (`arbiter.py:155-170`). It now commits and shows.
  The floor itself stays: the fact is still written into `health`, still firewalled.
- **The OCR auto-commit carve-out** — replaced by the attachment-sourced marking (D12).
- **The launcher-tile queue** from D8's GUI decision. Variant C's note threads stand;
  its queue placement does not.

## Binding constraints

Baked in from the start, not discovered in wave 3:

1. `wiki_citations.chunk_id` is `NOT NULL` (`0046:159`) behind a trigger requiring
   `citation.domain_code = chunk.domain_code = fact.domain_code` (`0046:196-211`), and
   `wiki/builder.py:520-530` INNER JOINs chunks. A clarification block must therefore be
   chunked **in the domain of the fact it clarifies**, not the note's captured domain.
2. **Session scope.** `integrate_note` is unstamped today, so it runs `SYSTEM_CTX`,
   all-domains (`worker.py:113-122`) — safe while the writer is code, not once a tool
   loop holds it. But narrowing to a single domain breaks resolution: entity lookup
   already ratchets in SQL to `domain_code IN (:dom, 'general')`
   (`analysis/entities.py:157,224,291,419,456`), and `has_domain_scope` under
   `owner_scoped` (`0015`) would hide the `general` half. **The conversation therefore
   runs owner-scoped to `(note_domain, 'general')`** — matching the existing ratchet —
   and a floored write into a domain the session does not hold happens **inside
   `commit_facts`**, deterministic code, in its own scoped session. Never a model-facing
   escalation.
3. No destructive verb in the model's vocabulary. `jbrain_app` holds `DELETE` on
   **five** tables — `facts, temporal_tokens, review_items, note_analysis, entities`
   (`0009:34-39`) — and the *ingest* path uses it, not only purge
   (`pipeline.py:947`, `_sweep_stale_ambiguous`). Retiring the inbox (D4) removes most of
   those call sites; what remains moves to `SECURITY DEFINER` and the grants are revoked.
4. `supersession.decide()` stays the implementation of the write tool, never a
   model-facing verb. This is what keeps the 75 scenario files' assertions alive.
5. **`_apply` is a whole-note declarative writer.** It retracts every fact of the note
   the re-run no longer asserts (`pipeline.py:904-916`) — that is what makes editing a
   note drop the facts it removed. Tool calls write as they are made (D1: a normal agent
   loop), so **the sweep becomes an explicit end-of-turn step**. Lose it and re-analysis
   stops being self-correcting.
6. The `entity_mentions` write is un-gated — it is the co-mention spine `neighborhood()`
   traverses and would degrade silently over months.
7. `entity_mentions.chunk_id` NOT NULL (`0006:89-104`) must accept a clarification-block
   anchor.
8. **No JSON-Schema `enum` anywhere** — it segfaults llama.cpp's harmony grammar
   (`STRIX_HALO_SETUP.md:592-601`). Values go in descriptions, validated in handlers.
9. **The unattended tool surface is enforced by the registry, not the prompt.** D8 is a
   property of which handlers are bound, not an instruction the model can be talked out of.
10. Note delete is a **soft** delete (`notes/repo.py:174-194`) — FK cascades do not fire;
    conversation purge is explicit in `analysis/purge.py`.

## Waves

**W1 — `commit_facts` + the end-of-turn sweep.** Extract the deterministic commit core
from `_apply` so old callers and new tools share one writer. **Must include the four
typed projections and provisional→confirmed promotion** (`pipeline.py:951-954,981-987`):
unhooked, appointments/labs/geofences freeze silently and the nightly hygiene sweep
hard-deletes never-promoted provisional entities. Plus the sweep as a callable step
(constraint 5). Reusable under every outcome; unblocks D9/D10.

**W2 — The conversation surface.** `note_conversations` and turn/tool-call tables on the
existing agent loop with a restricted registry (D8); the frozen-body + clarification-block
note model (D6); the "entity modified" chip (D3); the inbox redirect (D4). Ships value
with or without the write tools, which is the retreat.

**W3 — Write tools and `owner_prefs`.** `resolve_entity`, `assert_fact`, `note_mentions`,
`merge_entities`, `correct_fact`, `ask_owner`; `prefs_read` / `prefs_write` (D15).
**Includes re-authoring the input half of all 75 harness scenarios**: each step scripts an
`integrate.note` *intent* today (`tests/harness/runner.py:1-20` — "we are BOTH models"),
which under tools becomes a scripted tool-call sequence. The `expect` half is untouched.
This is where the local model's tool reliability is actually learned (D13).

**W4 — Cutover, port, and the rebuild sweep.** Port EMR (D9) and intake (D10) onto the
conversation. There is currently **no way to rebuild the graph while keeping the notes** —
the only no-terminal full re-derive destroys them. That sweep is the acceptance
instrument, the cutover tool, the rollback lever, and what `owner_prefs` re-runs against
(D15). Must be PWA-operable and resumable.

**W5 — Teardown.** Delete the old chain, the review-inbox screen and its card-filing code,
and the correction-note path — only once the new one is proven. Never split deletion from
replacement (coverage gate).

## Live defects folded in

Wave 0b's list, re-homed now that W0 is gone. All three are real today, independent of
this redesign, and each lands in the wave whose work it would otherwise break:

- **W1 — the capture-race settle clause compares against `notes.created_at`**, the
  *client's* capture time. An offline-flushed note with a promised attachment is
  eligible for body-only integration immediately, defeating the gate in exactly the
  case it exists for. It must compare against a server-side receipt time.
- **W3 — `merge_entity_pair` repoints facts with unfiltered `UPDATE`s**, so a
  cross-domain merge half-completes under a narrowed session. This blocks the
  `merge_entities` tool directly: constraint 2 puts the conversation on a narrowed
  scope, which is precisely the condition that breaks it. Fix by scoping the `UPDATE`s
  and failing loudly on rows left behind, never by widening the session.
- **Any wave — the live vision route is an abliterated checkpoint** whose GGUF template
  hard-codes a "never refuse, no pushback" system prompt **above** ours, with no API
  switch (`llm/local_catalog.py:869-878`). It structurally inverts the
  data/instruction boundary, which matters more once attachments feed a tool loop (D12).

## Risks accepted at ratification

Stated because each is a property the system has today and will not have after:

1. **Intake is third-party text and the agent holds write tools.**
   `adv_prompt_injection_body_inert.json` passes today because the pipeline only
   *extracts* — a note body cannot instruct anything. That is not true of a tool loop.
   The owner declined both a domain restriction and a read-only intake surface. `ASSISTANT.md`
   #10 is retired outright rather than amended.
2. **No spike.** llama.cpp may not drive the write tools well enough. Discovered in W3;
   the retreat is stopping at W2 with the pipeline still writing.
3. **Inferred sensitive values commit.** An inferred mental-health value lands visibly
   instead of waiting. Defensible on a single-owner box; no longer a floor.
4. **5–10× inference per note** (D14), on a serial single GPU. A capture burst or a
   rebuild will make chat sluggish while it works through.

## Docs to reconcile at merge

Larger than the pre-ratification list, because retiring the inbox and #10 changes what
several Living docs assert:

- `ASSISTANT.md` — #10 retired (D10); the memory-model section gains `owner_prefs`.
- `ANALYSIS.md` (largest) — the review-gate, arbiter-hold and I5-net sections; the
  correction-note path; `_apply`'s decomposition.
- `DESIGN.md` — the three-lane inbox is not a doc-drift fix any more; the inbox goes.
- `PREDICATE_CANONICALIZATION.md`, `ENTITY_GRAPH_REFOCUS_PLAN.md`, `entity.md`,
  `ARCHITECTURE.md`.
- `ROADMAP.md` — Phase 6 status appears to understate what shipped; add this plan.
- `ENTITY_GRAPH_INGEST_V2_PLAN.md` §16 (the rejection this plan reopens) and §6
  (re-derivability, preserved by D7).
- `backend/evals/README.md` — documents a table migration 0092 dropped.
- `docs/mocks/agent-ingest/` — variant C stands, its queue tile does not (D5).
