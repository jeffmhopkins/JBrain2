# Who owns a note's whole-note settle

> **Status:** In progress · **Last verified:** 2026-09-10 · **Waves:** S1✅ S2✅ S3✅ S4◻️ S5◻️
>
> **S1 shipped**, with three amendments the build forced. Each is argued in place below
> and marked **[amended in build]**: the key is a claim SET (`settle_owners text[]`)
> rather than a single owner, the derived-shadow subquery is deliberately NOT scoped by
> it, and the column carries a server default.
>
> **S2 shipped.** `settle_note` is split into `sweep_note` / `settle_tail` /
> `stamp_analysis`; `integrate_note` and `emr_parse` still call the composition and are
> byte-unchanged; the conversation calls `settle_tail` at the end of a clean pass
> (`analysis/clarify.settle_conversation`, from BOTH turn paths) and never
> `stamp_analysis`. One thing the recommendation below did not anticipate, argued in S2's
> section: the two REVIEW-CARD halves stayed in the composition rather than moving into
> `sweep_note`, so the new caller cannot reach them.
>
> **S3 shipped**, in the PR immediately after S2 and for the reason S1's amendment gave:
> the conversation now RELEASES its `conversation` claim at the end of a clean pass, so
> the leak S1 accepted knowingly is closed rather than growing. S4-S5 stand as written.

The question `AGENT_INGEST_CONVERSATION_PLAN.md`'s W5a blocked on, answered:
`settle_note` (`backend/src/jbrain/analysis/pipeline.py`) is whole-note and was filtered
by `note_id` alone, and up to three producers write one note. This doc
decides which of them may sweep it, on what key, and in what order the change lands.
Its sibling is `backend/src/jbrain/ingest/emr/ownership.py`, which settles the same
question for the *write surface* (D9); this one settles it for the *sweep*.

Everything below is mechanical. Where a claim is not proven, it says "uncertain" and
names the experiment.

## The finding: the loss is shipped, not prospective

`settle_note`'s sweep (`pipeline.py:1147-1159`) retracts every fact of the note that is
unpinned, non-derived, `active`/`pending_review`, and not in the `touched` set it was
handed. `touched` is built inside `commit_facts` from *that caller's own* writes
(`pipeline.py:1050`, returned at `1093-1104`). Nothing in the predicate names a writer.

So a producer's settle retracts every other producer's facts on the same note. That is
live today, on an ordinary note, for three independent reasons:

1. **Both jobs fan out of one event.** `note.ingested` binds `integrate_note` (trigger
   seed 0040, no filter) and `note_converse` (seed 0194, `event_types` only), and
   `queue.claim` orders by `run_after` alone (`queue.py:391-412`) — so which runs first
   is arbitrary. The worker runs one job at a time (`worker.py:551`, `process_one`), so
   when the conversation is claimed first its facts are committed and durable before
   `integrate_note` reaches `settle_note`, and are swept.
2. **The reply turn is always late.** `ask_owner` parks the conversation; the owner's
   answer arrives on an ordinary `/chat` turn, hours or days after `integrate_note`
   finished, and writes with extractor `note_ingest_reply` (`agent/replytools.py:303`).
   Those facts survive — until the note is next re-integrated. A re-ingest flips
   `integration_state` `'integrated' -> 'stale'` (`ingest/pipeline.py:231-234`),
   `backfill_pending_integration` re-enqueues on `integration_state <> 'integrated'`
   (`queue.py:632`), and `POST /notes/{id}/reanalyze` (`api/notes.py:448`) does it on
   demand. The second settle takes them.
3. **EMR notes already have two sweeps.** `integrate_note`'s trigger has no payload
   filter, so it runs on a health `Records` note beside `emr_parse`, whose
   `EmrNoteCommit.settle` is its own whole-note settle
   (`ingest/emr/integrate.py:358`). Each retracts the other's facts.
   `ingest/emr/integrate.py:343` admits this; `test_emr_import_handler_pg.py:583` is an
   `xfail(strict=True)` pinning it. The conversation is not a third writer there —
   `narrow_for_emr` strips its write verbs (`ingest/emr/ownership.py`) — so EMR is a
   two-writer problem.

