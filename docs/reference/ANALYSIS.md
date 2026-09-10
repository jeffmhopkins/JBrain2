# JBrain2 — Note Analysis Pipeline

> **Status:** Living · **Last verified:** 2026-09-10 — **The note conversation has a THIRD write verb, `close_reading`, and it is the whole-note reading** (`AGENT_INGEST_REWRITE.md` R1). It lands BESIDE `assert_fact` — nothing is deleted, nothing sweeps yet — and commits identical rows through the same `_assert_one` / `commit_facts` / `decide()`. What it adds is what an incremental write can never have: the note's `title` and `tags` on the same call, a `Reading` the pass accumulates (its union of fact ids, and whether any call was CLAMPED — a clamped reading is a prefix of the note, which is what the settle's gate will read in R3), and RECURRENCE. Recurrence is a HANDLER step, not a field: R0 put a `repeats` field in front of the live model on five recurring notes and got 0 parseable RRULEs in 113 values, 0 in 115 sharpened, and a phrase spelling that said what the note said 28 times in 118 — while parsing the model's own attested `quote` recovered the rule 198 times in 200. So `analysis/recurrence.py` reads the rule out of the span and writes the temporal token that carries it, which gives `app.temporal_tokens` a producer again and `appointment_projection._recurrence_rrule` an RRULE to read (the conversation passed `tokens=[]` from W3 until now, so a conversation-written recurring appointment projected as a one-off). It discards rather than guesses: no recurrence marker, a BOUND it cannot date ("Tuesdays until March"), or two different rules in one span all return nothing and the fact commits with the dates it had. Beside it, `resolve_entity` v2 answers with the resolved entity's CURRENT FACTS (newest state first, ≤10 an entity and ≤30 a call, narrowed to the conversation's read scopes on the entity's domain AND on each fact's — a floored health fact on a general entity is the case an entity-level check alone would leak) and names the candidates behind an ambiguous surface, which a new `distinguish` field answers in the note's own words — R0 measured the agent reading the graph 0 times in 144 runs under three personas, so a contradiction has to ARRIVE in a result it already asked for. Prior: **The note conversation's tool-call ledger now records BOTH turn paths** (`AGENT_INGEST_CONVERSATION_PLAN.md` W4c/1). `record_tool_call` had two callers — the worker's unattended pass and `ask_owner`'s self-record — so a `resolve_entity` / `assert_fact` / `correct_fact` on the owner's REPLY turn (an ordinary `/chat` turn) reached the graph and the D3 chip and never `app.note_conversation_tool_calls`. `NoteConversationRepo.writes()` was therefore a whole-PASS share, and wiring constraint 6's `settle_note(touched=writes().facts)` over it would have retracted every unpinned fact the owner's own answer just added. The fix records the reply turn at the same TURN SEAM the pass records at rather than pushing the recorder down into the shared tool dispatch: the dispatch serves every agent and knows nothing of note conversations, so a hook there would have to be threaded through every tool, where the seam keeps the two paths symmetric — one `ledger_rows` fold, one `record_tool_call` loop, one bind-by-run-id, now shared and living in `analysis/clarify.py` (the reply path's existing note-conversation seam, and the half `api/agent.py` can import without dragging the worker's turn runner into the API process). `api/agent.py` calls `clarify.record_reply_writes` in the same `finally` as `close_owner_reply` and BEFORE it, since the sweep will fire on the state that call sets. A ledger write that fails never fails the owner's turn — the writes already committed and a 500 would neither undo them nor recover the row — but it degrades the close to `record_failed`, which lands the conversation `failed`, the one state the sweep does not run on; the unattended pass answers its own recorder failure the same way. `ask_owner` still self-records inside its own transaction and `SELF_RECORDED_TOOLS` still skips it, so a reply turn that ends by asking AGAIN gets one row, not two. **The sweep itself is still unwired**, and who owns a note's whole-note settle is still undecided (W4c/2 and W4c/3): `integrate_note` remains the sole producer, and W5a stays blocked. Prior: **An accepted merge card is enacted on the pair as it stands now, not as the card was written.** `resolve_review`'s `merge_proposal` accept arm folded `entity_b` into the payload's `entity_a` verbatim, so two overlapping cards resolved in sequence repointed the second pair's rows onto a tombstone — a merge that silently undid the merge before it. `_accept_merge` now asks the scope guard first, then resolves each side through `entities.live_entity_by_id`, then `are_distinct`, then re-ranks with `plan_merge`, and records the pair it actually FOLDED so `_reverse_effects` stays exact ("Alias resolution & separation" below has the decision and why the inbox redirects where the agent's `merge_entities` no-ops). Prior: **A note a STRANGER wrote runs on a third tool set** (`AGENT_INGEST_CONVERSATION_PLAN.md` D10, W4's intake half). Nothing had to be wired to put intake on the conversation: `ingest/pipeline.py` emits `note.ingested` on every settled ingest whatever the provenance, so the `untrusted_origin` note an approved submission enacts into has been opening a `note_converse` thread since W2 — and W3 gave that thread the write verbs, which made plan risk 1 live and unmarked. What W4 adds is the difference. `agents.NOTE_INGEST_THIRD_PARTY_TOOLS` is the unattended six minus `ask_owner`, and it serves BOTH turns: a stranger's words may cause a FACT and nothing else. `resolve_entity`/`assert_fact` are untouched (D10: unrestricted in *what* it may write, same budgets, same `commit_facts`, same floor, same span check). `ask_owner` goes because its question is model-authored out of stranger-controlled text, lands in the owner's inbox in his own agent's voice after the materialize→approve step that is the intake feature's whole trust boundary, and the answer he types becomes chunked, embedded, citable source text on the note. The reply turn does not widen, because D8's premise — the owner is the only voice in the room — is false while the submitted body is still turn 0; `correct_fact` is the sharp loss and it is deliberate, since `decide()`'s correction branch reads neither confidence field and so force-supersedes AND pins. Enforcement is not the prompt: on the unattended pass `ask_owner` is not BOUND (`converse.executor_for_note` builds the note's registry without it, so the sidecar is never loaded), and on `/chat` `agents.narrow_for_third_party_note` is applied LAST over `analysis/thirdparty.conversation_is_third_party`, which fails closed — an unreadable note or conversation reads as third-party, so a failure narrows a turn rather than widening one. The predicate is `notes.provenance`, not a new column. **An EMR note has ONE writer, and it is not the model** (W4/D9 of `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md`). `fhir_status` is EMR-only, set by the parser, has no `assert_fact` field and cannot grow one, and is what `supersession._lab_status_transition` reads — the transition that keeps a FHIR *preliminary* reading from becoming a citable current value. So the importer writes through W1's seam directly (`AnalysisPipeline.commit_intent`, the half of `apply_intent` that does everything except the settle), and the conversation over an EMR note holds NO graph-write verb: `ingest/emr/ownership.emr_owned` mirrors migration 0122's own trigger filter, and both the ALLOWLIST (`agents.narrow_for_emr`, applied on the unattended pass AND on the `/chat` reply turn — the one place W4 breaks D8, because `correct_fact` PINS and a pinned lab head makes every later import of that reading `held`) and the worker's per-note REGISTRY (`NoteToolset(writes_graph=False)`) say so independently. `ingest/emr/integrate.EmrNoteCommit` is the multi-source caller: one `commit_intent` per parsed source in its own transaction, then ONE `settle_note` over the union. That fixed a live bug no test could see because every EMR test attached one file — a decrypted archive attaches MANY PDFs to one note, the settle is whole-note, and the per-attachment loop had each PDF's settle retract the PDFs before it, so a two-source import kept only the last source's readings. Layer 2 is untouched and stays a hard NON-COMMIT: the guard runs inside `lower_parse_result`, before an intent exists, so nothing downstream can un-hold a catch. **Still true and still unfixed, now proven:** `integrate_note` and `emr_parse` both fan out from one `note.ingested` on a health `Records` note and each ends in the whole-note settle, so the loser's facts are RETRACTED rather than merely written late — `integrate_note` running after `emr_parse` retracts EVERY fact the parse wrote, held by the strict-xfail `test_the_generic_integrator_does_not_retract_the_emr_parse_it_races`, which flips green the day it is fixed. It predates W4 (it is the shipped `apply_intent`, on both sides) and W4's EMR half fixed only the collision BETWEEN EMR sources. Deciding who owns a note's settle is nobody's yet: the intake half touched no settle path, so with both halves merged it is still open and still held by that xfail. **The two W4 narrowings compose, and the narrower wins.** A note can satisfy both predicates — an approved intake submission enacting into a health `Records` note with an EMR-shaped attachment is third-party-bodied AND importer-owned — so `narrow_for_third_party_note` INTERSECTS where `narrow_for_emr` SUBTRACTS, and the two commute: such a note's conversation holds `find_entity` / `read_entity` / `current_time` and nothing else, on both turns, with neither `ask_owner` nor a write verb bound in the worker's registry either. Prior: **The note conversation now WRITES the graph** (`agent/graphwritetools.py`, W3 of `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md`): the `note_ingest` persona holds `resolve_entity` (batch ≤12) and `assert_fact` (batch ≤8), plus `find_entity`/`read_entity`/`current_time` inherited unchanged — and nothing else, enforced by two locks rather than by the prompt (the closed `AgentProfile` allowlist and `toolregistry.NEVER_DEFAULT`). A third — the chat registry dropping both sidecars outright — was removed in the same wave, because the owner's reply turn IS a chat turn and without them it could discuss a correction and not record one. Both tools write through W1's `commit_facts`, so `supersession.decide()`, the domain floor, the ratchet, the citation anchor and the mention spine are byte-for-byte the shipped ones; the tools add no second write path, and `decide()` never becomes a model-facing verb. The batch shape is measured, not guessed (`evals/shape_probe.py`: 20/20 well-formed, 7.6 facts and 8.9 entities per turn against 1.0 for one-per-call). `assert_fact` carries no `domain`, `inferred`, `supersedes` or `correction` field and no JSON-Schema `enum` anywhere; `quote` is REQUIRED and checked against the note — an unattested quote still commits (Lever A) but at the 0.4 inferred-overwrite ceiling, so it cannot silently rewrite a stated value. Each element commits inside its own SAVEPOINT, so one bad element cannot undo the good ones. What the write path did unasked (replaced-and-kept-as-history, already-recorded, `held` with `decide()`'s own reason) is the tool's result text, and the rows it wrote are reported structurally — which is what finally fills `note_conversation_tool_calls.fact_ids` for the UNATTENDED pass — the `touched` set the whole-note settle sweep reads, and W2 had to ship empty. The owner's reply turn ran on `/chat`, which recorded into that ledger nowhere — closed since (see the head of this line). `reads_knowledge_base` flipped to True for the persona (constraint 2: the conversation reads narrowed to `(note_domain, 'general')`, computed from the note by `converse.note_read_scopes` on every turn rather than read back from the session row), and the ungated `POST /sessions/{id}/scope` is now closed against engine-opened personas. `domain_floor` matches separator- and case-insensitively with a dotted-base fallback, because the agent's only lever on a fact's domain is the predicate it spells. Prior: **The note conversation runs** (`note_converse`, `analysis/converse.py`): an ingested note now also opens an ordinary agent session under the `note_ingest` persona, reads the note as turn 0, and settles `settled` / `failed` — `failed` too for a turn that did not end cleanly, because the whole-note sweep must never run on a pass that asserted only a prefix. It is seeded onto `note.ingested` **beside** the shipped extraction pipeline, never instead of it, so the graph is written exactly as before and the thread is additive; the cost is one extra `agent.turn` per `note.ingested` EVENT — not per note: a re-ingest (an attachment landing on an already-ingested note, or a D6 clarification) is charged again in its own second thread, while a corpus rebuild is free because `backfill_pending_integration` enqueues `integrate_note` directly and emits no event — producing no graph writes at all in this wave, which is the accepted trade for the thread existing. The thread is VISIBLE: `note_ingest` is listed on the PWA's Full Brain tab (listed, not landed on — the tab still opens the curator), and it is not startable by hand (`ENGINE_ONLY_PERSONAS` keeps it out of `OWNER_AGENTS`, so the session and task routes refuse it while the two `agent` CHECKs still admit what the engine stores). A pass stranded `running` by a killed worker no longer takes its note out of the pipeline forever: `live_for_note` reclaims one older than `STALE_CONVERSATION` to `failed` before it reads (`queue.claim`'s stale-lock shape), never a `waiting_on_owner` thread, and the runner bounds its own turn by `NOTE_TURN_WALL_CLOCK`, which that horizon is derived from. `settled` is claimed only AFTER the transcript and ledger land, because W3 hangs the whole-note retraction off it and an empty ledger under `settled` would arm one. Turn 0 is the note **fenced as DATA** (the `intake/turn.py` recipient-frame pattern) — a note body can be third-party text and the persona will hold graph writes later, so the boundary goes in while it holds none. Every tool call the turn makes is recorded into the ledger and bound to its assistant turn, though nothing can call one yet: the allowlist and the executor's tool registry are both empty. One live conversation per note, enforced by the partial unique index with a graceful dispatcher skip in front of it so a re-delivered event is a logged no-op rather than a failed job. Also: **A re-ingest no longer destroys what points at a note's chunks** (`jbrain.ingest.carryover`). `ingest_note` deleted every chunk of the note and inserted fresh ones, and five tables hang off `app.chunks.id` — two of them ON DELETE CASCADE. So every re-ingest silently deleted the note's `entity_mentions` (`link_method='human'` rows included, and the id arrays a review reopen replays) and the `wiki_citations` of an already PUBLISHED revision, and blanked `facts.chunk_id`/`temporal_tokens.chunk_id` until a later re-integration re-anchored them. A cascaded citation cannot be re-anchored afterwards — the row is gone — so the repair runs while both generations of chunk exist: a rebuilt chunk KEEPS ITS ROW (and its embedding, and its `resolution_pin`s) when it comes back byte-identical, and otherwise hands its references to a surviving chunk that covers its span and holds the same characters there, shifting the chunk-relative mention spans by exactly the offset difference. Keeping the embedding has a price, and it is paid elsewhere: it keeps the row's `embedding_model` too, and `embed_note` only fills NULLs, so destroy-and-rebuild was quietly the ONLY thing that ever re-embedded a note's chunks after an embed-model change — a note edited or given an attachment healed itself, and now it does not. `chunks_embedding_idx` would therefore accumulate mixed-model vectors that only a genuinely rewritten body ever cleared, with no PWA or debug path to fix it. So `app.chunks` is now a target of the nightly `reembed_stale` sweep (`analysis/reembed.py`), which is where the re-embed-after-a-model-change path for chunks lives: it takes the rows that ARE embedded under a model that is no longer ours (NULL embeddings stay with `reconcile_unembedded_notes`, which re-enqueues `embed_note` for them every 300s), and it gets EIGHT batches a run rather than the one the small tables get, because chunks is the largest embedded table by an order of magnitude and one batch a night would take months to drain a model swap. It runs under SYSTEM_CTX (so it crosses every domain, exactly as ingest and `embed_note` already do — no scope is widened to make the sweep possible), and it needs no terminal: 0066 seeds the SCHEDULE disabled (the live box has it on — `docs/reference/MODEL_ACCESS_INVENTORY.md`) but seeds the TRIGGER `manual` and enabled, so Ops → Automations can Run-now it today and its toggle arms the nightly run. A rewritten body or a note that moved domain matches neither rule and behaves as before; `resolution_pin` rides the identical case only, because `chunk_id` is in its primary key and its `occurrence_index` is chunk-relative. Also: **Note clarification blocks** (D6/D7 of `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md`, migration 0193): a note keeps the body its author wrote and gains appended, timestamped answers in `app.note_clarifications`, composed onto the note's text at read time (`jbrain.notes.compose`) rather than written into `notes.body`, so an owner edit cannot destroy them and the composed text is what the chunker sees — which is what gives a fact drawn from an answer a chunk to cite. Precisely which offsets appending protects: `app.chunks.char_start`/`char_end` and the CHUNK-RELATIVE `entity_mentions` spans `_locate` derives from them. `app.facts` has no span columns at all; it cites a chunk by id, and the carry-over above is what protects that. The editor round trip is an EXACT-SUFFIX strip: `update_note` reconstructs what the note's own rows compose to and removes only that, refusing the PATCH (409, nothing written) when the text does not end in it. Cutting at the first `[clarification ` marker instead — the first cut — silently and permanently truncated any body that CONTAINED that literal, which ordinary prose does. A block's `domain_code` must equal its note's, enforced by a `SECURITY DEFINER` trigger on the 0045 subsection pattern: the RLS policy validates only the domain the writer names, and the FK bypasses RLS, so a general-scoped token could otherwise stamp a health note's clarification `general`. Appending re-drives ingestion from inside the append's own transaction, which makes the method owner-only (`app.jobs` is `is_owner()`). The privacy purge takes the blocks; the rebuild sweep keeps them (the fifth of its exemptions), because a rebuild re-derives FROM the notes. Also: **Note conversations** (`app.note_conversations` + `app.note_conversation_tool_calls`, `models/note_conversation.py`): the durable spine of the agent-conversation ingest (`docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` W2). A note conversation is an ordinary `agent_sessions` row plus an owner-only side table carrying its lifecycle (`running` / `waiting_on_owner` / `settled` / `failed`, at most one of the first two live per note) and the sha of the body it read; beside it a per-tool-call ledger records what each write CLAIMED — the entity/fact ids and domain names — which is the whole-conversation `touched`/`projected` accumulator the settle sweep needs and the D3 chip renders. Neither table is the firewall: that stays the RLS on the tables actually written. **Note deletion now purges the conversation WHOLE** — the `agent_sessions` row, not merely the side row, since the transcript holds the note's body and the owner's answers — and the corpus rebuild spares it, one of the five exemptions, alongside agent episodes, because no re-derive from the notes can reconstruct what the owner replied. Also: **A stale entity id now resolves through the fold, not around it.** `_resolve_from_intent` loaded the Integrator's `existing` entity by id with no status filter, so a re-analysis that echoed the loser's id back resolved a surface onto a merge tombstone and minted live facts and a live mention on a `status='merged'` row — silently un-doing the merge, needing no rebuild to fire. `entities.live_entity_by_id` now follows `merged_into_id` to the survivor (a fold does not re-point the tombstones already aimed at its loser, so an `a -> b -> c` chain is chased to its end, bounded), and withholds the resolution when the chain ends nowhere live. Prior: The corpus **entity-graph rebuild sweep** (`jbrain.analysis.rebuild`, the `graph_rebuild` action): re-derive the whole graph from the notes while KEEPING them, the acceptance/rollback instrument Ops → Reset could never be. It reused the purge's destructive half with three exemptions then — the facts a human verdict rests on survive (the pin, the chain below it, every fact named by a review item that outlives the purge, and every fact or entity MENTION that item's recorded effects will replay by id (the purge spares those ids, and the re-analysis in between re-asserts them on the survivor rather than on the tombstone, so the replay holds end to end), which no chain walk and no payload key reaches), only OPEN review items are retired, agent episodes are untouched — is resumable from a durable cursor one transaction per note, and chains into a three-job wiki repair, prune then rebuild then refresh (citations are ON DELETE SET NULL and article entity refs have no FK). "Whole graph" includes the deterministic half: an EMR note's `emr_parse` is re-enqueued alongside its re-integration, since a purge takes both producers' facts and only the generic one had a re-drive path. `decide()` now also refuses to resurrect a retracted row when a pinned head sits beside it OR the resolution's own recorded `retracted` effect names it — so a rebuild can neither re-litigate a settled decision into a fresh collision card nor quietly put a rejected value back. Fired from Ops → Automations, never scheduled to start itself. Also: the attachment settle window now measures from the server's `notes.received_at`, not the client's `created_at`, so an offline-flushed note with a promised attachment no longer arrives past its own window. Also: the write path split in two: `commit_facts` writes one pass of a note's facts, `settle_note` runs everything whole-note (the retraction and card sweeps, the projections, the `NoteAnalysis` stamp) and takes the touched-fact and touched-entity sets as explicit inputs, so a caller that commits over several passes settles their union once. Mentions became an incremental upsert keyed on (chunk, span, entity) plus a reconcile, replacing a wipe-and-reinsert that a second pass would have undone. And the re-extraction refresh path now re-anchors a fact's `chunk_id`: a re-ingest deletes the note's chunks and `facts.chunk_id` is ON DELETE SET NULL, so refreshed facts were silently dropping out of their wiki articles. Also: the entity fold is now a full-owner-only write: `merge_entity_pair` and the un-merge in `_reverse_effects` refuse a domain-narrowed session before their first statement, which is the guarantee; the `app.entities` trigger (INSERT and UPDATE) is a partial backstop that goes blind when the loser row is itself out of scope, because a row trigger never fires for a row RLS filtered out of the scan. A narrowed fold used to tombstone an entity and silently repoint only the facts that session could see. Prior: LLM token accounting: the AI usage card gained an all-time lifetime total (full-ledger `SUM` of the append-only `llm_usage`, unbounded by the fetch window), and today/month buckets now roll over at the owner's local midnight (SQL `AT TIME ZONE` against `owner_timezone`, degrading to UTC when unset) instead of UTC. The centralized recorder (`LlmRouter._record` → `SqlUsageRecorder`) remains the single chokepoint every production LLM call passes through. Prior: two ingestion-robustness fixes for note-plus-image capture. (1) The capture-race gate: `POST /notes` carries an `attachments_expected` count (migration 0154) so ingest and the integration reconciler defer integration until the promised attachments land (bounded by a settle window), preventing a premature body-only pass when the image uploads after the note. (2) Per-source extraction: the note body and each attachment now extract in separate `note.extract` calls (`prompt.group_texts_by_source`) so a content-rich attachment can't crowd the body's own facts out of a shared budget (the note losing its "car loan for the Kia" edges once the card image's OCR was present). Prior: per-kind conflict policy + commit-vs-review for Ingest V2 Levers A/B; same-name guard on the agent's own `existing` resolution.

Binding reference for Phases 2–3 (and the Phase 6 wiki's inputs). Produced
from the owner's workflow concept plus a red-team and design review; owner
decisions are marked **[decided]**.

## The workflow

```
capture (Phase 1)
  → chunk + embed + FTS            (local, no LLM — searchable within seconds)
  → extraction call                (one strong-model structured call:
                                    title, tags, facts[], entity mentions,
                                    temporal resolution)
  → integration call               (the Integrator agent reads the extraction +
                                    retrieved graph context, emitting an
                                    IntegrationIntent: entity resolutions, fact
                                    judgments, supersession/merge proposals —
                                    the agent decides MEANING)
  → arbiter (plan_intent)          (deterministic: validate the intent, weigh
                                    each fact, partition commit / review / reject;
                                    cross-subject + ambiguous force review)
  → apply (apply_intent)           (deterministic write through commit_facts:
                                    domain floor/ratchet + per-domain derived
                                    chunks, entity linking [agent resolution,
                                    deterministic resolver as fallback], per-kind
                                    supersession, + review-inbox items; then
                                    settle_note for the whole-note sweeps,
                                    projections and the analysis stamp.
                                    apply_intent IS commit_intent + settle_note;
                                    a caller with SEVERAL intents for one note
                                    calls commit_intent per intent and settles
                                    their union once — see the EMR line below,
                                    because settling per intent retracts the
                                    intents before it)

and the deterministic EMR half, which writes through the same seam and is the
one producer the model has no verb for (W4/D9) —
  note.ingested (health Records)
  → emr_parse                      (extract/OCR each decrypted PDF, fingerprint
                                    it to its parser, reconcile OCR reprints)
  → EmrNoteCommit.commit_source    (per source: Layer-2 firewall inside
                                    lower_parse_result, then plan_intent and
                                    commit_intent in that source's own
                                    transaction, carrying fhir_status)
  → EmrNoteCommit.settle           (ONCE, over every source's union)
nightly: entity hygiene, merge proposals, stale-model re-embedding
         (summaries AND note chunks — see reembed_stale),
         tag consolidation; (Phase 6) wiki triage, wiki_lint health sweep

beside it (AGENT_INGEST_CONVERSATION_PLAN.md, W3): the note conversation now
writes the graph THROUGH TOOLS, and through the SAME deterministic core —
  note.ingested
  → note_converse                  (the note as turn 0 of an ordinary agent
                                    conversation, `note_ingest` persona)
  → resolve_entity (batch ≤12)     (the shipped layered resolver + the mention
                                    spine, via commit_facts with no facts;
                                    the ONLY minting path, and the only source
                                    of the `e1`/`e2` handles a fact may name)
  → close_reading (batch ≤8)       (the WHOLE-note reading: title, tags and
                                    every fact. One Extraction per element
                                    through commit_facts: the same domain
                                    floor/ratchet, the same decide(), the same
                                    citation anchor. Per-element SAVEPOINT, so a
                                    bad element cannot undo the good ones. The
                                    handler reads a repeating schedule out of
                                    each fact's attested quote and writes the
                                    temporal token that carries the RRULE)
  → assert_fact (batch ≤8)         (the same write, one fact at a time — what
                                    the owner's REPLY turn adds after the
                                    reading. Identical rows; what it cannot do
                                    is say what the note says NOW)
```

The tools add **no second write path**: `supersession.decide()` is never a
model-facing verb (plan constraint 5), there is no `domain` / `inferred` /
`supersedes` field on either write verb, and what the write path did that the model
did not ask for — a value replaced and kept as history, a duplicate recognised,
a clash `held` — comes back as the tool's RESULT TEXT, which is the model's only
window into `decide()`. Each written row is reported structurally as well, and
that is what fills `note_conversation_tool_calls.fact_ids` — the whole-conversation
`touched` set the settle sweep reads.

Capture-to-searchable never waits on a cloud LLM: embeddings/FTS index
immediately; facts and entities are async enrichment.

Chunking stores two overlapping granularities per source — **paragraph** (the
precise citation unit) and **section** (larger retrieval windows that contain
those paragraphs). Extraction reads **paragraph chunks only** **[decided]**:
sections exist for search/retrieval, and feeding both concatenated the body to
the model ~2x on any multi-paragraph note (wasted tokens, a salience drag on
the fact budget). Paragraph chunks tile every source with no overlap and keep
span anchoring on the citation unit.

## Facts

A fact is a **semi-structured statement with a structural identity**, not a
free-text blob:

- Identity: `(subject, entity, predicate, qualifier)` — this is what makes
  "same fact, new value" detectable and re-extraction upsertable.
- `statement` (canonical one-sentence rendering — embedded, cited, shown),
  `value_json` for structured payloads (measurements: value + unit).
- `predicate` is free text plus the kind enum below — no controlled
  ontology **[decided]** — but **schema.org-guided [decided]**: extraction
  prefers schema.org type and property names where they exist
  (`Person.birthDate`, `worksFor`, `address`), coining `snake_case`
  predicates otherwise. LLMs know the vocabulary cold, so every model and
  prompt version converges on the same names — which is what keeps the
  structural identity key matchable across re-extractions. Nightly
  consolidation normalizes drift *toward* schema.org as the attractor.
  Domain complements: FHIR's Observation/LOINC shapes the Phase 7 typed
  health records; iCalendar RRULE encodes `recurrence`-kind temporal
  tokens.
- Assertion status: `asserted | negated | hypothetical | reported |
  question` — the wiki demotes everything below `asserted`. "Doctor wants
  to rule out diabetes" is not a diabetes fact.
- Provenance: `note_id`, `chunk_id`, `extractor` (model id),
  `prompt_version`, `confidence`.

### The fact grammar: a property graph **[decided]**

Every fact is an **edge addressed as `entity.predicate[.qualifier]`**,
pointing at a value (`me.weight → 182 lb`) or another entity
(`me.employer → Acme`). The structural identity key IS the graph address,
and the supersession chain on that address IS the property's **full
revision history** — `me.weight` yields a time series, `me.address` an
interval history, `appointment.scheduled_time` a reschedule chain — every
link citing its source note. Nothing is deleted, ever — except when a source
note is deleted: notes are the sole sources of truth, so deletion purges
every derived artifact (facts, mentions, tokens, review items incl. resolved
history, provisional entities no surviving note references, and the note's
agent ingest conversations — whole, session and transcript included) and
repairs affected supersession chains **[decided]**. The note's own
**clarification blocks** go with them — not derived, but part of the note's
text. Delete = gone; the note row itself stays soft-deleted, which is why the
block, episode and conversation purges are all explicit statements rather than
FK cascades.

Entity-row fields (`canonical_name`, summary) are **denormalized
projections of current facts**: a name change is an `entity.name`
transition with history, not an overwrite. The same rule that made
appointments reschedule-safe applies to every property: identity is
stable; properties are supersedable bindings.

**Notes source facts; the entity graph arbitrates current truth [decided].**
"Sole source of truth" above is about *provenance* — every fact traces to a note
and dies with it. It does **not** mean a note's prose is the current truth: a
note records the world *as captured*, frozen, while **what holds now** is the
entity graph after supersession and the review inbox have run — a later note
supersedes an old value, a correction note retracts an error, a conflict waits
in review. The live value of `entity.predicate` is the active head of its chain,
not whatever any one note (old or new) happens to say. Consumers that read raw
notes — notably the assistant's retrieval tools — must reconcile against the
graph rather than quote note prose as current (docs/reference/ASSISTANT.md "Notes are the
source of facts; the entity graph is the arbiter of current truth").

### Fact kinds and supersession **[decided: per-kind policy]**

| kind | example | temporal | conflict policy |
|---|---|---|---|
| `event` | "saw Dr. Patel June 3" | `valid_from` = occurrence | **never auto-supersede** — immutable; a conflict is an extraction error → review. The newest *mention* of an old event is usually the least precise. |
| `measurement` | BP 120/80, weight | instant + `value_json` | **never** — time-series, accumulate; same metric+time disagreeing → review |
| `state` | address, employer | `valid_from`/`valid_to` | newest-wins eagerly: close old interval (SCD-2), **supersede silently with retained history** (Lever B). The old fact stays true *about its interval*. |
| `attribute` | birthday, blood type | timeless | **hold `pending_review`, never auto-supersede** — two birthdays is a bug, not news |
| `preference` | "prefers aisle seats" | from `reported_at` | newest-wins, **silent** (Lever B — treated as a state change); superseded ones stay agent-visible |
| `relationship` | Bob —works_at→ Acme | interval | supersede only for functional predicates (small allowlist: employer, spouse…), **silently with retained history** (Lever B); default accumulate |

Supersession compares **fact validity time, never note capture time** — a
retrospective note about 2019 must not supersede the current address.
"Newest" = latest `reported_at` *among facts about the same validity
period*.

**Disposition default (Lever B, `docs/plans/ENTITY_GRAPH_INGEST_V2_PLAN.md`).** A clean
*strictly-newer* `state`/`preference`/functional-`relationship` supersession now enacts
**silently** — the old interval is closed and chained (history is retained, never
destroyed), and **no `fact_conflict` review card is filed**. A card is still filed when the
supersession is not clean-and-newer: a same-instant clash, a blocked/derived-defers-primary
case, or an `attribute` collision (which never auto-supersedes). This flipped the earlier
"every supersession flags review" default — the review inbox now surfaces genuine conflicts,
not routine value changes.

**Commit-vs-review (Lever A).** A fact **commits by default**, including an *inferred* one;
the retired weight-ceiling gate no longer routes low-weight/inferred facts to review. A fact
is held `pending_review` only when the arbiter raises an explicit reason — a safety flag
(cross-subject, ambiguous entity, attribute collision, low-confidence supersede guard) or the
**I5 sensitive-inference net**: an *inferred* fact on a deterministically floored-sensitive
predicate (health/finance/precise-location, e.g. an inferred `mood`/`diagnosis`) is held so a
sensitive value the note never literally stated can't commit silently. The fact's weight is
still computed and stored (the supersession low-confidence guard reads it); it is no longer a
review gate.

### Temporal model **[decided: always resolve to absolute]**

Bi-temporal: `valid_from`/`valid_to` (true in the world) vs `reported_at`
(= note capture time, client-side with timezone — the offline outbox means
server receipt time is wrong). Relative phrases are resolved at extraction
against the capture anchor and stored absolute with
`temporal_precision (instant|day|month|year|era|unknown)` plus the original
`temporal_phrase` for audit. "Last Tuesday" → a date; "when I was a kid" →
era-precision range; never store only-relative. Future-tense facts carry
`expected` status (they are not occurred events) and defer to the
appointments pipeline where applicable.

### Temporal tokens and appointment identity **[decided]**

Every resolved date/time expression is a first-class **temporal token** —
span-anchored like an entity mention: surface phrase, resolved absolute
value, `temporal_precision`, the capture anchor used, kind
(`point | range | recurrence`). Facts and structured records *reference*
tokens (keeping their own valid_from/to denormalized for query speed), so
every datetime in the system traces to the words that produced it and
re-resolution after an anchor correction is a targeted update.

**Appointments are entities with time as a binding, not identity.** An
appointment entity is stable; its scheduled time is a supersedable binding
to a temporal token (state-fact semantics: newest-wins + review flag,
full reschedule chain retained). "Dentist moved to Friday" = resolve the
mention to the existing appointment entity (candidate scope: upcoming
appointments; ambiguity → review inbox), mint a new token from the new
note, supersede the binding. The calendar/ICS feed reads the current
binding; the entity, its facts, and its citations survive any number of
reschedules. Past-tense references convert `expected` → `occurred`.

## Entities

- `entities` carry `kind`, `canonical_name`, summary + embedding, and
  **`subject_id`** when the entity is also a security subject — "Mom" the
  entity and Mom the subject are one identity; fact→subject attribution is
  a security field. Cross-*subject* misattribution is treated as a leak.
- `kind` follows the same **schema.org guidance** as fact predicates:
  prefer schema.org type names (`Person`, `Organization`, `Place`,
  `Event`, `Product`…), coining `snake_case` kinds only where schema.org
  has no fit (e.g. `appointment` as a temporal-token-bound entity). Same
  rationale: models converge on the vocabulary, so kinds stay matchable
  across re-extractions.
- Every link is span-anchored via `entity_mentions` (surface text + chunk +
  offsets), so merges are reversible: merge = tombstone
  (`merged_into_id`) + repoint, un-merge = re-resolve mentions.
- **A stale id resolves through the fold.** A caller holding an entity id may be
  holding one the owner has since merged away: every context builder filters
  `status != 'merged'`, so the Integrator is handed live ids only — but a merge that
  lands while an analysis is in flight makes the id it echoes back a tombstone, and any
  replay of a stored decision (the persisted `resolution_pin` rows already carry entity
  ids) would make that the ordinary case rather than a race.
  `entities.live_entity_by_id` is the one loader for it: it follows `merged_into_id` to
  the survivor, chases an `a -> b -> c` chain (a fold leaves older tombstones pointing
  at a row that is now itself merged) to its end under a bound, and returns None — the
  caller's ordinary "can't resolve this" path — when the id is unknown, out of scope, or
  the chain ends nowhere live. Redirecting is what the merge decided; refusing is not
  the safe alternative, because the loser's aliases stay on the tombstone, so falling
  through to `_exact_matches` would mint the duplicate straight back.
- **An accepted merge card is enacted on the pair as it stands NOW.** A card is a
  proposal held open across arbitrary time, so both its ids can go stale under it:
  an overlapping card resolved first turns one side into a tombstone, and the
  ranking that chose the card's direction (`plan_merge`, at *filing* time) predates
  any later promotion to `confirmed` or link to a subject. `resolve_review`'s
  `merge_proposal` accept arm used to fold the payload verbatim, so two overlapping
  cards resolved in sequence repointed the second pair's rows onto a tombstone — a
  merge that silently undid the merge before it. `_accept_merge` re-derives the pair
  instead: the scope guard first (a narrowed session sees an RLS-filtered
  `app.entities`, so a fold chain can dead-end or resolve to the wrong survivor for
  want of an invisible tombstone — none of the reasoning below is trustworthy under
  one, and a narrowed accept is therefore a scope refusal, not a can't-resolve one);
  then each side through `live_entity_by_id`, the same fold-following loader
  `_resolve_from_intent` uses; then `are_distinct` on the LIVE pair; then
  `plan_merge`. Where the agent's `merge_entities` no-ops on a stale side, the inbox
  redirects, because the two callers answer different questions — `merge_entities`
  re-enacts a decision already carried out, while accepting a card asserts for the
  FIRST time that these two are one thing. Discarding that would leave the third
  duplicate live under a log that says "resolved"; refusing outright would leave the
  card acceptable by no verb but `reject`, which writes a false permanent
  `distinct_from`. Both sides landing on one live entity IS a no-op — the assertion
  is already true, so nothing is written, the card records `merge_noop`, and the
  reopen says it folded nothing to undo. A side resolving nowhere live, or a
  permanent `distinct_from` on the live pair (which outlives a reopen by doctrine,
  so the re-queued card must not be acceptable), refuses and leaves the card open.
  The recorded effect names the pair actually FOLDED, never the payload's, which is
  what keeps `_reverse_effects` — which un-merges by id — exact.
- **A fold needs a full-owner session, and fails closed without one.** Facts
  carry their own `domain_code`, so a `general` entity routinely owns `health`
  and `finance` facts; on a domain-narrowed session (`owner_scoped`) RLS filters
  the repoint UPDATEs to the visible rows, tombstoning an entity while half its
  facts stay bolted to it. Nothing inside that session can notice —
  `RETURNING` also returns only visible rows, so counting the leftovers needs
  exactly the cross-domain read the narrowing forbids, and the tombstone UPDATE
  runs first, so it can match zero rows while the repoints partly succeed. There
  is no in-scope evidence a fold is safe, so `merge_entity_pair` (and the
  un-merge in `_reverse_effects`) refuses a narrowed session *before* its first
  statement, and the escalation becomes the caller's visible choice. That session
  guard is the guarantee.
- **The `app.entities` trigger is defence in depth, not the guarantee.** It refuses
  the merge tombstone (on INSERT as well as UPDATE) from a narrowed session, and is
  deliberately not `SECURITY DEFINER` — it reads nothing and bypasses no policy, so
  it adds no RLS-bypassing primitive a model-facing tool could reach. But a `BEFORE
  ... FOR EACH ROW` trigger only fires for rows the statement matched, and RLS
  filters the scan first: fold a `health` entity from a `general`-narrowed session
  and the tombstone matches zero rows, raises nothing, and the repoints still move
  every fact that session can see. The table cannot police the shape where the
  *loser row itself* is out of scope — only the session guard can, because it asks
  about the session rather than about a row.
- Auto-merge only on exact alias + same kind; everything else is a
  review-inbox proposal. Bare first names never auto-merge without
  co-mention signals. New entities are `provisional` until implicitly
  confirmed.
- Relational aliases ("Mom", "the Honda") live in `entity_aliases` —
  unambiguous in a single-owner corpus.
- **Relationship object binding is deterministic [decided: a pipeline net, not
  the model's job]**: a `relationship` fact's value IS its object node, so the
  property must render the object entity's NAME, never the statement sentence
  it's buried in (`spouse → "I have a wife Celine Hopkins."` is the bug). The
  model sets `object_entity_ref` inconsistently — sometimes naming the object,
  sometimes folding it into the statement, sometimes near-missing the mention —
  and that run-to-run flip swings an edge between linked and unlinked across
  re-extractions. `link_relationship_objects` makes the binding a pure function
  of `(mentions, fact)`: a near-miss ref (case/possessive drift) snaps to its
  mention; a dropped ref is recovered from `value_json` or the single
  non-subject mention the statement names. The object only ever binds to a
  mention the model ALREADY emitted — never a minted entity — so it cannot
  hallucinate a person (the risk that ruled out auto-minting). Notes written
  before the net are re-analyzed by a self-limiting startup backfill keyed on
  unlinked primary relationship edges.
- **Duplicate-fact collapse is deterministic [decided: an arbiter net, like the
  extraction's]**: `extraction.dedup_facts` collapses same-key restatements
  within an extraction, but the Integrator re-emits facts with no equivalent
  pass — so a note listing two medications in one sentence ("lisinopril 10 mg and
  hydrochlorothiazide 12.5 mg daily") can come back with one drug DUPLICATED, and
  the duplicate is the DEGENERATE one: the good copy binds the drug as its OBJECT
  entity (`Me.medication -> hydrochlorothiazide`, which `_object_named` grounds
  because the drug name is verbatim), while the spurious twin DROPS the object and
  folds the drug into a free-text statement. Left alone the arbiter commits the
  object-bearing copy active and holds the object-less twin for review (no object
  to ground, a paraphrased statement), so the owner sees a review card for a fact
  already on the graph. `arbiter.dedup_intent_facts` (run after predicate
  canonicalization, before the weight signals) groups facts on a base key that
  EXCLUDES the object AND the statement — entity.predicate.qualifier, assertion,
  value_json — then within a group an object-less copy is SUBSUMED by any
  object-bearing sibling and dropped, while edges to DIFFERENT objects (enumerated
  children, two distinct medications) all survive even when their statements
  coincide. The surviving copy is the one the arbiter would have committed
  (grounded, not inferred), never the drifted twin. **The statement is out of the
  key on purpose**: a prose-valued attribute puts its value in the SENTENCE and
  leaves value_json null (an `address` the model rendered nine ways — "the address
  should be…", "corrected address:…", "account address set to…"), so keying on
  statement would fragment one value into nine groups and file an
  attribute_collision card per paraphrase (the account-address explosion). Value
  distinction rides value_json (the datum note.extract requires for every non-edge
  fact) and the object node, never the free-text rendering.
- **Domain placement [decided: inherit + promote]**: an entity inherits the
  domain of the note that created it; a later mention from a *less*
  restrictive domain proposes promotion via the review inbox. Facts always
  carry their own domains regardless of their entity's domain.

### First person and the owner **[decided]**

Unattributed first person resolves to the **note's author-subject**: in the
owner's notes, "my BP" is the owner's; in a Phase-7 intake session, "I had my
gallbladder out" is that subject's. This is a resolution *rule* keyed to
note authorship — pronouns are never stored as aliases. The owner exists as
a canonical **"Me" entity** hard-linked to the owner subject row, the
implicit center of the graph **[decided]**. Quoted or relayed first person
("Mom says: I take lisinopril") attributes to the speaker with
`assertion=reported`; the default applies only to genuinely unattributed
statements.

### Alias resolution & separation **[decided]**

Resolution layers, cheapest first: exact alias match (case/diacritic
insensitive) → embedding similarity vs entity name+summary → batched cheap
LLM disambiguation with candidates → review inbox for the gray zone.

- **Bare first names [decided: auto-link + retro-recheck]**: if exactly one
  matching entity exists, mentions auto-link; the moment a second entity
  with the same name appears, all prior auto-linked mentions of that name
  are flagged for retroactive re-review. Low friction, self-correcting.
- **Declared names [decided: self-naming fact → exact alias]**: an asserted
  naming fact ("my full name is Jeffrey Mark Hopkins", `name`/`fullName`/
  `givenName`/`alternateName`/`nickname`/…) registers its value as an exact
  alias on the fact's entity, so a later bare "Jeffrey Mark Hopkins" resolves
  to the owner instead of forking a new person. Only `asserted` facts (a
  reported/negated/hypothetical name is not a declaration), and only when the
  name does not already key a *different* live entity. That collision is the
  high-confidence same-person signal: instead of widening one name across two
  entities (the wrong silent link), it **files a `merge_proposal`** directed by
  `plan_merge` so the more-anchored side (a subject/the owner, then confirmed,
  then older) survives — gated by the `distinct_from` edge so a rejected merge
  is never re-proposed, and deduped to one open card per pair across
  re-analysis. The alias itself inherits the entity's firewall partition and is
  append-only (a corrected name adds an alias; it never silently rewrites
  identity). Nicknames the note never states ("Jeff" with only "Jeffrey Mark
  Hopkins" declared) still need their own declaration or a human merge —
  declaration-driven, never guessed.
- **Role references [decided: via relationship facts]**: "my dentist" /
  "my boss" resolve through the relationship fact (`dentist_of`,
  `employer`) **valid at the note's time** — never static aliases, so a
  provider or job change can't silently misattribute later notes. No such
  fact at that time → review inbox. Kinship terms ("Mom") remain ordinary
  stable aliases.
- **Negative knowledge**: rejecting a merge proposal writes a permanent
  `distinct_from` edge — never re-proposed, and a hard constraint for the
  disambiguator. Rejections teach as much as confirmations.
- **Split detection**: conflicting `attribute` facts on one entity (two
  birthdays) are evidence of a hidden two-people merge — the system
  proposes a **split**, not a supersession; mention-level provenance makes
  the split a re-resolution of spans, not archaeology.
- **Same-name coexistence [decided: rejected — conservative collision wins].**
  Letting two live entities share a normalized name, auto-distinguished by
  context, was evaluated (multi-agent research + red-team) and rejected for
  this single-user system. The exact-match gate (`entities.resolve_entity`:
  one match auto-links, 2+ → one deduped `ambiguous_mention` card) already
  satisfies the intent — "don't misattribute when two same-named people
  exist" — more safely than coexistence would. The **agent's own `existing`
  resolution is held to this same gate**: `_resolve_from_intent` withholds a
  same-name `existing` override (surface matching 2+ live entities) so it falls
  through to the deterministic resolver and its collision card — the agent can
  never commit a per-run bare-name guess the resolver itself would refuse
  (integrate-v13 pairs a mechanical-counting prompt with this engine backstop).
  Coexistence is a net loss
  here: re-analysis rebuilds mentions wholesale with no pinned routing, so
  resolving a bare name through the LLM would be **non-deterministic across
  re-runs** (a silent flip — the one outcome no layer may produce); the
  "retro-recheck" of bare first names would fan one new common name out into
  a review card per historical mention (worse than the single deduped card);
  feeding `distinct_from` to the per-mention disambiguator is a no-op (it
  picks one entity per mention — `distinct_from` only constrains *merges*,
  where it is already enforced); and `_exact_matches` being domain-blind is
  load-bearing, not a leak (declared-name collision needs cross-domain
  visibility). The only correction worth building is the **human-initiated
  split** (above) — never an auto-minted second entity. So "bare-first-name
  retro-recheck" and "layer-3 `distinct_from`" are moot *by design*; the
  auto-link half of the bare-first-names rule stands, the retro-recheck half
  is superseded by the collision card.

## Domains and the firewall

- Every fact, entity, mention, and derived chunk carries a domain and sits
  under the standard `has_domain_scope` RLS policy.
- **Mixed-domain notes [decided: split]**: analysis derives per-domain
  chunks from a mixed note; citations always point at a chunk in the
  *fact's own domain*, so no citation ever crosses the firewall and the
  RLS test for it is straightforward. The original note remains the source
  of truth in its capture domain; derived chunks reference their spans.
- Classification bias is asymmetric: misclassifying *into* health/finance
  is cheap; *out of* them is a leak. Domain can ratchet **up** without
  review, never down. Health/finance keywords block `general` assignment
  without review. Titles and tags are generated per-domain-content so a
  note list never leaks a sensitive auto-title.
- **The floor is matched on the predicate's MEANING, not its spelling.**
  `domain_floor` keys ~45 clearly-sensitive predicates to the domain they
  force, and its table is written in the canonical camelCase the
  `note.extract` prompt teaches. That was sufficient while the prompt was the
  only writer; it is not now that the note-conversation agent writes
  predicates through `assert_fact`, where the natural spelling is snake_case.
  So the lookup strips separators and case (`blood_pressure`,
  `Blood Pressure` and `bloodPressure` are one predicate) and a dotted path
  falls back to its base segment (`bloodPressure.systolic` floors as
  `bloodPressure` does). Both rules only ever ADD a floor. This is what
  D18 of `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` actually rests on:
  the agent has no `domain` field and never will (the firewall red-team
  rule) — it chooses a fact's domain only by choosing its PREDICATE, so the
  predicate lookup is the thing that has to hold, and a novel predicate the
  registry has never seen lands in the note's own domain, ratcheted.

## Privacy routing **[decided: cloud for everything, for now]**

All domains may use cloud LLMs (Anthropic/xAI) during development — recorded
as an explicit opt-in in config, not an accident. The LLM adapter's task
profiles carry a routing axis from day one so the end-state — **everything
local once a GPU lands** — is a config flip, not a refactor. Until then the
docs must not claim the domain firewall is a network-privacy boundary: the
adapter is the egress point. Intake-link subjects' data (Phase 7) re-raises
this decision explicitly before launch.

**Runtime routing overrides.** Per-task provider selection and (for Grok)
reasoning effort are runtime-editable from the settings screen, persisted in
`app.settings` under `llm_task_overrides` (task → `{spec, reasoning_effort}`).
The router reads this map on every call and merges it at the **highest**
precedence — **DB override > env pin (`JBRAIN_LLM_TASKS`) > strength tier >
task default** — so the screen is the live control surface and takes effect
without a restart. `reasoning_effort` (`none|low|medium|high`, xAI's own
default when unset is `low`) is sent **only** to the xAI/Grok provider; it is
never sent to `local` (an OpenAI-compatible server would reject the field) nor
to Anthropic (which steers reasoning via a separate `thinking` mechanism, out
of scope here). A malformed stored override is ignored on read — bad saved
config must never break an LLM call. Exposed via `GET`/`PUT /api/settings/llm`.

## Reprocessing and corrections

- Re-extraction (model/prompt upgrade) **upserts on the structural identity
  key**: same key → update rendering in place, re-anchoring the citation on
  the note's current chunks; key gone → `retracted_by_reextraction` (not a
  conflict, no inbox noise); new key → insert. `prompt_version` makes corpus
  re-runs a planned, budgeted migration. Re-anchoring is not cosmetic: a
  re-ingest *replaces* the note's chunks and `facts.chunk_id` is
  `ON DELETE SET NULL`, so a refresh that left it alone would strand the fact
  with no citation — and the wiki builder INNER JOINs chunks, so the article
  would rebuild without it, silently. (Since `jbrain.ingest.carryover`, a chunk
  that comes back unchanged keeps its row and a replaced one hands its
  references over, so most re-ingests no longer null anything. This re-anchor
  is still the backstop for the cases carry-over declines — a rewritten body, a
  domain move — and for a fact whose identity key moved between chunks.)
  **Every** in-place path re-anchors — the
  refresh, the interval close, the held-row refresh, and a relationship's
  derived shadow, which nothing else would ever re-link because the retraction
  sweep deliberately skips derived rows. Only a fact THIS note owns is
  re-anchored; an in-place update landing on another note's fact leaves that
  note's citation to its own re-integration.
- The note's **mentions** are upserted incrementally, keyed on (chunk, span,
  entity) — not (chunk, span), which is not unique: `_locate` anchors every
  surface it cannot find at the same zero-width span, and two mentions may share
  one surface. The reconcile that drops what is no longer asserted runs in
  `settle_note`, over the union of every pass's ids, for the same reason the
  fact sweep does. A re-asserted row keeps its id, so re-analysis no longer
  churns the co-mention spine, an un-merge can still replay stored
  `mention_ids` across an intervening re-analysis, and `created_at` is now
  first-link time rather than last-re-analysis time (which changes the
  entity page's mention ordering to a stable one). A re-run that re-asserts
  the same mentions writes nothing at all, so it no longer re-dirties every
  mentioned entity's article — `confidence` is compared with a tolerance to
  make that true, since the column is `real` and a resolver's float64 never
  round-trips exactly.
- **Re-run = the same incremental pass [decided]**, on demand via
  `POST /api/notes/{id}/analyze` (202 + job id, a plain `integrate_note` job;
  409 while an analysis is already queued/running, or while ingest/OCR will
  run one anyway — the gate owns that sequencing). The retraction sweep
  carries two repairs so a re-run leaves a coherent graph: a retracted fact
  must not keep another fact superseded — survivors re-attach to the first
  non-retracted transitive supersessor or are restored (active, link
  cleared, SCD-2 close reopened when it came from the retracted fact), the
  same chain repair note deletion runs — and **open** review cards
  referencing a retracted fact, plus open ambiguous-mention cards for names
  the re-extraction no longer references, are retired. Resolved and
  dismissed items are human history and survive any re-run; pinned facts
  never enter the sweep at all. **Full unwind-on-re-run was rejected
  [decided]**: purge stays a deletion-only privilege of note deletion,
  because tearing the note's artifacts down to replay them would discard
  exactly what incremental repair preserves — pins, resolution history, and
  the cross-note supersession evidence other notes' facts hang off.
- **The corpus rebuild sweep is the one exception to that, and it is an
  operator instrument, not a re-run path.** `jbrain.analysis.rebuild` re-derives
  the WHOLE graph from the notes while keeping the notes — the acceptance check
  on a pipeline change, and the rollback lever, that Ops → Reset (which drops
  the schema and takes the notes with it) could never be. It reuses the purge's
  destructive half with five exemptions that answer two of the rejection's
  three worries: **the facts a human verdict rests on survive** — the pinned
  row, the chain it superseded, and every fact a review item that OUTLIVES the
  purge names, which no supersession walk can reach because resolving a card
  writes no chain edge at all (it pins the winner and retracts the loser, and
  a rejected `low_confidence_inference` only retracts); **only open review
  items are retired**, so resolved, dismissed and *deferred* history stands —
  and it stands with its facts, since a card whose payload was purged is a
  dangling pointer whose reopen silently no-ops; agent episodes are untouched,
  since nothing re-derives them; **note conversations** are untouched for the
  same reason and one stronger, in that a thread holds the owner's answers to
  the agent's clarification questions, which no re-derive from the notes can
  reconstruct, and destroying one would orphan a question still waiting in the
  notes inbox; and the notes' **clarification blocks** are untouched, since
  they are source the owner typed and the whole premise of the sweep is
  re-deriving *from* the notes — a rebuild that took them would break
  re-derivability corpus-wide and silently. Both halves of "a review item that
  outlives the purge" are **derived, never enumerated**: the statuses are the
  complement of the one status the purge deletes, and the payload keys are the
  single list (`fact_id`, `fact_a`, `fact_b`, `source_fact_id`) that the
  card-delete reads too — taken kind by kind from every kind the `review_items`
  CHECK admits (including `inverse_proposal`, which is filed outside
  `decide()`'s `review_kind` and names its fact by `source_fact_id`), so a
  `fact_id` kind or a parked card cannot be missed the way an
  enumerated-from-the-bug-report list missed both.
  **A card's payload is not the only way it names a row.** A settled resolution
  records the ids it MOVED in `resolution->'effects'` — `mention_ids`,
  `fact_ids`, `object_fact_ids` for a merge fold, `fact_ids` for a predicate
  remap — and a reopen replays exactly those ids, one UPDATE each. A
  `merge_proposal` payload holds two ENTITY ids and nothing else, so a
  payload-only spare set spares none of them: the reopen would restore the
  entity row (from the recorded prior status, a value) while moving zero
  mentions and zero facts. A half un-merge, silent, and worse than a clean
  no-op. So those id arrays are a second spare arm, and the entity-mention wipe
  is conditional on it for the same reason the fact delete is — a replay finds
  rows by id or not at all. (The purge is that promise's first half; whether a
  later re-analysis of the note preserves the mention ids it re-asserts is the
  mention writer's own contract.)
  Sparing is only half of what
  keeps a settled decision settled: `decide()` filters retracted rows out of
  its live set, so it also takes a **retracted twin as a re-extraction** and
  refreshes it in place, rather than inserting a fresh active twin — without
  which one rebuild files one collision card per settled decision, corpus-wide.
  Two things can make a retracted row a settled verdict rather than the
  machine's own `retracted_by_reextraction` (which must still resurrect when
  its key comes back): a **pinned head** beside it, or the resolution's own
  recorded **`{"action": "retracted"}` effect** naming it. The second arm is
  not redundant — a `low_confidence_inference` REJECT retracts and pins
  *nothing*, so a pinned-head-only guard silently re-mints the value the owner
  rejected as a fresh active row with no card filed. The effect is also the
  honest discriminator: it is the decision itself, recorded, not an inference
  from a neighbouring row. Either way the match is on value AND
  exact `valid_from`, so validity drift on re-extraction still falls through to
  the re-flag; that limit is inherent, not a gap in the spare set. The third
  worry stands unanswered and is
  inherent to a rebuild: **cross-note supersession chains that reach no pin are
  dissolved and re-derived.**
  **The re-derive drives BOTH producers.** A health `Records` note's facts come
  from the generic LLM extraction *and* from the deterministic EMR parsers
  (`emr_parse`), which are what turn a lab PDF into cited analyte readings; both
  fan out from one `note.ingested` at ingest, and only the first had a re-drive
  path. Re-queuing integration alone would hand back a rebuilt medical record
  holding only the LLM's read of it, silently — so the sweep re-enqueues
  `emr_parse` for every note still matching stage 2's markers, and the drain
  waits on that job too before chaining the wiki repair. It is enqueued
  directly rather than by re-emitting `note.ingested`, which would claim the
  chunks were rebuilt, give integration a second producer beside the sweep's own
  drain, and put stage 1 back in scope. No ordering is promised between the two
  passes and none is at ingest either; the Layer-2 location firewall is a
  property of the parser's own lowering, so re-driving restores it for exactly
  the facts it ever covered — it never guarded the generic extraction.
  It runs one transaction per note from a durable
  cursor, so it is resumable, and it **chains into a three-job wiki repair** —
  `wiki_prune` first (only it can archive an article whose `entity_ref` the
  sweep orphaned; `wiki_rebuild` iterates those very refs and so cannot repair
  a dead one), then `wiki_rebuild` to re-derive survivors and restore their
  citations, then `wiki_refresh` for entities the re-derivation newly minted,
  which have no article row for `wiki_rebuild` to visit. Without that repair a
  graph re-derive degrades every published revision to chunk-only claims
  (`wiki_citations.fact_id` is ON DELETE SET NULL) and orphans articles to dead
  entity ids (`wiki_articles.entity_ref` is a soft ref with no FK). It is fired from
  Ops → Automations ("Run now" on the `graph_rebuild_start` trigger) and
  reports progress on its run; a recurring drain schedule resumes a run
  stranded by a restart. Never automatic, never scheduled to start itself.
- **Human decisions are pinned overrides**: review-inbox resolutions,
  entity merges/rejections, domain corrections, and tag fixes survive any
  reprocessing; auto-supersession cannot override a pinned fact, only
  re-flag it.
- Doctrine split: *prose* (wiki) is corrected via correction notes;
  *structured pipeline outputs* (tags, domains, entity links, fact status)
  are corrected directly in the review inbox. A correction note's "elevated
  weight" is implemented as pinning the facts it asserts.
- **Where that elevation lives**: the note's own agent conversation, not the
  arbiter. `agent/graphwritetools.NoteTarget.is_correction` reads
  `notes.provenance == 'owner_correction'` (the four owner-gated producers:
  `file_correction`, `POST /api/wiki/{id}/corrections`,
  `POST /api/review/{id}/correction`, and the lint card's `correct` verb), and
  `_assert_one` sets `ExtractedFact.correction` on a fact the correction note's
  own text ATTESTS — full weight, force-supersede + pin through
  `supersession.decide()`. An INFERRED fact in a correction note is **not**
  elevated. This is the rule `arbiter.plan_intent(correction=True)` implemented,
  ported unchanged off `integrate_note` ahead of its deletion
  (`AGENT_INGEST_CONVERSATION_PLAN.md` W5). Nothing about it is model-facing:
  `assert_fact` has no `correction` field and the capture API has no
  `provenance` field.
- Contested (flagged-and-unreviewed) facts are **held out of wiki builds**;
  the wiki never publishes an unreviewed supersession.
- The pipeline records per-note stage state so retries are idempotent and a
  worker crash never double-extracts (full engine arrives Phase 5; Phase 3
  ships the minimal watermark).

## Attachments: the analysis dispatcher

Every attachment flows through a media-type **dispatcher** that routes to a
registered tool chain; every tool implements the same extractor interface,
so backends are config, not code:

| media | chain |
|---|---|
| `text/*` | decode |
| `application/pdf` | per-page text layer (PyMuPDF); pages without one render to images → image chain |
| `image/*` | OCR (vision-LLM via the adapter, cross-validated by the deterministic RapidOCR sidecar) **and** captioning (vision-LLM) as separate products |
| `video/*` | ffmpeg → audio track → transcription backend; keyframes → image chain (fast-follow; needs ffmpeg) |
| `audio/*` | **shipped:** whisper.cpp via the on-box llama-swap gateway, an async `transcribe_attachment` job → `kind='transcript'` cache row (docs/archive/WHISPER_TRANSCRIPTION_PLAN.md) |

Extractors return **provenanced segments**: source anchor (page, frame
time, audio range), kind (`text-layer | ocr | transcript | caption`),
tool+version, confidence. Chunks built from segments inherit the anchor, so
citations can point at *"video X @ 02:13"*, and re-analysis after a tool
upgrade is a targeted job over the old tool's segments — same philosophy as
re-embedding and re-extraction.

Dispatcher-level policy: per-domain backend routing (rides the privacy
routing axis — sensitive-domain media can be pinned to local tools), and
per-task size/cost budgets with a sample-or-summarize fallback for large
media. Phase mapping: Phase 2 ships the dispatcher + text/PDF chains;
Phase 3 adds vision backends (they require the LLM adapter); audio
transcription is now shipped on whisper.cpp via the on-box llama-swap gateway
(the model loads on demand and is freed after each job), mirroring the OCR job
— not a synchronous extractor, since it is a slow gateway call. It is off by
default (empty `whisper_url`); video transcription (ffmpeg → audio) is the
fast-follow.

Image-chain modes [decided: **default full**; per-attachment on-demand run;
the description kind rides `'caption'`]: the `image_analysis_mode` user
setting (`app.settings`, read per job) picks **full** — one `vision.ocr`
transcription call plus one `vision.caption` call producing a salient
multi-sentence description the fact pipeline mines [decided: the description
states the information itself — names, dates, quantities, relationships,
states, one sentence per distinct detail — never a narration of the medium
("a screenshot showing…") or its UI chrome] — or **ocr**, the
transcription call only with no caption row. The job payload's optional
`mode` overrides the setting: `POST /attachments/{id}/analyze` enqueues
`{mode: "full"}` for one attachment regardless of the global mode (also the
re-run path — the handler re-describes via delete+insert of the caption row
and re-runs OCR only if its cache row is missing, then re-ingests).
Confidence caps are unchanged: OCR 0.7, description 0.6.

**Dual-engine OCR cross-validation** (`../plans/RAPIDOCR_PLAN.md`): the
`vision.ocr` call now runs alongside the deterministic **RapidOCR** sidecar
(`asyncio.gather`), and **both** readings are stored as `kind='ocr'` rows,
distinguished by `tool` (`rapidocr` vs the VLM's `provider:model`). The chunker
(`image_segments`) prefers the RapidOCR row per anchor — falling back to the VLM
row when RapidOCR is empty — so the fact pipeline reads verbatim OCR, not the
model's reading, and divergence is logged (`ocr.crosscheck` agreement). Both
rows keep the **0.7 cap regardless of engine** — the cross-check raises trust in
the string, never a fact's auto-supersede power. A sidecar that's off/unreachable
degrades to VLM-only; the cross-check never fails the OCR job.

**Analysis gating [decided: keyed on outstanding vision work]**: ingest
enqueues `integrate_note` only when no `ocr_attachment` job is queued or
running for ANY of the note's attachments and the run enqueued none — so an
image note is extracted once, *with* its OCR text (the OCR handler's
re-ingest enqueues the analysis), never a blind body-only pass plus a
re-run. The gate keys on outstanding **work**, never on extract kinds or
the image-analysis mode: flipping ocr→full on an already-cached attachment
enqueues no job and must not block — captions then arrive only via the
on-demand endpoint, which is intended. A queued analyze job dedups a second
enqueue; a *running* one does not (it may have read stale chunks — a fresh
pass must follow). Two escape hatches keep the gate from stranding a note
unanalyzed: oversized images are skipped at enqueue with no cache row, so
they are never outstanding and never block; and an OCR job that exhausts
its retries falls back to enqueueing **body-only analysis** directly (the
failed job row stays the durable record — a re-ingest would just re-enqueue
OCR and loop). The startup backfill respects the same gate. Embedding is
never gated: capture-to-searchable still waits on nothing.

**Capture-race gate [decided: keyed on client-declared attachment intent].**
Capture posts the note and its attachments as **separate requests** (the offline
outbox: `POST /notes`, then a `POST /notes/{id}/attachments` per file), so the
first ingest can run *before* a promised image has uploaded — seeing zero
attachments and no outstanding OCR, the work-gate above has nothing to hold on,
so it would emit `note.ingested`, drive a blind body-only extraction, and (once
the image lands and OCRs) redo it — the exact double-pass the gate exists to
prevent, plus a ~minutes window showing the wrong analysis. The client knows at
create time how many files follow, so `POST /notes` carries an
**`attachments_expected`** count (`notes.attachments_expected`, migration 0154);
ingest defers the emit until at least that many attachments are present. The
uploading attachment's own ingest re-drives it once they land (then the OCR work
gate takes over), so an image note is still extracted **once, with its OCR text**.
The integration reconciler (`backfill_pending_integration`) honors the same wait —
else it would body-only integrate during the upload window and defeat the gate —
bounded by a **settle window** (`INTEGRATION_ATTACHMENT_SETTLE_SECONDS`) so a
promised attachment that never arrives (a failed upload) integrates on what it has
rather than stranding. That window is measured from **`notes.received_at`**, the
server's receipt instant — never `created_at`, which is the *client's* capture time
(the offline outbox flushes later), so a note captured yesterday and flushed now
would arrive already past its own window and defeat the gate in exactly the case it
exists for. Absent/0 (a plain note, or a client that doesn't send the hint) =
today's immediate behavior; the common no-attachment path is never delayed.

Guards on what extraction feeds the fact pipeline: structured
medical/financial documents are *detected and routed* (deferred to the
Phase 7 typed parsers) rather than free-extracted into hundreds of facts;
facts derived from OCR carry reduced confidence, and a low-confidence fact
never auto-supersedes a more-confident prior — `supersession.decide()` parks it
in `pending_review` behind a `low_confidence` card instead. That guard keys on
the model's **self-confidence** (threaded as `ExtractedFact.self_confidence` →
`Candidate.self_confidence`), not the deterministic plan weight: the weight
model grants a surface-attested fact its full ceiling regardless of how unsure
the model was about the *read*, so the self-report is what protects a confident
prior from a blurry OCR value.

## Model routing & cost

| task | tier |
|---|---|
| `note.extract` (title+tags+facts+entities+temporal, one call) | strong |
| `entity.disambiguate` (batched, only uncertain mentions) | cheap |
| `fact.adjudicate` (batched, only retrieved candidates) | cheap |
| `correction_note.extract` | strong |
| `vision.ocr` / `vision.caption` (P3 image backend) | strong (vision) |
| embeddings | local container |

**Provider routing [decided]**: every task is individually configurable to
`anthropic | xai | local` (+ model), defaulting to **`xai:grok-4.3` for
all tasks** during dev. The `local` provider (OpenAI-compatible endpoint) is
wired from day one as the all-local escape hatch, but is **unreachable until an
operator opts in** (off by default; see below). **Vision-LLM is the
first OCR backend [decided]** — Tesseract remains a later config option on
the dispatcher's routing axis.

**Tiers track prompt strength**: the settings screen groups tasks by their
prompt `strength:` (high / low / vision), so the screen tells the truth about
which work is heavy rather than a cosmetic grouping. New routable tasks the API
returns outside those groups fall into an "Other" group, so nothing is dropped.

**Self-hosted local models [opt-in]**: local hosting is OFF by default — the
stock deploy is cloud-only and no default ever points at `local`. An operator
enables it at install (or `jbrain enable-local-models`), which provisions a
curated set from `jbrain.llm.local_catalog` and starts the `local-llm` compose
profile: a llama-swap gateway so several GGUF models share one OpenAI endpoint.
Rather than build llama.cpp ourselves (a moving target against the gfx1151
Vulkan/ROCm runtime), the gateway image is based on the community-maintained,
hardware-tested `kyuz0/amd-strix-halo-toolboxes` llama.cpp image (Vulkan/RADV by
default; a `rocm-*` tag is selectable via `LOCAL_LLM_BASE` and is faster but
needs `/dev/kfd` + `seccomp=unconfined`), with llama-swap layered on. Host
prerequisites the image can't supply: **kernel ≥ 6.18.4** (older has a gfx1151
stability bug) and avoid `linux-firmware-20251125` (breaks ROCm). The optional
`scripts/strix-halo-host-setup.sh` (`jbrain strix-halo-host-setup`) applies the
host tweaks from strix-halo-toolboxes.com — kernel params
(`amdgpu.gttsize`/`ttm.pages_limit`/`amd_iommu=off` for the ~124GB unified pool),
GPU device permissions, and a `tuned` perf profile — and is never run by the
installer (it edits GRUB and needs a reboot). The catalog is
the single source of truth — the settings screen surfaces enabled models as
routing choices (vision tasks filter to vision-capable ones) and
`scripts/local-llm-setup.sh` reads its JSON manifest to download weights. Tuned
for an AMD Strix Halo class box (large unified memory, ~256 GB/s): MoE /
small-dense models only. Recommended set is **Qwen3-VL-30B-A3B** (vision + cheap
text) and **gpt-oss-120b** (reasoning), kept resident together; routing stays a
deliberate per-task/tier choice.

**Token accounting [decided]**: every adapter call persists a usage row
(`llm_usage`: task, provider, model, input/output tokens, timestamp) —
written fire-and-forget so accounting can never fail or slow a call.
Owner-only RLS (telemetry, not domain data; still gets the standard
isolation test). The ledger is append-only and never pruned, so it is the
lifetime ground truth. Surfaced live on the LLM Settings screen as an **AI
usage card**: today / this month / all-time totals with per-task breakdown,
aggregated at query time. The today/month buckets roll over at the owner's
local midnight (SQL `AT TIME ZONE` against `owner_timezone`, degrading to UTC
when unset); the all-time total is a full-ledger `SUM` bounded by no window.

**Cost estimates [decided]**: the card shows estimated dollars alongside
tokens, computed at query time from a config price table
(`JBRAIN_LLM_PRICES` JSON of `provider:model` → $/M input, $/M output)
seeded with **grok-4.3 at $1.25/M in, $2.50/M out** (xAI docs, June
2026; cached input $0.20/M exists but isn't modeled — estimates are
deliberately conservative). Models missing from the table show tokens
only, never a guessed price. Query-time pricing means a table update
re-prices history — acceptable at personal scale, and the tokens remain
the ground truth. ≈ **$0.01/note** at grok-4.3 rates.

One guaranteed `note.extract` call **per source** (the note body plus one per
attachment — per-source extraction, below) + up to two conditional cheap calls per
note; ~5–7k tokens ≈ $0.01 for a plain note at grok-4.3 rates, roughly +$0.01 per
attachment source. Conflict detection is bounded by candidate
retrieval (SQL identity match, else pgvector top-k scoped to same
entity+domain+kind) — never corpus-wide comparison. Concurrent offline-sync
bursts serialize per (entity, predicate) to keep supersession chains
deterministic. Over-extraction is the known quality risk: a soft cap on
facts-per-note that **scales with note length** (`prompt.fact_cap`: a
word-count proxy clamped to `[MIN_FACTS, MAX_FACTS]` — a one-liner and a long
journal entry no longer share one ceiling), advertised in the per-note prompt
and enforced in `parse_extraction` (the two read the same number); honest
confidence; review-inbox rejection rate as the prompt-tuning signal. When the
budget actually clips the tail (`dropped_facts > 0` — a pasted article, a
medical-history dump), the pipeline files an **`extraction_truncated`** review
card so the loss is visible, not silent: an informational notice whose only
verb is dismiss (it wrote no graph state), retired automatically when a
larger-budget re-run fits.

**Chunk-level map-reduce for long input [decided].** A long note (a pasted
article, a medical-history dump) is split into token-bounded **groups** of
paragraph chunks (`prompt.group_texts`, `GROUP_CHAR_BUDGET`), each extracted in
its OWN `note.extract` call with its own length-scaled fact budget, then merged
(`extraction.merge_extractions`) into one note-level extraction. So the yield
scales with the note instead of clipping at one note-wide cap or a single
call's output-token ceiling. The reduce reuses the very machinery that
reconciles facts across NOTES: union the mentions and tokens, **re-run the
deterministic object binding over the full mention set** (so a relationship
whose object entity was named in another group still links), then dedup on the
structural identity key so a property restated across groups collapses to one;
`dropped_facts` sums each group's truncation for the note-level card. A note
that fits one group makes exactly one call — the short-note path is unchanged.
Groups run sequentially and a malformed group fails the note like any single
extraction (the merge is in-memory; the commit + settle pair runs once, after,
in one transaction). Cross-group coreference is bounded by group size (several
paragraphs); a context header for later groups is possible future work.

**Per-source extraction [decided: a group never mixes the note body with an
attachment].** One shared `note.extract` call gives every source's blocks a
SINGLE fact budget, so a content-rich attachment (a scanned membership card, a
receipt) can crowd the note body's own first-party facts out of that budget
entirely — the observed failure where a note reading *"car loan for the Kia,
attached as an image"* lost its `owns → car loan` / `owns → Kia` edges the moment
the (unrelated) card image's OCR was present: the model, handed body + dense card
text in one call under a "ceiling, not target" budget, emitted the card's account
facts and dropped the body's. This is a **sole-source-of-truth** violation — the
note's own words are the source of truth; an attachment is enrichment that must
never delete them. So grouping partitions by source (`prompt.group_texts_by_source`,
keyed off `Chunk.attachment_id`): the note body is one source, each attachment
another, and each extracts in its OWN call with its own budget (body-first, so its
title wins the reduce). The existing map-reduce reduce then re-binds objects across
the full mention set and dedups on the structural key, so cross-source coreference
and duplicates still resolve. A note with a single source (the common plain note)
is exactly one group/one call — unchanged; a note with body + N attachments makes
N+1 calls, the deliberate cost of never losing a body fact to an attachment (and of
never letting two attachments crowd each other). This is the *extraction-input*
guarantee behind the ingest-level capture-race gate above: the gate ensures the OCR
text is present for the ONE extraction; per-source grouping ensures that extraction
keeps the body's facts alongside it.

**Enumerated and symmetric relationships [decided: one edge per individual,
never a sentence-valued attribute].** "I have four daughters, A, B, C and D"
must emit a separate relationship edge per named child (each its own
`object_entity_ref`), not one edge to the first-named with the rest left as
bare mentions — the prompt teaches the fan-out and the dedup key (which
includes `object_entity_ref`) keeps the distinct edges. A relationship between
two named people is always a `relationship` edge, never an `attribute` whose
value restates the sentence ("Lydian and Elora are identical twins" is
`Lydian.sibling →twin Elora`, not `twinStatus → "…the whole sentence…"`); the
reciprocity registry then materializes the symmetric mirror on the other
party's stream, and kinship (`children`/`parent`, alongside the existing
`parent_of`/`child_of`) reciprocates the same way.

## Review inbox integration

One generic `review_items` queue (already designed) absorbs: fact
conflicts, attribute collisions, entity-merge proposals, ambiguous
mentions, domain promotions/demotions, low-confidence extractions,
fact-budget truncations (`extraction_truncated`).
Resolutions write pinned overrides and, where the fix is prose-shaped,
draft correction notes.

**Resolutions record their graph effects, and reopen reverses them
[decided: full unwind].** Each resolution writes an `effects` array into
the item's resolution jsonb capturing the prior state every write
destroyed: pins record the fact's prior status/pin/supersession link,
retractions the prior status, merges the tombstoned entity's prior state
plus the exact mention/fact row ids that were repointed (which is what
makes un-merge a replay, not span archaeology), domain moves the prior
domain and pin. Reopening a resolved or dismissed item reverses those
effects in the same transaction that re-queues it and stamps a
`reopened_at` marker into the jsonb (the UI's tombstone). The one
exception: permanent `distinct_from` edges survive reopen by doctrine — a
reopened merge-rejection re-queues the item but the edge stays, and the
reopen response says so. Dismissals record no effects; their reopen is a
bare re-queue. Reversing a *merge* effect carries the fold's scope rule: the
un-merge refuses a domain-narrowed session before reversing any effect at
all, so a refused reopen leaves the whole resolution intact rather than half
of it.
