# W5's two unowned preconditions — the `note_analysis` stamp and the `integrated` flip

> **Status:** Scheduled · **Last verified:** 2026-09-10

`SETTLE_OWNERSHIP.md` S5 and `AGENT_INGEST_CONVERSATION_PLAN.md` W5a gate the same PR:
delete `integrate_note` and the deterministic chain, leaving the note conversation as the
only producer of an ordinary note's graph. D13 says no PR removes a producer before its
replacement is merged and green, and `integrate_note` is **three** producers wearing one
name — the `analyzer` sweep, the `note_analysis` stamp, and the
`notes.integration_state = 'integrated'` flip. Two of the three replacements were recorded
as explicitly unowned (`SETTLE_OWNERSHIP.md` preconditions 3 and 4). This doc decides them
against the code, so the teardown does not discover them half-deleted.

It decides no waves of its own. Its waves are S5 / W5a; this is the reading S5 is scoped
from. Everything below is mechanical, and where a claim is not proven it says "uncertain"
and names the experiment.

**Headline, and the reason this doc grew a fourth section.** Both preconditions turn out to
be answerable and cheap. The thing that is neither is a third question they surfaced:
after `integrate_note` goes, **nothing retracts anything on an ordinary note, ever.** Not
as a leak, as a total. Section 4 argues that this — not the stamp and not the flip — is
what actually gates W5, and that the plan does not currently say so.

---

## 1. Precondition 3 — where a note's title and tags come from

### What is true today

`stamp_analysis` (`analysis/pipeline.py:1408-1457`) upserts one `app.note_analysis` row per
note — `title`, `tags`, `extractor`, `prompt_version`, `analyzed_at`, `domain_code` — with
an `on_conflict_do_update` that sets all six unconditionally (`:1441-1455`). No `WHERE`, no
`COALESCE`. Last writer wins, and a writer with no title writes NULL.

It is reached from exactly two production paths, both through the `settle_note` composition
(`pipeline.py:1218-1225`): `integrate_note` via `apply_intent` (`pipeline.py:620`), and
`EmrNoteCommit.settle` (`ingest/emr/integrate.py:374`). `emr_parse` runs only on an
`emr_owned` note. So on an **ordinary** note `integrate_note` is the sole stamper, and W5
deletes it.

The conversation cannot stamp. Its four write verbs are `resolve_entity`, `assert_fact`,
`correct_fact`, `merge_entities` (`agent/agents.py`), none of which carries a title or a
tag, and `graphwritetools._assert_one` builds `Extraction(title="", tags=[], …)` per fact
purely to reach `commit_facts` (`agent/graphwritetools.py:1037`). Wiring it to
`stamp_analysis` as-is writes `title = NULL, tags = {}` over the analyzer's row.
`clarify.settle_conversation` therefore calls `settle_tail` alone (`analysis/clarify.py:619`),
and `tests/integration/test_conversation_settle_pg.py:243` pins that it does not stamp.

### The complete consumer set — and two corrections to `SETTLE_OWNERSHIP.md`

Every reader of `app.note_analysis` in the repo:

| Reader | Reads | Surface |
|---|---|---|
| `Note.analyzed` (`models/notes.py:92-98`) | `EXISTS` on the row | `NoteOut.analyzed` (`api/notes.py:90`), `NoteInfo.analyzed` (`notes/service.py:73`) |
| `SqlAnalysisRepo.note_analysis_view` (`analysis/repo.py:224-229`) | `title, tags, analyzed_at, extractor` | `GET /notes/{id}/analysis` |
| `tag_consolidate` (`analysis/tagconsolidate.py:44-58`) | `tags` (rewrites in place) | nightly maintenance action |
| `purge_note_artifacts` (`analysis/purge.py:478`) | deletes the row | note purge / rebuild |
| `backfill_deleted_note_artifacts` (`analysis/purge.py:738`) | `EXISTS` | boot sweep candidate predicate |

Two claims in `SETTLE_OWNERSHIP.md`'s "title/tags gap" paragraph do not survive the code,
and the code wins.