Empirically pinned in `backend/tests/integration/test_settle_cross_producer_pg.py`:
after the analyzer's `apply_intent`, the note carries `industry active xai:grok-4.3`
beside `allergy retracted note_ingest`. The premise half passes (an ordinary
`assert_fact` write is unpinned, non-derived, `active`); the property half is
`xfail(strict=True)`.

**Say "recurring loss", not "a race".** A race implies a coin flip with a winner. The
conversation never re-asserts — it asserts once and corrects by supersession — so it
only ever loses, and it loses *again* on every later settle of that note. The same
settle's `_reconcile_mentions` (`pipeline.py:1602-1610`) hard-`DELETE`s the
conversation's mention rows too, which costs the co-mention spine `_promote_corroborated`
counts through and the stored `mention_ids` an un-merge replay needs. And it is silent:
the D3 rung renders the write from the tool result recorded at the time, and the only
reader of the ledger is `agent/asktools.py:89` — nothing re-reads `facts.status`, so the
thread still shows the fact as written.

This reframes W5a. Removing `integrate_note`'s settle is not "closing a prospective
race" — the sweep as written is a shipped data-loss bug with two producers already in
the corpus, and it should be fixed on its own terms before any producer moves.

## (b) Attribute the sweep — but not by `Fact.extractor`

**Yes, attribute it.** The sweep exists for exactly one job: retract what a *re-derivation
of the same note by the same producer* stopped asserting (`pipeline.py:1139-1146`). That
is a statement about one producer's own output. Applying it across producers is not a
stricter version of the rule, it is a different rule that nothing asked for.

`Fact.extractor` (`models/analysis.py:183`, `facts.extractor text NOT NULL` in migration
0006:188) does distinguish the writers — `f"{provider}:{model}"` (`pipeline.py:536`),
`note_ingest` (`agent/graphwritetools.py:498`), `note_ingest_reply`
(`agent/replytools.py:303`), `emr:deterministic` (`ingest/emr/integrate.py:67`) — and
there is no NULL or blank fall-through to worry about: the column is `NOT NULL` and every
writer passes a value. But equality on it is the wrong key, for two mechanical reasons:

- **The analyzer's value is not stable.** It carries the live provider and model from
  `effective_spec("integrate.note", …)` (`pipeline.py:524`). The owner changes that from
  the PWA (task overrides, on-box model install/uninstall), and a router fallback changes
  it without anyone asking. An equality-scoped sweep then stops retracting the note's own
  stale facts after a switch: the previous model's rows match no future sweep and stay
  `active` forever. That breaks the sweep's *primary* job, silently and permanently — a
  worse failure than the one being fixed, because stale-but-active facts are citable.
- **One producer, two strings.** `note_ingest` and `note_ingest_reply` are the same
  conversation across two runs. An equality sweep on the unattended pass would eat the
  owner's own reply-turn writes.

Normalizing the string does not save it: the analyzer's `:`-prefix is the *provider*, so a
prefix rule is as unstable as the whole value, and `note_ingest*` shares no delimiter with
it. Any normalization is a producer mapping written out at each call site.

**Build the mapping as a column.** Add `settle_owners` to `app.facts` and to
`app.entity_mentions`, stamped at commit time from an explicit argument threaded through
`commit_facts`. Values are a three-word vocabulary — `analyzer`, `conversation`, `emr` —
held as module constants, not a DB `CHECK` (no enum, no migration to add a fourth). The
sweep and `_reconcile_mentions` filter on it. Backfill is deterministic from `extractor`:
`note_ingest`/`note_ingest_reply` -> `conversation`, `emr:deterministic` -> `emr`,
everything else -> `analyzer`.

**[amended in build] It is a claim SET (`text[]`), not one owner, and the settle
RELEASES rather than retracts.** This doc originally specified `settle_owner text`, one
producer per row. Building it showed that shape cannot state the truth, because
co-assertion is the ordinary case: both producers read the same note off the same
`note.ingested` event, and a salient claim is exactly what both write down — at which
point `decide()` refreshes ONE row rather than minting two, and `_upsert_mentions` keeps
ONE mention row per (chunk, span, entity). A single owner then forces a choice between
two wrong answers, and this doc's own words condemn both:

- *last asserter takes the row* — the analyzer re-extracts what the owner's reply turn
  committed, takes it, and retracts it on the next pass whose extraction phrases the
  identity key differently. The owner's answer vanishes silently: the shipped bug again,
  wearing the fix's clothes.
- *first writer keeps it* — the analyzer's own stale row, once co-asserted by the
  conversation, is unsweepable by the analyzer forever, which is the "stale but citable"
  failure section (b) calls worse than the one being fixed.

So each settle removes only its OWN claim from the rows it no longer asserts, and
retracts (or, for a mention, deletes) only a row whose set is now empty. *The note
asserts X for as long as any producer still says X.* A claim is joined, never taken over
(`AnalysisPipeline._claimed_by` is remove-then-append, so a re-run cannot duplicate its
own claim and make the emptiness test unreachable). Cost, accepted knowingly: a row the
conversation ever co-asserted is no longer retractable by the analyzer alone, so a stale
co-asserted fact outlives a note edit until S3 gives the conversation a sweep, or the
note is purged/rebuilt. That is the immortality direction, and it is the survivable one:
it leaks a row, where the alternative deletes the owner's answer.

**[amended in build] The derived-shadow subquery is NOT claim-scoped.** This doc listed
it alongside the sweep. Scoping it was a regression: a shadow is a projection of its
source, not an independent claim, and an in-place refresh adds the refresher's claim to
the SOURCE only (`_update_shadows_in_place` copies rendering, never claims). A
claim-scoped shadow sweep therefore strands the reciprocal — active forever behind a
retracted source, and still cited by `wiki/builder.py`. It reads the source's status
alone, exactly as it did before S1.

Two alternatives, and why not:

- *Define the analyzer's set by exclusion* (`extractor NOT IN (…)`) — no migration, stable
  under a model swap, and wrong in the failure direction: a producer added later is swept
  by the analyzer until someone remembers the list, which is silent deletion again.
