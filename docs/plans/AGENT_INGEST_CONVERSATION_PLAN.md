# Agent-Conversation Ingestion — Build Plan

> **Status:** In progress · **Last verified:** 2026-09-11 · **Waves:** W1✅ W2✅ W3◐ W4◐ W5❌superseded
>
> **W5 IS SUPERSEDED by `AGENT_INGEST_REWRITE.md`.** The owner redirected the work to a
> complete rewrite of ingestion with a wipe of the notes, the graph and the predicate
> registry, so the teardown W5a/W5b/W5c describe — and the gate they were blocked on —
> no longer applies. Void with them: D13 read as a BLOCKER on this wave, the "what W5 may
> not delete" list below and in each xfailed scenario's `xfail` string, and matching the
> old extractor's output field-for-field. Everything above W5 is shipped and stands; the
> successor builds on it. The measurements in W3/T5 — `required` buys PRESENCE, not
> MEMBERSHIP, and the six-gap probe results — are NOT superseded: they describe the box,
> not the old path, and the successor reasons from them.
>
> **W4c/1 landed:** the tool-call ledger now records BOTH turn paths. The owner's reply
> turn records at the same seam the unattended pass records at
> (`clarify.record_reply_writes`, called from `api/agent.py` before `close_owner_reply`) —
> not in the shared tool dispatch, which serves every agent and would need a
> note-conversation hook in every tool. `ConversationWrites.facts` is therefore the
> whole-conversation union constraint 6 needs.
>
> **W4c/3's second wave (S2) landed; W4c/2 is CLOSED AS UNBUILDABLE.** `settle_note` is
> split into `sweep_note` / `settle_tail` / `stamp_analysis`, and the conversation now
> runs the settle's TAIL at the end of a clean pass, from both turn paths
> (`analysis/clarify.settle_conversation`) — so its facts finally project and reproject.
> It runs NO sweep. One was built (S3) and removed on a proof: within a session this
> producer's ledger never shrinks, so a release could only ever reach other sessions'
> claims, and judging those needs a complete current READING of the note, which a record
> of what a pass WROTE is not — its silence is the designed output. A sound conversation
> sweep is empty and a non-empty one is unsound; SETTLE_OWNERSHIP.md's S3 section carries
> the argument and the four demonstrated failures. **W5a stays blocked** on the stamp and
> the `integration_state` flip (preconditions 3 and 4), and the day it lands, the
> conversation's replacement for the analyzer's sweep is an EXTRACTION, not a ledger.
>
> **W4c/3 is decided and its first wave has landed.** The settle's owner is per PRODUCER,
> not per note: `settle_note`'s sweep and `_reconcile_mentions` are scoped by a stamped
> claim set, `settle_owners` (`analysis/settle_owner.py`). Each producer releases only
> its OWN claim on the rows it no longer asserts, and a row is retracted (a mention,
> deleted) only when its last claim goes — so `integrate_note` cannot reach what the
> conversation asserts, and a fact BOTH of them assert survives either one letting go,
> which is the common case with two producers on one note. That closed SHIPPED loss, not a prospective race — `integrate_note`
> and `note_converse` fan out of one `note.ingested` event, so the analyzer's settle had
> been retracting the conversation's facts (and deleting its mention spine) on every
> settle of the note, the owner's reply-turn writes included. The decision and the
> remaining waves are `docs/plans/SETTLE_OWNERSHIP.md`. **W4c/2 (wire the conversation's
> own sweep) was built as S3 and dropped — see the note above — and W5a is still blocked,
> now on the stamp and the state flip alone.**
>
> W4's two halves have both landed and are merged. INTAKE (D10): the third tool set, and
> the finding that the port itself had already happened by accident in W2. EMR (D9): the
> `apply_intent` split, the multi-source settle fix, and a conversation holding no
> graph-write verb. They compose — `narrow_for_third_party_note` intersects where
> `narrow_for_emr` subtracts — and a note that is both gets the intersection. What W4
> still owes is in its section: publishing the deterministic import into the thread, and
> the older `integrate_note` / `emr_parse` settle race neither half owns. Both halves were
> then closed against an independent adversarial review of the merged wave — its blocker
> was that the `apply_intent` split was NOT behaviour-preserving and could leave a held
> fact with no review card, on every ordinary note; see the seam bullet in W4.
>
> W3 in flight. **T5** landed the six-gap decision the harness re-point exposed:
> `assert_fact` is v3 (`when_end` and a numeric `confidence`), three gaps are accepted with
> the measurement and a "W5 must not delete" line written into each xfailed scenario, and
> the corpus is 52 passing / 23 strict-xfail of 75. The finding that decided four of the
> six — `required` buys presence, not membership — is now `TOOL_SURFACE.md` correction 8.
> Landed before it: **T3** — the unattended/on-reply split (D8) and the verbs
> behind it, `correct_fact` (D11) and `merge_entities` (staged only, constraint 12) —
> plus the fixes from two adversarial reviews: on the write surface the weight cap now
> lands on the field `decide()` reads, `replaces` is gone, an id `object` becomes a real
> edge, and the reply turn can reach `assert_fact`; and **T4** — the scenario harness
> re-pointed onto the write tools, then corrected until it stopped passing on assertions
> that could not fail (49 passed / 26 xfailed). Two things T4 was briefed to do are
> **not** done and are recorded as open: the eval corpora and the real-model adversarial
> scenario.

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
  **derived** from the turn caps — which is the second half: the runner had
  no wall clock at all (`_MAX_TURN_WALL_CLOCK_S` is `api/agent.py`'s, around the /chat
  stream), and this is the first handler driving a full ReAct turn from the worker. 30
  minutes, far below /chat's 7500s because this turn has no sub-agent fan.
  ⟲ **"Twice `NOTE_TURN_WALL_CLOCK`" was the derivation, and it was wrong about which
  turns sit in `running`** (`AGENT_INGEST_REWRITE.md` R3's third review). The owner's
  REPLY turn sits there too — `claim_waiting` puts it there — and it runs under /chat's
  cap, more than twice that horizon, so a long reply was reclaimed while still writing and
  R3's reconciler then enqueued a rival pass whose sweep retracted what it had committed.
  The horizon is twice the LONGER of the two caps now, and the /chat number has one
  spelling (`models/agent.TURN_WALL_CLOCK`) for both readers.
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
no route and no tool in W2; W3/T2b's owner-reply path (`analysis/clarify.py`) is the caller, pairing the answer with the question `ask_owner` recorded. It enqueues its own `ingest_note`
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
  unredactable field is not a limit the owner can work around. *Closed in W3/T2b, with the
  writer: `list_clarifications` + `delete_clarification` behind `GET`/`DELETE
  /notes/{id}/clarifications[/{id}]`, the delete re-driving ingestion in its own
  transaction. The listing is half the fix — the note screen renders blocks as text, so
  without ids there is nothing to name.*

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

*Landed (T4): the harness describes the tool path.* `runner._compile_intent` is gone;
`runner._tool_calls` compiles each step's scripted extraction into the `resolve_entity` /
`assert_fact` arguments a faithful agent would send, and `NoteGraphWriter` executes them
against real Postgres — one function, not 75 files, exactly as briefed. A `tool_calls` key
on a step overrides it where the faithful default cannot express the case (one scenario
uses it). `settle_note` runs ONCE per note over the union of every call's writes
(constraint 6), unioned in-process by a `_LedgerPipeline` subclass until the 0191 ledger
is the source.

*Corrected after an independent adversarial review of that re-point.* The re-point's own
commit reported "54 real passes, 21 documented gaps"; the review found four of those
passes vacuous, two xfail reasons mis-attributed, and ~20 `expect` assertions weakened
rather than deleted. All of it is now either restored, xfailed or written down —
`backend/tests/harness/README.md` carries the six-gap table, the 26-scenario xfail index
and a table of every changed-but-not-xfailed assertion. The corpus is **49 passing / 26
strict-xfail of 75**, and the five conversions were all greens that could not fail:
`adv_negation_then_reassert` (its zombie guard needs a negated row the tool cannot write),
`hist_retrospective_closes_open_interval` and `plan_relative_date_resolution` (assertions
retargeted onto the scenario's own statement text), `rel_enumerated_children_fan_out` (the
arbiter's four derived `gender` facts silently absent), `own_joint_co_ownership` (the
`{share: joint}` value silently absent). Two production bugs the re-point fixed but did
not pin — the `_QUANTITY` unit class and `_KIND_HINTS["drug"]` — now have regression
tests, along with four more mis-parses of the same shape (`1/2`, `120/80`, `03/19/1986`,
`1e3`, and leading zeros) and the thirteen registry types no hint word could reach,
`Observation` among them — the only default route to `kind: measurement`.

**Still open from T4, both briefed above and neither done:**

- **The eval corpora are untouched.** `src/jbrain/evals/integrate_runner.py` and
  `src/jbrain/evals/integrate_cases/00_core.json` still score the `integrate.note` prompt
  and the `IntegrationIntent` shape, and `tests/unit/test_integrate_eval.py` still runs
  them in CI. They are green, and they measure a path the note conversation no longer
  takes. (The plan's path `evals/integrate_runner.py` is `src/jbrain/evals/`.)
- **The new adversarial scenario was not written.** `adv_prompt_injection_body_inert.json`
  is still the tautology this paragraph names, and it is now *more* of one: the write path
  is a tool loop, which is a shape hostile note text could plausibly drive, and no scenario
  exercises the loop at all.

*Scope reduction recorded rather than discovered.* `docs/research/agent-ingest/
X5-EVALS-TESTING.md` designed this re-point as an N-turn `FakeLlmClient` router replaying
scripted tool calls through the real `AgentLoop`, promising that `expect{}` "survives
verbatim". The implementation calls the two handlers directly instead. Neither half held:
`expect{}` did not survive verbatim (see the README's two tables), and the loop itself —
`max_steps`, the per-conversation budgets, the tool sidecars' schemas and the harmony
grammar they compile to — is now **outside** the harness. The 75 scenarios are
behaviour-preservation evidence for the WRITE PATH only. W5a's ~940-line deletion rests on
that narrower claim.

*Landed (T1): `owner_prefs`.* `app.owner_prefs` (migration 0195, owner-only RLS, ENABLE
+ FORCE) holds one capped document of standing instructions, injected into every note
conversation's **system prompt** ahead of the note and framed as the owner's
instructions rather than as the note's DATA. `prefs_read` is the `read`-class load;
`prefs_write` **stages an `owner-prefs` Proposal and never writes** (D17) — the only
writer is `prefstools.owner_prefs_executor`, dispatched by
`connectortools.build_leaf_executor` on the owner's approval. It is a **delta**: one
call moves one numbered rule (add / replace / remove), addressed by `rule_number` plus
the rule's own text, with no full-rewrite verb in the surface at all — a full-replace
verb reachable from a note-driven turn is a standing-instruction overwrite primitive,
which is why the archivist's upsert shape was copied for the read half only. A rule is
one LINE, numbered at render time, so no edit renumbers its neighbours; a
replace/remove staged against one numbering refuses at enact if the rule it named has
changed underneath it. Caps are 50 rules / 1k chars per rule / 8k chars of document
(well under `archivist_memory`'s 20k, because this one is paid on every note turn), and
every cap is checked on an in-memory rule list **before any write is opened** — at
staging it is text the model can act on, and at enact it is a skipped leaf, never a
raise that would roll back the sibling leaves of the same enact transaction.

**Still open from T1** (revised once T2a and T2b landed beside it): both tools are in
`toolregistry.NEVER_DEFAULT` and in **no profile's allowlist**, so neither is reachable.
`prefs_read` stays out for good — **`TOOL_SURFACE.md` Cut #1 is taken**: the document is
already in the system prompt (`converse._rules`), so the tool would offer the persona a
second, overlapping memory surface for something it has been handed. It was built as
briefed and is left in place, unreachable, rather than deleted. `prefs_write` still waits
on the ON-REPLY set, which no task in this wave built. The `reads_knowledge_base` blocker
it had is **gone** — T2a flipped the flag to True, so `app.proposals`' domain-narrowed
RLS is satisfiable and the handler's text refusal under empty scopes (tested) is now the
guard for a W2-era session rather than for every turn. Neither tool has a D3 chip beyond
the minimal `toolSummary.ts` entry, and the "how many existing notes would this change"
report the `owner_prefs` section promises is not built — it needs the scoped per-rule
re-run.

*Landed (the GUI half of W3).* The **D3 "entity modified" rung** and the **two-tab
inbox** (D4/D5), both as extensions of shipped components rather than new surfaces.

- The rung is one more rung inside `StepRow`'s existing detail. Writes ride the tool
  result as `ToolResultEvent.facts` (`FactWrite`: statement, per-fact `domain`, a
  `written|replaced|held` status the WRITE PATH reports, `from_attachment` for D12) plus
  a call-level `truncated`, folded by `transcript.ts` and persisted by `fromTurn`. The
  step's seven states and their wording are pure (`agent/entityWrites.ts`); a
  supersession renders through `ClaimDiffView`, extracted from `ClaimDiff.tsx` so the
  review inbox and the transcript share the app's one diff renderer. The domain is
  **named** everywhere it is shown, through a single helper, and an unrecognised code
  says so instead of degrading to a bare dot. No edit affordance: correction is a reply.
- **The rung renders from the PERSISTED TURN, not from the 0191 ledger** — deliberately.
  The ledger stores `fact_ids` with no per-fact status and no statement, so it can say
  *that* a call wrote and *which domains* it touched and never which of the seven states
  a write reached; and it has no `tool_call_id`, so joining it to a step is positional
  and breaks exactly on the truncated turn that motivates it. The turn already survives
  the event stream (it is how every other step replays), so it is one shape with two
  arrival paths rather than two sources disagreeing on one screen. **The gap this leaves,
  named rather than papered over:** a turn cut off by `max_steps` writes no assistant
  turn, so on reopen its calls exist ONLY in the ledger and render nowhere. Closing that
  is the recorder moving into the tool dispatch plus persisting a truncated prefix —
  W3's backend half, where `ok` and the written ids come from the write path anyway.
- The inbox is `notes · wiki` on the shipped `.review-segs` track. `GET /api/review/notes`
  (owner-only) unions `note_conversations` in either live state with the staged Proposals
  a note conversation raised (or of an instructions kind), oldest wait first; a `running`
  first pass is listed and uncounted. **"The inbox only redirects" is a property of the
  CONTRACT**: the row carries a `session_id` and an `agent` and no item id, and there is
  no endpoint on this surface to post a decision to — a test asserts both, so growing an
  answer affordance means deleting a test. A row hands off exactly as a Tasks run does.
- The launcher's Review tile badge — the only signal, polled only while the launcher is
  on screen — now sums both tabs (mock fidelity item 10); the notes half degrades to the
  wiki count alone if its endpoint fails.

*Closed against an independent adversarial review of W3 (2026-09-09).* Ten findings,
every one reproduced by the reviewer against the branch, and none of them visible to CI —
which is the pattern worth carrying more than any single fix: **every one of the three
worst lived where two halves were each tested alone.**

- **A server-authored message was filed as Jeff's answer onto his note.** `/chat` called
  `record_owner_reply` with `body.message` and no check on `proposal_outcome` /
  `deferred_outcome`, the two flags that mark a turn whose message the SERVER wrote. Tap
  **Enact** on an inline card in a thread that ended with a second `ask_owner`, and the
  outcome summary was paired with the agent's open question, appended as a D6 block,
  re-ingested into chunks and embeddings, and the question was consumed — so the real
  answer could never be paired. `record_owner_reply` takes `owner_authored` now, checked
  inside as well as at the call site.
- **The D3 rung was wired to a payload the backend never sent.** `FactWriteRef` emitted
  `{fact_id, label, domain, outcome}`; the rung read `status`, `predicate`, `qualifier`,
  `value`, `replaced`, `from_attachment`, plus a `truncated` that existed nowhere at all.
  Fed the real JSON, `tallyWrites` fell through `else tally.written += 1` and rendered a
  HELD fact — one `decide()` refused to make live — as **written**, which is the single
  failure `ask_owner.tool` and the persona prompt both exist to surface. Four of D3's
  seven states were unreachable, `ClaimDiffView` never rendered, and **D12 had no
  producer**. Fixed end to end: `status` is derived server-side by one table and is a
  REQUIRED field (a default of "written" is a silent claim a fact is live); an unmapped
  outcome word reads as `held`, because the two errors are not symmetric; D12's evidence
  is the provenance of the chunk the quote is attested against; `truncated` comes from the
  clamp the tool already reported to the model. `testdata/fact_write_contract.json` is now
  the shared artefact — real `model_dump(mode="json")` output, asserted by the backend and
  folded through `applyEvent` by the frontend, because every green test on both sides had
  been building its own input.
- **`ToolOutput.halt` was honoured on two of three dispatch loops.** `_produce_buffered`
  never read it, and `/chat` picks that producer whenever reflexion buffer-retry is on —
  so after `ask_owner` flipped the thread to `waiting_on_owner` the loop ran on for up to
  19 more steps of `correct_fact`, `merge_entities` and `prefs_write`. It halts now, a
  halted turn is never re-produced (that would ask the owner twice), and buffer-retry is
  forced off for a note conversation as it already was for a spawner — a re-produce
  re-dispatches every write whatever the stop reason.
- **The eraser shipped with no PWA affordance.** `grep -rn "clarification" frontend/src`
  returned nothing: the routes existed and passed 20 tests, but no browser could issue the
  DELETE and `DEBUG_ACCESS.md` exposes no generic HTTP verb. W2's "the wave that ships the
  writer ships the eraser" was not met. The note screen's Note tab now carries a collapsed
  **"Answers you gave"** panel (DESIGN.md "Note view"), absent for a note with no blocks.
- **Both clarification routes were `PrincipalDep`.** W2's recorded limit said in so many
  words that W3 must not offer this behind a token surface. `OwnerDep` now — and there
  were no HTTP-level tests for either route, which is how it shipped.
- **An enact the executor refused was reported as enacted.** `enact` marked every
  `plan.enactable` leaf `enacted` regardless, and `owner_prefs_executor` returned silently
  on a stale `prev`. A refusal is `LeafRefused` now, caught per leaf into `held`; the
  stated reason for swallowing it ("a raise would roll back the sibling leaves") never
  applied, since `prefs_write` stages exactly one leaf per proposal.
- **`INSTRUCTION_PROPOSAL_KINDS` held `owner_prefs`; the kind is `owner-prefs`.** The kind
  arm of the union was dead code, and the test that declined to cover it did so on a
  premise 0195 had already retired.
- **`prefs_write` was reachable only on the turn where the document was invisible.**
  `with_standing_instructions` had one call site, the unattended pass; `/chat` passed
  `profile.prompt` raw. So D15 and `ASSISTANT.md` were both false, and the model was asked
  to edit a numbered list it had never seen. `api/agent._standing_instructions` is the
  other half, and fails the turn rather than running without them — the direction
  `converse._rules` already chose, and this is the turn that force-supersedes and pins.
- **The notes-tab row identified a domain by colour alone** — a `DomainDot` whose `title`
  does not exist on touch. `domainWord` was one import away.
- **Comments and docs claiming behaviour the code did not have**, corrected in place; and
  `prefstools`' "every failure is TEXT" is now enforced by wrapping both handlers rather
  than asserted, following `asktools._guarded`.

*Found while fixing, not in the review.* `test_note_reply_write_pg.py` asked for
`owner_ctx` by parameter, but that name is a plain helper and not a fixture — so all
**eight** of its `correct_fact` / `merge_entities` tests errored at setup and had never
once executed. With the one-line wrapper `test_ask_owner_pg.py` already uses they run; the
one that then failed was cross-test pollution (the only test addressing by NAME, resolving
onto an entity a sibling test had already given a `homeLocation`), not a defect.

*Left open, deliberately.* `ASSISTANT.md` says the reply turn holds twelve tools, which is
what `NOTE_INGEST_ON_REPLY_TOOLS` contains; the reviewer counted ten because the built
registry does not bind `assert_fact` on that turn. That is a sibling task's fix, and the
doc is right about the allowlist, so the number is left standing rather than corrected to
match a bug.

- Two shipped bugs fixed on the way: an empty lane rendered a `0` count pill (D5's "no
  zero to clear"), and the session handoff mapped every non-`curator` persona to the
  Research tab, so a `note_ingest` redirect would have landed on the wrong tab and shown
  an empty chat. `modeForAgent` now derives it from `MODE_AGENTS`.
- `toolSummary.ts` gained labels and inline-arg policies for the five W3 tools, and
  `inlinePiece` now renders a BATCHED argument elementwise (the whole W3 tool surface
  batches). Their `.tool` sidecars land on sibling branches, so the roster gate carries a
  named, **self-clearing** `_FORWARD` set: the moment a sidecar lands, a test says to
  delete the name from it.
- Found wrong in the chosen mock, and recorded in `docs/mocks/agent-ingest-inbox/`:
  variant A relabels `pending · decided` to `notes · wiki` without saying what becomes of
  the **decided log and its `reopen`** — a shipped, `DESIGN.md`-binding full unwind. Built
  with the log one level down inside the wiki tab rather than silently deleted.

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
0.4 inferred-overwrite ceiling, so it cannot overwrite a confident prior. No
`domain`/`inferred`/`supersedes`/`correction` field and no `enum` anywhere. Per-element
SAVEPOINT. Handles (`e1`…) are per CONVERSATION, held in the writer; `assert_fact`
accepts a handle or the exact surface that earned one and nothing else, so
`resolve_entity` stays the only minting path. Budgets are engine-side per conversation
(8 resolve / 10 assert calls) with the remainder appended to every result.

*Corrected after an adversarial review of the graph-write surface — five findings, all
reproduced before they were fixed. Recorded here because four of them were things this
plan and `TOOL_SURFACE.md` both asserted and no code enforced:*

- **The 0.4 cap was stored on the field nothing reads.** `decide()`'s low-confidence
  guard keys on `self_confidence`; `graphwritetools` capped `confidence` and wrote a bare
  `1.0` into `self_confidence`, so an unattested `assert_fact` went `active` and
  superseded an attested prior while the result line told the model it could not overwrite
  anything. The tool surface has no confidence field to report (R3), so the engine's own
  span check IS the self-report and now sits on both. The claim is also stated correctly
  now: an unattested value cannot overwrite a CONFIDENT prior — `attribute` reaches
  `attribute_collision` first and holds both sides; `state`/functional-relationship reach
  the weight guard and the head stays live.
- **The `quote`-omission argument for `correct_fact` was a misreading of the same
  guard.** `decide()`'s correction branch reads neither confidence field — it
  force-supersedes on the flag alone — so a capped correction would still overwrite and
  would merely file the owner's own word as a 0.4 guess. The conclusion (no `quote`)
  stands; the reason is that its attestation is WHO SPOKE. See TOOL_SURFACE correction 5,
  amended.
- **`correct_fact`'s `replaces` is removed**, and with it the multi-row retry. The
  affordance could not work: `entity_view` yields several groups at one key only for a
  NON-functional relationship, which is exactly the shape `decide()`'s correction branch
  skips (it needs a `single_head` address), and a set-valued edge's identity IS its object
  (`_facts_at_key`). The retry left both original edges live, added a third, and reported
  `ok … replaced`. The handler now lists what is live and refuses.
- **An `object` that is an entity id is resolved and adopted** before the write, under the
  turn's own read scopes. Passed through raw it was matched against a handle table holding
  only the subject, so it never resolved: the row landed `object_entity_id = NULL` with the
  bare uuid as its literal value and `pinned=True`, the real edge superseded. The shared
  write path also refuses any id-shaped object no handle answers to, so `assert_fact`
  cannot reach the same state.
- **`resolve_entity`/`assert_fact` are now BOUND on the chat registry too.** D8's on-reply
  set has always allowlisted them, but `build_registry` dropped both sidecars
  unconditionally — so on a reply turn neither was offered and neither could dispatch, and
  `correct_fact` was the only write verb left. A correction at an empty address commits
  `insert_pinned=True`, so every fact the owner taught a note thread was pinned against
  future supersession, including by later notes. They bind the way `ask_owner` does
  (through the conversation row, never an argument); `NEVER_DEFAULT` plus the allowlist is
  what keeps them closed, which is what was doing the real work anyway.
- **`CORRECT_CALL_BUDGET` was inert**, because the handler built a fresh `NoteGraphWriter`
  per call and re-created `ToolCallBudget(6)` with it. One writer per conversation now
  serves all four verbs, so the budget counts down and the handle table survives the turn.
  The docstring claim that handles span the unattended pass and the reply was never true
  and is gone: those are two processes.

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
  model asked for. **As of W3, for the UNATTENDED pass only** — `record_tool_call` had two
  callers, the worker's pass and `ask_owner`'s self-record, and the owner's reply was an
  ordinary `/chat` turn that touched the repo nowhere, so a `resolve_entity` /
  `assert_fact` / `correct_fact` on a reply reached the D3 rung and never the ledger,
  while `clarify.close_owner_reply` still mapped that turn to `settled`. That was the
  precondition beside constraint 6, and it is **LANDED in W4c/1**: the reply turn records
  at the same seam the pass records at (`clarify.record_reply_writes`, called from
  `api/agent.py` before `close_owner_reply`), not down in the shared tool dispatch — the
  dispatch serves every agent and would need a note-conversation hook threaded through
  every tool, where the turn seam keeps the two paths symmetric. `writes().facts` is now
  the whole-conversation union, and the settle's TAIL reads it (S2). No sweep reads it:
  W4c/2 built one and it was removed (SETTLE_OWNERSHIP.md S3). **`mention_ids` still has no
  channel** — the ledger has no column for them and migrations are not this task's — so
  the conversation reconciles no mentions at all, which is the same
  conclusion the missing channel forced: an empty `mention_ids` set would delete every
  mention of the note (constraint 7's failure, exactly). The cost is a `conversation`
  mention claim nothing releases, bounded to one chunk generation because
  `entity_mentions.chunk_id` is `ON DELETE CASCADE`.
- **The rescope gap is closed** in `AgentSessionRepo.set_scopes` (refused for
  `ENGINE_ONLY_PERSONAS`, 409 at the route), so it holds for every caller rather than
  one route. On the retroactive half: **accepted, not backfilled** — a W2-era thread
  wrote no graph, and `converse.note_read_scopes` recomputes a turn's scopes FROM THE
  NOTE rather than reading the stored row, so a backfill would only make a stale row
  look authoritative.

*Landed (T2b): `ask_owner`, and the owner-reply path W2 built the storage for.* The
question and the `waiting_on_owner` state land in ONE transaction, written by the
HANDLER — the first piece of the recorder move W2 left open, and not an optimisation: the
owner can reply before the runner's post-turn `_record` runs, and the reply path reads
that ledger row to know what the answer answers, so a question recorded later is a
question nothing can pair. `converse.ledger_rows` skips what the handler already wrote.
**"And stop" is the LOOP's**, not the prompt's: the handler returns `ToolOutput(halt=…)`
and `AgentLoop` finishes the turn on `stop_reason="awaiting_owner"` without another model
call — the bare twin of the `deferred` contract, honoured on both `run_stream` and `run`
so it is a property of the loop rather than of which entry point a caller picked. That
stop reason is the only producer of `waiting_on_owner` (`models.note_conversation.
state_for_stop`, which now owns the whole ending→state mapping), so constraint 6 holds by
construction: `settled` is reachable only from a clean `end_turn`, and a pass that stopped
to ask cannot claim it. A second ask while one is open is refused rather than recorded —
two open questions would leave the reply path guessing which one the owner's message
answers, and that answer becomes source text on the note.

The owner's reply is filed by the ENGINE, in `analysis/clarify.py` off `/chat` (D6's
block is deliberately not a tool): paired with the recorded question, appended as a
timestamped clarification block, which enqueues its own re-ingest, and the thread returns
to `running` — then closes in the turn's `finally` by the same `state_for_stop`, because
`running` holds the note's one live slot. The state moves BEFORE the append, so the
failure mode is a lost block (recoverable: the answer is still in the thread) rather than
a note that collects the same answer twice as source text.

**`note_body_sha` has its reader**, and it is this path. Compared before the append, it
answers "has anything OTHER than this conversation changed the note?", and it is
re-stamped only when it matched — the thread has seen every character of the new composed
text, since it asked the question and read the answer. On a mismatch the block is still
appended (the owner answered what was asked) and the stale sha is left standing, which is
the true statement: this thread has not read the note as it now stands.

**The eraser ships with the writer**, as W2's recorded limit demands: `GET` and `DELETE
/notes/{id}/clarifications[/{id}]` over two new repo methods. An answer is free text the
owner typed — a password, a diagnosis, a name — and appending makes it the note's own
chunked, embedded, searchable text; the listing exists because the note screen renders
blocks as text, so a block's id was otherwise unreachable. The delete re-drives ingestion
in its own transaction, like the append: a redaction whose old chunk stayed in the index
is not one.

*Recorded limit, not fixed here.* Whether the answer's re-ingest opens a SECOND thread is
a race: its `note.ingested` is suppressed while the reply turn holds the note `running`
and opens a fresh pass if it arrives after the turn closed. Both outcomes are safe today
(the shipped `integrate_note` re-derives the graph either way, and the sweep is not wired
yet), and both were already in W2's cost model, but the wave that hangs `settle_note` off
`settled` has to make it deliberate rather than timing-dependent.

*Reconciled when T2a and T2b merged.* The three tasks widened the same allowlist, the
same `NEVER_DEFAULT` and the same persona prompt in parallel; the merged result is the
UNION, not a choice. `NOTE_INGEST_TOOLS` is the six names — `resolve_entity`,
`assert_fact`, `ask_owner`, `find_entity`, `read_entity`, `current_time` — and
`NEVER_DEFAULT` carries all five write verbs the plan has shipped (`prefs_read`,
`prefs_write`, `resolve_entity`, `assert_fact`, `ask_owner`). The persona prompt is one
v2 describing all six and no others, re-pinned. `ask_owner` joins the per-note registry
`converse` builds (it is not note-bound — it finds its conversation through
`ToolContext.agent_session_id` — so one handler serves both it and the chat registry the
reply turn uses), which leaves the `NoteConverseRunner.executor` fallback the inert empty
registry T2a made it. Two by-name registry builders arrived, one per task;
`graphwritetools.note_registry` is the survivor, since it also checks that a sidecar
declares the name it was loaded for.

*Landed (T3): the ON-REPLY set, and the split itself.* `NOTE_INGEST_TOOLS` is now two
frozensets — `NOTE_INGEST_UNATTENDED_TOOLS` (the six) and `NOTE_INGEST_ON_REPLY_TOOLS`
(those six plus `correct_fact`, `merge_entities`, `prefs_write`, `search`, `read_note`,
`relate`) — with `agents.agent_for_owner_reply` as the seam and `/chat` its only caller.
`prefs_write` is reachable at last, `prefs_read` stays out for good (TOOL_SURFACE Cut #1),
and both new verbs joined `NEVER_DEFAULT`.

**Where the choice had to live, and why it is not where R2 implies.** The unattended pass
and the reply turn are different code — `converse.py`'s per-note executor in the worker,
`chat()` in the API — but they are gated by the SAME field: `LoopTurnExecutor.run_turn`
passes `profile.tools` as `tools_allow` exactly as `/chat` does. So the profile cannot
quietly mean "unattended", and a flag on one set was never the alternative; the split is a
second RESOLUTION FUNCTION. The asymmetry is deliberate: `AgentProfile.tools` holds the
NARROW set, so every caller that has not heard of the split (the worker, the task runner,
the session listing) narrows a turn, and only an explicit `agent_for_owner_reply` widens
one. Both directions are pinned at the dispatch gate rather than on the dataclass — over
every shipped sidecar, at every scope, the unattended profile admits exactly the six and
the widened one exactly the twelve — and the worker's own registry is asserted to hold no
on-reply handler at all, so the two locks fail independently.

**`correct_fact` addresses by identity key** `(entity, predicate, qualifier)`, resolved
under the TURN's read scopes — which is the firewall, since the write session is the
owner's full scope (constraint 2) and cannot be one. An entity the conversation cannot
read is an entity it cannot correct. On a key holding several live rows (a set-valued
relationship: each distinct object is a co-equal current edge) the handler lists what is
live and REFUSES: it shipped with an `f1`/`f2` retry under `replaces`, and the review
above is why that is gone — those keys are exactly the ones `decide()`'s correction branch
skips, and a set-valued edge's identity IS its object, so the retry could only ever add a
third edge while reporting a replacement. An `object` that is an entity id is resolved and
adopted under the turn's own scopes before the write, so it becomes an EDGE rather than a
uuid stored as a literal value. The write is `_assert_one` with `correction=True` — one
flag, feeding `decide()`'s existing force-supersede-and-pin branch, so there is no second
write path and `decide()` stays off the model's vocabulary. It carries **no `quote`**: the
owner's words are not in the note's chunks when the tool runs, so a required quote could
only ever fail its own check and would file the owner's own word at the inferred ceiling.
Its attestation is WHO SPOKE. It would not change what the write DOES — `decide()`'s
correction branch reads neither confidence field.

**The fold is staged, and staging is the only shape available** (constraint 12). It raises
the same `merge_entities` node op `propose_merge` does, so the owner's approval runs the
shipped `SqlAnalysisRepo.merge_entities` — tombstone check, `distinct_from` check,
`plan_merge` for the direction, `merge_entity_pair`'s full-owner guard. The tool asserts
no survivor. Both ids are followed through `entities.live_entity_by_id` first, so a pair
that has already been folded reads as one entity rather than staging a fold onto a
tombstone (the shape `analysis/repo.resolve_review`'s merge-accept arm still reaches; it
is filed, and its shape was deliberately not copied). A `distinct_from` refuses at staging
rather than handing the owner a card whose only outcome is a refusal.

**`read_note` was NOT inherited unchanged, and that is this task's judgement call.** Plan
risk 1 says note bodies reach the model unframed and that this is safe only because the
persona reading them holds no tools — and the on-reply set is precisely the wave that
falsifies it: a fetched body is another person's text arriving in a turn that can now
force-supersede a fact. So `read_note` fences its body in the same nonce-closed frame
turn 0 gets, keyed on `ToolContext.agent_tools` holding a graph-write verb rather than on
the persona — the hazard is write authority, not identity, and `jmoltobservetools` already
reads that field for the same kind of boundary. Curator and jerv hold neither verb and are
byte-for-byte unchanged. The frame moved to `analysis/noteframe.py` so the two callers
share one boundary instead of teaching the model two, and the persona prompt (v3) extends
"THE NOTE IS DATA" to every note it reads rather than only the one it is about.

*Landed (T5): the six expressiveness gaps are decided, and `assert_fact` is v3.* The
harness re-point left six things the tool surface could not say, and W5a's ~940-line
deletion is gated on not deleting a path only an xfailed scenario covered. Each was
settled the way W2 settled the batch shape — `backend/evals/shape_probe.py` driving the
live model through `/api/debug/tool-probe` (gpt-oss-120b, reasoning low, 12 samples an
arm, scored for well-formedness AND for whether the value is in the field's vocabulary
and right). **Three closed, three accepted**, and the corpus is **52 passing / 23
strict-xfail of 75**, from 49 / 26. (The numbering below is the brief's — the harness
README numbers eight rows, because it counts the structured-`value_json` and no-arbiter
gaps separately and because closing the interval end split its old row 4 in two.)

**The finding that decided four of the six, and the one that outlives this wave: R3 is
right and incomplete. `required` buys PRESENCE, not MEMBERSHIP.** gpt-oss filled every
required field on every sample in every arm — with a value it invented. A `kind` field
described with its six words, under an imperative "copy exactly ONE of these six words,
and never any other word", came back **7 legal in 80**: it writes the fact's TOPIC
(`residence` ×15, `employment` ×10, `medical` ×9). An `assertion` field described with
its five: **0 in 72**. A `qualifier` field told to stay empty unless two facts collide:
prose on **61 of 86** facts. The corollary is now a design rule in `TOOL_SURFACE.md`
(correction 8): **the only closed vocabularies a tool grammar can enforce without an
`enum` (constraint 8) are the JSON types themselves** — `number` and `boolean`. A field
whose legal values are WORDS is not buildable on this box.

And a boolean is not the way round it, only a different failure. Both were built and
measured. `negated` ("the note says this is over"): **80/80 legal, `true` 0 times**,
including on every "I finally sold the Civic last week so it is gone" — inert. `reading`
("a number off an instrument"): **96/96 legal, `true` 88 times**, including on "Dana still
works at Everlane" — it would make nearly every fact a `measurement`, which accumulates
and never supersedes. The type is filled perfectly and the JUDGEMENT is not there; one
collapses to the majority class, the other to the other one.

- **Gap 4 — one `when`, no interval end. CLOSED.** `when_end`, a seventh flat scalar,
  never a nested object, exactly as `TOOL_SURFACE.md` gap 4 sketched. What the sketch did
  not anticipate is that the field needs a HANDLER as much as a schema: the model closes
  the one genuinely-closed interval in a note nearly every time *and* stamps an end on
  nearly every other — **53 over-applications in 64 items**, as "present", "last week",
  "unspecified", and today's date on a fact the note dated today. `_close_interval`
  refuses an end that is not a date, has no `when` to close, or does not pass the START'S
  OWN PERIOD; that last comparison is period-against-period, and an instant-against-instant
  test would have admitted every "today on a fact dated today" and closed the owner's
  current address at the end of today. The measurement that says the discipline is enough: on the SHIPPING eight-field schema the model produced 45 spurious ends in 56 items and the handler admitted **0 of 45**, while admitting 4 of the 7 that named the real interval - the other three arrived with a blank `when`, which is the conservative direction and equals the behaviour before the field existed. Those eight fields also cost nothing: 8.0 facts a turn and the same well-formedness as the six-field control on the same note. Flips
  `hist_retrospective_closes_open_interval` and `hist_idempotent_retrospective_refresh`.
- **Gap 6 — no channel for the model's own confidence. CLOSED, and it is the SAFETY one.**
  A JSON **`number`**: the string spelling of this field came back "high"/"low" every time
  (0 of 24 legal), the number spelling 94 of 94. `self_confidence` is now `min(engine span
  check, model number)` — **only ever lowers**, which is `TOOL_SURFACE.md` cut 3's own
  words, written as grounds to cut the field and in fact the property that makes it safe:
  a model claiming 1.0 on a quote the note does not contain still lands at the 0.4
  ceiling. The direction that matters for a guard that HOLDS facts is the false positive,
  and across 94 facts on two notes the live model marked down **zero** legible ones. It
  under-reports rather than over-reports — on a note whose middle line is explicitly
  unreadable it landed *on* 0.5 as often as below it, and 0.5 is not `< LOW_CONFIDENCE` —
  so it is a backstop for the clearly illegible case, not a calibrated dial, and that is
  written into the sidecar's wording. Two integration tests, because the guarantee is only
  observable end to end. **The calibration is measured and short of the threshold, and that is stated rather than papered over.** Across 121 facts on three notes the live model marked down zero legible ones — the direction that matters for a guard that HOLDS. On a note whose middle line is explicitly unreadable it converges on exactly **0.5**, and 0.5 is not `< LOW_CONFIDENCE` (0.5). Sharpening the description from 'below 0.5' to a concrete '0.3 or lower' made it more CONSISTENT (7 of 10 smudged facts marked down, against 6 of 11) without moving it below the threshold. So the channel is built and proved — `test_note_graph_write_pg.py` holds a 0.25 read behind a `low_confidence` card — and on this box today the model does not emit a number that fires it. Moving `LOW_CONFIDENCE` to catch 0.5 is deliberately NOT done: it is a live threshold the whole `note.extract` path also feeds, and widening a hold rule to catch one model's rounding would park real facts behind cards on a box whose owner has no inbox.
- **Gap 5 — an `object` string silently becoming an edge. CLOSED, as a validation fix
  rather than a schema change**, as the brief guessed. A literal that happened to equal a
  resolved surface became an edge to that entity — an entity's own nickname became a
  self-edge and the display projection then had no name fact to read. The registry already
  declares which predicates take an edge (`value_shape: ref`), so a declared non-ref
  predicate takes its object literally whatever it spells. An explicit handle still wins
  everywhere; an undeclared tier-2 predicate keeps the permissive link.
- **Gap 3 — no `qualifier`. ACCEPTED, with a bounded channel and no new field.** The
  measurement above says a `qualifier` field would split 71% of identity keys, and a split
  key means nothing ever supersedes — strictly worse than the collision it was meant to
  fix. What ships instead is the channel `registry.decompose_predicate` already read and
  the v3 sidecar now teaches: `name.nickname.friends` stores as name.nickname + friends,
  bounded to the five registry predicates declaring a `qualifier_vocab`. With gap 5 it
  flips `name_legal_reprojects_canonical`. A LONG-TAIL qualifier is still dropped.
  **Measured, and the channel is open but unreached.** The harness's perfect model uses it, which is what flips `name_legal_reprojects_canonical`; the LIVE model does not. Probed on a note with three audience-scoped nicknames, **0 of 39** nickname facts carried a third dotted segment — and the model did not reach for the registry's base spelling either, writing `has nickname`, `hasNickname` and `calledByFriends` where the registry declares `name.nickname`. So the blocker is one layer earlier than the qualifier: predicate NORMALIZATION. `has nickname` matches no `renamed_from`, so it lands as a novel predicate and `decompose_predicate` never gets the chance to recover anything. Registry work again — widen the attractors — not tool work. What v3 buys today is that the channel exists and is documented, so a model that does write the registry spelling is understood.
- **Gap 1 — no `kind`. ACCEPTED.** 7/80 as a string, 88/96 false-fires as a boolean.
  Closing it is **registry work, not tool work**: `_fact_kind` already prefers a declared
  predicate's `kind` over the subject type's default, so declaring `bodyWeight`,
  `bloodPressure`, `medicationRegimen` and their kin under `schema/defs/` closes seven
  scenarios without asking the model anything. That is tier-1 work under
  `ENTITY_GRAPH_REFOCUS_PLAN.md`, deliberately not done here.
- **Gap 2 — no `assertion`. ACCEPTED.** Neither spelling is a channel. R1's claim that
  "'That's simply false' is already expressible as `assertion: "negated"`" is FALSE for
  this surface and cannot be made true; `TOOL_SURFACE.md` correction 9 records it. The
  unattended pass cannot state a negation, and the owner's reply turn — `correct_fact`,
  which force-supersedes and pins — is what carries it.

**What W5 may therefore not delete.** Each accepted gap is a promise that something else
still carries the meaning, and each xfailed scenario's own `xfail` string now names its
survivor. Collected: `facts.assertion` and its CHECK, `supersession.CURRENT_ASSERTIONS` /
`_IRREALIS` and the negated-supersedes arm of `decide()`, and `extraction.ASSERTIONS`
(gap 2 — the EMR importer and the reply turn still write non-asserted rows and three read
surfaces filter on the column); `_fact_kind`, the registry's per-predicate `kind`
declaration and the `attribute_collision` card (gap 1 — the only thing between an
undeclared measurement and a silent overwrite); `facts.qualifier`, the identity key that
includes it, and `decompose_predicate` plus its `qualifier_vocab` declarations (gap 3);
`facts.value_json`, `_quantity_value`'s unit split and `values_equal`'s cross-unit
comparison (gap 4's structured-value half — the EMR importer and the projections write and
read structured values through the non-model path). The arbiter is the one accepted gap
with no survivor: deleting it is the plan, and `rel_enumerated_children_fan_out` is where
the loss is recorded.

*Also found, and corrected in place.* `health_low_confidence_ocr_guard` — the corpus's one
SAFETY scenario — was xfailed on a root that is no longer true. Its confidence channel is
live and proved; what blocks it is two gaps upstream of the guard, because
`medicationRegimen` is declared by no type so `_fact_kind` lands it as `attribute` and the
collision routes to `attribute_collision` before `decide()`'s state-supersession arm (where
the low-confidence branch lives) is ever reached, and its qualifier `antihypertensive` is
long-tail so the two regimens do not share an identity key at all. Declaring one predicate
closes both. This is the sharpest argument for the tier-1 declaration work: the guard the
plan most wants is unreachable for a registry reason, not a tool one.

*Also: the harness snapshot gained a column.* `closed` (`valid_to IS NOT NULL`) — before
it, the only column a scenario could read a closed interval out of was `value_json`, which
an EDGE never stores, so the close scenario had to assert the word "ended" in a payload the
write path does not produce.

**W4 — Cutover.** Port EMR (D9) and intake (D10) onto the conversation. **Keep EMR
firewall Layer 2 as a hard non-commit** — `ingest/emr/firewall.py:3-28` has no
domain-floor backstop and `address`/`geo` are deliberately outside the floor, so it is
the only guard keeping a home address out of `health`. Note that the card W1 now files
**never existed before**: the guard and the handler that discarded its catches landed the
same day (`490c54987`, `166e24691`, 2026-07-03), so the control fired silently for its
entire life. Its card lands on the wiki tab (D4). The rebuild sweep is already in hand from W1, so it can serve as cutover instrument
and rollback lever.

*Landed (the intake half, D10): the third tool set, and the finding that the port had
already happened.* **Intake was never wired onto the conversation, because nothing had to
be.** `ingest/pipeline.py` emits `note.ingested` on every settled ingest whatever the
provenance, so the `untrusted_origin` note an approved submission enacts into
(`proposaltools.intake_note_executor`) has been opening a `note_converse` thread since W2
— and W3 handed that thread `resolve_entity` / `assert_fact` / `ask_owner`. Risk 1 was
therefore live and unmarked on the W3 branch, and `ASSISTANT.md` #10 ("untrusted-origin
content never triggers a background job") had been false since W2 with nothing saying so.
D13 holds trivially: no producer moved, and the shipped materialize → Proposal → approve →
enact → `integrate_note` path is byte-for-byte unchanged.

So what W4 owed was the DIFFERENCE, and it is a **third frozenset**, not a flag
(constraint 9): `NOTE_INGEST_THIRD_PARTY_TOOLS` — the unattended six minus `ask_owner` —
serving **both** turns. The rule it encodes is *a stranger's words may cause a FACT and
nothing else*. Both graph writes stay, unnarrowed, at the same budgets, through the same
`commit_facts` — D10's "unrestricted in *what* it may write" is honoured exactly. What
goes is `ask_owner`: its question is model-authored out of stranger-controlled text and
lands in the owner's notes tab in his own agent's voice, AFTER the materialize → approve
step that is the whole trust boundary of the intake feature, and the answer he types is
appended to the note as source text and re-ingested — chunked, embedded, citable. Nothing
reaches the submitter (no set holds an egress verb, and the intake session is a different
principal on a different table), so it is not an exfiltration hole; it is an unreviewed
inbound message channel, and D2 stands in its place.

**The reply turn does not widen, and that is the substantive call.** D8 unlocks
`correct_fact` / `merge_entities` / `prefs_write` on the premise that the owner is the
only voice in the room. On a third-party note he is not — the submitted body is turn 0
and is still in the turn's context, which is this plan's own reason for keeping `web_*`
out of the on-reply set. `correct_fact` is the sharp one: `decide()`'s correction branch
reads neither confidence field, so it force-supersedes AND pins, and a stranger who can
shape what the owner types gets a fact no later note can supersede. `prefs_write` is the
durable one: a rule landing in `owner_prefs` is corpus-wide prompt injection in every
future note conversation's system prompt. The cost is real and stated: the owner cannot
`correct_fact` from an intake thread. He has not lost the verb — it is reachable from a
reply into any note conversation whose body he wrote.

Enforcement is where R2 asks for it. On the unattended pass `ask_owner` is not BOUND:
`converse.executor_for_note` builds that note's registry without it, so the sidecar is
never loaded and there is no handler for a later allowlist edit to make callable. On
`/chat` the allowlist is the lock, applied by `agents.narrow_for_third_party_note` LAST
(it undoes `agent_for_owner_reply`'s widening) over `thirdparty.conversation_is_third_party`,
which fails closed at every step — an unreadable conversation or note reads as
third-party, so a DB blip narrows a turn rather than widening one. The predicate is
`notes.provenance`, deliberately not a new column: 0111 already admits the value, the
enacting executor already sets it, and `queue.INTEGRATION_BACKFILL_ORDER_BY` already
reads it, so a second marker would be a second thing to keep true. The frame is the SAME
nonce-closed fence with one clause naming whose text it is — belt to the tool set's
braces, and on the same boundary rather than a second one.

`tests/integration/test_intake_conversation_pg.py` builds the shipped chain (mint →
redeem → submission → the real `intake_note_executor` → conversation) and proves the
capability-token principal's reach did not grow by one row: zero on `notes`,
`note_conversations`, `note_conversation_tool_calls`, `note_clarifications`,
`agent_sessions`, `agent_turns`, `entities`, `facts`, `chunks` and `proposals` while the
owner sees every planted row, and RLS refusal on the three INSERTs that would let a
submitter file its own turn, its own clarification block, or its own conversation.

*Corrected 2026-09-09 (adversarial review).* As shipped it planted rows in only SEVEN of
those ten, so `assert seen == 0` was unfalsifiable on `facts`, `chunks` and `proposals` —
and `facts` is the one the whole premise rests on (the conversation writes facts out of a
stranger's text; can the stranger read them back?). All ten are planted now. Separately,
the fail-closed proof for `conversation_is_third_party` reached only its FIRST branch:
the test believed a soft delete leaves the thread behind, but `delete_note` →
`purge_note_artifacts` → `_purge_conversations` deletes the whole `agent_sessions` row
and cascades the side row, so `get()` returned `None` every time and the `_Broken` repo
was never called. The note-is-gone and exception branches are unit cases now
(`tests/unit/test_note_converse.py`), where a conversation row can exist with no note
behind it; flipping either to fail open now fails.

*Landed (the EMR half, D9): the boundary is `fhir_status`, and it is now in code.*
`TOOL_SURFACE.md` gap 3 named the hazard and the answer both: `fhir_status` is EMR-only,
set by the parser, and `supersession._lab_status_transition` is what reads it — the
transition that keeps a FHIR *preliminary* reading from becoming a citable current value
(constraint 4). `assert_fact` has no such field, and constraint 8 forbids the enum-shaped
vocabulary one would need. So the importer writes through W1's seam **directly** and the
conversation over an EMR note holds **no graph-write verb at all**.

- **The seam.** `apply_intent` splits into `commit_intent` (resolve, plan-to-extraction,
  `commit_facts`, the held-fact review cards) and `settle_note`. `apply_intent` is now
  the two of them, one after the other, and returns what it always returned.
  `ingest/emr/integrate.EmrNoteCommit` is the multi-source caller — it commits one
  parsed source at a time, in that source's own transaction, unions
  `touched`/`projected`/`mention_ids` and the extractions, and calls `settle_note`
  **once**.

  **The split was written as behaviour-preserving and was not, and the correction is
  where the seam now stands** (found by an adversarial review of W4, 2026-09-09, on the
  ORDINARY note path — nothing EMR about it). Putting the held-fact cards inside
  `commit_intent` moved them from AFTER the whole-note settle to BEFORE it, and the two
  steps write `app.review_items` in opposite directions: `_file_inference_reviews`
  skipped a card when an OPEN one already matched, `settle_note` deletes open cards
  pointing at facts it just retracted. Their keys disagreed —
  the card's was `(note_id, entity_ref, predicate, qualifier)`, the held row's is
  `(note_id, entity_id, predicate, qualifier, object, domain_code)` — so a re-analysis
  that resolved one `entity_ref` to a DIFFERENT entity (or a note an owner PATCH moved
  between domains) minted a fresh held row, had its card suppressed by the previous
  run's, and then watched the settle delete that one: a `pending_review` fact with no
  card, which is precisely what N11 exists to prevent. **The fix is the KEY, not the
  order**: the dedup now keys on the held ROW's id, the same identity
  `_insert_held_fact` refreshes on, so the two agree and the invariant holds on either
  side of the settle. `test_a_re_resolved_held_fact_never_ends_up_with_no_card` is the
  pin and is parametrized over both orders — it failed on the shipped order and passed
  on the pre-split one, which is exactly how the regression stayed invisible.
- **That fixed a live bug, and the bug is the argument for the port.** A decrypted EMR
  archive attaches MANY PDFs to one note; each is fingerprinted, parsed and lowered
  separately. Driving those through `apply_intent` in a loop ran the whole-note settle
  per attachment, so the second PDF's settle retracted the first PDF's facts — a
  two-source import kept only the last source's readings. Nothing caught it because every
  EMR test in the suite attached exactly one file. Reproduced against the old shape before
  the fix (only one attachment's citations stayed live), and pinned by
  `test_two_emr_attachments_on_one_note_both_survive_the_settle`, which discriminates on
  the **cited chunk's attachment id**: the two fixtures' analytes overlap, so an
  analyte-name assertion is satisfied by either source alone and proves nothing.

  **Stated plainly beside it: on the live box this fix is not yet realizable.** In
  production `integrate_note` and `emr_parse` BOTH fan out of one `note.ingested` on a
  health `Records` note with no ordering between them, and each ends in a whole-note
  settle — the collision the section below records as still open and owned by nobody. A
  real multi-PDF import therefore loses either ALL the EMR facts or none of them,
  depending on which producer settles last, and that whole-note loss MASKS the
  per-attachment one this bullet fixes. The test is real and the fix is real — the loop
  is gone and `EmrNoteCommit` settles once — but the benefit is only collectable once the
  race is decided rather than raced. Do not read "a two-PDF import now keeps both PDFs"
  as an end-to-end production claim; read it as "the importer no longer destroys its own
  earlier attachments", which is the half this wave owns.
- **The narrowing, two locks.** `ingest/emr/ownership.emr_owned` mirrors migration 0122's
  own trigger filter (health + `Records` + an EMR-shaped attachment) — deriving "the
  importer owns this" from anything else would let the two disagree.
  `agents.narrow_for_emr` subtracts `NOTE_GRAPH_WRITE_TOOLS` from the ALLOWLIST, applied
  by `analysis/converse.py` on the unattended pass and by `clarify.reply_profile_for_session`
  on the `/chat` reply turn; `graphwritetools.NoteToolset(writes_graph=False)` declines to
  BIND the handlers in the worker's per-note registry. Constraint 9 says the surface is
  the registry's, so both are here and they fail independently.
- **This is the one place W4 breaks D8.** The owner replying does NOT unlock a write
  surface on an EMR note. `correct_fact` at an empty address commits active + PINNED, and
  a pinned lab head makes every later import of that reading `held` — the owner would
  silently freeze a value the next draw is meant to supersede. What the reply turn keeps
  is `ask_owner`, the entity reads, `search`/`read_note`/`relate` and `prefs_write`.
- **What the conversation adds over the deterministic parse: nothing yet, and that is the
  honest answer.** The parse is deterministic and total; there is no meaning for a model
  to supply. What the conversation is for on an EMR note is being a place the import can
  be explained and questioned — and that half is NOT built here: the deterministic run
  still reports only through `review_items` (firewall / parked-read / unrecognised-source
  cards), and `emr_parse` and `note_converse` are still two jobs off one event with no
  ordering between them. Publishing the import into the thread's ledger and its D3 chip is
  the next task, and it needs the ordering decided rather than raced.
- **The `record_tool_call` precondition is sidestepped here, not solved.** Nothing in
  W4's EMR half wires it: on an EMR note the conversation writes no facts at all, so its
  ledger is empty *because it is empty*, and the note's one settle is the importer's. The
  precondition stood for every other note and for D10 — and was closed separately in
  W4c/1, by recording the owner's reply turn at the turn seam (`record_reply_writes`)
  rather than in the tool dispatch.
- **The rebuild sweep needed no change**, and was checked rather than assumed:
  `rebuild._rebuild_one` already re-enqueues `emr_parse` (`_EMR_REPARSE_SQL`) inside the
  same transaction as the purge, so a corpus rebuild re-drives the deterministic producer
  and is the cutover instrument and rollback lever this wave asks for.
- **D13 holds.** `emr_parse` keeps working throughout; no producer removed, no trigger
  touched, no migration.

*Closed against an adversarial review of the merged wave (2026-09-09).* Its blocker was
the `apply_intent` split and is recorded at the seam bullet above; the intake half's two
test gaps are recorded at the isolation paragraph. Three more, and what each cost:

- **Both W4 narrowings were untested at their allowlist call sites.** Inverting the
  predicate in `clarify.reply_profile_for_session` — so the EMR reply turn kept the full
  on-reply surface and every ORDINARY note got narrowed instead — left 192 tests passing;
  deleting the runner's `if note_owned_by_emr(note): profile = narrow_for_emr(profile)`
  left 114 passing. Every test naming these functions drove them as pure functions, tested
  the registry lock, or monkeypatched the reply lookup away at the route, so the "two
  locks that fail independently" claim held for the registry only.
  `test_a_note_the_importer_owns_runs_the_unattended_pass_with_no_write_verb` and
  `test_the_reply_turn_over_a_live_emr_note_loses_the_writes_and_a_plain_one_keeps_them`
  drive both over a real note and both mutants now die.
- **`EmrNoteCommit._resolved` was last-wins where its siblings union.**
  `touched`/`projected`/`mention_ids` are sets; `resolved` is a MAP, and the EMR refs are
  semantic keys (`org:…`, `cond:…`, `obs:…`) that `new`-mode resolution mints a fresh
  provisional for per intent — so one ref on two attachments is two entities and the dict
  merge kept only the last. The settle reads that map for `_register_declared_aliases`,
  `_reproject_entities` and `_promote_corroborated`, so the dropped entity's facts were
  spared by the sweep while its projection and promotion silently never ran. Latent rather
  than live today — the EMR vocabulary carries no naming predicate and `entity_promotion`
  defaults off — but the asymmetry is a trap, so displaced entities are now re-filed under
  their own id and the two fixtures drop three entities without it.
- **`emr_owned` is MUTABLE, and it is recorded rather than guarded.** `NoteUpdate` lets a
  PATCH change `domain` and `destination`; `update_note` then sets `ingest_state="pending"`
  → re-ingest → `note.ingested` → a fresh conversation, and a note moved off `Records` or
  out of `health` no longer reads as importer-owned, so the conversation gets
  `resolve_entity`/`assert_fact` over facts the deterministic parse wrote with a
  `fhir_status` no tool can carry. It is not guarded because **the tool set is not where
  this bites**: the same move takes the note out of 0122's trigger filter, so `emr_parse`
  stops firing, while `integrate_note` still fires on the same event and its whole-note
  settle retracts the parse's facts outright. The first-order loss is the domain move
  destroying the EMR graph; the model's write verbs are second order behind it, and both
  are the same open question as the `integrate_note`/`emr_parse` race below. A guard, when
  that question is decided, has a durable marker already written by the importer itself
  and needs no new column: `app.facts.extractor = 'emr:deterministic'` on the note answers
  "did the parse already write here", which is the question that actually matters, where
  0122's filter answers "will it write here next".

Also recorded, out of scope and untouched by this wave: `readtools.py`'s `search` returns
`format_search(...)` UNFRAMED while `read_note` fences its body, and `search` sits in
`NOTE_INGEST_ON_REPLY_TOOLS` beside the graph writes — so an owner-note reply turn can
pull other notes' excerpts into a write-capable turn with no DATA frame. Pre-existing to
W3 and W4; both W4 narrowings happen to remove `search`, so it is reachable only on the
owner's own notes, which is the shape W3 judged acceptable for `read_note` before the
frame went in. It should get the same frame, on the same `agent_tools`-keyed boundary.

*Still open in W4* (this was written as "intake (D10) is not ported", which the intake
half above did that same day): the *older, larger* collision this work PROVED but did not
fix. `integrate_note` and `emr_parse` both fan out from one `note.ingested` on a health
`Records` note, both write facts on that note, and each ends in the whole-note settle. `ANALYSIS.md` already says "no ordering is promised between the two
passes and none is at ingest either" — what it does not say is that the LOSER's facts are
RETRACTED, not merely ordered late.
`test_the_generic_integrator_does_not_retract_the_emr_parse_it_races` is the evidence: run
the real `integrate_note` path over a note `emr_parse` has already written, and every fact
the parse wrote is gone. It is a strict xfail, so it fails the suite the day it is fixed.
That predates this wave (it is the shipped `apply_intent`, on both sides) and it is out of
the EMR half's scope. It was written expecting D10's port to decide who owns a note's
settle; D10's port turned out to move no producer at all (it is a tool-set difference and
nothing else), so with both halves merged this is **still open and owned by nobody** —
W5's, or its own task, and the strict xfail is what keeps it from being forgotten. Note the mercy that hides it in the small case: a REJECTED plan
skips the settle entirely, so a note whose extraction yields nothing usable does not
retract — which is why this bites hardest on the notes whose PDFs the extractor reads well.

**The two halves compose, and the narrower wins.** The predicates are independent and a
note can satisfy both: an approved intake submission enacts into an `untrusted_origin`
note (D10), and if the owner filed that submission to health / `Records` with the archive
or a PDF attached, `emr_owned` reads the same note as importer-owned (D9). Nothing forbids
that note, and getting its tool set wrong is SILENT — the thread looks identical, and the
only difference is a fact written out of a stranger's text onto a note the deterministic
parse is authoritative for. So the merged answer is the **intersection**:
`narrow_for_emr` SUBTRACTS `NOTE_GRAPH_WRITE_TOOLS` and `narrow_for_third_party_note`
INTERSECTS `NOTE_INGEST_THIRD_PARTY_TOOLS` — it was an assignment while it was the only
narrowing, which would have handed `resolve_entity` and `assert_fact` straight back
whenever it ran second, which is the order both call sites use. Intersecting makes the two
commute, so "third-party runs LAST" is now belt over braces (it still matters against
`agent_for_owner_reply`, which WIDENS). Such a note's conversation holds `find_entity` /
`read_entity` / `current_time` on both turns, and the worker's registry binds neither
`ask_owner` nor a graph write for it —
`test_a_note_that_is_both_third_party_and_emr_owned_gets_the_intersection` and
`test_a_note_that_is_both_a_strangers_and_the_importers_binds_only_reads` are the pins.

The other thing the merge had to settle is the FAILURE direction, where the two halves
genuinely disagreed. Both reply-turn predicates read the same conversation row and the
same note on the same turn; `thirdparty.conversation_is_third_party` failed CLOSED and
`clarify.reply_profile_for_session` failed OPEN, each defensible alone. Composed, a note
read that blipped narrowed the turn to the third-party set — which still holds
`resolve_entity` and `assert_fact` — and left them bound on an EMR note, the one place a
model write is unsupersedable (`correct_fact` at an empty address PINS, and a pinned lab
head holds every later draw). So the EMR lookup now fails closed too: no conversation row,
no note, a soft-deleted note or any exception all narrow, for each predicate
independently.

*W5a attempted, and the gate is NOT met — the wave is blocked, not skipped.* The
six-gap half of the gate holds and was honoured: every one of the 23 `xfail` strings
and the README's eight-row table were read against the code, and nothing on the "may
not delete" list was touched. What blocks the wave is the OTHER half, D13's per-PR
rule, and it fails on two independent counts the plan's own line numbers hid:

- **`pipeline.py:305-478` is `integrate_note` exactly, and it is still the sole
  producer of the whole-note settle.** The conversation's write path is `commit_facts`
  only (`agent/graphwritetools.py`), which does nothing whole-note. **S2 closed the
  first half of that** (docs/plans/SETTLE_OWNERSHIP.md): `settle_note` is split into
  `sweep_note` / `settle_tail` / `stamp_analysis`, and the conversation now calls
  `settle_tail` at the end of a clean pass, from BOTH turn paths
  (`analysis/clarify.settle_conversation`) — so the entity reprojection, the
  corroboration promotion and the appointment / EMR / geofence / device projections
  finally run over what it wrote. Before that a conversation-written appointment landed
  in no projection at all, and the only thing hiding it was the bug S1 fixed.
  What `integrate_note` still owns ALONE: every retraction on the note, the mention
  reconcile, the declared-alias sweep, the stale-ambiguity and truncation cards
  (deliberately left in the composition, since they are note-keyed and producer-blind and
  a second sweeper would delete the analyzer's cards), the `note_analysis` stamp that
  `api/notes.py`'s `analyzed` flag and `/analysis` read, and the
  `notes.integration_state = 'integrated'` flip that `queue.backfill_pending_integration`,
  the workflow reconciler and `rebuild.py` all key on. `emr/ownership.py` used to state
  part of this as "the CONVERSATION adds no sweep of its own today"; that sentence is
  corrected, because a producer does not need a sweep of its own to LOSE, only a
  co-writer that has one. Its LEDGER precondition is landed (W4c/1):
  `ConversationWrites.facts` is the whole-conversation union across both turn paths, so
  `sweep_note(touched=writes().facts)` would no longer retract what the owner's own reply
  just added (`models/note_conversation.py`) — and both turn paths stamp ONE
  `conversation` owner. What no longer follows from it is a SWEEP: S3 wired one to this
  ledger and it was removed, because a write ledger cannot say what a note stopped
  saying. **None of this unblocks W5a.** Ownership is decided and scoped (W4c/3,
  SETTLE_OWNERSHIP.md S1) and the tail is wired (S2), but the conversation retracts
  nothing, stamps no `note_analysis` and flips no `integration_state`, so deleting
  `integrate_note` still strands the corpus at `pending_integration`. Pinned by
  `test_note_converse_pg.py::test_a_finished_pass_settles_the_conversation_and_not_the_note`.
- **`arbiter.py` cannot go at all, and that is W4's own doing.** Its EMR half routes
  the deterministic importer through `arbiter.plan_intent`
  (`ingest/emr/integrate.py`), and `commit_intent` — the seam that importer and the
  eval runner write through — itself calls `plan_to_extraction` and `compute_signals`.
  `ArbiterPlan` / `PlannedFact` / `plan_intent` / `plan_to_extraction` /
  `compute_signals` therefore have a live non-model producer. Only the three helpers
  `integrate_note` alone calls — `recover_dropped_fields`, `derive_kinship_gender`,
  `dedup_intent_facts` — are W5a's to take, and only after the first bullet clears.

So W5a's real size is not ~940 lines; it is three helpers plus `integrate_note`, and
it is gated on the settle wiring, which is a *replacement*, not a deletion. Sequence
it as W4c and only then W5a. W4c/1 is **landed** — the ledger records both turn paths,
recorded at the turn seam rather than in the tool dispatch (`clarify.record_reply_writes`;
the dispatch serves every agent and knows nothing of note conversations, so the hook would
have to be threaded through every tool, and the seam keeps the pass and the reply
symmetric). W4c/3 is **decided and landed** in its first wave: the `integrate_note` /
`emr_parse` race the third writer would have made three-way is closed by scoping each
producer's sweep to the claims it holds, which also closed the conversation's live loss
and turned `test_emr_import_handler_pg.py`'s settle-collision xfail green. Its SECOND
wave (S2) split `settle_note` and gave the conversation the settle's TAIL, which it had
never had. W4c/2 — the conversation's own sweep — was built as S3 and CLOSED AS
UNBUILDABLE: a write ledger cannot establish that a note stopped saying something, so a
sound sweep for this producer is empty. W5a stays blocked on what is left: the
`note_analysis` stamp and the `integration_state` flip
(docs/plans/SETTLE_OWNERSHIP.md preconditions 3 and 4, and S4), and its replacement for
the analyzer's sweep has to be an extraction rather than a ledger.

What did land under W5a: two genuinely dead pieces inside `arbiter.py`, both
unreachable regardless of the gate — `plan_to_extraction`'s `commit_only` arm (A1b-ii-1's
safety, superseded by A1b-ii-2's index routing, no caller since) and `ArbiterPlan`'s
write-only `merge_proposals` / `distinct_proposals`, which nothing read, so the module
docstring's "merges and distinct-from proposals always route to review" was false.

**W5 — Teardown, decomposed.** W5a: the old chain (`pipeline.py:305-478` + `arbiter.py`,
~940 LOC), gated on W3's runner re-point **and on the six-gap decision above, which is the
other half of that gate**: an accepted gap is a path no scenario can cover, so the "what
W5 may not delete" list in W3/T5 — and the same list in each xfailed scenario's own `xfail`
string — is binding on this wave. Nothing on it is dead code just because the harness is
green without it. W5b: the arbiter card kinds and the inbox's
ingest tab, with an explicit surviving-kinds list. W5c: correction-note retirement plus
the `SECURITY DEFINER` move and grant revoke — a security-path change needing its own RLS
isolation test, which cannot ride a 3,000-line deletion. **Port `file_correction`
first**: `PHASE6_WIKI_PLAN.md` §4 names `plan_intent(correction=True)` as the wiki
correction loop's shipped exit criterion, and `wiki/lint.py`'s stale-claim card offers it
as a `correct` action.

*Landed (the precondition).* **`file_correction` is not `correct_fact` under another name,
and converging them would have been wrong.** `correct_fact` addresses ONE identity key
`(entity, predicate, qualifier)` resolved against the graph, REFUSES a key holding several
live rows, and is bound only inside a note conversation (`replytools._bound`). The wiki
lever takes PROSE, from a Talk thread anchored to an `article_id` or from a review card —
places with no note conversation to be inside of — and its product is the NOTE, which is
the part that cannot be dropped: `wiki_citations.chunk_id` is NOT NULL and
`wiki/builder.py` INNER JOINs chunks, and the corpus rebuild re-derives the graph from
notes, so a correction that left no note would have nothing to cite and would evaporate on
the next rebuild. Three producers mint that note (`agent/wikiwritetools.file_correction`,
`POST /api/wiki/{id}/corrections`, `POST /api/review/{id}/correction`) and all three are
kept.

What was actually broken by W5a was the note's BACK half, not its front: the elevation was
two lines inside `integrate_note` (`provenance == 'owner_correction'` →
`plan_intent(correction=True)`), both inside the deleted range. So the flag is what was
ported, onto the write path the conversation already uses: `NoteTarget` carries the note's
provenance and `graphwritetools._assert_one` sets `correction=True` on a fact the
correction note's own text ATTESTS — the arbiter's rule verbatim, refusing half included
(`fact_correction = correction and signals_i.surface_attested`; an inferred fact in a
correction note follows the ordinary capped path), at the same `weight = 1.0` and with the
model's self-report suppressed so the number cannot drift. Nothing model-facing changed:
no sidecar edit, no version bump, no re-pinned digest, and no field the model could fill to
claim a force-supersede. The discriminator is server-read provenance — `CreateNoteRequest`
has no such field and every producer sits behind an owner principal — which is what makes
it safe on a pass the owner is not present for, and it can never collide with D10:
`is_third_party` and `is_correction` are disjoint answers to the same field.

Evidence: `tests/integration/test_note_correction_pg.py`, end to end from the
`file_correction` handler through `ingest_note`, the production `note_converse` wiring and
`supersession.decide()` — the pin, the refusal, the pin holding against a later ordinary
note, and an ordinary note in the same shape doing none of it. **The port needed nothing
inside W5a's deletion range**, so the two waves do not conflict.

*Also found and fixed there.* `note_converse` could not run on a real box at all:
`tasks.scheduler._owner_principal_id` is annotated `str | None` but `app.principals.id` is
a `uuid` column, so the handler built a `SessionContext` from a `uuid.UUID` and died in
`scoped_session`'s `set_config`. Both other callers wrap the value at their own call site
(`tasks_tick`; `PlanContinuationRunner`, whose test comment reads "raw uuid, like
production"), and every note-conversation test injects the id itself, so nothing caught
it. Fixed at the source.

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
