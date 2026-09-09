# Agent-Conversation Ingestion — Build Plan

> **Status:** In progress · **Last verified:** 2026-09-09 · **Waves:** W1✅ W2✅ W3◻️ W4◻️ W5◻️

Owner-ratified 2026-09-08, then revised the same day against six independent cold
reviews (`docs/research/agent-ingest/COLD_REVIEW_FINDINGS.md`). Research behind it: the
eighteen dossiers in `docs/research/agent-ingest/`, consolidated in `SYNTHESIS.md`. The
proposed tool list is `docs/research/agent-ingest/TOOL_SURFACE.md`, whose batch shapes
W2 measured against the live model rather than leaving them guessed.

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
`LoopTurnExecutor`, persists through `AgentTranscript`, records every tool call into the
0191 ledger and binds it to its assistant turn, and settles `settled` / `failed` —
`failed` also for a turn that did not end cleanly, since constraint 6's sweep must never
see a truncated pass. Turn 0 is the note **fenced as DATA** (`framed_note`, the
`intake/turn.py:_RECIPIENT_FRAME` pattern), closing the unframed-body half of risk 1
while the persona still holds no tools. The dispatcher gained the graceful arm in front
of `note_conversations_one_live`, so a re-delivered event is a logged skip rather than an
IntegrityError in a worker. The trigger ships **enabled**: a disabled one would ship W2's
retreat point already retreated from. Its tool registry is empty as well as its
allowlist. Still open for W3: the executor's registry, `reads_knowledge_base`, the
`waiting_on_owner` producer, and moving the recorder into the tool dispatch so `ok` and the
written ids come from the write path.

*Closed against an adversarial review of that task (2026-09-09).* Seven findings, and the
wave's own retreat point was the first of them:

- **The thread was invisible, so the wave did not deliver what it promised.** The
  transcript ROUTE renders it, but the PWA filters every session through
  `useFullBrain.MODE_AGENTS`, and `note_ingest` was in neither tab — the owner paid an
  `agent.turn` per note for a thread only a debug token could reach, which is CLAUDE.md
  #10's escape hatch, not its answer. `note_ingest` now sits on the **Full Brain** tab's
  listing set (it is a conversation about the owner's own notes; every Research agent is
  defined by reading none of them). Listing only, split from a new `NEW_AGENT_OPTIONS`
  that drives the picker and the tab's auto-open: a note thread is always the newest Full
  Brain session, so listing it in the *same* set would have made every captured note take
  the surface. No chip, no inbox tab, no notes-tab redirect — those stay W3 (D4).
- **A conversation stranded in `running` was permanent, and it silently removed its note
  from the pipeline.** Nothing reaped one, and `_has_live_conversation` suppresses every
  later `note_converse` for that note while it stands — so a worker SIGKILLed mid-turn
  (which `Ops -> Update` produces on every deploy: `docker compose stop -t 30 worker`)
  took the note out for good, on a box with no terminal. Now `live_for_note` reclaims a
  stale `running` pass to `failed` before it reads, `queue.claim`'s stale-lock shape —
  fused into the read it would otherwise block, so both gates get it. `waiting_on_owner`
  is never reaped: it holds the owner's question. The horizon is `STALE_CONVERSATION`,
  **derived** as twice `NOTE_TURN_WALL_CLOCK` — which is the second half: the runner had
  no wall clock at all (`_MAX_TURN_WALL_CLOCK_S` is `api/agent.py`'s, around the /chat
  stream), and this is the first handler driving a full ReAct turn from the worker. 30
  minutes, far below /chat's 7500s because this turn has no sub-agent fan.
- **A failure inside `_record` settled the conversation anyway.** `status="done"` and
  `state="settled"` were latched before the persist, and the `except` swallowed its
  raise — leaving `settled` with an empty transcript and an empty ledger. That is not a
  benign hole: W3 hangs `settle_note` off `settled` and reads `touched` from the ledger,
  so an empty one reads as "this note says nothing" and arms a retraction of the note's
  whole non-pinned graph. The persist now happens first and the settle is conditional on
  it; the run records `stop_reason="record_failed"` so the two failure kinds stay apart.
- **The orphan cleanup is gone rather than fixed.** The session row and the
  `note_conversations` row are written in ONE transaction (`AgentSessionRepo.create_on`),
  so a lost race to the one-live index rolls back both. There is no compensating delete
  left to fail silently, and no non-`IntegrityError` path that leaks a session either.
  This matters more now the thread is listed: an orphan would be a permanent empty chat.