**Correction 1 — `agent/externaltools.py` is not a consumer.** The doc names
`externaltools.py:411-414`'s *"already saved … `<title>` — analysed 3d ago"* line as a
loser of a blanked title. That line's title comes from `latest_channel_analysis`
(`external/corpus.py:201-236`), which selects `s.title, s.analyzed_at` from
`app.external_sources` — the provider's own video title, written by the video-ingest upsert
at `external/corpus.py:120-142`. It never touches `note_analysis`. Strike it from the list.

**Correction 2 — the harm is bigger than the doc's own correction says, because the W5
failure is not a blanked title, it is a MISSING ROW.** The doc corrects an earlier claim
with *"it is not the notes list — `NoteOut` carries no title, only the `analyzed` boolean,
which is an `EXISTS` on the row and survives a blanking."* Both halves are true and the
conclusion does not transfer to W5. A blanking leaves the row (so `analyzed` stays true);
**not stamping at all leaves no row**, and then:

- `Note.analyzed` is false for every note, forever. `lifecycleChip`
  (`frontend/src/notes/lifecycle.ts:39-53`) reads `indexed && !analyzed` as **"analyzing…"**
  — an amber chip on every note in the home stream, permanently.
- `GET /notes/{id}/analysis` returns `analyzed_at: null`, and `AnalysisTab.tsx:521` renders
  the whole tab as *"analysis runs after indexing — nothing here yet."* Not a missing `<h2>`
  — **no facts, no entities, no temporal tokens**, on a note whose conversation wrote facts.
