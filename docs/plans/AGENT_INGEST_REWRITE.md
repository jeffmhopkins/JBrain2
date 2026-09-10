# Agent-forward ingestion — the rewrite

> **Status:** Scheduled · **Last verified:** 2026-09-10 · **Waves:** R0◻️ R1◻️ R1b◻️ R2◻️ R3◻️ R4◻️ R5◻️ R6◻️

**This doc supersedes the unbuilt waves of `AGENT_INGEST_CONVERSATION_PLAN.md`
(W5a/W5b/W5c), `SETTLE_OWNERSHIP.md` S4–S5, and `W5_PRECONDITIONS.md`'s
RECOMMENDATIONS.** It does not supersede their FINDINGS, which are the input to
everything below and are cited throughout. The three docs keep their history; each
gains a banner naming this one.

The owner redirected the work: *"The whole goal was a complete rewrite of the ingestion
system. We shouldn't be trying to hold on to the old tests, or the old way of ingesting
notes. I'm okay with a radical departure, erasure of the previous, and doing this right
with an agent-forward paradigm."* And: *"I'm also okay with a complete deletion of the
existing database"* — clarified to **notes, the graph derived from them, and the
predicate registry; the tasks and settings stay.** A later clarification put the wiki
tables in the wipe as well, as DATA: the articles go, the wiki code stays.

What that inverts. Everything written about preserving `integrate_note`'s behaviour
field-for-field is void — D13's "no producer removed before its replacement is merged"
as a blocker on this wave, the "W5 MUST NOT DELETE" lists, the 23 strict `xfail`
scenarios read as permanent limits, and `W5_PRECONDITIONS.md`'s COALESCE-seam /
first-line-title / keep-the-analyzer's-title-half recommendations. They were correct
answers to *"how do we take this out without breaking the corpus"*. There is no corpus
to break.

A second owner decision, settled the same way and folded in here: **"the agent asks when
it isn't sure, one channel."** That replaces D2's *commit clear facts, ask only when
blocked* with something stronger — **uncertainty reaches the owner as a CONVERSATION,
never as a card** — and it turns the write path's "I could not settle this" from a row the
owner adjudicates into a RESULT the agent reads and acts on. §2 works out what it costs;
the short answer is that it shrinks the plan, the tool surface and the schema, and most of
it is already built.

What does NOT change: the binding constraints of the parent plan that are properties of
the box rather than of the old path — **no JSON-Schema `enum` anywhere in a tool
sidecar** (constraint 8, the gpt-oss harmony segfault), the closed tool allowlist as the
enforcement (D16/constraint 9), RLS and the domain floors, `supersession.decide()` as the
implementation of a write tool and never a model-facing verb (constraint 5), and the
measured rule that outlived the wave that found it: **`required` buys PRESENCE, not
MEMBERSHIP** — a field whose legal values are WORDS is not buildable on this box, and a
boolean is a different failure, not a way round it.

Everything below is mechanical against the code. Where a claim is not proven it says
**uncertain** and names the experiment. Where the docs and the code disagree, the code
wins and the doc is corrected in place.

---

## 1. The spine: a reading, not a ledger

`SETTLE_OWNERSHIP.md` S3 proved one thing and it survives the redirection intact:

> A producer may release a claim on X only when it has RE-DERIVED the note and found it
> no longer says X. "The note no longer says X" is a claim only a producer with a
> COMPLETE CURRENT READING of the note can make.

A write ledger can never license a retraction, because it records what a producer WROTE,
not what it READ — and a pass that read the note and chose to write nothing is
indistinguishable from one that never looked. S3 demonstrated that four ways
(`SETTLE_OWNERSHIP.md` "The four demonstrated failures"), the last of them firing on the
feature's own happy path.

The proof names its own door: **an EXTRACTION — a complete current reading.** Under the
old frame that door was shut, because only `note.extract` could produce one and the plan
was deleting it. Agent-forward it is open: **ask the agent to state what the note says,
completely, at the end of a pass.** That is a reading, not a write record, so it
satisfies the invariant on the invariant's own terms.

Call it **the closing reading**, and the verb that carries it **`close_reading`**.