- **The DATA frame is closed with a per-turn nonce** — `[CAPTURED NOTE #<rand>] … [END
  CAPTURED NOTE #<rand>]`. `intake/turn.py`'s open-ended prefix is enough for a persona
  that holds no tools in any wave; this one is the seat the graph writes go in, and an
  unterminated frame is impersonable by the text it fences (a body writes its own end
  marker and its own second header, and nothing says which is the system's).
- **`note_ingest` is now genuinely not selectable.** It was in `OWNER_AGENTS`, so
  `POST /sessions {"agent":"note_ingest"}` was accepted — inert behind the empty
  allowlist, and exactly the door W3 must not find open. `ENGINE_ONLY_PERSONAS` splits
  "an owner may pick it" from "the engine may store it"; the two `agent` CHECKs still
  admit it (their RLS suites iterate `STORABLE_OWNER_AGENTS`).
- **The ledger binds by `run_id`**, not by newest-assistant-turn — free, since
  `record_exchange` stamps it, and W3's owner reply is a second run in the same session.
- **The cost is per `note.ingested` EVENT, not per note.** A re-ingest is a second thread
  and a second turn: an attachment landing on an already-ingested note, and every D6
  clarification (`append_clarification` enqueues `ingest_note` itself). A photo captured
  WITH its `attachments_expected` hint pays once — the emit gate defers until OCR is
  done — so the "image notes always cost two" reading is wrong. `graph_rebuild` does not
  amplify at all: `backfill_pending_integration` enqueues `integrate_note` directly and
  emits no event.

*Recorded limit, not fixed here.* `resolve_event` returns a whole-event error on the
first unresolvable trigger and discards the enqueues it had already computed, so a worker
running **pre-0194 code against a ≥0194 database** stops enqueuing `integrate_note` for
every note — 0194's trigger resolves to an action its registry does not have. This is the
one window where D13's "no producer removed before its replacement is merged" is
transiently violated. It is not fixed in this wave because the defect is the dispatcher's
and predates T4 (any event type with two triggers, one unresolvable, loses the other's
enqueue), and separating the E3 resolution error from the E1/E2 authorization errors —
which SHOULD stay whole-event fail-closed — is its own change with its own tests. The
window is narrow: `Ops -> Update` quiesces the worker across `migrate`, so the normal path
never sees it; it needs an image rolled back without its schema. And `integrate_note`
recovers by itself through `backfill_pending_integration`. What does not recover is the
conversation, which in this wave writes nothing.

*Clarification blocks, as built (migration 0193).* The body column is never appended to;
`app.note_clarifications` holds `(note_id, seq, question, answer, session_id, domain_code,
created_at)` and `jbrain.notes.compose.compose_body` joins them onto the body for the four
readers of a note's text — `_note_info` (list/get/PATCH, and so the note view and
`read_note`), the search leg's `body_preview` (`search/repo.py`), the ingest chunk build
(D7), and the integrator's chunkless body fallback. An un-clarified note composes to its
body byte-for-byte, and blocks append *after* the body, so no chunk boundary moves.

**What that offset claim actually guarantees, precisely.** The offsets that exist are
`app.chunks.char_start`/`char_end` (indices into the composed text) and the CHUNK-RELATIVE
spans on `app.entity_mentions` that `analysis/pipeline.py`'s `_locate` derives from them.
`app.facts` has **no span columns**; it cites a chunk by id. So "appending after the body
cannot shift an anchored citation" was never the mechanism protecting a fact — what
threatens a fact's citation is the re-ingest, a different problem with a different fix
(below). Earlier drafts of this section, of `compose.py` and of 0193's header said facts
carry `char_start`/`char_end`. They do not.

Five consequences worth carrying:

- **The editor round trip, and the truncation bug it first shipped with.** The note editor
  loads `NoteInfo.body`, which is composed, and PATCHes the whole string back, so
  `update_note` has to remove the blocks again — otherwise an untouched save bakes them into
  the column and the next read doubles them. The first cut did that by splitting the incoming
  text at the first `"\n\n[clarification "`, which is **ordinary prose**: paste a clarified
  note's displayed text into a new note (or simply type it), let that note get its own first
  clarification, and an untouched re-save silently and permanently deletes every paragraph
  after the pasted line. `strip_clarifications` now RECONSTRUCTS the exact suffix from the
  stored rows and removes only that, and refuses (`ClarificationsAltered` → HTTP 409, nothing
  written) when the text does not end in it. Nothing scans the body for a marker; a note
  cannot be truncated by its own content. This is still what makes "frozen" enforced rather
  than conventional (COLD_REVIEW E's objection to `DESIGN.md:697`).
- **RLS is the note's, not owner-only.** `USING (app.has_domain_scope(domain_code))`, the
  notes/chunks/facts policy — *not* the `is_owner()` posture of `graph_rebuild_runs`/
  `archivist_memory`, which hold metadata and scratchpad. A clarification is the owner's words
  about a health or finance note and the same sentence is already firewalled in `app.chunks`;
  owner-only would let the narrowed session a note conversation runs as (constraint 2) read
  across the firewall. Grants are `SELECT, INSERT, DELETE` plus `UPDATE (domain_code)` alone,
  so the text is immutable in Postgres and the domain still carries on a note move.
- **A trigger, not the policy, ties the block's domain to its note's.** The policy checks only
  the domain the writer NAMES, and the FK to `app.notes` bypasses RLS the way FK checks always
  do — so a *general*-scoped capability token could stamp `general` on a clarification of a
  **health** note, and the owner would then read that text inside the health note's body,
  where D7 makes it a health chunk and health facts. 0193 now carries the 0045 subsection rule
  (`SECURITY DEFINER`, `BEFORE INSERT OR UPDATE`), which also turns the domain carry on a note
  move from a convention into a requirement. Non-negotiable #3: in Postgres, not app code.
- **The re-ingest is no longer a destroy-and-rebuild** (`jbrain.ingest.carryover`). This is
  COLD_REVIEW section E item 6, which W1 did **not** cover: W1 re-anchored `facts.chunk_id` on
  the refresh path, but `wiki_citations.chunk_id` and `entity_mentions.chunk_id` are ON DELETE
  **CASCADE**, so a re-ingest deleted a published revision's citations and every mention of the
  note outright — `link_method='human'` ones included, and the id arrays a review reopen
  replays. A cascaded row cannot be re-anchored afterwards; it is already gone, which is why
  W1's remedy does not transfer. So `ingest_note` now keeps a rebuilt chunk's ROW when it comes
  back byte-identical, and otherwise hands its references to a surviving chunk that covers its
  span AND holds the same characters there (mention spans shift by exactly
  `old.char_start - new.char_start`). An appended block is precisely that case: a long note's
  body chunks are identical, a short note's single paragraph is absorbed by one containing it.
  A genuinely rewritten body, or a note that moved domain, matches neither rule and behaves as
  before. **The one reference it does not carry is `app.resolution_pin`**, whose primary key
  contains `chunk_id` and whose `occurrence_index` is chunk-relative: two old chunks
  re-anchored onto one new chunk would collide on the key, and no span shift can correct an
  occurrence count taken inside different text. Pins survive the identical-chunk case and are
  lost in the absorbed-chunk case, exactly as they were before.
- **Purge sides.** The privacy delete takes the blocks (explicitly — the note delete is soft,
  so 0193's cascade never fires); the rebuild sweep keeps them, or the graph stops re-deriving
  from the notes corpus-wide and silently. `backfill_deleted_note_artifacts` counts them as a
  candidate predicate so the intake-link teardown's soft deletes are swept too.

The append path is `SqlNotesRepo.append_clarification` plus its `NotesRepo` Protocol entry —
no route and no tool in W2; W3's `ask_owner` is the caller. It enqueues its own `ingest_note`
inside its transaction rather than relying on a caller to remember. That enqueue makes the
method **owner-only**: `app.jobs` is `is_owner()` RLS, so it works under the narrowed owner
session constraint 2 describes (tested) and a capability-token caller gets a raw
`ProgrammingError` from the job insert — fail-closed and correct, but a driver error rather
than a refusal, so W3 must not offer this behind a token-authenticated surface.

**Two limits recorded rather than fixed here, both W3's, both because W2 ships no writer:**

- *A block is unframed and its marker is forgeable from inside its own text.* A crafted answer
  — or, once `ask_owner` exists, an agent-authored question over a hostile note body (risk 1:
  `read_note` still returns bodies unframed) — can put a second, fabricated, wrongly-timestamped
  block inside a real one, and a reader cannot tell them apart. There is **no data-loss path**:
  the suffix is reconstructed from the rows, so a forged marker is only characters (tested).
  T4's `framed_note` helps only the note conversation's turn 0, where the composed body arrives
  inside the DATA frame; it does nothing for `read_note`, which is risk 1's remaining half.
  Making a block unforgeable means either an out-of-band marker the editor round trip must also
  survive, or rendering blocks from rows instead of from text — a note-screen change D6 rules
  out for W2, and a decision that belongs with `ask_owner` and the D3 chip.
- *There is no redaction path for a single block.* 0193 grants `DELETE`, so the schema is
  ready, but no repo method and no route reach it — a secret typed into an answer can today be
  removed only by deleting the whole note, losing the body and the graph with it.
  `ingest/emr/intake_handler.py` scrubs `notes.body` for exactly this reason. **The wave that
  ships the writer ships the eraser**: on a box with no terminal (CLAUDE.md #10) an
  unredactable field is not a limit the owner can work around.

*The block's line structure renders where it is read, and only there.* `Stream.tsx` renders the
body as a text child inside a 3-line clamp with no `white-space`, so the bubble preview shows a
block as one run-on line — as it does every multi-line note, and always has. The note SCREEN
runs the body through the assistant Markdown renderer (`NoteScreen.tsx` → `agent/markdown.tsx`),
which makes the blank line a paragraph and the soft newlines `<br>`s, so a block renders as
designed. `white-space: pre-wrap` on `.note-body` would change how every note in the stream
renders — a design change, and D6 says the note screen does not change — so nothing was changed
there. Plain text stays the right block format because it is the only one correct on both.

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

*Two things W2's reviews left specifically for W3, both about flipping
`reads_knowledge_base` to True to satisfy constraint 2.* First, **the flip alone widens
nothing retroactively**: `read_scopes` is also what is persisted as the session row's
`domain_scopes`, so every W2-era note session keeps `[]` forever and a reply turn into an
old thread would read nothing. Decide backfill-or-accept deliberately rather than
discovering it. Second, **`POST /sessions/{id}/scope` is ungated on persona**. The
engine-only split closes session *creation*, but `rescope_session` will happily rewrite an
engine-opened note conversation's scopes — so the moment the flag flips, the owner-facing
route can widen the graph-write persona past `(note_domain, 'general')` on a session the
owner never started. Inert today only because a False profile's stored scopes are never
read.

*Landed (T2a): the two tools that write the graph.* `agent/graphwritetools.py` +
`resolve_entity.tool` / `assert_fact.tool`, batched (≤12 / ≤8) on the measured shapes —
the flat fallback is not held open, since it is equally well-formed and yields exactly
one item per turn. Both write through W1's `commit_facts`: the resolver, the mention
spine, `decide()`, the floor, the ratchet and the citation anchor are the shipped ones,
so the tools add no second write path and `decide()` never becomes a verb. `quote` is
required and CHECKED — an unattested quote still commits (Lever A) at the arbiter's own
0.4 inferred-overwrite ceiling, so it can never silently rewrite a stated value. No
`domain`/`inferred`/`supersedes`/`correction` field and no `enum` anywhere. Per-element
SAVEPOINT. Handles (`e1`…) are per CONVERSATION, held in the writer; `assert_fact`
accepts a handle or the exact surface that earned one and nothing else, so
`resolve_entity` stays the only minting path. Budgets are engine-side per conversation
(8 resolve / 10 assert calls) with the remainder appended to every result.

Three things that answer questions the plan had left open:

- **D18's "the agent chooses the domain" is the agent choosing the PREDICATE**, and
  nothing else — there is no field, and the conversation is scoped so nothing can land
  outside `(note_domain, 'general')` or below the note's own domain. What that leans on
  is `domain_floor`, whose table is keyed on the camelCase spellings the `note.extract`
  prompt teaches, while a tool-writing model emits `blood_pressure`. It now matches
  separator- and case-insensitively with a dotted-base fallback, so a clinical fact
  cannot land in `general` because of a separator. Both rules only ever add a floor.
- **The `fact_ids` ledger is filled**, through a `facts` chip on the tool result
  (`contracts.FactWriteRef` → `ToolOutput` → `ToolResultEvent` → the transcript step →
  `ledger_rows`), so what is recorded is what the write path REPORTED, never what the
  model asked for. The sibling wiring `settle_note` can now read `touched` from
  `NoteConversationRepo.writes()`. **`mention_ids` still has no channel** — the ledger
  has no column for them and migrations are not this task's — so a `settle_note` call
  must NOT pass an empty `mention_ids` set: `_reconcile_mentions` would delete every
  mention of the note (constraint 7's failure, exactly).
- **The rescope gap is closed** in `AgentSessionRepo.set_scopes` (refused for
  `ENGINE_ONLY_PERSONAS`, 409 at the route), so it holds for every caller rather than
  one route. On the retroactive half: **accepted, not backfilled** — a W2-era thread
  wrote no graph, and `converse.note_read_scopes` recomputes a turn's scopes FROM THE
  NOTE rather than reading the stored row, so a backfill would only make a stale row
  look authoritative.

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