- *One settle per note by construction* — a coordinator settling over the union of all
  producers. Not reachable: the conversation can park on `ask_owner` indefinitely
  (`analysis/converse.py`'s "question stands"), so the union is unknown when
  `integrate_note` finishes. Waiting for it leaves the note unswept, unstamped and never
  flipped to `'integrated'`, which the reconciler reads as "not integrated" and re-enqueues
  forever.

**Fail toward a loud error, never toward a quiet retraction.** A producer that forgets
to stamp must not inherit `analyzer` and have its rows swept by a job that never wrote
them.

**[amended in build] The column ships `NOT NULL DEFAULT ARRAY['analyzer']`, and the
loudness moved rather than went.** No default was specified here, so that forgetting
would be a constraint violation. In this codebase it would not have been: nothing in
`src/` writes `app.facts` or `app.entity_mentions` by hand — no raw INSERT, no
`insert(Fact)`, no bare `Fact(...)` — so the only writes the default can reach are ~50
raw-SQL fixture inserts across 26 integration test files, every one of them standing in
for an analyzer row. Rewriting all of them inside an urgent data-loss fix buys nothing on
the box and produces a diff no reviewer can check. What enforces the stamp instead:
`settle_owner` is a required keyword with no default on `commit_facts` / `commit_intent`
/ `apply_intent` / `settle_note` (pyright flagged all ten call sites the day it landed),
and `tests/unit/test_settle_owner.py` fails on any write site in `src/` — raw SQL,
`insert(Fact)` or the declarative constructor — with no `settle_owners` in it. That test
exists because pyright CANNOT catch the constructor case: declarative `__init__` is typed
`**kw: Any`.

*Was uncertain, now settled by the experiment this paragraph asked for.*
`_upsert_mentions` matches on `(chunk_id, char_start, char_end, entity_id)` with
multiplicity and **keeps the existing row's id**, so a span both producers assert is one
row — and with a single owner, whichever of them stamped it last could hard-DELETE a span
the other still anchors a fact to. The claim set removes the choice: the re-assert JOINS
the set, and each reconcile releases only its own claim. The experiment ran as specified —
`test_settle_cross_producer_pg.py::test_a_span_both_producers_anchor_survives_either_reconcile`
asserts the same surface from both producers, runs each reconcile in turn, and checks the
row survives both and keeps its id.

## (c) The rule

> **A note's settle is owned per producer, not per note: each producer releases only its
> OWN claim — `integrate_note` the `analyzer` claim, `emr_parse` the `emr` claim, the note
> conversation the `conversation` claim — a row is retracted only when its last claim goes,
> and the `note_analysis` stamp is owned separately, by the one producer that has a title
> and tags, which today is `integrate_note` alone.**

It covers the three cases without a special case. On an ordinary note the analyzer and
the conversation both sweep and neither can reach the other's claims — including on the
rows and spans they BOTH assert, which stand until both let go. On an `emr_owned` note
(`ingest/emr/ownership.emr_owned`) the importer and the analyzer both sweep and neither can
reach the other, while the conversation writes nothing there anyway. In the D13 window
where `integrate_note` and `note_converse` run side by side off one event, both producers
keep working and no producer is removed.

### The conversation does not need the sweep. It needs the tail.

Splitting the ownership question from the *stamp* question is what makes this cheap, and
it exposes something the plan had upside-down. `settle_note` is three things in a row:
a sweep (`1147-1195`), a **tail** — `_reproject_entities`, `_promote_corroborated`,
`project_appointments` / `project_emr` / `project_place_geofences` /
`reconcile_device_bindings` (`1197`, `1228-1234`) — and the `note_analysis` upsert
(`1200-1221`). The tail is the half the conversation is actually missing: nothing in
`commit_facts` projects (it says so at `pipeline.py:1037-1039`) and `graphwritetools`
calls no projection, so **a conversation-written appointment lands in no projection and a
conversation-written `name.*` fact never reprojects `canonical_name` today**. It is masked
right now only because the analyzer's settle retracts those facts and then projects their
entities — the projection runs over a dead fact and removes the row. Scoping the sweep
without giving the conversation the tail swaps one silent gap for another, so they land
together.

The conversation may never need a sweep at all. Its verbs are `resolve_entity`,
`assert_fact`, `correct_fact`, `merge_entities` (`agent/agents.py:586-588`); it asserts one
fact at a time and revises by supersession, so it has no "the re-extraction dropped a fact"
event for a sweep to express. A sweep for it is optional (S3), not a precondition.

**[amended in build] That paragraph is true about the conversation's own writes and
misleading about the corpus, now that S1 has shipped.** A claim is released by a settle,
and the conversation has none — `graphwritetools` calls `commit_facts` and nothing else,
and the only production callers of `settle_note` are `apply_intent` and
`EmrNoteCommit.settle`. So a `conversation` claim, once recorded, is never released by
anything, and every row carrying one — the conversation's own AND every row both
producers assert — is retractable by no sweep from the day S1 merges. Edit a note to
drop a claim both producers wrote and the graph keeps asserting it: a note that is no
longer the sole source of truth for its own facts. The leak is the DEFAULT state, not a
consequence of anyone disabling anything, and it grows monotonically with every
co-asserted fact.

That is a trade made with open eyes — before S1 those rows were retracted by a producer
that had not written them and could not tell them from its own, which is how the owner's
answers were disappearing — and the surviving failure is the visible, correctable one.
But it means **S3 is scheduled work rather than an option**, and it is what stops the
bleeding. What could still remove such a row meanwhile: the note purge and the corpus
rebuild sweep, FK cascade on note deletion, review-item retraction, and the owner's own
`correct_fact`, which supersedes so the stale value stops being current.

***S3 has since shipped**, so the paragraph above describes the window between S1 and S3
rather than the state of the code. The conversation releases its claim at the end of a
clean pass. The one part of it that still stands is the MENTION half: the ledger records
no mention ids, so the conversation's `sweep_note` call skips the mention reconcile and
its mention claims go unreleased — bounded, because `entity_mentions.chunk_id` is
`ON DELETE CASCADE` and a re-ingest wipes that chunk generation.*

### The title/tags gap is real — and it decides between (i), (ii) and (iii)

Verified independently, and the reading holds. `settle_note` stamps
`title=extraction.title or None` and `tags=extraction.tags` (`pipeline.py:1200-1207`) and
the `on_conflict_do_update` sets `title`, `tags`, `extractor`, `prompt_version`,
`analyzed_at` and `domain_code` unconditionally (`1210-1220`) — there is no
`WHERE`, no `COALESCE`, no "only if absent". The conversation has no title or tags verb, so
the `Extraction` it would hand a settle is empty: `graphwritetools.py:1036` builds
`Extraction(title="", tags=[], …)` per fact just to reach `commit_facts`, and the harness —
which already runs the conversation-side settle this wiring would ship — builds its union
as `title=""`, `tags=[]` and says why: *"Title and tags are empty because the tool surface
has no verb for either"* (`backend/tests/harness/runner.py:167-177`, settling at `:505`).
So a conversation settle wired as-is writes `title = NULL`, `tags = {}` over whatever
`integrate_note` stamped.

One correction to how the harm has been described: **it is not the notes list.** `NoteOut`
(`api/notes.py:79-101`) carries no title — only the `analyzed` boolean, which is an
`EXISTS` on the row (`models/notes.py:97`) and survives a blanking. What actually loses the
title is `GET /notes/{id}/analysis` (`analysis/repo.py:226`, `:270`), which the note's
Analysis tab renders as its `<h2>` (`frontend/src/components/AnalysisTab.tsx:548`), plus
`agent/externaltools.py:411-414`'s "already saved … *<title>* — analysed 3d ago" dedup
line. `tags` is the input `analysis/tagconsolidate.py:46-57` rewrites, so blanking them
also empties what that sweep normalizes. Real loss, different surface.

**Recommended: (i), split `settle_note`.** `sweep_note(owner=…)`, `settle_tail(resolved,
projected)`, `stamp_analysis(title, tags, extractor)`. *Shipped in S2, with the signatures
adjusted as that section records.* `integrate_note` and `emr_parse`
call all three; the conversation calls the tail only, and the gap in (4) never arises
because the conversation never stamps. It is a pure refactor: no model-facing surface, no
sidecar, no schema, nothing to re-pin, no harmony-grammar risk. It also follows a seam this
file already has — `apply_intent` / `commit_intent` (`pipeline.py:578` / `:630`) are
already split on exactly this line, commit without settle; this is the same cut one level
up.

**(ii), a title/tags verb, is worse.** It grows a closed allowlist that is itself the
security guarantee (D16, constraint 9), and every new write verb must also be added to
`NEVER_DEFAULT` (`agent/toolregistry.py:72`) or the wildcard hands it to the chat
curator on every ordinary turn. Constraint 8 forbids a JSON-Schema `enum`, so the tag
vocabulary cannot be bounded in the schema — and `tagconsolidate.py` exists precisely
because free tags drift. It spends turn budget on a serial GPU for something the extraction
already produces as a by-product (risk 4 already accepts 5–10× inference per note). And it
does not even fix the problem: two stampers still race, the later one still wins.

**(iii), derive title/tags server-side at settle, is worse.** The only inputs are the note
text and the ledger. Deriving from the note text without a model is a first-line heuristic
worse than what exists; deriving with one is `note.extract` under another name. The ledger
holds statements, not the note's gist — a note whose conversation asserted one fact would
be titled by that fact. Same last-writer-wins race as (ii).

### What must become true before the conversation can own the settle

Only relevant the day W5 deletes `integrate_note`, at which point the conversation becomes
the sole producer and must take the sweep, the stamp and the state flip:

1. **A ledger union across both turn paths.** Landed as W4c/1 — `api/agent.py` now calls
   `clarify.record_reply_writes` before `close_owner_reply`, so `ConversationWrites.facts`
   is whole-conversation (`models/note_conversation.py:313-352`).
2. **A gate on an incomplete ledger.** `record_reply_writes` returns `False` when it could
   not record (`analysis/clarify.py`), and an unrecorded write is a fact the sweep
   would retract. The sweep must not fire on a `False`, nor on a truncated turn or one
   ending `awaiting_owner` (constraint 6). *Landed with S3: all three are one gate on the
   pass's STATE, `SETTLED`, because the `False` is degraded to `record_failed` at the call
   site and `state_for_stop` maps every non-clean ending away from `settled`.*

   *That gate's own reach had to be widened twice, because a stop reason is only as
   honest as the code that produces it, and three separate places were laundering a
   truncation into `end_turn` before it ever reached `state_for_stop`:*

   - *a provider LENGTH cut, which both adapters report as `max_tokens` and
     `agent/loop.py` collapsed — now classified once, by `loop._round_stop`, at all
     three natural-end sites (`run`, `run_stream`, `_produce_buffered`), which had
     drifted apart. It also catches a `tool_use` round whose `tool_calls` are empty;*
   - *a stream that ended before the provider ever sent a stop reason. The
     openai-compatible adapter has refused that since it was found live
     (`LlmStreamTruncatedError`); the Anthropic route had no such guard and yielded a
     fragment wearing `stop_reason="end_turn"`. It refuses now too;*
   - *`close_owner_reply`, which could not tell a thread the owner's reply re-opened from
     one the worker's 30-minute unattended pass is still inside — so an ordinary `/chat`
     message during a live pass settled it and swept against a ledger that had not been
     written yet. It requires the positive `reopened` signal `record_owner_reply` already
     returns.*
3. **A title/tags source.** Unowned: the plan names `note_analysis` exactly once
   (`AGENT_INGEST_CONVERSATION_PLAN.md:1282`) and never says where the title comes from
   afterwards. Recommendation for that day: keep the analyzer's title half as its own small
   non-graph producer feeding `stamp_analysis`, rather than (ii) or (iii). *Uncertain* —
   this is a plan decision, not a code fact, and it should be ratified before S5 is
   scoped.
4. **A home for `integration_state = 'integrated'`.** Flipped outside `settle_note`, by
   `integrate_note` alone (`pipeline.py:542`), and read by `queue.py:632`,
   `workflow/dispatcher.py:373`, `workflow/scheduler.py:78` and `analysis/rebuild.py:284`.
   A conversation that parks on `ask_owner` for days cannot be what flips it, or the note
   is "not integrated" for as long as the owner is asleep and the reconciler re-enqueues it.

## (d) Sequencing — no producer removed before its replacement is green

Per-PR, D13's rule holds at every step: S1–S4 remove nothing.

### S1 — Scope the sweep by settle owner ✅ SHIPPED

Migration 0196 adds `settle_owners text[] NOT NULL DEFAULT ARRAY['analyzer']` to
`app.facts` and `app.entity_mentions`, backfilled from `extractor` as above (mentions
backfill to `{analyzer}`: nothing distinguishes an existing conversation mention, and
those rows were being deleted anyway). `commit_facts` takes the producer and claims it;
`integrate_note` passes `analyzer`, `graphwritetools`/`replytools` pass `conversation`,
`ingest/emr/integrate.py` passes `emr`. `settle_note` and `_reconcile_mentions` release
that producer's claim and act only on rows left unclaimed; the shadow subquery stays
source-driven (both amendments above).

Flipped `test_settle_cross_producer_pg.py`'s xfail and `test_emr_import_handler_pg.py`'s
settle-collision xfail to passing. No producer moved. What S1 did NOT do, and what a
reader should not assume from "shipped": the conversation ran no tail and had no sweep of
its own, and two producers still stamp `note_analysis` on an EMR note (S4). **The first
of those is now closed by S2** — the conversation runs the tail — and the sweep is S3.

Scoped by the owner key: the fact retraction and the mention reconcile. NOT scoped, and
still note-keyed and producer-blind, are the settle's two REVIEW-CARD halves —
`_sweep_stale_ambiguous` (`ambiguous_mention`) and `_sync_truncation_review`
(`extraction_truncated`). Both delete a co-writer's open card; no graph row is lost, but
the card is only refiled if that producer runs again, which the `integrate_note`-only
re-enqueue paths do not guarantee. `_sweep_stale_ambiguous` is worst in the EMR
direction, where the settling producer's names are semantic keys (`org:Quest`,
`cond:E11.9`) that spare no surface-named card at all. Argued in full in
`analysis/settle_owner.py`; each is tracked as its own task.

