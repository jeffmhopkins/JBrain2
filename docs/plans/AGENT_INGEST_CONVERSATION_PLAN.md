# Agent-Conversation Ingestion — Build Plan

> **Status:** In progress · **Last verified:** 2026-09-09 · **Waves:** W1✅ W2◻️ W3◻️ W4◻️ W5◻️

Owner-ratified 2026-09-08, then revised the same day against six independent cold
reviews (`docs/research/agent-ingest/COLD_REVIEW_FINDINGS.md`). Research behind it: the
eighteen dossiers in `docs/research/agent-ingest/`, consolidated in `SYNTHESIS.md`. The
proposed tool list is `docs/research/agent-ingest/TOOL_SURFACE.md`. No code written yet.

## Thesis

A note is turn 0 of a conversation, and that conversation is **the ordinary agent
conversation** — same loop, same memory, no second hidden ingest path. The agent reads
the note, writes what it means through tools, and shows you what it did. You correct it
by talking to it. Deterministic code still owns *how* a write lands:
`supersession.decide()`, the domain floors, span attestation, the projections.

**The note screen is not a surface this plan changes.** A note conversation is the
ordinary agent conversation — the same loop, memory and chat surface as Full Brain —
with the note as turn 0. The writes and the clarification asks are tool components
inside it. There is no Record tab, no as-captured toggle, and no bespoke clarification
treatment; the GUI work is two components and an inbox that redirects.

The model supplies meaning; the engine supplies mechanics. What ratification changed is
the *posture*: the agent does not hold facts back for approval. It commits its reading
and makes the write legible, and disagreement is a reply, not a queue.

## Decisions ratified

| # | Decision |
| --- | --- |
| D1 | **One agent, one conversation type.** A note conversation is the same agent, loop and memory as chat — but **its own closed tool allowlist**, never the curator wildcard (D16). |
| D2 | **Clear facts commit; the agent asks only when it cannot proceed.** No confidence threshold, server-side or model-side. `ask_owner` is for genuine ambiguity, not caution. |
| D3 | **Every tool call is visible as a custom tool component inside the conversation**, expandable to what changed — not a note-screen surface. It must render `written · replaced · held · from a photo · failed · truncated · writing…`, with each write's domain named **in words, never colour alone**. Correction is conversational. |
| D4 | **The inbox becomes two tabs, and it only redirects.** A **notes** tab lists ingestion questions *and pending approvals* waiting on you; tapping opens the conversation. **Nothing is answerable from the inbox** — the conversation is the only place ingestion is decided, or the inbox becomes a second surface where that happens. A **wiki** tab holds findings that never start from a note. |
| D5 | **Questions live in their note's conversation** — findable from the notes tab, which is the discoverability answer the original "no queue at all" lacked. Still no push and no nagging badge. |
| D6 | **A note keeps its original body frozen** and gains appended, timestamped clarification blocks as you answer. This is a **storage** decision with no bespoke rendering: the existing note view renders appended text as text, and the note screen does not change. *Shipped as `app.note_clarifications` (0193) composed onto the note's text at read time — never into `notes.body`, which a `PATCH` would overwrite whole.* |
| D7 | **Re-derivability stays binding.** Clarification blocks are chunks of the same note, so the graph re-derives from notes alone and citations have a real chunk. |
| D8 | **Unattended, the first pass gets graph tools only.** Nothing outward-facing runs while you are asleep. The full surface unlocks when you reply. |
| D9 | **EMR import goes through the agent conversation, like a note.** Large imports chunk across several turns. |
| D10 | **Intake commits like a note, unrestricted** in *what* it may write. `ASSISTANT.md` #10 is amended, not retired (D16 changed what that costs). |
| D11 | **Correction notes are retired.** `correction=True` survives as what `correct_fact` sets, so force-supersede + pin keep their semantics. |
| D12 | **Attachment-sourced facts commit**, marked as attachment-sourced on the chip. |
| D13 | **No gate spike.** Build straight through; W1 and W2 land before anything is deleted. |
| D14 | **Throughput accepted as-is.** No fast path, no quiet-hours mode. |
| D15 | **`owner_prefs`** — a standing-instructions document; see below. |
| D16 | **The note conversation has a closed tool allowlist**, an explicit `AgentProfile` with `tools=frozenset({...})`, never `allow=None`. `file_correction`, `add_source_exclusion`, `make_intake_link` and `remember` are provably outside it. |
| D17 | **`prefs_write` stages a Proposal you approve**, the `remember.tool` pattern — enforced by code, not by the model behaving. |
| D18 | **The agent chooses the domain for predicates the registry has never seen.** For the ~45 registered sensitive predicates the floor still wins regardless (`pipeline.py:1945-1949`); the agent's choice is load-bearing only on novel ones. This reverses `arbiter.py:163`'s "never a model per-fact domain" rule, deliberately and in a bounded way. |