- The Analysis tab's re-run button polls until `analyzed_at` moves (`AnalysisTab.tsx:470`).
  With no stamper it never moves, so the PWA's only no-terminal re-analysis lever spins
  forever (CLAUDE.md #10).

So the stamp is not cosmetic and it is not the title. **A `note_analysis` row per note is
load-bearing for the PWA's note lifecycle.** Whatever fills `title` is a second, smaller
question.

### Is the title worth an LLM call of its own?

The `title` and `tags` fields are two keys of the same JSON object `note.extract` already
returns (`analysis/extraction.py:109-118`); the analyzer pays nothing extra for them today
— they are a by-product of a call made for the facts. The question W5 poses is whether to
keep paying for the *call* once the facts come from somewhere else.

Note that the box already has a zero-LLM title in production for a neighbouring purpose:
`converse._title` (`analysis/converse.py:231-235`) names the thread in the Chats list from
the note's first non-blank line, clamped — *"a conversation about a note should be findable
by the note, not by 'Note 3f2a…'"*. It is the same problem with the same input.

### The options, with their real costs

**(A) Keep `note.extract` as a small non-graph producer feeding `stamp_analysis`** — the
recommendation `SETTLE_OWNERSHIP.md` records as uncertain. `_extract_note`
(`pipeline.py:326-372`) survives the teardown; a thin handler runs it, calls
`stamp_analysis(title, tags, extractor)` and writes nothing else.
*Cost:* one full `note.extract` call per note, per re-ingest — with per-source grouping
(`pipeline.py:456-461`) that is one call per source, so a note with three attachments is
four calls. That is the majority of the token cost W5 was expected to remove, spent on two
string fields. *But:* the same call is the only extraction-complete reading of a note this
system has, and section 4 argues the corpus needs one for reasons that have nothing to do
with titles. If section 4's answer keeps an extraction alive anyway, (A) is free.

**(B) A dedicated title/tags call.** A small prompt over the note body, `max_tokens` in the
low hundreds, no facts, no mentions, no temporal tokens.
*Cost:* still one LLM call per note, but perhaps a tenth of (A)'s tokens; a second prompt +
schema + `PROMPT_VERSION` lineage to maintain; and it buys nothing for section 4.

**(C) A title/tags verb on the conversation.** Argued down in `SETTLE_OWNERSHIP.md` and the
argument holds, with one refinement worth stating because it cuts the other way from how
constraint 8 is usually invoked. **Constraint 8 does not forbid this verb.** A title is free
text and tags are an open list; neither needs a JSON-Schema `enum`, so neither trips the
harmony segfault. What kills it is the measured behaviour beside constraint 8:
`AGENT_INGEST_CONVERSATION_PLAN.md:938-950` — *`required` buys PRESENCE, not MEMBERSHIP* —
and `tagconsolidate.py` exists precisely because free tags drift. Add the structural costs:
a fifth write verb widens the D16 allowlist that *is* the security guarantee, must be added
to `NEVER_DEFAULT` (`agent/toolregistry.py`) or the wildcard hands it to the chat curator on
every ordinary turn (constraint 9), and it does not fix the stamp — two stampers still race
and the later still wins. And it fails in the direction that costs most: a pass that ends
`waiting_on_owner` or truncated has no `settled` state, so on the endings where the model
never got to the verb there is no row at all, which is the failure above.

**(D) Derive server-side, no model.** `title` = the note's first non-blank line, clamped —
`converse._title`'s rule, already shipped and already the string the owner sees for that
note in another list. `tags` = `[]`.
*Cost:* zero tokens, zero new surface, no race. The Analysis tab's `<h2>` degrades from a
model-written summary to the note's opening line — often the same thing, and for a health
`Records` note the importer's `"Medical records"` is not better. `tags` stops being
produced; `tag_consolidate` normalizes an empty corpus and becomes a no-op sweep over
nothing. Tags are surfaced only as pills on the Analysis tab (`AnalysisTab.tsx:549-556`) and
are searched by nothing (`tagconsolidate.py`'s own docstring says so: *"tags are not yet
surfaced/searched"*), so what is lost is a display row.

### Recommendation

**Split the answer, because the row and the title are different problems, and do NOT
ratify (A) on the title's account.**

1. **The ROW is mandatory and its writer is the conversation's settle.** Add a
   `stamp_lifecycle`-shaped call — or `stamp_analysis` with title/tags made optional and
   `COALESCE`d rather than overwritten — invoked from `clarify.settle_conversation`, on
   **every** pass ending that read the note, including `waiting_on_owner`. It writes
   `analyzed_at`, `extractor`, `prompt_version`, `domain_code`; it must never write NULL
   over an existing `title`/`tags`. This is what keeps the chip, the tab and the re-run
   poller alive and it costs nothing.
2. **The TITLE takes (D), not (A) and not (B).** A title is worth a `substring` and it is
   not worth an LLM call. `converse._title`'s rule is already in the tree, already applied
   to this exact note, and produces a string the owner has already been shown. If an
   extraction survives W5 for section 4's reasons, promote it to the title source then —
   that is a strictly better title arriving for free, and the `COALESCE` shape in (1) is
   already the seam that lets a better producer overwrite a weaker one without a second
   design.
3. **Tags and title do NOT get the same answer.** A derived title is a real title. A derived
   tag is not a tag. Tags stop being produced; `tags` stays `{}` on new rows, existing tags
   are left alone (that is what the `COALESCE` in (1) is for), and `tag_consolidate` keeps
   normalizing the corpus it already has. Say so in the ROADMAP residual rather than
   pretending the field is still fed.
4. **Notes that already have a row keep it.** The `COALESCE` shape is the whole mechanism:
   an existing analyzer-written title and tag array survive every future stamp. No backfill,
   no migration. The `on_conflict_do_update` set list is what changes.

*Uncertain, and the experiment.* Whether a first-line title is acceptable to the owner is a
judgement this doc cannot settle from the code. **The experiment is cheap and should run
before S5:** compute `converse._title(note)` beside the stored `note_analysis.title` for the
existing corpus (read-only, one query plus a Python line) and look at the disagreement rate
and shape. If first-line titles are bad on more than a small tail, (B) becomes the answer
and (A) still does not — a full extraction for two strings is not defensible on its own.

### What would have to be tested

- The stamp is written on a pass ending `waiting_on_owner` and on one ending `settled`, and
  the note's `analyzed` flag is true after each (integration, real Postgres).
- A conversation stamp over a note that already carries an analyzer-written title and tags
  leaves both intact — the direct inverse of today's blanking, and the test that keeps the
  `COALESCE` from being refactored away. This is the mirror of the existing pin at
  `tests/integration/test_conversation_settle_pg.py:243`, which must be rewritten rather
  than deleted (it currently asserts the conversation does NOT stamp).
- `GET /notes/{id}/analysis` returns a non-null `analyzed_at` and the conversation's facts
  for a note no analyzer ever touched.
- RLS: `note_analysis.domain_code` is NOT NULL with an FK to `app.domains`
  (`models/analysis.py:217`), and the new writer runs under the conversation's context, not
  `SYSTEM_CTX`. A health note's stamp must not be writable or readable cross-domain.

---

## 2. Precondition 4 — what flips `notes.integration_state = 'integrated'`

### What is true today

One writer of `'integrated'`, in one place: `analysis/pipeline.py:543-546`, inside
`integrate_note` and outside `settle_note`. Three things follow that the doc's precondition
text does not say and that change the decision:

1. **It is unconditional.** It runs whether or not `apply_intent` committed anything. On a
   rejected plan `apply_intent` returns `{}` without settling (`pipeline.py:618-619`), and
   the flip still fires. So the state does **not** mean "this note's graph is complete." It
   means **"the note's graph producer ran to completion on it."** That is the semantics W5
   has to preserve, and it is a much easier one to preserve.
2. **`emr_parse` does not write it.** `EmrNoteCommit.settle` settles and stamps but flips
   nothing (`analysis/rebuild.py:274-277` says so explicitly). Today an `emr_owned` note is
   flipped by `integrate_note` running beside the importer. After W5 the EMR path loses the
   flip too — it is not only an ordinary-note problem.
3. **It is invisible.** No API field, no PWA reader (`grep integration_state frontend/src
   backend/src/jbrain/api` is empty). It is purely internal orchestration. Nothing the owner
   sees depends on its *value*; four internal readers depend on its *behaviour*.

The readers:

| Reader | Predicate | Consequence if never flipped |
|---|---|---|
| `queue.backfill_pending_integration` (`queue.py:632`) | `integration_state <> 'integrated'` | re-enqueues `integrate_note`, 100/call |
| `workflow/dispatcher._already_active` (`dispatcher.py:396-403`) | skip when `== 'integrated'` | a re-delivered `note.ingested` is never suppressed |
| `workflow/scheduler` (`:78`, action at `:100-110`) | the durability guarantee it names | the dropped-event safety net has nothing to key on |
| `analysis/rebuild._integration_drained` (`rebuild.py:270-298`) | `<> 'integrated'` → not drained | the corpus rebuild never drains |

`ingest/pipeline.py:231-234` flips `'integrated' → 'stale'` on every re-ingest, which is how
an edit becomes eligible again.

### The doc's objection is real, and it is worse than stated

> *"A conversation that parks on `ask_owner` for days cannot be what flips it, or the note
> is 'not integrated' for as long as the owner is asleep and the reconciler re-enqueues it."*

Verified, mechanically, in both halves.

**The park is unbounded.** `waiting_on_owner` is deliberately never reaped — *"it holds the
owner's question and waits as long as the owner does"* (`models/note_conversation.py:70-71`)
— and `state_for_stop` maps an `ask_owner` ending to it (`:110-121`). `settled` is reachable
from `running` alone (`_ALLOWED_SOURCES`, `:82-90`). So a flip gated on `settled` is gated on
an event that may never arrive.

**The re-enqueue is a 5-minute loop, not a one-off.** Migration 0041 seeds
`reconcile_pending_integration` on a 300-second schedule with `next_run_at = now()`, and the
scheduler ticks every 30s. Its `NOT EXISTS` guard (`queue.py:635-641`) suppresses only while
a `queued`/`running` twin exists — a **failed** job does not suppress. And after W5 the job
kind has no handler, so `process_one` fails it with *"no handler for kind"*
(`worker.py:172-178`). The steady state is: every 5 minutes, up to 100 `integrate_note` jobs
enqueued, each failing and retrying its way to `failed`, forever, with an error per job in
the Ops run log. Not a stall — a permanent noise floor on a box the owner maintains through
the PWA.

**And a reader the objection does not name makes it worse.** `_integration_drained` is what
the corpus rebuild waits on before chaining `wiki_prune` / `wiki_rebuild` / `wiki_refresh`
(`rebuild.py:391-400`). The rebuild is the remedy `SETTLE_OWNERSHIP.md` S3 leans on as the
owner's non-terminal recovery path for the accepted leak. With no flip, **every** rebuild
takes the `overdue` deadline branch (`rebuild.py:400-405`) instead of the drained branch —
it still completes, loudly and late, but the design's happy path is gone. The remedy the
accepted loss depends on is degraded by the precondition the accepted loss did not consider.

**One thing the objection gets wrong in the reassuring direction.** After W5 the note
conversation has *no* reconciler at all. `backfill_pending_integration` enqueues
`integrate_note`; nothing anywhere enqueues `note_converse` off a state column. Only the
event dispatcher does, and the dispatcher's own `note_converse` arm already carries the
right guard — skip on a queued twin **or** a live conversation
(`dispatcher.py:404-416`). So today the conversation producer has no dropped-event safety
net, and W5 removes the only one the note had.

### The options

**(i) Flip on `settled` only.** The failure above. Rejected.

**(ii) Flip at the end of every `note_converse` pass that read the note, whatever its
ending** — `settled`, `waiting_on_owner`, and `failed` alike. Preserves today's semantics
exactly: today's flip fires on a rejected plan, i.e. on a run that committed nothing, for
the same reason — the state records that the producer *ran*, not that it *succeeded*.
*Cost:* a note whose pass failed is marked integrated and is not retried by the reconciler.
That is also true today for a note whose extraction was rejected, so it is not a regression;
it is an existing property being carried across.

**(iii) Flip on `settled` and `waiting_on_owner`, leave `failed` un-flipped** so the
reconciler retries a genuinely failed pass. Narrower than (ii) and closer to what one wants,
but it needs the reconciler repointed carefully or a failed pass re-enqueues every 5 minutes
against a note that will fail again — the same loop, slower.

**(iv) A new state value.** `INTEGRATION_STATES` already contains `'integrating'` and
`'skipped'` and nothing in `src/` writes either (`models/notes.py:26-28`; the CHECK is
migration 0029), so a fourth value is available with no migration. No reader wants one.

### Recommendation

**(ii), plus repointing the reconciler — and the two must land together.**

1. **The flip moves to the `note_converse` handler's terminal block**, beside the state
   transition that is already *"NOT best-effort"* (`analysis/converse.py:466-470`) — the one
   place in that handler that already refuses to be silently skipped, for the same reason:
   a stuck note on a terminal-less box is unrecoverable. It fires on every pass ending,
   including `waiting_on_owner`. Keep the semantics honest in the column's comment
   (`models/notes.py:43-44`), which currently says *"until the integrate_note job runs and
   commits it"* — it never meant "commits it" and after W5 it will not mean `integrate_note`
   either.
2. **`reconcile_pending_integration` is repointed from `integrate_note` to `note_converse`**
   (`queue.py:605-670`), and its `NOT EXISTS` guard gains the live-conversation clause the
   dispatcher already applies (`dispatcher.py:404-416`) — otherwise the sweep re-enqueues a
   note parked on `ask_owner` every 5 minutes, which is precondition 4's own failure wearing
   a different hat. This is what gives the conversation producer the dropped-event safety
   net it has never had. `has_active_analysis` hardcodes `kind = 'integrate_note'`
   (`queue.py:344`) and moves with it, as does `POST /notes/{id}/analyze`
   (`api/notes.py:466`) — the PWA's re-run button, which must enqueue a `note_converse`
   after W5 or the Analysis tab's only lever is dead.
3. **`_integration_drained`'s in-flight job list** (`rebuild.py:288-292`, currently
   `('integrate_note', 'emr_parse')`) becomes `('note_converse', 'emr_parse')`, or the
   rebuild reads a graph the conversation is still writing.
4. **`dispatcher._already_active`'s `integrate_note` arm** (`dispatcher.py:396-403`) is
   deleted with the kind; its `note_converse` arm already exists and is correct.

**The residual this creates, named rather than discovered later.** A note edited while its
conversation is parked on `ask_owner` re-ingests, flips `'integrated' → 'stale'`, and is then
*skipped* by the repointed reconciler because a live conversation exists — so the edit is
never re-read. Today `integrate_note` has no live-conversation guard and re-integrates.
Three candidate treatments, none of them decided here: reclaim the parked thread when
`note_conversations.note_body_sha` no longer matches the composed body
(`models/note_conversation.py:156-162` — the field exists for exactly this question, and
`clarify` is already its reader); or let a `'stale'` note override the guard; or accept it
and surface the parked thread in the notes tab. **`note_body_sha` acquiring a second job is
the shape S3 failure 4 was**, so whichever is chosen, re-read every existing writer of that
field against the new job before wiring it (`SETTLE_OWNERSHIP.md` S3, "the lesson worth
carrying past this wave").

### What would have to be tested

- A pass that ends `waiting_on_owner` leaves the note `'integrated'` (integration, real
  Postgres) — the direct pin on the objection.
- `reconcile_pending_integration` enqueues nothing for a note with a live conversation, and
  enqueues `note_converse` for an indexed, un-integrated note with none. Both against the
  real SQL, not a fake — the predicate is the artifact.
- Re-ingest → `'stale'` → the reconciler re-enqueues → the note is integrated again, end to
  end, which is the edit path.
- `POST /notes/{id}/analyze` returns a `note_converse` job id and 409s while one is active.
- `_integration_drained` returns False while a `note_converse` job is in flight for a
  rebuilt note.

---

## 3. The three helpers `integrate_note` alone calls

`SETTLE_OWNERSHIP.md` S5 says `recover_dropped_fields`, `derive_kinship_gender` and
`dedup_intent_facts` go with `integrate_note`. **Confirmed: one production caller each**, all
three in `integrate_note` (`pipeline.py:505`, `:510`, `:519`); every other reference in the
repo is `tests/unit/test_analysis_arbiter.py` or a docstring. Deleting `integrate_note` makes
all three unreachable, so leaving them is dead code and taking them is not a second decision.

Whether the conversation has an equivalent is a different question per helper, and the
answer is not the same three times.

- **`recover_dropped_fields`** (`analysis/arbiter.py:403-…`) repairs a *pipeline seam* that
  will not exist: it backfills the `object_entity_ref` / `value_json` / `temporal` the
  Integrator dropped when it re-typed a fact the extraction had already carried. With no
  Integrator there is no re-typing step and nothing to recover. **Clean deletion, no
  capability lost.**
- **`dedup_intent_facts`** (same module) collapses a fact the Integrator emitted twice into
  its best-grounded copy. Same argument, one caveat: the duplicate it removes is a *model*
  behaviour, not an Integrator-specific one, and the conversation can assert the same fact
  twice. It does not need this helper to survive it — `assert_fact` re-asserting an identity
  key returns `ALREADY` with the same `fact_id` (`SETTLE_OWNERSHIP.md` S3, step 1), which is
  a stronger dedup than this helper's heuristic. **Clean deletion.**
- **`derive_kinship_gender`** (`arbiter.py:345-400`) is the one that is not a seam repair.
  It emits a fact the note never states: *"four daughters named …"* → a `gender` fact on each
  child object, `inferred=True`, which `_gender_grounded` then attests so it commits. It has
  no equivalent on the conversation's path, and none is reachable from a per-fact write verb
  — the derivation is a whole-note inference over a roster, not a property of any one
  `assert_fact` call. The harness already measures the loss and names it: *"No arbiter
  (accepted). `derive_kinship_gender` and the rest of the arbiter's derivations do not run
  on this path, so facts main inferred are simply absent
  (`rel_enumerated_children_fan_out`: 8 facts where main wrote 12)"*
  (`backend/tests/harness/runner.py:86-89`).

  **So deleting it IS a capability loss, and it is already measured.** It is not a silent
  one — the scenario carries its own `xfail` string — and it is the same loss
  `AGENT_INGEST_CONVERSATION_PLAN.md` records against the arbiter as *"the one accepted gap
  with no survivor."* The recommendation is not to keep the helper (it takes an
  `IntegrationIntent` the conversation never builds, so keeping it means keeping the intent
  shape) but to **carry the loss into `ROADMAP.md` as a residual in the archiving PR**, per
  `DOC_LIFECYCLE.md` transition 4, so it does not vanish with the plan. The registry route
  is the real remedy and it is tier-1 work under `ENTITY_GRAPH_REFOCUS_PLAN.md`, not W5's.

---

## 4. What retracts anything after the analyzer is gone

This is the question preconditions 3 and 4 both lead to, and it is the one that should gate
W5. `SETTLE_OWNERSHIP.md` states its premise plainly — *"the door back in is an EXTRACTION,
never a ledger"* — and S3 proves a write ledger cannot license a retraction. What the plan
does not say out loud is the consequence.

### The state after W5, stated exactly

`sweep_note` has two production callers, both through the `settle_note` composition:
`apply_intent` (i.e. `integrate_note`) and `EmrNoteCommit.settle`
(`ingest/emr/integrate.py:374`). Delete the first and:

- an **`emr_owned`** note keeps a sweep — the importer re-derives from the PDF and releases
  the `emr` claim;
- an **ordinary** note has **no sweep at all**. Not a leak of co-asserted rows: no
  retraction of any row, by any producer, ever.

That is a strictly larger loss than the one S3 accepted with open eyes. S3's accepted
residue was *"a fact whose identity key is never re-asserted, on a note never purged or
rebuilt"* — bounded by supersession, `correct_fact`, review retraction, purge and rebuild,
because the analyzer was still there releasing claims and retracting what it stopped
asserting. After W5 the bounding producer is gone. The owner deletes a sentence from a note
and the fact it produced stays `active` and citable until the note is purged or the whole
corpus is rebuilt — and the rebuild is the remedy precondition 4 just degraded.

`ANALYSIS.md`'s "Reprocessing" is the Living doc that asserts the retraction behaviour, and
it becomes false for ordinary notes on the day W5 merges. That is a `DOC_LIFECYCLE.md`
transition-5 obligation in the same PR, and it is also the clearest statement of what the
wave costs.

### The three doors, and which one to walk through

**(a) An extraction-fed sweep.** The harness already demonstrates the licensing condition,
and its comment is the clearest statement of the invariant in the tree: *"What licenses it
is that each step is a whole-note RE-DERIVATION. The harness is the model, emitting a
complete extraction per step, so it satisfies the sweep's invariant the way the analyzer's
`Extraction` does and a write ledger never can"* (`backend/tests/harness/runner.py:487-493`).
Production has no such step.

**Be precise about what this would cost, because it is easy to overstate the fit with
precondition 3.** `sweep_note` releases claims by **fact id** (`touched: set[uuid.UUID]`,
`pipeline.py:1290-1294`), and a non-graph extraction commits nothing, so it produces no ids.
Feeding a sweep from an extraction that does not write means a **new, key-based sweep**: for
each active fact of the note, is its `(entity, predicate, qualifier)` present in the fresh
extraction? That is a different mechanism from the one built, with its own failure surface —
the extraction resolves surfaces to entity *refs*, not to committed entity ids, so the key
match needs a resolution pass, which is most of `commit_facts`. **Uncertain, and this is the
experiment W5 actually needs:** build the key-match against the existing corpus in read-only
mode and measure how many currently-active facts a fresh extraction of their own note fails
to match. If that number is small the mechanism is viable; if it is large the mechanism
would empty the graph and (a) is dead. Nothing in this doc should be read as having done
that measurement.

**(b) The evidence-based retraction S3 recorded as a future remedy.** A fact whose note has
been re-ingested and whose `chunk_id` is now NULL is a fact whose cited text is gone —
producer-agnostic, no model, satisfies the invariant. Verified: `facts.chunk_id` is
`ON DELETE SET NULL` (migration `0006:132`) while `entity_mentions.chunk_id` is
`ON DELETE CASCADE` (`0006:92`), and `ingest/carryover.py` keeps a byte-identical chunk's row
outright and *re-anchors* a covered one, repointing `app.facts` and `app.temporal_tokens`
(`ingest/pipeline.py:82`). So a NULL `chunk_id` after a re-ingest genuinely means neither
reuse nor re-anchor matched.

**And that is exactly why it is not a drop-in replacement.** Neither rule matches when the
owner *rewrites* the body — at which point **every** fact of the note has a NULL
`chunk_id`, including the ones the rewrite kept. Under the analyzer that was harmless: the
same run re-asserted what survived. With no re-asserting producer, (b) alone retracts a
rewritten note's entire graph and puts nothing back. S3 flagged this in one line (*"a fact
can lose its chunk for reasons other than its text disappearing"*); it is worth spelling out,
because (b) reads like the cheap answer and in a post-W5 world it is the dangerous one.
**(b) is sound only in combination with a re-asserting producer, which is (a).**

**(c) Accept it, in writing.** Ordinary notes lose retraction; the note stops being the sole
source of truth for its own facts in the direction that matters (a deletion no longer
propagates); purge and rebuild are the only remedies, and the rebuild's drain gate must be
fixed by precondition 4 first for that remedy to work at all.

### Recommendation

**W5 must not land on (c) by default, and today it would.** The plan's W5a text gates the
wave on the stamp and the flip; this doc's answer to both is cheap, and clearing them would
leave the wave *appearing* ready while the corpus quietly loses retraction for good.

So: **add a third gate to S5** — before `integrate_note` is deleted, either (a) is built and
green, or the loss is accepted explicitly in `ROADMAP.md` and `ANALYSIS.md` with the owner
told what it means in the PWA's terms (*"editing a note to remove something no longer
removes it from the graph; use Ops → rebuild"*). Do not take (b) alone. This doc recommends
running (a)'s read-only measurement **first**, because it is the only input that tells W5
whether it is a teardown or a rebuild, and it costs one query and no schema.

*And if (a) is built, precondition 3 collapses into it.* The extraction that licenses the
sweep carries `title` and `tags` in the same payload it already returns
(`analysis/extraction.py:109-118`), so option (A) stops being "an LLM call for two strings"
and becomes free. That is the ordering this doc recommends: **decide the retraction first,
and let it decide the title** — which is why §1's recommendation is a `COALESCE` seam a
better producer can overwrite, rather than a title source that would have to be unpicked.

---

## 5. What this makes bigger or smaller for W5

**Bigger — found in the code, not currently in the plan:**

- **Temporal tokens lose their only producer.** `_upsert_tokens` runs inside `commit_facts`
  (`pipeline.py:1070`) off `extraction.tokens`, and the conversation always passes
  `tokens=[]` (`agent/graphwritetools.py:1037`); the EMR path passes `tokens=[]` too
  (`analysis/arbiter.py:751`); nothing under `agent/` references `TemporalToken`. So after W5
  `app.temporal_tokens` stops being written. The concrete casualty is **appointment
  recurrence**: an appointment's RRULE is read only off its fact's token
  (`analysis/appointment_projection.py:184`, `:428`), so a conversation-written recurring
  appointment produces a one-off calendar event. Already true today for conversation-written
  facts and masked by the analyzer co-writing the note; after W5 it is total. The
  `temporal_tokens` list in `GET /notes/{id}/analysis` (`analysis/repo.py:258-264`) goes
  permanently empty too.
- **The conversation has no reconciler.** Nothing enqueues `note_converse` off a state
  column — only the event dispatcher does. W5 removes the note's only dropped-event safety
  net unless precondition 4's repoint lands with it (§2).
- **`POST /notes/{id}/analyze` and `has_active_analysis` hardcode `integrate_note`**
  (`api/notes.py:466`, `queue.py:344`). The first is the PWA's only re-analysis lever
  (CLAUDE.md #10) and it dies silently — a 202 for a job kind with no handler.
- **The rebuild's drain gate** (`rebuild.py:288-292`) names `integrate_note` and must be
  repointed, or the corpus rebuild watches for a job that can never run.
- **Retraction, §4** — the largest of these by a distance.
- **`_extract_note` becomes test-only.** Its only production caller is `integrate_note`; the
  survivors are `tests/harness/runner.py:369`, `tests/eval/runner.py:44,198` and two
  integration tests. Either it is promoted to a producer (§1 (A) / §4 (a)) or W5 leaves
  ~50 lines of `src/` reachable only from tests, which is its own gate.

**Smaller than the plan says:**

- **The three helpers are a clean take**, two of them without any capability question at all
  (§3), and the third's loss is already measured and `xfail`-pinned rather than latent.
- **The stamp is not the hard part.** Precondition 3 reads like a design problem and is
  mostly a `COALESCE` and a call site; the title is a `substring` the repo already computes
  for this note in another list.
- **`integration_state` needs no new value and no migration.** Its meaning today is weaker
  than its name suggests — it records that the producer ran, not that it succeeded — so
  preserving it is a move, not a redesign.

## Related

- `docs/plans/SETTLE_OWNERSHIP.md` — preconditions 3 and 4 are its; S3's section is the
  proof this doc's §4 builds on; S5 is the wave this doc scopes.
- `docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md` — W5a's gate, constraint 8, D13.
- `docs/reference/ANALYSIS.md` — "Reprocessing", the Living doc §4 makes false for ordinary
  notes.
- `backend/src/jbrain/analysis/settle_owner.py` — the claim-set key all of this is scoped by.