Three holes an independent review of the first cut found, all closed before merge and
all worth keeping in mind for S2-S5: the shadow-adoption branch re-homes a row onto this
note and must re-claim it from scratch (`_existing_facts` is not note-scoped, so the
adopted shadow can come from another note); `_update_shadows_in_place` copies rendering
and not claims, which is why the shadow sweep must not read a shadow's own set; and
`Fact(...)` / `EntityMention(...)` type-check with no stamp at all, so the static guard
has to look for the constructors and not only for raw SQL.

### S2 — Split `settle_note`, give the conversation the tail ✅ SHIPPED

`sweep_note` / `settle_tail` / `stamp_analysis`. `integrate_note` and `emr_parse` keep
calling all three, through a `settle_note` that is now their composition — no behaviour
change for either. The conversation calls `settle_tail` once per pass end (clean end, not
truncated, not `awaiting_owner`), so its facts finally project and reproject.

Three things the build settled that this section had left implicit:

- **The seam is `analysis/clarify.settle_conversation`, and BOTH turn paths call it.**
  The unattended pass calls it from `converse._run_turn` after the state block (the
  `question_stands` branch can still turn a `settled` verdict into `waiting_on_owner`, so
  settling before it would project a pass that is in fact still waiting). The owner's
  reply turn calls it from `api/agent.py` after `close_owner_reply`, which now RETURNS
  the state it wrote so the gate is the call that decided it rather than a re-derivation
  beside it. Without the second caller, a conversation that ended by asking a question
  would never project what the answer wrote — and, after S3, would never release a claim
  at all, since `ask_owner` is the common ending.