### `owner_prefs`

A single capped Markdown document of standing instructions ("how to handle recipe
notes", "stop splitting ingredients"), on the archivist's cross-session-memory shape
(`models/archivist.py`, `agent/archivisttools.py`): an owner-only table, read and write
tools, no separate Settings editor.

- **Injected into every note conversation's prompt**, ahead of the note.
- **`prefs_write` fires only on your explicit request, and stages a Proposal** (D17). The
  archivist's bare full-replace upsert is *not* the model to copy for the write half —
  it is safe only because it is `permission: web` and reads no untrusted text. Use
  **delta ops** (`memory_edit`'s add/replace/remove on numbered rules), not a full
  rewrite.
- **New rules apply forward only.** When one lands, the agent reports how many existing
  notes it would change and offers to re-run them. The W1 rebuild sweep is **corpus-wide
  only**; its run row and cursor accommodate a scope predicate cleanly, but the scoped
  per-rule re-run is **W3 work**, not W1.

## What ratification removed

- **Wave 0, the gate spike and its kill numbers.** The design no longer rests on the
  agent's commit-vs-hold judgment. llama.cpp tool reliability and the serving-stack
  question are learned in W3.
- **The question expiry ladder.** Nothing blocks on an answer, so nothing expires.
- **The I5 sensitive hold** (`arbiter.py:159-171`). The floor itself stays.
- **The OCR auto-commit carve-out**, replaced by the attachment-sourced marking (D12).
- **Ingest review cards** — the arbiter-derived kinds (`low_confidence_inference`,
  `ambiguous_mention`, `new_predicate`). **Not** the `review_items` table itself: see
  constraint 4.

## Binding constraints

Corrected against the code by the cold reviews; the pre-review versions of 1, 2, 3 and 5
were wrong.

1. **Citations.** A re-ingest deletes a note's chunks and `facts.chunk_id` is
   `ON DELETE SET NULL` (`0006:187`), so every in-place update path must **re-anchor**,
   and only for a fact **this note owns** — `_existing_facts` carries no `note_id`
   predicate, so a refresh can land on another note's fact by corroboration and
   re-anchoring it would make `wiki/builder.py` cite the wrong note. Four paths owe it:
   the refresh branch, `decide()`'s in-place **close** branch (reachable on this note's
   own open row), `_insert_held_fact`'s idempotent held-row refresh, and the **derived
   shadow** written by `_materialize_inverse` — whose `chunk_id` the same cascade nulls
   and which the settle sweep deliberately never re-inserts, leaving the reciprocal edge
   silently absent from the *object* entity's article.
   `wiki_citations.chunk_id` is `NOT NULL` (`0046:159`) behind a trigger
   requiring `citation.domain = chunk.domain = fact.domain` (`0046:187-217`), and
   `wiki/builder.py:527` INNER JOINs chunks. The mechanism for a fact that ratchets above
   its note's domain **already exists**: `_citation_chunk` (`pipeline.py:1645-1687`)
   get-or-creates a `source_kind='derived'` same-domain copy. So a clarification block is
   **chunked normally, in the note's captured domain** — chunking it in the fact's domain
   would make it unsearchable (derived chunks carry no embedding and `search/repo.py:27`
   excludes them). The derived-chunk INSERT needs the same escalation a floored fact
   write gets (constraint 2).
2. **Session scope.** `integrate_note` *is* stamped on the main path; `SYSTEM_CTX` comes
   from `pipeline.py:311`, where the handler opens its own session. Narrowing is **not**
   free: `_exact_matches` (`entities.py:563-580`) carries **no domain predicate** and is
   layer 1 of `resolve_entity`, so narrowing silently mints duplicates, under-counts
   `AmbiguousEntity` and `same_name_entity_ids`, and weakens the alias collision guard.
   The conversation therefore runs owner-scoped to `(note_domain, 'general')` **and**
   entity *reads* keep an explicit cross-domain path through a `SECURITY DEFINER`
   resolver that re-asserts the ratchet internally. Floored fact writes and derived-chunk
   writes escalate **inside `commit_facts`**, never as a model-facing verb.
3. **Destructive verbs.** `jbrain_app` holds `DELETE` on **seven** tables — `0009:34-39`
   plus `entity_aliases` and `entity_mentions` from `0006:260-261`. Note that in this
   repo `SECURITY DEFINER` is the idiom for *bypassing* RLS (`0045:203`, `0046:183-188`),
   so any such delete function must re-assert the domain predicate internally and be
   unreachable from any model-facing tool. **This does not extend to the merge path** —
   W1 took the opposite route there deliberately (see constraint 12).
4. **`review_items` and `pending_review` survive.** `decide()` returns
   `insert_status="pending_review"` at twelve sites, and `_lab_status_transition`
   (`supersession.py:410,484`) is how a **FHIR preliminary lab reading** is represented —
   `emr_projection.py:117,132,141-150` reads it back. Only the *screen's* ingest tab and
   the arbiter-derived kinds go. Deleting the status would make a preliminary reading a
   citable current value; deleting the table would strand held rows with no resolver.
5. **`supersession.decide()` stays the implementation of the write tool**, never a
   model-facing verb.
6. **The settle sweep is whole-note and whole-conversation.** `_apply` retracts every
   non-pinned, non-derived fact of the note not in `touched` (`pipeline.py:904-917`).
   `touched` must **accumulate across the whole conversation as durable state** — a
   per-turn sweep retracts the previous turn's commits. And it must **not run on a
   truncated turn**: `loop.py:131-133` sets `max_steps=20` and
   `max_consecutive_tool_errors=3`, and a turn ending partway has asserted only a prefix.
   Sweep only on a turn that ended cleanly and not `awaiting_owner`.
7. **`_rebuild_mentions` becomes an upsert plus a reconcile, split across the seam.**
   It was `DELETE … WHERE note_id` then re-insert (`pipeline.py:1278`); called per tool
   call that wipes what the previous call wrote. The key is
   `(chunk_id, char_start, char_end, entity_id)` **matched with multiplicity** — one
   existing candidate popped per asserted mention — because even that is not unique:
   `_locate` returns the *first* occurrence of a surface and falls back to a zero-width
   span on `chunks[0]`, so two entries can share a span with different entities *and* two
   entries can produce the identical row the old wipe-and-reinsert preserved. A plain
   `ON CONFLICT` upsert would silently merge rows and would need a unique index the data
   cannot satisfy. **The reconcile half must run in `settle_note`** over the union of
   every pass's asserted ids — a per-pass reconcile deletes the previous pass's mentions,
   which is the same failure this constraint exists to prevent. The mentions write stays
   un-gated by confidence: it is the co-mention spine `repo.py:667-684` builds and
   `neighborhood()` traverses.
8. **No JSON-Schema `enum` in a `.tool` sidecar.** The segfault is scoped to gpt-oss's
   harmony path and the enum × full-optional-field interaction
   (`STRIX_HALO_SETUP.md:592-601`) — sound as an authoring rule, not a blanket claim.
   Note `neighborhood.tool` already carries that exact shape, so it cannot be offered in
   this persona's tool union.
9. **The tool surface is enforced by the registry, not the prompt.** D8 and D16 are
   properties of which handlers are bound. Every write tool must also be added to
   `NEVER_DEFAULT` (`toolregistry.py:34-41`), or the `allow=None` wildcard hands it to the
   curator on every ordinary chat turn.
10. **Every new table needs an RLS isolation test** (CLAUDE.md #3). W2 and W3 add four.
11. Note delete is a **soft** delete (`notes/repo.py:174-194`), but the same transaction
    hard-deletes the note's chunks, so `chunk_id` cascades do fire. Conversation purge is
    explicit.
12. **An entity fold is a full-owner-only write.** `merge_entity_pair`'s four `UPDATE`s
    are silently narrowed by RLS, so a cross-domain merge half-completes: some facts
    repoint, others strand on the tombstone. W1 makes the fold fail closed by asking
    Postgres `app.is_full_owner()` before any statement runs, backed by a trigger on
    `app.entities`. Two consequences the plan must carry: a narrowed note conversation
    can therefore only **stage** a fold, never enact one — the owner's enact is already a
    full-owner session — and the trigger is a **partial** backstop, because when the
    loser row is out of scope RLS filters it from the scan and no row trigger fires at
    all. The Python guard is what covers that shape.

## Waves

**W1 — Commit core, rebuild sweep, and the citation bug.** Extract `commit_facts` from
`_apply` (one call site, `pipeline.py:533`) — **including `_register_declared_aliases`,
`_upsert_tokens`, `_materialize_inverse` and `_propagate_supersession_to_shadows`**. The
whole-note reconciliation — the three projections, the device binding,
`_reproject_entities`, `repair_chains`, `_sweep_stale_ambiguous` and
`_sync_truncation_review` — belongs to `settle_note` instead: it is per-note work that
must run once after the last commit, not per commit. W1 delivers the SEAM for the
`touched`/`projected` ledger (constraint 6), not the ledger: `CommitOutcome` is an
in-memory frozen dataclass, so accumulating that state durably across a whole
conversation is W2/W3 work. Incremental mentions upsert (constraint 7).

Also here, because they are cheap and independent: **the rebuild sweep** — it composes
`purge_note_artifacts` + `backfill_pending_integration`, both shipped and already
resumable, and it is the only corpus-scale proof this refactor preserved behaviour. It
must preserve pinned facts (`purge.py:96` has no such filter today) and resolved review
history (no status filter today), and **chain into a wiki rebuild**, since
`wiki_citations.fact_id` is `ON DELETE SET NULL` and `wiki_articles.entity_ref` is a soft
ref with no FK. Plus the settle-clause fix (`queue.py:635`), the `merge_entity_pair`
scoping fix, and **the refresh-path citation bug**: `pipeline.py:2048-2058` never
re-links `chunk_id` after a re-ingest nulls it, so refreshed facts silently vanish from
their articles. D6 makes that fire on every answered question.

Verified by `test_apply_intent_pg.py` (20 tests), `test_reanalysis_pg.py` (4), the 75
scenarios unchanged, and a corpus rebuild diff.

**Filed by W1, fixed since (2026-09-09):** `_resolve_from_intent` loaded the
Integrator's `existing` entity by id with no `status != 'merged'` filter, unlike
`_exact_matches`, so a re-analysis that echoed the loser's id back resolved a surface
onto a merge tombstone — minting live facts and a live mention on a merged row while the
survivor's were swept, silently un-doing the merge, needing no rebuild to fire. (Every
context builder does filter merged, so on the live box the id has to arrive by a race —
a fold landing mid-analysis — or by a future replay of a stored decision; the code path
itself was unconditional.) The id now resolves through the
fold (`entities.live_entity_by_id`): a tombstone redirects to the survivor it recorded
in `merged_into_id`, a chain is chased to its end, and a tombstone with no survivor
withholds the resolution as before. That also closes the caveat on the sweep's spared
`mention_ids`: un-merge replay now survives an intervening re-analysis end to end.

**W2 — Conversation shell only.** `note_conversations` + turn/tool-call tables on the
existing loop; the closed `AgentProfile` (D16) as a mechanism with an empty graph-tool
set; the frozen-body + clarification-block note model. **No chip, no redirect** — 116
`.tool` files contain no graph-write verb, so there is nothing to render yet. Honest
retreat point: the agent reads a note in a visible thread.

*Landed:* the D16 profile — `note_ingest` in `agent/agents.py` with `tools=frozenset()`
and empty `extra_tools`, its prompt sidecar, and migration `0192` widening both agent
CHECKs. `reads_knowledge_base=False` for now, which **W3 must revisit**: constraint 2
wants the conversation owner-scoped to `(note_domain, 'general')`, and `False` zeroes the
session's read scopes, so the domain-visible entity read tools cannot be reached under it.

*Landed:* the conversation itself — the `note_converse` action (`analysis/converse.py`),
seeded onto `note.ingested` **beside** `integrate_note`, not instead of it (D13). It opens
an ordinary `agent_sessions` row under `note_ingest`, drives one turn through the shared
`LoopTurnExecutor`, persists through `AgentTranscript` (so the thread renders on the
shipped transcript route with no frontend work), records every tool call into the 0191
ledger and binds it to its assistant turn, and settles `settled` / `failed` — `failed`
also for a turn that did not end cleanly, since constraint 6's sweep must never see a
truncated pass. Turn 0 is the note **fenced as DATA** (`framed_note`, the
`intake/turn.py:_RECIPIENT_FRAME` pattern), closing the unframed-body half of risk 1
while the persona still holds no tools. The dispatcher gained the graceful arm in front
of `note_conversations_one_live`, so a re-delivered event is a logged skip rather than an
IntegrityError in a worker. The trigger ships **enabled**: one extra `agent.turn` per note
producing no graph writes at all this wave is risk 4, and a disabled trigger would ship
W2's retreat point already retreated from. Its tool registry is empty as well as its
allowlist. Still open for W3: the executor's registry, `reads_knowledge_base`, the
`waiting_on_owner` producer, and moving the recorder into the tool dispatch so `ok` and the
written ids come from the write path.

*Clarification blocks, as built (migration 0193).* The body column is never appended to;
`app.note_clarifications` holds `(note_id, seq, question, answer, session_id, domain_code,
created_at)` and `jbrain.notes.compose.compose_body` joins them onto the body for the three
readers that matter — `_note_info` (list/get/PATCH, and so the note view and `read_note`),
the ingest chunk build (D7), and the integrator's chunkless body fallback. An un-clarified
note composes to its body byte-for-byte, and blocks append *after* the body, so no existing
`char_start`/`char_end` moves. Three consequences worth carrying:

- **The editor round trip.** The note editor loads `NoteInfo.body`, which is now composed, and
  PATCHes the whole string back. `update_note` therefore cuts at the first block marker before
  storing — otherwise an untouched save bakes the blocks into the column and the next read
  doubles them. This is what makes "frozen" enforced rather than conventional (COLD_REVIEW E's
  objection to `DESIGN.md:697`): the editor stays, and it simply cannot reach the blocks.
- **RLS is the note's, not owner-only.** `USING (app.has_domain_scope(domain_code))`, the
  notes/chunks/facts policy — *not* the `is_owner()` posture of `graph_rebuild_runs`/
  `archivist_memory`, which hold metadata and scratchpad. A clarification is the owner's words
  about a health or finance note and the same sentence is already firewalled in `app.chunks`;
  owner-only would let the narrowed session a note conversation runs as (constraint 2) read
  across the firewall. Grants are `SELECT, INSERT, DELETE` plus `UPDATE (domain_code)` alone,
  so the text is immutable in Postgres and the domain still carries on a note move.
- **Purge sides.** The privacy delete takes the blocks (explicitly — the note delete is soft,
  so 0193's cascade never fires); the rebuild sweep keeps them, or the graph stops re-deriving
  from the notes corpus-wide and silently. `backfill_deleted_note_artifacts` counts them as a
  candidate predicate so the intake-link teardown's soft deletes are swept too.

The append path is `SqlNotesRepo.append_clarification` plus its `NotesRepo` Protocol entry —
no route and no tool in W2; W3's `ask_owner` is the caller. It enqueues its own `ingest_note`
inside its transaction rather than relying on a caller to remember.

**W3 — Write tools, chip, tabs, `owner_prefs`.** The tools in `TOOL_SURFACE.md`; the
"entity modified" chip (~80% shipped — reuse `ToolOutcome.entities`, `StepRow` and
`toolSummary.ts`; **keep `ClaimDiff.tsx`**, it is the only diff renderer); the two-tab
inbox (D4); `owner_prefs` + `prefs_write`'s Proposal staging (D17). Re-point
`runner._compile_intent` to emit tool calls — **one function, not 75 files**; only
`rel_conjoined_past_employers.json` authors an intent. Then fix the `expect` halves: 9
scenarios assert a card that will no longer be filed, 8 assert `pending_review` statuses,
and **47 assert `count: 0` and become vacuously true** — delete those rather than leave
them green. The eval corpora (`evals/integrate_runner.py` and its cases) are superseded
too. Author a **new** adversarial scenario running a real model against a hostile body:
the re-authored `adv_prompt_injection_body_inert.json` is a tautology, by its own
description.

**W4 — Cutover.** Port EMR (D9) and intake (D10) onto the conversation. **Keep EMR
firewall Layer 2 as a hard non-commit** — `ingest/emr/firewall.py:3-28` has no
domain-floor backstop and `address`/`geo` are deliberately outside the floor, so it is
the only guard keeping a home address out of `health`. Note that the card W1 now files
**never existed before**: the guard and the handler that discarded its catches landed the
same day (`490c54987`, `166e24691`, 2026-07-03), so the control fired silently for its
entire life. Its card lands on the wiki tab (D4). The rebuild sweep is already in hand from W1, so it can serve as cutover instrument
and rollback lever.

**W5 — Teardown, decomposed.** W5a: the old chain (`pipeline.py:305-478` + `arbiter.py`,
~940 LOC), gated on W3's runner re-point. W5b: the arbiter card kinds and the inbox's
ingest tab, with an explicit surviving-kinds list. W5c: correction-note retirement plus
the `SECURITY DEFINER` move and grant revoke — a security-path change needing its own RLS
isolation test, which cannot ride a 3,000-line deletion. **Port `file_correction`
first**: `PHASE6_WIKI_PLAN.md:255-261` names `plan_intent(correction=True)` as the wiki
correction loop's shipped exit criterion, and `wiki/lint.py:790` offers it as a card
action.

The stated per-PR rule is *no PR removes a producer before its replacement is merged and
green* — the wave-level split of deletion from replacement is deliberate (D13).

## Risks accepted at ratification

1. **Intake is third-party text and the agent holds write tools.**
   `adv_prompt_injection_body_inert.json` passes today only because the pipeline extracts
   rather than acts, and its own description says the harness deliberately does not
   comply — so it proves nothing about a tool loop. D16 removes the sharpest edge (the
   curator wildcard reaching `file_correction`, whose docstring names the very gate D1
   removes); what remains is that stranger text drives an owner-identity session, since
   `owner_scoped` restricts domain data and never identity (`0015:13-15`). Also:
   `read_note` returns bodies **unframed** (`readtools.py:799-811`), safe today only
   because bodies are owner-authored — `intake/turn.py`'s `_RECIPIENT_FRAME` is the
   pattern to adopt.
2. **No spike.** llama.cpp may not drive the write tools well enough; discovered in W3.
   The retreat is stopping at W2 — which, honestly stated, leaves a thread that does not
   explain the graph while the old pipeline writes out of band.
3. **Inferred sensitive values commit**, and under D18 a novel-predicate clinical fact
   lands wherever the agent says.
4. **Cost.** 5–10× inference per note, plus — per answered question — a full re-chunk,
   re-embed, re-integration and one LLM article rebuild per mentioned entity, on a serial
   GPU, while you wait in the thread.
5. **The abliterated checkpoint is *selectable* for the vision route and for
   `agent.turn`**, not the live default (`router.py:58-59,181` default to
   `xai:grok-4.3`). Selecting it under this design inverts the data/instruction boundary
   in a persona holding write tools.

## Docs to reconcile at merge

`ASSISTANT.md` (#10 amended; #3/#5/#6 vs. `owner_prefs`; the memory-model section),
`ANALYSIS.md` (largest — review gates, arbiter holds, the I5 net, `_apply`'s
decomposition), `DESIGN.md` (the inbox becomes two tabs; the note-body treatment and the
chip need mock variants under rule 2; the Analysis tab's fate),
`PHASE6_WIKI_PLAN.md` (the correction-note exit criterion), `EMR_IMPORT_PLAN.md` (§3.6
firewall, §6.3/§6.6 cards), `SERVICES.md:218`, `ENTITY_GRAPH_INGEST_V2_PLAN.md` §16 and
§6, `PREDICATE_CANONICALIZATION.md`, `ENTITY_GRAPH_REFOCUS_PLAN.md`, `entity.md`,
`ARCHITECTURE.md`, `ROADMAP.md`, `backend/evals/README.md`, `docs/mocks/agent-ingest/`
and `docs/mocks/silent-queue/`.

Migrations to un-seed or amend, not docs: `0040` (the seeded `resolution.changed`
trigger, whose consolidate pipeline is the only driver of retroactive predicate
consolidation), `0009` (DELETE grants), `0024`, `0118`, `0120`.