**This is not the thing S3 ruled out.** S3's exclusion is exact and narrow: *"It is NOT a
new tool verb: asking the model to assert its own EXHAUSTIVENESS is the least reliable
thing this repo has measured"* — the `assertion`-from-a-five-word-list result, 0 legal in
72. That is a field where the model must pick a WORD that means "I am complete". The
closing reading asks for no such field. It asks the model to RESTATE the note's content,
which is a generative task the box already does well: the same model on the same box
produces 8.0 well-formed facts a turn on the shipping eight-field schema
(`AGENT_INGEST_CONVERSATION_PLAN.md`, gap 4's measurement), and `note.extract` has been
doing exactly this task in production since Phase 2. **Exhaustiveness is a property of
the OUTPUT the engine then diffs, never a claim the model makes.** That is the same
licensing `note.extract` has today — the prompt asks for everything, the truncation card
fires when the budget clips it, and nothing anywhere asks the model whether it was
thorough.

The harness has been running this exact mechanism, green, since W3, and says so in the
one place in the tree that states the invariant best:

> *"What licenses it is that each step is a whole-note RE-DERIVATION. The harness is the
> model, emitting a complete extraction per step, so it satisfies the sweep's invariant
> the way the analyzer's `Extraction` does and a write ledger never can"*
> — `backend/tests/harness/runner.py:487-493`

`backend/tests/harness/scenarios/rerun_retracts_removed_fact.json` is that mechanism
passing. **The design below is the harness's shape, promoted to production.** That is
the single strongest piece of evidence this plan has, and it is worth stating plainly:
the mechanism is not speculative, it is under test and green; what production lacks is
the producer.

### What the reading buys, in one list

Everything the teardown was blocked on collapses into it:

| Blocked on | Closed by the reading |
|---|---|
| Retraction after `integrate_note` (`W5_PRECONDITIONS.md` §4 — the largest loss, "no retraction of any row, by any producer, ever") | The reading's fact ids are `touched`; `sweep_note` runs unchanged. |
| Precondition 3, `note_analysis` title/tags | `title` and `tags` are two more fields on the same call, exactly as they are two more keys on `note.extract`'s JSON. |
| `app.temporal_tokens` losing its only producer | The reading carries `when` / `when_end` / `repeats`, so `_upsert_tokens` has input again. |
| Appointment recurrence (RRULE read off the fact's token, `appointment_projection.py:422-438`) | `repeats` — the one genuinely NEW field the surface needs. |
| The settle's two review-card halves being unreachable from the conversation (`pipeline.py:1180-1187`: they "want the `extraction` the conversation does not have") | Moot, twice over. The conversation now HAS an extraction — and the one-channel decision deletes both halves outright (§2), so the settle is three steps rather than five. |
| A key-based sweep as a *new mechanism* with its own failure surface (`W5_PRECONDITIONS.md` §4(a)) | Not needed. The reading COMMITS, so it produces ids, so the shipped id-based sweep works. |

That last row is the one worth dwelling on. `W5_PRECONDITIONS.md` §4(a) worried that an
extraction which does not write produces no fact ids, so feeding a sweep from it needs a
new key-based match — *"a different mechanism from the one built, with its own failure
surface"* — and recommended measuring the key-match rate against the corpus first. **The
closing reading writes.** Re-asserting an identity key already on file returns `ALREADY`
carrying the same `fact_id` (`SETTLE_OWNERSHIP.md` S3 step 1), so the reading's ids
include every fact it re-states, and `sweep_note(touched=…)` is the shipped function with
no changes at all. §4(a)'s experiment is therefore not needed — and could not have been
run anyway, since the wipe deletes its input. §6 names what replaces it.

---

## 2. The target design, end to end

### A note arrives

1. **Ingest is unchanged.** `ingest/pipeline.py` chunks, embeds, composes clarification
   blocks (D6), and emits `note.ingested`. Attachments, OCR and the vision path are
   untouched. Nothing in this rewrite is upstream of the graph.
2. **One producer claims it.** The dispatcher opens a `note_converse` thread — the note
   is turn 0 of an ordinary agent conversation (D1). `integrate_note` no longer exists,
   so the fan-out that made two producers race off one event is gone by construction,
   and with it the whole of `SETTLE_OWNERSHIP.md`'s founding bug.
3. **The unattended pass.** Tools: `resolve_entity`, `close_reading`, `ask_owner`,
   `find_entity`, `read_entity`, `current_time` — the shipped unattended set with
   `assert_fact` replaced by `close_reading` (`agent/agents.py:490`). The agent reads the
   note, resolves its cast to handles, reads the graph where it needs to, and ends with
   the reading.
4. **The reading commits.** `close_reading` runs through `commit_facts` — real
   resolution, real `supersession.decide()`, real domain floor and ratchet, real citation
   anchoring, `settle_owner=CONVERSATION`. Nothing about *how* a write lands changes;
   this rewrite replaces *who supplies the meaning*, which is the parent plan's thesis
   and is the half that was already right.
5. **The pass ends.** `converse._run_turn`'s terminal block, in this order:
   - **state transition** (already "NOT best-effort", `converse.py:465-467`);
   - **`settle_conversation`** — and it now runs the WHOLE settle for a pass that
     produced a reading: `sweep_note` (release the `conversation` claim on what the
     reading no longer asserts, retract what that leaves unclaimed), the two review-card
     halves, `settle_tail` (reprojection, corroboration promotion, appointment / EMR /
     geofence / device projections), `stamp_analysis` (title, tags, extractor,
     prompt_version);
   - **the `integration_state = 'integrated'` flip**, on EVERY ending.

### What licenses the retraction, stated as the gate

The sweep fires **iff** all of:

- the pass produced at least one `close_reading` call, and
- the pass ended cleanly — `state_for_stop` gave `SETTLED` (not truncated, not
  `waiting_on_owner`, not `record_failed`), and
- no `close_reading` call reported a clamp, and
- the pass is not on the third-party surface.

...and, on a third-party note, the pass is not third-party (§2, "where one channel has no
channel").

The clamp clause is `_batch`'s existing report (`graphwritetools.py:1198-1218`), promoted
from a cosmetic result line to a safety gate. `_batch`'s own docstring is the reason it
must be: *"`maxItems` is not reliably compiled into llama.cpp's tool grammar — the handler
is the only real ceiling."* A clamped reading is a PREFIX of the note, and a sweep against
a prefix retracts the tail. **A clamp files no card** — the agent says so in the thread;
see "The clamped pass" below.

**Fail toward not sweeping.** Every degraded ending — truncation, a park on `ask_owner`,
a failed ledger record, a clamp, a pass that simply never called `close_reading` —
lands on today's behaviour: facts commit, projections run, the note stamps and flips, and
nothing is retracted. That is the direction `SETTLE_OWNERSHIP.md` chose on purpose (*fail
toward a loud error, never toward a quiet retraction*), and it means every failure of the
new mechanism degrades to the state the box is in today rather than to data loss.

### Where `ask_owner` fits, and why the S3 failure-4 shape cannot recur

The agent asks whenever it is not sure — the one-channel rule, which widens D2's "only
when blocked" and is worked out in "One channel" below. The pass parks `waiting_on_owner`,
unbounded and deliberately never reaped (`models/note_conversation.py:70-71`). It stamps
and flips; it does not sweep. The owner's answer is appended as a timestamped
clarification block (D6, `app.note_clarifications`), the note re-ingests, and the reply
turn ends with its own `close_reading` — over the note's CURRENT composed text, answer
included. That reading sweeps.

S3's failure 4 was: the owner answers, the note's text only GROWS, and the settling
session is re-stamped into a generation of one, so every other session's facts are
retracted. It cannot recur here, because the release is no longer keyed on a generation
at all. The reading states what the note says NOW; a fact the answer did not remove is
re-stated and its id lands in `touched`; nothing about which session wrote it is
consulted. **The lesson that produced that failure still binds** — *a field that acquires
retraction authority needs every existing writer of it re-read against the new job* — and
this design takes no field's authority: `note_body_sha` keeps its one honest question
(*did the note move under me?*) and gains no second job.

### What produces title and tags

`close_reading` carries them, and they are free — the same by-product they are on
`note.extract` today, on a call made for the facts. `stamp_analysis` upserts them
unchanged. Two rules on the upsert, both from `W5_PRECONDITIONS.md` §1's findings (the
findings survive; only the recommendation was voided):

1. **The ROW is written on every pass ending that read the note**, `waiting_on_owner`
   included. §1's Correction 2 is the reason and it is load-bearing: a blanked title
   leaves the row, but NOT STAMPING AT ALL leaves no row, and then `Note.analyzed` is
   false forever (`models/notes.py:92-98`), `lifecycleChip` shows a permanent amber
   "analyzing…" on every note in the home stream (`frontend/src/notes/lifecycle.ts:39-53`),
   the Analysis tab renders "nothing here yet" over a note whose graph is written
   (`AnalysisTab.tsx:521`), and the re-run button polls an `analyzed_at` that never moves
   (`AnalysisTab.tsx:470`) — the PWA's only no-terminal re-analysis lever, spinning
   forever (CLAUDE.md #10).
2. **`title`/`tags` are `COALESCE`d, not overwritten**, so a pass that ended without a
   reading stamps the row without blanking a title an earlier reading wrote. This is
   §1's seam, kept for a different reason than it was proposed for: not to protect the
   analyzer's title from the conversation, but to protect a *complete* pass's title from
   a *degraded* one.

**Strike `agent/externaltools.py:411-414` from the consumer list** — §1's Correction 1,
verified again here: that line's title comes from `app.external_sources` via
`external/corpus.py:201-236`, never `note_analysis`.

### What flips `integration_state`

`W5_PRECONDITIONS.md` §2's recommendation, adopted whole. It is the one recommendation in
that doc the redirection does not touch, because it answers a question about orchestration
rather than about preserving the old extractor.

- The flip moves to the `note_converse` terminal block and fires on **every** pass ending
  (option (ii)). It preserves today's semantics exactly: the flip is unconditional today
  (`pipeline.py:542`) and fires even on a rejected plan, so the state has always meant
  *"the note's graph producer ran to completion on it"*, never *"the graph is complete"*.
- `reconcile_pending_integration` repoints from `integrate_note` to `note_converse`
  (`queue.py:605-670`) and gains the live-conversation clause the dispatcher already
  applies (`dispatcher.py:404-416`). This is the dropped-event safety net the conversation
  producer has never had.
- `has_active_analysis` (`queue.py:344`) and `POST /notes/{id}/analyze`
  (`api/notes.py:466`) repoint with it, or the PWA's re-run button 202s a job kind with
  no handler.
- `_integration_drained`'s in-flight list (`rebuild.py:288-292`) becomes
  `('note_converse', 'emr_parse')`.
- `dispatcher._already_active`'s `integrate_note` arm (`dispatcher.py:396-403`) is deleted
  with the kind.

§2's named residual is carried forward unchanged and unsolved: *a note edited while its
conversation is parked on `ask_owner` re-ingests, flips to `'stale'`, and is then skipped
by the repointed reconciler because a live conversation exists.* It is Open decision **O4**
in §8.

### What produces the projections

Unchanged. `settle_tail` already runs `_reproject_entities`, `_promote_corroborated`,
`project_appointments`, `project_emr`, `project_place_geofences` and
`reconcile_device_bindings` (`pipeline.py:1373`), and the conversation has called it since
S2. What changes is that its input stops being a partial write ledger and becomes a
complete reading — and that `app.temporal_tokens` starts being written again, so
`appointment_projection._recurrence_rrule` (`:422-438`) has an RRULE to read and a
recurring appointment stops projecting as a one-off.

### One channel — the write path REPORTS, the agent DECIDES, the owner is asked

The owner settled this after it was put to them explicitly: **"the agent asks when it
isn't sure, one channel."** That is stronger than D2 (*commit clear facts, ask only when
blocked*) and it replaces it. **Uncertainty reaches the owner as a CONVERSATION, never as
a card.** A card is a second channel to the owner, adjudicated somewhere the note is not,
by a person who has to reconstruct what the sentence meant — and it exists because the
deterministic write path had no one to tell.

**The structural change.** Today `supersession.decide()` both DECIDES and FILES: it hits
a conflict, picks `pending_review`, and `pipeline.py:3042` writes a `ReviewItem` the owner
later adjudicates. Under one channel it reports and stops. What it could not settle
becomes a **tool result the agent reads**, and the agent decides — because the agent has
what `decide()` never had: the note's full text, the graph it just read, and a channel to
the owner.

**Most of this is already built, which is the finding that makes the change small.**
`_upsert_fact` already returns `FactWrite(hold_reason=decision.review_kind,
conflicting=conflict.statement)` (`pipeline.py:2992-2999`), and `_write_line` already
renders it to the model as:

> `held  Me.homeLocation → 412 Oak St — clashes with <the other statement> (fact_conflict)
> — recorded but NOT live. Ask the owner which is right`
> — `graphwritetools.py:1094-1100`

The agent-facing channel exists, carries the reason AND the conflicting statement, and
already tells the agent to ask. The card is filed **beside** it, from the `if
decision.review_kind is not None:` block immediately below the same branch
(`pipeline.py:3042`, and its twin on the inverse path at `:3259`). **Removing the second
channel is deleting those two blocks**, plus the wording change that turns "Ask the owner
which is right" from advice into the pass's obligation.

`decide()` stays authoritative on what LANDS. Constraint 5 is untouched: supersession is
the implementation of a write tool and never a model-facing verb, so the model cannot
force a supersede, cannot un-hold a held row, and cannot set `pinned`. What moves is only
who is told.

#### Every `decide()` card site, and what it becomes

| Site | Today | Becomes |
|---|---|---|
| `supersession.py:648` — event/measurement, same instant, different value | `fact_conflict` + hold | **result.** "same metric at the same instant, different value — held." The agent has both readings in front of it and either re-reads the note or asks. |
| `:662` — attribute whose current head is PINNED | `attribute_collision` + hold | **result.** "the owner pinned a different value here." The agent must not argue with a pin; it asks. |
| `:669` — attribute collision, BOTH sides held | `attribute_collision` + hold both | **result, and it must say the OTHER row was held too** — this is the one site that changes existing state, and a result that omits it under-reports what the write did. |
| `:703` — non-functional edge, opposite-polarity same object | `fact_conflict` + hold | **result.** |
| `:766` — closed-interval correction of a pinned head | `fact_conflict` + hold | **result.** |
| `:777` — closed-interval correction below the confidence floor | `low_confidence` + hold | **result** — and see the `confidence` decision in §3: with the model field gone this fires only on the ENGINE's span check, i.e. on a quote the note does not contain, which is a thing the agent can fix by re-quoting. |
| `:806` — irrealis candidate vs an asserted head | `fact_conflict` + hold | **unreachable from the note path** — the tool surface has no `assertion` field, so only the EMR importer and `correct_fact` can produce an irrealis candidate. Silent for the conversation; kept for those two. |
| `:814` — open head is PINNED | `fact_conflict` + hold | **result.** |
| `:829` — open head, candidate below the confidence floor | `low_confidence` + hold | **result**, same note as `:777`. |
| `:855` — a supersede that is not Lever-B-silent (same-instant, or a `preference`) | `fact_conflict`, **status `active`** | **result, and the clearest case for the change**: the write LANDS LIVE and files a card anyway. That card is pure notification of a thing that already happened. It becomes a line in the result and nothing else. |
| `pipeline.py:3215` — derived-defers-to-primary on the inverse path | `fact_conflict` + hold | **result**, reported on the fact whose reciprocal was refused. |
| `supersession.py:476` — `_lab_status_transition`, a preliminary FHIR reading | `low_confidence` | **stays a card, and it is the genuine exception.** It is on the EMR path, and the conversation holds NO graph-write verbs on an `emr_owned` note (`NoteToolset.writes_graph=False`, `graphwritetools.py:1234-1243`) — there is no agent in that room to hand a result to. |

**What happens to a held row with no card to promote it.** Two routes, both already built:
a later reading rates it live (`PROMOTED`, `pipeline.py:206` — "a previously held row this
pass rates live"), or the owner answers and the reply turn's `correct_fact`
force-supersedes. And it is not invisible in the meantime: `GET /notes/{id}/analysis`
selects every fact of the note with its `status` and no filter (`analysis/repo.py:146-160`,
`:234`), so a held row renders on the note's Analysis tab. The thread is the channel at
the time; the tab is the durable view. No card is load-bearing for either.

#### `ambiguous_mention` and the resolve path

`resolve_entity` **already** returns the ambiguity to the agent, and refuses to guess:

> `err  entities[2] 'Dana': several of the owner's entities share that name, so this is
> ambiguous. Say which one from the note, or leave it out.`
> — `graphwritetools.py:632-637`

No handle comes back, so no fact can be written against it — the safety property holds
without a card. What the result does NOT do is **name the candidates**, while
`_file_ambiguous_review` files a card that does (`pipeline.py:1778-1787`, `entity_ids`).
So the card was carrying information the agent needed and was never given. **That is the
change**: the result names them, and the card goes.

It needs one thing back from the agent, which is §3's second new field: a way to say
which. Today `resolve_entity` takes `{surface, kind}` and nothing else.

#### What is left in the inbox

The distinction that does the work is not *how confident* — it is **who the notice is
for**.

- **A question about THIS note's reading** — a conflict, a collision, a low-legibility
  read, an ambiguous name, a truncated pass. The agent has the note and a channel.
  **These become results, and the agent asks.** Every card kind above is one of these.
- **A firewall catch** — `domain_promotion` (`pipeline.py:2963`, a novel predicate the
  agent asked to file above its note's domain, D18) and `inverse_proposal`
  (`pipeline.py:3137`, a reciprocal that would land a fact on a DISTINCT security
  subject's stream, which writes nothing and proposes). **These STAY cards, and handing
  them to the agent as results would be backwards**: the agent is the party the control
  fired on. A firewall catch is not a question, it is a notice, and its correct behaviour
  — wrote nothing, told the owner — is already right. The EMR location firewall's card
  (`ingest/emr/firewall.py`) is the same shape.
- **A corpus-scoped proposal** — `confirm_entity` (`pipeline.py:1962`, promoting a
  corroborated-but-contested provisional entity) and `merge_proposal` / `distinct_from`
  (`:2037`, `:2069`). Neither is a question about this note.
  - `merge_proposal` **should become a conversation**: a fold IS a question, the reply
    turn already holds `merge_entities` (which stages and can never enact, constraint 12),
    and "I think these two Danas are the same person — want me to fold them?" is a
    message. The enact stays owner-only and full-owner-only. *Uncertain whether a merge
    the agent notices on a LATER note should re-open the earlier note's thread or start
    its own — see O7.*
  - `confirm_entity` **should go silent.** It is bookkeeping — a provisional entity
    corroborated by a second note — and the owner has no opinion to contribute. Promote
    on corroboration or leave it provisional; do not ask.
- **The wiki linter** — `wiki_contradiction`, `wiki_stale_claim` (`wiki/lint.py:536`,
  `:671`). Never starts from a note. Stays.

So `review_items` survives (constraint 4 is untouched — `pending_review` as a STATUS is
what represents a preliminary FHIR lab, and `emr_projection.py` reads it back), and it
shrinks to **firewall catches plus wiki-lint findings**. Everything a person would call an
inbox item about their own note becomes a message in that note's thread.

**D4's two tabs collapse to one channel with two lists.** The notes tab was already only a
redirector, and under one channel it is not a card table at all — it is a query over
`note_conversations.state = 'waiting_on_owner'`, which is literally the list of threads
waiting on the owner. Beside it, a much shorter findings list: firewall catches and lint.
D5 stands unchanged — questions live in their note's conversation, no push, no badge.

**And a consequence for the settle.** With no `ambiguous_mention` cards and no truncation
card, the settle's two review-card halves have nothing to retire:
`_sweep_stale_ambiguous` (`pipeline.py:1458-1495`) and `_sync_truncation_review`
(`:1496-1564`) both go, and the settle collapses back to `sweep_note` + `settle_tail` +
`stamp_analysis`. `review_items.settle_owner` (migration 0197, S1b) loses every reader —
the surviving card producers are never retired by a settle — so the column is dead and can
be dropped in the same migration as the wipe, or left; it is one line either way.

#### The clamped pass: a message, not a card

The gate above still refuses to sweep on a clamp. **What it does NOT do any more is file a
`reading_truncated` card** — that was this doc's earlier answer and it does not survive
one channel. A truncation is not a question the owner can answer, so `ask_owner` is wrong
too. It is a thing to SAY: the agent ends the pass with "this note is long — I recorded
what I got through and did not finish it," which is the one channel doing exactly its job,
costs nothing, and needs no kind, no payload renderer and no resolution arm. The engine's
whole responsibility is the gate.

#### The third-party surface, where one channel has no channel

`NOTE_INGEST_THIRD_PARTY_TOOLS` drops `ask_owner` deliberately: a stranger's note must not
open an unreviewed inbound message channel wearing the owner's own agent's voice
(`agents.py:579` and the reasoning above it). So on an `untrusted_origin` note the agent
**cannot** ask, and one channel has no channel. The answer, stated rather than inherited:

1. **Uncertainty is silence.** D2's clause survives here and only here — *what is clear
   commits, what is not is left alone*. An ambiguous name returns no handle, so no fact is
   written; that is already the behaviour and it needs nothing.
2. **A `decide()` hold still holds, and no card is filed.** The row lands
   `pending_review`, inert, not live, visible on the note's Analysis tab. The agent gets
   the result and can do nothing with it, which is correct: a stranger's text has no
   standing to argue with a value already on file.
3. **A third-party reading COMMITS but does not SWEEP.** This is a new clause on the gate
   and it is a safety fix the question surfaced: without it, a stranger-shaped `untrusted
   origin` body would license a retraction of the owner's facts. A reading is a write, not
   a licence, when the reader is not the owner. One clause, and it removes the only path
   by which third-party text can cause a retraction anywhere in the system.

### The wiki

Wiped as data (§6), kept as code, and it needs no change to work against the new graph:
`wiki/builder.py:519-534` selects `app.facts` JOIN `app.chunks` by `entity_id` and knows
nothing about which producer wrote the row. The conversation's writes already anchor
through `_anchor_chunk` / `_citation_chunk` (`pipeline.py:2261`, `:2306`), so citations
have a real chunk.

Two consequences worth stating, since the wipe makes them cheap to act on:

- **`wiki_citations.chunk_id NOT NULL` + the builder's INNER JOIN mean a fact with a null
  `chunk_id` is invisible to the wiki.** That is not new, and it is the reason the closing
  reading must keep anchoring rather than writing bare facts. It also means the
  re-anchor obligations of constraint 1 stay binding on every write path.
- **The `file_correction`-must-mint-a-NOTE shape is now re-examinable.** It was forced by
  that NOT NULL plus the rebuild re-deriving the graph from notes — a correction that left
  no note would have nothing to cite and would evaporate on the next rebuild. Both halves
  are still true, so the shape is still *sound*; what the wipe removes is the cost of
  changing it. **Recommendation: do not change it in this rewrite.** It is Phase 6's
  question, `PHASE6_WIKI_PLAN.md` §4 names it as that plan's shipped exit criterion, and
  bundling it here mixes a wiki decision into an ingestion one. Recorded as **O6** so the
  next wiki wave starts from the re-opened question rather than from the inherited answer.

---

## 3. The tool surface

### What exists today

| Verb | Where | Set |
|---|---|---|
| `resolve_entity` | `graphwritetools.py:593`, sidecar `tools/resolve_entity.tool` | unattended, on-reply, third-party |
| `assert_fact` | `graphwritetools.py:709`, sidecar v3 (8 fields) | unattended, on-reply, third-party |
| `correct_fact` | `graphwritetools.py:793` + `replytools` | on-reply only (force-supersede + PIN) |
| `merge_entities` | staged only (constraint 12) | on-reply only |
| `ask_owner` | `replytools`, session-addressed | unattended, on-reply — NOT third-party |
| `find_entity` / `read_entity` / `current_time` | inherited reads | all three sets |
| `search` / `read_note` / `relate` / `prefs_write` | inherited | on-reply only |

Three frozensets, `agent/agents.py:490` / `:526` / `:579`, and which handlers are BOUND is
the whole of the enforcement (constraint 9). That structure is right and is kept.

### What must be added

**1. `close_reading` — the whole-note reading. NEW.**

The one structural addition. Shape, deliberately `assert_fact` v3's item plus three
things:

```
close_reading:
  title:  string   — what this note is about, one short line
  tags:   array of string
  facts:  array (maxItems 8, clamp REPORTED) of
            subject, predicate, object, statement,
            when, when_end, quote            (assert_fact v3 minus `confidence` — see 3)
            repeats                          (NEW — see 2)
```

Rules the sidecar and the handler enforce:

- **No `enum`, anywhere.** Every field is a string, a number, or an array of strings.
  Constraint 8 is satisfied by construction, and — a distinction worth writing down
  because it has confused this work before — `note.extract`'s schema DOES use `enum`
  freely (`prompts/note_extract.prompt:133-137`, `:159-162`) and always has. Constraint 8
  is scoped to the harmony TOOL grammar, not to a structured-output completion. A field
  that was cheap on `note.extract` is not automatically cheap here.
- **`title` is free text; `tags` is an open list.** Neither needs a closed vocabulary, so
  neither trips the segfault. `analysis/tagconsolidate.py` keeps normalizing tag drift,
  which is what it exists for.
- **Several calls are allowed and are unioned.** `max_facts` is 40 on the extraction
  path today (`note_extract.prompt:11`) and the tool item cap is 8, so a long note needs
  several. The pass's calls union into one reading; the settle is one, at the end
  (constraint 6, unchanged).
- **The model is never asked whether it was complete.** No `complete` boolean, no
  `is_final` flag. See §1 — the engine knows the pass ended clean and unclamped; the
  model only writes content.

**2. `repeats` — recurrence. NEW field, and the only capability the tool surface cannot
express at all today.**

Recurrence reaches `app.appointments.rrule` off the fact's temporal token
(`appointment_projection.py:184` for the scheduled time, `:422-438` for the recurrence
predicate). `_upsert_tokens` fills tokens from `extraction.tokens`; the conversation
passes `tokens=[]` (`graphwritetools.py:1038`), so today a conversation-written recurring
appointment projects as a one-off. That is live, masked only while the analyzer co-writes
the note, and total the day `integrate_note` goes.

Proposal: **`repeats` is an RRULE string**, validated by a strict parser in the handler
and DISCARDED (not held, not guessed) when it does not parse — the same discipline
`_close_interval` (`graphwritetools.py:379`) applies to `when_end`, which is the pattern
that made a field the model over-applies 45-times-in-56 safe by admitting 0 of the 45.

- *Why this is not a WORDS field.* An RRULE is a grammar, like an ISO date, not a
  membership choice from a list. The date shapes are the measured-buildable class
  (`when` / `when_end`), and the model already writes RRULEs on this box today:
  `note.extract`'s `temporal_tokens[].rrule` is a free `string|null`
  (`note_extract.prompt:163-166`), filled by the live model on the production
  extraction path.
- *And be precise about what is NOT evidence.* `plan_recurring_gym.json` is green, and it
  proves nothing about recurrence reaching a calendar: its scripted `value_json`
  is `{"rrule": "FREQ=WEEKLY;BYDAY=MO", "start": …}`, the tool path flattens that to a
  string, and the scenario's `value_contains` matches the flattened literal. No temporal
  token is written and `app.appointments.rrule` is never read. That is exactly the shape
  of green the harness README warns about, and it is why R0 owes a NEW scenario asserting
  the RRULE end to end rather than re-reading this one.
- **Uncertain, and the experiment is cheap and already tooled.** *Does the live model
  write a parseable RRULE through a TOOL schema, as opposed to through the extraction
  schema?* Run a `backend/evals/shape_probe.py` arm — gpt-oss-120b, reasoning low, 12
  samples, through `/api/debug/tool-probe`, scored for parseability AND for whether the
  rule matches the note's phrase — on a corpus of recurring-appointment notes. If it
  fails, the fallback is `repeats` as the note's own PHRASE ("every Tuesday", "first
  Monday of the month") parsed server-side, which is strictly easier for the model and
  strictly harder for the engine. Decide it on the probe, not on argument — that is how
  all six of the v3 gaps were decided.

**3. `confidence` — REMOVED from the tool schema. A model field is deleted, on the
measurement.**

This is the simplification one channel buys, and it is decided by evidence rather than by
taste. `confidence` was closed as v3 gap 6 and its only consumer is the `low_confidence`
hold in `supersession.decide()` (`:777`, `:829`). The measurement that shipped with it says
the model's number **never reaches the threshold on this box**: across 121 facts on three
notes the live model marked down zero legible ones, and on a note whose middle line is
explicitly unreadable it converges on exactly **0.5**, which is not `< LOW_CONFIDENCE`
(0.5). Sharpening the wording made it more consistent without moving it under the line, and
moving `LOW_CONFIDENCE` to meet it was deliberately not done. So the field is filled
perfectly, acted on never.

Under one channel it is also the wrong shape: a model that is unsure is supposed to ASK,
not to write a number that something else quietly acts on.

**What is NOT removed is the guard.** `self_confidence` is `min(engine span check, model
number)` — *only ever lowers* — and the ENGINE's half is a real signal that fires: a quote
the note does not contain caps the fact at 0.4, well under the floor, and the hold does
happen. Deleting the model's half leaves `self_confidence = the span check`, the guard
intact, and `test_note_graph_write_pg.py`'s 0.25-read pin still meaningful. What is lost is
a channel that was measured never to carry anything. `_self_report`
(`graphwritetools.py:1157-1176`) goes with the field.

*Uncertain, and worth saying:* this bets that a model asked to ASK when it cannot read a
word does so more reliably than it marks the word down. That is not measured. **The
experiment:** the same `shape_probe` corpus that produced the 121-fact number, re-run
against a smudged-note prompt that says "if you cannot read it, ask" — count the asks.
Cheap, and it belongs in R0 beside the `repeats` arm.

**4. `distinguish` on `resolve_entity` — NEW field, and the price of dropping
`ambiguous_mention`.**

The card the resolve path files names the candidate entities; the tool result does not
(§2). Naming them in the result is half the change; the agent needs a way to answer. So
`resolve_entity`'s item grows a third field:

```
resolve_entity:
  entities: array of { surface, kind, distinguish }
```

`distinguish` is free text from the NOTE — "the cardiologist", "Dana Whitfield", "the one
in Boulder" — matched by the resolver against the candidates' names, kinds and summaries,
which `_disambiguate` already assembles as `{id, name, kind, summary}`
(`pipeline.py:1687-1690`). Empty on the ordinary call. No enum, no vocabulary, so
constraint 8 is untouched.

The flow is then one channel end to end: resolve → *"'Dana' is ambiguous: Dana Whitfield
(person, staff engineer at Everlane), Dana Reyes (person, Boulder). Re-send with
`distinguish`, or ask the owner"* → the agent either answers from the note or calls
`ask_owner`. No card at any step.

*Uncertain, and named:* whether a free-text `distinguish` actually narrows. Layer 1 of the
resolver is `_exact_matches` on name (`entities.py:563-580`) and carries no notion of a
discriminator, so this needs a small matcher over the candidate summaries the card already
renders. **The experiment is a unit test, not a probe**: build the matcher against the
candidate shapes and check it separates the cases `_file_ambiguous_review` currently files.
It also raises O7 — whether the LLM disambiguator (layer 3) survives at all.

**5. What is deliberately NOT added.**

- **`kind`.** 7 legal in 80 as a string; 88 of 96 false-fires as a boolean. Closing it is
  registry work — `_fact_kind` already prefers a declared predicate's `kind`
  (`graphwritetools.py:1116`), so declaring `bodyWeight`, `bloodPressure`,
  `medicationRegimen` and their kin under `schema/defs/` closes seven scenarios without
  asking the model anything. Tier-1 work under `ENTITY_GRAPH_REFOCUS_PLAN.md`, and §5
  says which scenarios it unblocks.
- **`assertion`.** 0 legal in 72 as a string; the boolean spelling fired 0 times in 80,
  including on every "I finally sold the Civic". Neither spelling is a channel. **But the
  reading changes what is LOST by not having it** — see §5, because this is where the
  redirection buys the most.
- **A long-tail `qualifier`.** Prose on 61 of 86 facts, and an over-applied qualifier
  SPLITS an identity key so nothing ever supersedes again — worse than the collision it
  was meant to fix. The dotted path (`name.nickname.friends`) stays, bounded to the
  registry predicates declaring a `qualifier_vocab`; the measured blocker is one layer
  earlier (predicate normalization: the live model writes `has nickname` where the
  registry declares `name.nickname`, 0 of 39), and that is registry work too.
- **A structured `value_json`.** The model is never asked to nest — TOOL_SURFACE gap 5's
  deliberate narrowing, kept.
- **Any field that reports the model's own uncertainty.** `confidence` is the one that
  existed and it is removed above; nothing replaces it. Under one channel a model that is
  unsure asks, and the write path's own signals (the span check, the domain floor,
  `decide()`'s outcome) are the engine's business and are reported back to the agent as
  results rather than solicited from it.

### The three sets after the rewrite

```
NOTE_INGEST_UNATTENDED_TOOLS  = {resolve_entity, close_reading, ask_owner,
                                 find_entity, read_entity, current_time}
NOTE_INGEST_ON_REPLY_TOOLS    = UNATTENDED | {assert_fact, correct_fact, merge_entities,
                                              prefs_write, search, read_note, relate}
NOTE_INGEST_THIRD_PARTY_TOOLS = UNATTENDED - {ask_owner}
```

`assert_fact` **moves to the reply turn only.** Its job there is "record one more thing
the owner just told me", which is incremental by nature and must never license a sweep. On
the unattended pass the reading IS the write, so there is exactly one write verb and no
duplicated tokens. A reply turn that has re-read the whole note ends with `close_reading`
too, and that is what sweeps; a reply turn that only added a fact does not sweep, which is
correct.

`NOTE_GRAPH_WRITE_TOOLS` (`agents.py:586`, the set W4's two narrowings subtract) becomes
`{resolve_entity, close_reading, assert_fact, correct_fact, merge_entities}`. Both new
names join `toolregistry.NEVER_DEFAULT` in the same edit, or the curator's `allow=None`
wildcard hands them to every ordinary chat turn (constraint 9).

---

## 4. What gets deleted

Two tiers. Tier 1 is unconditional. Tier 2 depends on **O1** (§8), the EMR question.

### Tier 1 — the model-side chain and its tests

| Module / span | Lines | Note |
|---|---|---|
| `analysis/pipeline.py:401-580` `integrate_note` | 180 | the handler |
| `analysis/pipeline.py:582-636` `apply_intent` | 55 | `integrate_note`'s only caller; EMR uses `commit_intent` + `settle_note` directly |
| `analysis/pipeline.py:326-372` `_extract_note` | 47 | its only production caller is `integrate_note` |
| `analysis/integrate.py` | 64 | the Integrator agent |
| `analysis/integrate_prompt.py` | 53 | |
| `analysis/prompts/integrate_note.prompt` | 168 | |
| `analysis/prompts/note_extract.prompt` | 259 | the reading replaces it |
| `analysis/prompt.py` (`EXTRACTION_SCHEMA`, `fact_cap`, `group_texts`, `PROMPT_VERSION`) | 167 | see the `prompt_version` note below |
| `analysis/extraction.py` — `parse_extraction` and its parse half | ~350 of 1086 | the VALUE OBJECTS (`Extraction`, `ExtractedFact`, `ExtractedToken`) and the write-path helpers (`domain_floor`, `ratchet_domain`, `finalize_temporal`, `normalize_*`, `parse_datetime`) all SURVIVE — they are the commit path |
| `analysis/intent_parse.py` | 293 | |
| `analysis/graph_context.py` | 362 | the Integrator's graph-aware context builder |
| `analysis/persist.py` | 286 | `IntegrationRunLog` — only caller `integrate_note` (`pipeline.py:563`) |
| `analysis/pins.py` | 151 | only importer `persist.py` |
| `analysis/trace.py` | 143 | only caller `_file_inference_reviews` (`pipeline.py:948`) |
| `analysis/arbiter.py` — `derive_kinship_gender` (`:345-402`), `recover_dropped_fields` (`:403-493`), `dedup_intent_facts` (`:494-574`) | 230 | one production caller each, all in `integrate_note` (`pipeline.py:505`, `:510`, `:519`) |
| `analysis/flow_trace.py` — the `extract` / `intent` / `plan` arms | ~120 of 266 | the `vision` arm survives (`ingest/ocr.py:289`) |
| `evals/integrate_runner.py` + `evals/integrate_cases/00_core.json` | 362 | scores a prompt that will not exist |
| **subtotal, the old chain** | **~3,290** | |

And the card machinery one channel closes — a second, independent deletion the review-inbox
decision buys:

| Module / span | Lines | Note |
|---|---|---|
| `analysis/pipeline.py:3042-3100` + `:3259-3290` — the two `if decision.review_kind is not None:` card blocks | ~100 | the `FactWrite` report beside them already carries the reason and the conflicting statement (`:2992-2999`) |
| `analysis/pipeline.py:1742-1790` `_file_ambiguous_review` | 49 | replaced by naming the candidates in `resolve_entity`'s result |
| `analysis/pipeline.py:1496-1564` `_sync_truncation_review` | 69 | a clamped pass says so in the thread |
| `analysis/pipeline.py:1458-1495` `_sweep_stale_ambiguous` | 38 | nothing left to retire |
| `analysis/pipeline.py:1962-1990` `_file_confirm_entity_card` | 29 | `confirm_entity` goes silent |
| `agent/graphwritetools.py:1157-1176` `_self_report` | 20 | the `confidence` field goes |
| `analysis/repo.py` — the resolution arms for `fact_conflict` / `attribute_collision` / `low_confidence` / `ambiguous_mention` / `extraction_truncated` / `confirm_entity` (`:1505-1595`, `:1621-1630`, `:1813-…`) | ~150 | `merge_proposal` and `domain_promotion` arms survive |
| `analysis/display.py` — the card-field renderers for the dead kinds | ~80 of 211 | |
| **subtotal, the card machinery** | **~535** | |
| **Tier 1 `src/` total** | **~3,825** | |

Tests, all Tier 1:

| File | Lines |
|---|---|
| `tests/integration/test_extraction_pg.py` | 2167 |
| `tests/integration/test_apply_intent_pg.py` | 1567 |
| `tests/unit/test_analysis_arbiter.py` | 1427 |
| `tests/eval/{runner,cases,assertions}.py` | 1015 |
| `tests/integration/test_integrate_persist_pg.py` | 276 |
| `tests/integration/test_integrate_note_pg.py` | 255 |
| `tests/unit/test_integrate_eval.py` | 241 |
| `tests/integration/test_settle_review_cards_pg.py` | 404 |
| **subtotal** | **7,352** |

Partially rewritten rather than deleted, so not counted as deletions:
`tests/integration/test_analysis_gating_pg.py` (509 — the ingest/OCR gate survives, its
`integrate_note` assertions repoint), `test_settle_cross_producer_pg.py` (the two-producer
premise is gone; keep the file as the ONE-producer settle pin),
`test_conversation_settle_pg.py:243` (currently pins that the conversation does NOT stamp —
now the inverse).

**Tier 1 total: ~11,180 lines.** Against the parent plan's "~940 LOC", which was
`pipeline.py:305-478` + `arbiter.py` read as a span. The real number is an order of
magnitude larger because the chain is a chain: an Integrator prompt, its schema, its
parser, its graph-context builder, its run log, its pins, its trace, its eval corpus, and
6,900 lines of tests that exist to pin a producer nothing will run.

**Schema objects fed only by the deleted chain:** `app.resolution_pin` (memoized
Integrator decisions — `models/workflow.py:129`; also see `ingest/carryover.py:23`, which
carries it across a re-ingest). It has no other writer and no reader outside `persist.py`,
so the TABLE goes with the chain, not just its rows. `app.graph_rebuild_runs` survives —
the corpus rebuild is producer-agnostic and stays.

**`prompt_version` does not die with the prompt.** `facts.prompt_version` and
`note_analysis.prompt_version` are what make a corpus re-run "a planned, budgeted
migration instead of silent drift" (`analysis/prompt.py:10-13`). After the rewrite the
version they carry is the note-conversation persona's prompt version, sourced the same way
from its own prompt file's frontmatter. The column stays; its source moves.

### Tier 2 — conditional on O1

If EMR is re-pointed off `IntegrationIntent` (§8, O1):
`analysis/intent.py` (284), the rest of `analysis/arbiter.py` (~520 — `plan_intent`,
`ArbiterPlan`, `PlannedFact`, `compute_signals`, `plan_to_extraction`, the grounding
predicates), and `pipeline.commit_intent` / `_resolve_from_intent` /
`_file_inference_reviews` / `canonicalize_intent` (~390). **~1,200 more lines**, plus a
rewrite of `ingest/emr/importer.py`'s intent-building half (~200 changed, not deleted).

If EMR is NOT re-pointed, all of that stays alive for one deterministic producer, and the
`low_confidence_inference` card kind stays with it.

---

## 5. What the tests become

### The old chain's tests go

All 6,948 lines in §4. They are not a safety net for this rewrite; they are a
specification of the producer being removed. Keeping them would mean keeping the producer.

### The harness is the behavioural spec, and it was already written against the new design

`backend/tests/harness/` is the one asset that survives the redirection unchanged in
purpose, and it is more than survivable — **it is already the target design under test.**
The runner drives `resolve_entity` + `assert_fact` through the real `NoteGraphWriter`
against real Postgres, emits a complete extraction per step, and sweeps on it
(`runner.py:494-511`). The one thing it does that production does not is exactly the one
thing this plan adds. What changes in the harness is a rename and a narrowing, not a
rewrite:

1. **`_tool_calls` targets `close_reading` instead of `assert_fact`** — the same
   synthesised arguments, one call shape further along. The contract stays: *the
   synthesiser is never tuned to the engine*; if a scenario fails, the ENGINE changed.
2. **The scenario format's `extraction` key becomes the READING.** Today a scenario
   authors a `note.extract` JSON and the runner lowers it to tool arguments, dropping four
   fields (`assertion`, `kind`, long-tail `qualifier`, structured `value_json`) that no
   longer reach the graph — a documented, confusing gap between what an author writes and
   what is tested (`harness/README.md`, "Four of those authored fields no longer reach the
   graph"). With `note.extract` deleted the intermediate is fiction. **Re-cut the format
   onto the reading's own shape**, so what an author writes is what the model would send.
   The `tool_calls` override stays for the cases the faithful default cannot express (a
   deliberately fumbled `quote`, an object left as a literal) and keeps its rule: it is
   not a way to make a scenario pass.
3. **`sweep_note` in the runner stops being a divergence** and becomes the thing under
   test, because production now does the same on the same licence.

### The 23 xfails, re-read against a surface that can now be extended

The `xfail` strings' "W5 must not delete" lines are void. What is NOT void is the
*measurement* in each — those were bought with live-model probes and they still describe
this box. Sorted by what the redirection actually changes:

**(a) Newly closable — the blocker was the frozen surface, and it is unfrozen (1).**

- `plan_recurring_gym` is GREEN today on a flattened `value_json` string (§3), so it
  asserts nothing about recurrence reaching a calendar. **Recurrence in production** is
  what `repeats` closes, and it is the one capability with no survivor at all today. It
  needs a NEW scenario asserting the RRULE reaches `app.appointments.rrule` through
  `_upsert_tokens` and `_recurrence_rrule`, and `plan_recurring_gym`'s own assertion
  should be tightened onto that column rather than left matching a sentence.

**(b) Closed by the reading, or by the agent ASKING (6).** The gap-1 (`assertion`)
scenarios split. Six of the ten are disposals and endings — `own_acquire_then_dispose`,
`own_theft_ends_ownership`, `own_reacquire_same_entity`,
`own_dispose_refresh_swallows_negation`, `plan_cancelled`, `adv_negation_then_reassert`.
Under a *ledger* those needed a negation channel, because note 2 had to reach back and
negate note 1's fact.

**One channel changes the answer, and sharpens it.** Two distinct cases hide in the six,
and they were previously answered as one:

- *The ending is in THIS note.* "We lived there from 2019 until 2023" — the reading states
  the fact with `when_end` and `_close_interval` admits it. Nothing new is needed; this is
  v3's shipped channel, and it is what makes these scenarios re-authorable rather than
  merely re-labelled.
- *The ending is in a LATER note.* "I finally sold the Civic" against an `owns Civic`
  written by a note from last year. The reading of note 2 cannot retract note 1's fact and
  never could — the sweep is per-note by construction. But the agent holds `read_entity`
  and is told to read before it writes, so it SEES the active `owns Civic`. Under one
  channel that is not a card to file, it is a question to ask; the owner confirms; the
  reply turn's `correct_fact` force-supersedes. **So the channel that closes these was
  always `correct_fact`, and what one channel changes is that reaching it stops being a
  fallback and becomes the designed path.**

That reframes the open question. It is no longer *"can the tool surface say negated"* —
it cannot, and it does not need to. It is **"does the agent notice the contradiction and
ask?"** which is a prompt-and-loop question, measurable against the live model rather than
arguable from the schema. The re-authoring in R0 tests the first case; the second needs a
live-model scenario, which is R5's adversarial work carrying a second passenger.

**(c) Genuinely still unreachable through the model (4).** `adv_standalone_negation_active`
("I am not diabetic" — a negative fact with no prior to close), `rel_reported_secondhand`
(hearsay provenance), `own_disputed_low_confidence`, `health_diagnosis`'s negation half.
These need a WORDS field and the box will not fill one. The surviving channel is
`correct_fact` on the owner's reply turn, unchanged.

**(d) Registry work, not tool work (8, unchanged by this plan).**
`adv_unit_change_false_conflict`, `health_bp_timeseries`, `health_med_change`,
`hist_backdated_measurement_insert`, `hist_dst_boundary_local_day`,
`hist_preference_retrospective_still_supersedes`, `adv_value_json_abuse`,
`health_low_confidence_ocr_guard`. `_fact_kind` already reads a declared predicate's
`kind`; declaring the predicates closes them. `health_low_confidence_ocr_guard` is the
corpus's one SAFETY scenario and declaring `medicationRegimen` alone closes both of its
roots — the sharpest argument for the tier-1 declaration work, and it belongs to
`ENTITY_GRAPH_REFOCUS_PLAN.md`, not here.

**(e) Extractor-shaped, and they die (1) or change meaning (2).**

- `rel_enumerated_children_fan_out` — the arbiter's `derive_kinship_gender`, "8 facts where
  main wrote 12". This was the one accepted gap with no survivor, and the redirection
  **removes the reason it had no survivor.** The loss was structural only because the
  extraction prompt forbids inference (*"Do not infer unstated facts (a gender from
  'wife')"*, `note_extract.prompt`) and the inference was bolted back on afterwards by a
  deterministic helper. The reading is not the capture stage of a two-stage pipeline; it
  is the agent stating what the note MEANS, and the ratified posture is that a clear
  inference commits (D2's surviving half — one channel widened when the agent ASKS, it did
  not narrow what commits). **So this becomes a line in the reading's prompt, not a helper and not an
  `xfail`.** Re-author it as a live scenario; if the model does not fan the roster out,
  record the loss then, with a measurement rather than an inheritance.
- `i5_inferred_sensitive_holds_for_review` — passes today asserting the OPPOSITE of its
  filename, because the I5 hold lived in the arbiter and D2 removed it. With the arbiter's
  model half gone the filename is not just stale, it names a mechanism that no longer
  exists anywhere. **Rename it** to what it actually pins (the domain floor), in this
  rewrite rather than "kept only because two ratified docs cite it by name" — this doc is
  one of the successors to those docs and hereby stops citing it.
- `adv_prompt_injection_body_inert` — a tautology by its own description, and *more* of
  one now: the harness IS the model, so a scripted extraction that declines to comply
  proves only that the script declines. The write path is a tool loop, which is a shape
  hostile text could plausibly drive. **It cannot be fixed inside the harness.** It needs
  a live-model adversarial scenario, which W3 was briefed to build and did not — carried
  here as wave R5 rather than left as an open note for a third time.

**(f) Pre-existing engine gaps, unaffected (2).** `own_transfer_subject_cannot_move`
(candidate read scopes to one entity), `adv_same_first_name_collapses` (the auto-link rule
fires on one exact match). Neither is about the producer.

### What the suite becomes

- **`tests/harness/`** — the behavioural spec, re-cut onto the reading. Target: the 52
  currently-green scenarios stay green through the re-cut (that IS the acceptance test for
  R2), plus buckets (a) and (b) attempted and measured.
- **`tests/integration/test_note_converse_pg.py`** — grows the pass-ending matrix:
  clean+reading sweeps; clean-no-reading does not; clamped does not and files a card;
  `waiting_on_owner` stamps and flips and does not sweep; every ending flips
  `integration_state`. `test_a_finished_pass_settles_the_conversation_and_not_the_note`
  inverts and is rewritten, not deleted — it is the red test that has been holding this
  wave honest.
- **`tests/integration/test_note_graph_write_pg.py`** — grows `close_reading`: title/tags
  reach `note_analysis`, `repeats` reaches `app.appointments.rrule`, a re-assert returns
  `ALREADY` with the same id so the reading's `touched` covers unchanged facts. Its
  0.25-read pin **stays and must still pass** — with the model's `confidence` gone the read
  is capped by the span check instead, and that is the pin that proves the guard survived
  the field's deletion.
- **Card-to-result, and this is the acceptance test for §2.** Each of the ten reachable
  `decide()` sites asserts (a) the row's `status` is unchanged from today, (b) NO
  `review_items` row is written, and (c) the result text names the reason and the
  conflicting statement. (a) is what keeps constraint 5 honest: the model gained no power
  over what lands. `test_settle_review_cards_pg.py` (404 lines) goes — it pins the
  retirement of cards that are no longer filed.
- **RLS** — every table the wipe touches keeps its isolation test (CLAUDE.md #3); the
  wipe migration adds none, and `analysis/settle_owner.py`'s `tests/unit/test_settle_owner.py`
  guard (no write site in `src/` without `settle_owners`) extends to `close_reading`.
- **`backend/evals/`** — `shape_probe.py` stays and is the instrument for the `repeats`
  arm and the bucket-(b) re-authoring. The `integrate_cases` corpus goes; a
  `close_reading` corpus replaces it, which is one of the two things W3 was briefed to do
  and did not.

---

## 6. The wipe

### Scope

**WIPED — notes and everything derived from them.**
`notes`, `note_clarifications`, `chunks`, `note_analysis`, `attachments`,
`attachment_extracts`, `facts`, `entities`, `entity_aliases`, `entity_distinctions`,
`entity_mentions`, `temporal_tokens`, `resolution_pin` (and its table, §4),
`canonical_predicates`, `predicate_aliases`, `note_conversations`,
`note_conversation_tool_calls`, `review_items`, `graph_rebuild_runs`; the projections
`appointments`, `appointment_locations`, `place_geofence`, `geofence_state`, `encounters`,
`encounter_providers`, `encounter_diagnoses`, `lab_results`; and the wiki
`wiki_articles`, `wiki_sections`, `wiki_citations`, `wiki_index`, `wiki_links`,
`wiki_revisions`, `wiki_talk_topics`, `wiki_talk_posts`, `wiki_source_exclusions`.

The wiki is in scope by the owner's word, and independently by derivation: every wiki row
is a projection of facts that are themselves projections of notes, and
`wiki_citations.chunk_id` is `NOT NULL` against a chunk table being emptied. **Wiped as
DATA. The wiki code stays** — `PHASE6_WIKI_PLAN.md` is the project's next frontier
(CLAUDE.md), the builder needs no change to work against the new graph (§2), and removing
the subsystem is not a call anyone made.

**KEPT — the box's operation and its identity.**
`tasks`, `task_groups`, `task_runs`, `settings`, `triggers`, `schedules`, `pipelines`,
`actions`, `principals`, `domains`, `view_scope`, `view_audit`, `device_sessions`.

**Decided for everything else.** The default is KEEP; each deletion is justified.

| Table(s) | Call | Why |
|---|---|---|
| `agent_sessions`, `agent_turns`, `agent_episodes`, `agent_episode_refs`, `agent_memory`, `agent_session_plans`, `turn_attachments`, `turn_tool_artifacts` | **KEEP, minus the note threads** | These hold ordinary chat, not note ingestion. `note_conversations.session_id` FKs `agent_sessions.id ON DELETE CASCADE` (`models/note_conversation.py:246`), so deleting only the sessions that appear in `note_conversations` takes the note threads and leaves every chat thread. The corpus snapshot under `Last verified` has no note conversations at all, so this is a no-op today and a correct rule tomorrow. |
| `runs`, `run_steps`, `llm_usage`, `box_events` | **KEEP** | Operational history and cost accounting; not derived from notes. The run rows for deleted `integrate_note` jobs are history of a thing that happened. |
| `jobs` | **KEEP, minus dead kinds** | Delete queued/running `integrate_note` rows, or the worker fails them one by one with "no handler for kind" forever after the deploy (`worker.py:172-178`). |
| `external_sources`, `external_source_chunks` | **KEEP** | Video/article corpus, keyed on URLs, never on notes; carries its own `title`/`analyzed_at` (`external/corpus.py:120-142`). Nothing in it derives from a note. |
| `research_reports`, `research_run_state`, `research_share_links`, `report_groups` | **KEEP** | Deep-research output, produced from the web. No note lineage. |
| `lists`, `list_items` | **KEEP** | `list_items.source_note_id` is `ON DELETE SET NULL` (`models/lists.py:51`), so a checklist survives the wipe with its provenance blanked. Deleting an open shopping list because its source note went is a loss the owner did not ask for. |
| `proposals`, `proposal_nodes`, `intake_links`, `intake_sessions`, `intake_submissions` | **KEEP** | The approval substrate is not note-derived. `intake_submissions.note_ids` is a plain array with no FK (`models/intake.py:103`), so it will hold dangling ids — cosmetic, on a display trace, and cheaper than deleting the owner's record of what was submitted. Named so it is not rediscovered as a bug. |
| `owner_prefs` | **KEEP** | Standing instructions the owner wrote (D15). Not derived from anything. |
| `events` | **KEEP** | The workflow event log is append-only and domain-firewalled (`models/workflow.py:102-119`); a `note.ingested` row is a record that something happened, and the dispatcher's `dispatched_at` bookkeeping is what stops a re-delivery re-firing. Truncating it would make every past event undelivered. |
| `subjects` | **KEEP** | Identity/firewall substrate (`models/core.py:30`), read by `visible_subjects` and `family_group`. Not derived from notes, and it belongs with `principals`/`domains` above. |
| `archivist_memory`, `pet_memory`, `pet_state`, `jmolt_*`, `jcode_sessions`, `jlaunch_runs`, `aprs_packets`, SDR tables, `generated_images`, `location_fixes`, `host_metrics`, `host_metrics_hourly`, `deploy_history`, `model_reservations`, `media_analysis_results` | **KEEP** | Unrelated subsystems. Nothing derives from a note. |

`entities` deserves one sentence, because it is the one KEEP-looking table in the WIPE
list: an entity is not derived from one note, but the graph is derived from the corpus,
and an entity with no surviving mention is what `_delete_orphaned_entities`
(`analysis/purge.py`) removes anyway. Wiping the corpus and keeping its entities would
leave every entity row uncitable.

*The corpus sizes this section was scoped against are a snapshot as of this doc's
`Last verified` date, not a standing fact* (DOC_LIFECYCLE R1): low tens of notes and
chunks, low hundreds of facts, tens of entities and review items, ~150 registered
predicates, ~20 wiki articles. The point of the numbers is that the wipe is small enough
that a TRUNCATE is instant and the day-one corpus is genuinely empty — not any particular
count.

### The migration question

Three options were weighed; the recommendation is (a), and it turns on a fact about how
this box is operated rather than on schema aesthetics.

**(a) Keep the chain; add a data-wipe migration. RECOMMENDED.**

One new migration whose `upgrade()` is a `TRUNCATE … RESTART IDENTITY CASCADE` over the
WIPED list plus the two scoped deletes (note-thread `agent_sessions`, dead-kind `jobs`),
and whose `downgrade()` raises — data deletion is not reversible and pretending otherwise
in a `downgrade` is worse than refusing.

*Why it wins.* **It is the only option that is already no-terminal.** `Ops → Update` runs
`docker compose run --rm migrate` (`deploy/update-inner.sh:984`), which is
`alembic upgrade head`. The owner presses Update; the wipe happens as part of the deploy
they were already going to do. No shell, no `.env` edit, no host step — CLAUDE.md #10
satisfied by construction rather than by a new feature. The keepers need no dump and no
restore, because nothing drops them. CI and testcontainers are unaffected: the migration
truncates empty tables in a fresh schema and costs nothing. It is reviewable — a table
list a reviewer can check against §6's table above, which is exactly the review a
collapsed baseline makes impossible.

*Its cost, stated.* The chain keeps growing, and `alembic upgrade head` from scratch keeps
getting slower for every test session and every CI job. That cost is real and it is not
what this plan should pay down: a baseline collapse is its own change with its own risk,
and bundling it with a data wipe means a failure in either is a failure in both.

**(b) Collapse to a fresh baseline and restore the keepers. NOT recommended.**

The owner's keepers are the problem, not the schema. `settings` (133 rows), `tasks` (6),
`triggers` (25), `schedules` (19), plus `principals`/`device_sessions` for not being
locked out — all of which are ALSO seeded by migrations, so restoring the owner's edited
copies over a freshly-seeded default set is an upsert with a conflict policy nobody has
written. `deploy/reset-inner.sh` is the existing machinery for exactly this shape and its
own comment says what it costs: it carries the owner key across and *"even seed-table
edits"* are gone. Making it carry four more tables means writing that conflict policy, on
a live box, inside a change that also rewrites ingestion. And the benefit — a shorter
chain — is not something the owner asked for.

**(c) `deploy/reset-inner.sh` as-is.** Rejected on the same ground: it restores the
automations to seed defaults, which contradicts *"I want to keep the tasks in the
settings."*

**What (a) obliges, per CLAUDE.md #8.** `scripts/dev-setup.sh` needs no new dependency —
the wipe adds no tool and no setup step. Say so in the PR rather than leaving the reviewer
to check. The one deploy-time consequence is ordering, and it is the ordering the deploy
already has: `docker compose stop api worker` before `migrate`
(`deploy/update-inner.sh`), so nothing is writing rows into tables being truncated.

### Day one after the wipe

Worth stating plainly, because it is the moment the design gets its first real test and
because "the corpus rebuild re-derives from notes" — the producer-agnostic escape hatch
the settle work leaned on — has nothing to re-derive from.

**The box comes up with an empty graph and an empty wiki, and fills as the owner captures
notes.** Concretely, in order:

1. `app.notes` is empty. `backfill_pending_integration` finds nothing to enqueue. The
   home stream is empty; no note carries an "analyzing…" chip because there are no notes.
2. The wiki is empty. `wiki_rebuild` over zero entities is a no-op, and the wiki tab shows
   nothing rather than a shelf of stale articles about people whose facts are gone.
3. The automations run exactly as before — every task, trigger, schedule and setting
   untouched. Daily news, jmolt, SDR, location, the ops surface — none of it
   goes through ingestion and none of it notices.
4. **The first note the owner captures is the acceptance test.** It ingests, opens a
   `note_converse` thread, resolves its cast, ends with a `close_reading`, sweeps nothing
   (there is nothing to sweep), stamps a title, flips to `integrated`, and shows the owner
   what it did in the thread (D3). The note's Analysis tab renders facts and entities with
   a real `analyzed_at`.
5. **The second capture of the same note is the real test.** Edit it to remove a sentence;
   the re-ingest flips it `stale`, the reconciler enqueues a `note_converse`, the new
   reading does not state the removed fact, and the sweep retracts it. That single
   round-trip is what `integrate_note` did and what nothing has done since — and it is
   the one behaviour whose absence `W5_PRECONDITIONS.md` §4 called "a strictly larger
   loss" than anything the settle work had accepted.

"Working on day one" is (4) and (5), on the owner's real box, with the owner watching the
thread. That is the sign-off wave (R6), and the plan stays `In progress` until it lands
(DOC_LIFECYCLE R4's one legitimate hold).

---

## 7. Sequencing

Per-PR discipline is unchanged (`PROCESS.md`); what changed is that D13's *"no producer
removed before its replacement is merged"* no longer blocks the wave, because the corpus
the rule was protecting is being deleted on purpose. It still shapes the ORDER: the
replacement lands and is green before the old producer goes, so that a failure between two
PRs leaves a working box rather than a half-built one.

**R0 — Three measurements, no production code.** The `shape_probe` arm for `repeats`
(§3.2); the smudged-note ASK arm that decides whether dropping `confidence` is safe
(§3.3); and the bucket-(b) re-authoring of the six disposal scenarios against a reading
(§5). All three are cheap, all three change what R1 builds, and each ends with its answer
written into this doc. The `distinguish` matcher (§3.4) is a unit test rather than a probe
and can ride R1.

**R1 — `close_reading`, beside `assert_fact`.** The sidecar, the handler, `_upsert_tokens`
fed from the reading, `repeats` and its strict parser, the clamp promoted to a reported
signal, and `resolve_entity` gaining `distinguish` plus the candidate names in its result.
Bound on the unattended set; `assert_fact` still bound too. Nothing is deleted and nothing
sweeps yet: a reading commits exactly as `assert_fact` does. Green on
`test_note_graph_write_pg.py`.

**R1b — one channel: card to result.** Separable from R1 and worth its own PR, because it
is a behaviour change to the SHIPPED write path rather than a new verb, and because its
acceptance test is a matrix rather than a feature. Delete the two `review_kind` card
blocks, `_file_ambiguous_review`, `_file_confirm_entity_card`, and the `confidence` field
with `_self_report`; widen the hold result's wording; name the candidates in
`resolve_entity`. `merge_proposal` becomes a message in the thread. `domain_promotion`,
`inverse_proposal`, the EMR firewall card and wiki lint are untouched. `decide()`'s
`Decision.review_kind` stays — it is what the result reads.

**R2 — the harness re-cut.** `_tool_calls` onto `close_reading`, the scenario format onto
the reading, the runner's `sweep_note` re-labelled from divergence to spec. **Acceptance:
every currently-green scenario stays green** (52 of 75 at this doc's `Last verified`). This is the wave that proves the reading
carries everything the old surface carried, and it runs before anything is removed.

**R3 — the settle moves.** `settle_conversation` runs the whole settle for a pass with a
reading: `sweep_note`, `settle_tail`, `stamp_analysis` — and NOT the two review-card
halves, which R1b deleted, so the settle is three steps rather than five. The gate of §2,
third-party clause included. The `integration_state` flip moves to the terminal block and the reconciler,
`has_active_analysis`, `POST /notes/{id}/analyze` and `_integration_drained` repoint with
it, in this PR — they are one change, and splitting them leaves a box that re-enqueues a
dead job kind every five minutes.

**R4 — the deletion.** The old-chain half of §4, in one PR, because the chain is a chain
and a half-deleted one does not typecheck. `assert_fact` narrows to the reply set.
`review_items` loses `low_confidence_inference` and `new_predicate` (subject to O1), and
`review_items.settle_owner` loses its last reader. The docs of §9 are reconciled in the
same PR.

**R5 — the wipe, and the two things W3 owed.** The migration of §6. Beside it, the two
items W3 was briefed to build and did not, now unblocked because their targets exist: a
`close_reading` eval corpus, and a real-model adversarial scenario against a hostile note
body driving a tool loop (risk 1, and `adv_prompt_injection_body_inert`'s standing
tautology).

**What has to be true before the owner's box runs R5:** R2 green; R3 green with the
pass-ending matrix passing; R4 merged so no dead job kind is enqueued after the truncate;
an `Ops → Export` taken (the existing PWA lever, `supervisor/src/supervisor/app.py:393`)
so the pre-wipe state is recoverable without a terminal; and the owner told, in the PWA's
terms, that the first capture after the update is the first note the new system has ever
seen.

**R6 — sign-off on the box.** (4) and (5) of §6's day-one list, confirmed by the owner in
the thread. The plan archives on this.

---

## 8. Risks and open decisions

Stated as questions with options, not papered over.

**O1 — Does EMR come off `IntegrationIntent`?** *Cannot be decided from the code.* The
deterministic importer routes through `arbiter.plan_intent` and `pipeline.commit_intent`
(`ingest/emr/integrate.py:300-313`), which is the ONLY reason `arbiter.py`, `intent.py`
and ~390 lines of `pipeline.py` survive Tier 1. Reading `plan_intent` (`arbiter.py:94-186`)
against EMR's input, it is close to a pass-through there: EMR mints no ambiguous
resolutions and no inferred facts, so the ambiguity flags and the I5 net never fire and
what remains is `validate_intent` plus a weight. So a re-point — EMR builds an
`Extraction` and calls `commit_facts` + `settle_note` directly, like every other producer
— looks cheap and would take Tier 2's ~1,200 lines with it.
*Options:* **(i)** leave EMR alone in this rewrite and keep Tier 2 alive (recommended for
sequencing: EMR is a different plan, `EMR_IMPORT_PLAN.md`, and the owner's redirection was
about notes); **(ii)** re-point it in R4 and delete the arbiter entirely; **(iii)** re-point
it in a follow-on wave once the note path is proven.
*Recommendation:* (iii). It keeps this rewrite's blast radius on notes, and (ii)'s payoff
is line count rather than capability.

**O2 — Is `repeats` an RRULE or a phrase?** Decided by R0's probe (§3). If neither works,
recurrence stays lost and that is a capability regression to record in `ROADMAP.md`, not
to hide.

**O3 — Does the agent NOTICE a contradiction with a fact another note wrote, and ask?**
Reframed by the one-channel decision (§5(b)). The half of the six disposal scenarios whose
ending is stated in the same note is decided by R0's re-authoring and is a schema question.
The other half is not a schema question at all and never was: the reading of note 2 cannot
retract note 1's fact, the agent can SEE that fact through `read_entity`, and the channel
that closes it is ask → `correct_fact`. So the open question is behavioural — does the
model reach for the question — and it needs a live-model scenario (R5), not a probe.
*This is the largest single unknown in the plan.*

**O3b — Does dropping `confidence` cost anything?** The field is measured never to fire the
guard it feeds, so removing it costs nothing observable; the bet is that a model told to ask
when it cannot read a word does so more often than it marked the word down (which was zero
times in 121 facts, so the bar is low). R0's smudged-note arm counts the asks. If the model
neither marks down nor asks, the honest conclusion is that this box has no legibility
channel at all, and that is worth knowing plainly rather than keeping a field that
simulates one.

**O4 — The parked-thread edit.** Carried forward from `W5_PRECONDITIONS.md` §2 unsolved: a
note edited while its conversation is parked on `ask_owner` is skipped by the repointed
reconciler, so the edit is never re-read. *Options:* reclaim the parked thread when
`note_conversations.note_body_sha` no longer matches the composed body; let a `'stale'`
note override the guard; accept it and surface the parked thread in the notes tab.
**Whichever is chosen, re-read every existing writer of `note_body_sha` against the new
job first** — that field acquiring a second job is exactly the shape of S3's failure 4.
*Recommendation:* the `'stale'` override, because it adds no job to an existing field.
Not decided here.

**O5 — What happens when a reading is honestly incomplete?** The gate refuses to sweep on
a clamp or a truncation, which is safe. But a model that simply *forgets* a fact on an
otherwise clean, unclamped pass produces a reading that looks complete and is not, and the
sweep retracts a true fact. This is the residual risk the whole design rests on, and it
cannot be gated away — it is the same risk `note.extract` has carried in production since
Phase 2, which is the honest comparison and the reason to think it is survivable rather
than a proof that it is. *The measurement:* over the first N notes after the wipe, log
every retraction the sweep performs with the fact's statement, and read them. On a small
corpus that is a handful of lines a week and it is the only calibration available, since
the pre-wipe corpus §4(a)'s experiment would have used is gone. **Uncertain**, deliberately
so, and named rather than assumed away.

**O6 — Must `file_correction` still mint a note?** Re-openable now that the wiki is being
rebuilt (§2). Not this rewrite's question; recorded so Phase 6 starts from the question.

**O7 — Does the LLM disambiguator (resolver layer 3) survive?** *Not decidable from the
code, and surfaced by the one-channel decision rather than by the rewrite.*
`_disambiguate` (`pipeline.py:1653-1741`) is a second, cheaper model making an identity
call — with a snippet of context, a list of candidates, and none of the note. The ingesting
agent has the whole note, the graph it just read, and a channel to the owner. Under one
channel, a second model deciding what the first could decide is a third party in a design
that just went to one.
*Options:* **(i)** keep it as a cheap pre-pass and let `distinguish` handle what it
declines — no deletion, two deciders; **(ii)** delete it and let the resolver return
candidates straight to the agent (`_disambiguate` ~89 lines, `entity_disambiguate.prompt`
35, `evals/disambiguate_runner.py` 128 and its corpus — ~260 more lines, and one fewer
model call per note); **(iii)** keep it but demote it to a HINT in the result rather than a
verdict.
*Recommendation:* (ii), decided after R1's `distinguish` matcher exists and can be measured
against the cases layer 3 currently answers. Do not bundle it into R1b — that PR is already
changing the write path's relationship to the owner, and this changes the resolver's.

**O8 — Where does a staged merge appear?** §2 says a fold becomes a message rather than a
card, and the reply turn already holds `merge_entities`. What is undecided is where the
message goes when the agent notices the duplicate while reading a LATER note: re-open the
earlier note's thread, raise it in the current one, or start a thread of its own. All three
are one channel; they differ in where the owner finds it. Not decided here.

**O9 — May one pass raise SEVERAL questions?** `ask_owner` enforces one open question at a
time (`agent/asktools.py`: a second call while `waiting_on_owner` is refused and told what
is outstanding). That cap was sound when the inbox was a card queue and asking was the
exception. Under one channel it becomes the design's main cost, and it is paid in the
owner's attention rather than the box's CPU: a note with three ambiguities costs three
passes, three re-ingests and three trips to the inbox, spread over however long the owner
takes to answer each — and each pass re-reads the note from scratch while the facts it
already committed sit unprojected in between.

Batching also *improves the answers*, which is the part that is not just efficiency: the
owner sees the whole ambiguity at once, so an answer to one question can inform another
("it's the cardiologist" changes how "the new med" reads), and the candidate context the
resolver already has — *Dr. Alice Chen, cardiology, 4 notes* vs *Dr. Ray Chen, paediatrics,
2 notes* — is the information the retired `ambiguous_mention` card was carrying and the
agent was never handed. Mocked end to end for the owner (an interactive walkthrough of a
three-question note) rather than argued.

What it costs to build: the one-at-a-time latch is what makes `record_owner_reply`'s claim
atomic — the `waiting_on_owner → running` flip IS the latch that stops a second reply
appending the same answer twice, and `latest_question` reads exactly one open ask. A batch
needs a question SET with per-question answers, so the claim moves from the state flip to
something addressed per question, and the clarification block composes several Q/A pairs
rather than one. That is the real work; the tool schema is the easy half. **Not decided.**

**O10 — Nothing tells the owner a thread is waiting.** `waiting_since` is measured and
rendered in the inbox row, and nothing acts on it. There is a notifications SSE stream
(`api/notifications.py`) and an `fcm_token` table, and `ask_owner` uses neither — so a
question is discovered whenever the owner next opens the PWA, and `waiting_on_owner` is
never reaped, so an unanswered one waits forever while holding the note's single live slot.

That was tolerable while the inbox was one of two channels and cards accumulated quietly.
It is not tolerable as *the* contract: "the agent asks when it isn't sure" is only a
contract if asking reaches the owner. Decide the notification (push on first ask? a daily
digest? nothing, and rely on the habit of opening the app?) and decide the reaper's
policy separately — a stale-question timeout has to choose between failing the pass, which
discards a reading the agent already wrote, and settling it unanswered, which commits a
reading the agent said it could not finish. **Not decided.** Both halves are cheap to build
and neither is obvious to choose.

**Carried risks, unchanged from ratification.** Intake is third-party text driving an
owner-identity session (risk 1) — the third frozenset still narrows it and
`close_reading` replaces `assert_fact` inside that set, so a stranger's words may still
cause a fact and nothing else. Cost: the reading restates the note in full on every pass,
which is comparable to what `note.extract` cost and cheaper than the 5–10× the parent plan
accepted, because the Integrator's second call is gone. The abliterated checkpoint remains
selectable and remains the wrong thing to select for a persona holding write tools.

---

## 9. Docs to reconcile

In the PR whose wave makes each false, per `DOC_LIFECYCLE.md` transition 5.

- **`docs/reference/ANALYSIS.md`** — the largest. Its review-gate sections describe a card
  inbox that mostly stops existing (§2), and "Reprocessing" is the Living doc that
  asserts the retraction behaviour; it stays TRUE under this design (unlike under the
  teardown, which would have made it false), but its mechanism changes from an extraction
  by `integrate_note` to a reading by the conversation. Also the review gates, the arbiter
  holds, the I5 net, `_apply`'s decomposition.
- **`docs/reference/ASSISTANT.md`** — #10 (untrusted-origin content and background jobs),
  the memory model, `owner_prefs`.
- **`docs/reference/DESIGN.md`** — the largest change after ANALYSIS.md. The inbox's two
  tabs collapse to one channel with two lists (§2); the notes list stops being a card table
  and becomes a query over `note_conversations.state`; the held-fact treatment on the
  Analysis tab becomes load-bearing, since it is now the durable view of anything the write
  path could not settle. The write chip and D3's rendering are unchanged.
- **`docs/reference/ARCHITECTURE.md`**, **`docs/ROADMAP.md`** — Phase 2/3 no longer
  describe a two-stage extract→integrate pipeline.
- **`docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md`** — bucket (d) of §5 is its tier-1
  backlog, now with eight named scenarios.
- **`docs/reference/PREDICATE_CANONICALIZATION.md`** — `canonical_predicates` and
  `predicate_aliases` are wiped; the registry re-accumulates from the new corpus.
- **`docs/plans/PHASE6_WIKI_PLAN.md`** — the wiki tables are wiped and rebuilt; O6.
- **`docs/plans/EMR_IMPORT_PLAN.md`** — O1.
- **`backend/tests/harness/README.md`** — §5 rewrites its gap table, its "what W5 may not
  delete" section (void) and its "gate this corpus does NOT close" section (closed).
- **`backend/evals/README.md`** — the `integrate` corpus goes, a `close_reading` corpus
  arrives.
- **`docs/plans/README.md`** — this doc's row, and the three superseded rows.
- **Migrations to un-seed or amend, not docs:** `0040` (the `note.ingested` →
  `integrate_note` trigger seed, and the `resolution.changed` trigger whose consolidate
  pipeline drove retroactive predicate consolidation), `0041`
  (`reconcile_pending_integration`'s schedule, repointed in R3), `0194` (the
  `note_converse` seed, which becomes the only note producer).

## Related

- `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` — the parent. W1–W4 are shipped and this
  doc builds on them; W5a/b/c are superseded by §4 and §7.
- `docs/plans/SETTLE_OWNERSHIP.md` — S1/S1b/S2 shipped and are the substrate this design
  writes through; S3's proof is §1's spine; S4/S5 are superseded by §2 and §7.
- `docs/plans/W5_PRECONDITIONS.md` — its FINDINGS are cited throughout and are the most
  useful reading in the repo for what the old chain produced that nothing else does; its
  RECOMMENDATIONS are superseded by §2.
- `docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md` — the two-tier predicate model, and the
  home of §5 bucket (d).
- `backend/tests/harness/README.md` — the behavioural spec and the measurements this plan
  reasons from.