- **The two REVIEW-CARD halves did NOT move into `sweep_note`.**
  `_sweep_stale_ambiguous` and `_sync_truncation_review` are S1's residuals: note-keyed,
  producer-blind, each deleting a co-writer's open card. Putting them in `sweep_note`
  would have handed that reach to S3's new caller as well — the conversation deleting the
  analyzer's `ambiguous_mention` and `extraction_truncated` cards, on re-enqueue paths
  that never refile them. They stay in the `settle_note` composition, which only
  `integrate_note` and `emr_parse` call, so the residual is exactly the size S1 left it.
  `_register_declared_aliases` stays there too, for the plainer reason that it reads an
  `Extraction` the conversation does not have.
- **`sweep_note` takes no `Extraction` at all** — an id set is all a sweep needs, and
  that is what makes it callable by a producer with a ledger and no extraction.
  `settle_tail` likewise takes entity ID SETS rather than the `resolved` map, since all
  it ever read off a `ResolvedEntity` was its `id`.

`tests/harness/runner.py` moved to the same two calls: it was the one place a
conversation stamped `note_analysis`, with the empty title its tool surface has no verb
for.

### S3 — The conversation's own sweep (W4c/2) ✅ SHIPPED

Filed here as "optional; explicitly droppable" before S1 was built. S1 changed that: with
the claim set shipped and no conversation settle, nothing ever releases a `conversation`
claim, so every co-asserted row was permanently un-retractable and the set grew with use
(argued in full above and in `analysis/settle_owner.py`). S3 closes it.

`clarify.settle_conversation` now calls `sweep_note(settle_owner=CONVERSATION,
touched=NoteConversationRepo.writes().facts, mentions=None)` before the tail it gained in
S2. `touched` is the whole-conversation union W4c/1 made complete across both turn paths;
a per-TURN share would release the OTHER turn's claim, and on a row only the conversation
asserts that is the last claim. With S1 in place the blast radius is its own claims: it
releases `conversation` and retracts only rows no producer asserts any more.

**The state gate is necessary and was not sufficient.** All three refusals in (2) above
are the same fact about the pass — `state_for_stop` gives `settled` to a CLEAN stop
alone. A truncated turn lands `failed`; a turn that ended on `ask_owner` lands
`waiting_on_owner`; and a turn whose ledger did not record lands `failed` too, because
both callers degrade the stop reason to `record_failed` — `api/agent.py` when
`record_reply_writes` returns False, `converse._run_turn` when its own `_record` raises.
So the sweep reads a state written by the code that knows how the pass ended, rather than
re-deriving the question beside it, and `close_owner_reply` returns that state for exactly
this reason. The unattended pass settles AFTER its state block, because the
`question_stands` branch can still turn a `settled` verdict into `waiting_on_owner`.

**The first cut shipped that gate and nothing else, and an independent review found the
hole before it merged. It is worth writing down, because it is this design's recurring
mistake in a new place: `touched` was SESSION-scoped while the sweep is NOTE-scoped.** A
conversation session that asserted nothing therefore released the `conversation` claim on
every unpinned row of the note — including every claim laid down by every EARLIER
conversation on it — and retracted whatever that left unclaimed. No model variance is
needed to reach it. `emr_owned` reads note state that MUTATES: the owner captures a health
`Records` note, the body ingests before any attachment lands (`analysis/converse.py` names
that as an ordinary shipped re-ingest), so conversation #1 runs with the full write surface
and asserts facts; the PDF then lands, the note re-ingests, and conversation #2 opens on a
note that is now `emr_owned`, with `narrow_for_emr` and the per-note registry leaving it no
write verb at all. It reads, replies, ends cleanly — and released the lot.

The correction is three refusals BEYOND the state gate, and one change of key. The key:
`touched` is `NoteConversationRepo.writes_for_generation`, the union of every conversation
session on the note whose `note_body_sha` matches — passes over the SAME TEXT are one
derivation by one producer — plus the settling session unconditionally, since a pass
vouches for its own writes whatever its sha. The refusals, each a case where an empty or
partial ledger would otherwise read as "the note no longer says that":

- **the note moved under this thread** (`note_body_sha` no longer matches the composed
  body). The pass is judging text that has changed since; the conversation that change
  opened is the one entitled to release. Without it the generation union inverts: a stale
  settler unions only its own generation and retracts the CURRENT one's writes.
- **the pass held no graph-write verb** — the EMR case above, keyed on the same
  `emr_owned` predicate `narrow_for_emr` and `reply_profile_for_session` use, so "could
  this pass write?" has one answer across all three.
- **an empty generation ledger.** With nothing asserted over this text by any pass, there
  is no re-derivation to compare against and the release would run on the whole note.

Why this is not the "union releases nothing" objection that killed the first idea of a
note-scoped `touched`: an UNCONDITIONAL union spares every id the conversation ever wrote,
so it can never release. Scoped by sha, an edited note drops the previous generation out of
the union — which is exactly, and only, the case the sweep exists for.

All three refusals fail toward a LEAK, which is the direction this design fails in
deliberately. The tail still runs in every one of them: projecting is never destructive.

Three things S3 does NOT close, each stated so the next reader does not assume otherwise:

- **the mention half.** `mentions=None` skips the mention reconcile, because the ledger
  has no mention-id column and an empty set would delete the spans of facts the
  conversation still asserts (the plan says this in W4c/1's own bullet). The
  `conversation` mention claim therefore goes unreleased — bounded to one chunk
  generation by `ON DELETE CASCADE`, unlike the fact leak this wave closes.
- **the review cards.** They stayed in `settle_note` (S2's section), so the conversation's
  sweep cannot reach them. Deliberate: it keeps S1's residual the size S1 left it.
- **a pass that parks on `ask_owner` and is never answered** releases nothing, ever. The
  gate is what makes that correct rather than unfortunate — a thread still waiting has not
  finished reading the note — but the claim stands until the owner replies or the note is
  re-ingested and a later conversation settles over it.

### S4 — One stamper per note

Two producers still stamp `note_analysis` on an EMR note (`emr` writes
`title="Medical records"`, the analyzer writes the extracted title); last writer wins. Not
destructive — both are real titles — but it should be decided: the importer's stamp wins on
an `emr_owned` note. Small and independent of S1–S3.

### S5 — W5a teardown, unchanged in its gating

Only after S1, S2 and S3 are green. A PR that deletes `integrate_note` is removing three
producers at once — the `analyzer` sweep, the `note_analysis` stamp and the
`integration_state` flip — and must land each one's replacement in the same PR, per the
preconditions above. The three helpers `integrate_note` alone calls
(`recover_dropped_fields`, `derive_kinship_gender`, `dedup_intent_facts`) go with it, not
before it.

## Related

- `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` — the parent plan; constraint 6 is the
  whole-note sweep, D13 the per-PR rule, W4c/3 this decision.
- `backend/src/jbrain/ingest/emr/ownership.py` — the same question for the write surface.
- `docs/reference/ANALYSIS.md` — "Reprocessing", which the sweep implements.
