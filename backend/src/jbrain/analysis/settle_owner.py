"""Who still asserts a note's rows — the whole-note settle's producer key.

`settle_note` retracts every unpinned, non-derived, active/pending_review fact of a
note it was not handed in `touched`, and `_reconcile_mentions` deletes that note's
mentions the same whole-note way. Up to three producers write ONE note — the analyzer
(`integrate_note`), the note conversation (`agent/graphwritetools.py`, both runs) and
the EMR importer (`ingest/emr/integrate.py`) — and on an ordinary note the first two
fan out of a single `note.ingested` event, so a note-keyed sweep retracts whatever the
co-writer just committed. That is shipped loss, not a race: the conversation asserts
once and revises by supersession, so it never re-asserts and loses again on EVERY later
settle of the note (re-ingest, `queue.backfill_pending_integration`,
`analysis/rebuild.py`). The reasoning and the sequencing are
`docs/plans/SETTLE_OWNERSHIP.md` (S1); this module is its vocabulary.

**Why a stamped column and not `Fact.extractor`.** `facts.extractor` is `text NOT NULL`
(migration 0006) and every writer sets one, so it does distinguish the writers — but
equality on it is the wrong key, twice:

- The analyzer's value is `f"{provider}:{model}"` (`analysis/pipeline.py`), read live
  from the router. A PWA task override, an on-box model swap or a provider fallback
  changes it with nobody asking, and an extractor-scoped sweep would then stop
  retracting the note's OWN stale rows from the previous model — they match no future
  sweep and stay `active`, i.e. citable, forever. That breaks the sweep's primary job.
- `note_ingest` (unattended pass) and `note_ingest_reply` (the owner's reply turn) are
  ONE producer across two runs. Split by string, the unattended pass's settle would eat
  the owner's own reply-turn writes.

A `settle_owners` stamp survives both: the analyzer keeps claiming `analyzer` across any
model change, and `NoteGraphWriter` claims `conversation` whichever extractor string the
run carries — the grouping is structural rather than a naming convention.

**Why a SET and not one owner.** Because co-assertion is the ordinary case, not an edge
case. Both producers read the same note off the same event, and a salient claim ("she is
allergic to shellfish") is exactly what both write down; `decide()` then refreshes one
row rather than minting two, and `_upsert_mentions` keeps ONE mention row per
(chunk, span, entity). A single-owner column has to answer "whose row is this?" with a
name, and both available answers are wrong:

- *last asserter takes it* — the analyzer re-extracts what the owner's reply turn
  committed, takes the row, and retracts it on the next pass that phrases the identity
  key differently. The owner's answer vanishes silently: the original bug, wearing the
  fix's clothes.
- *first writer keeps it* — the analyzer's own stale row, once co-asserted by the
  conversation, becomes unsweepable by the analyzer forever. That is the mirror failure
  SETTLE_OWNERSHIP.md calls "worse than the one being fixed", because a stale-but-active
  fact is citable.

So the row records CLAIMS, and the settle is a release rather than a retraction: each
producer removes its own claim from the rows it no longer asserts, and only a row whose
claim set is now empty is retracted (a mention, deleted). The invariant is the one
sentence worth remembering: **the note asserts X for as long as any producer still says
X.** A claim is joined, never taken over (`AnalysisPipeline._claimed_by`), with one
exception: adopting a derived shadow re-homes the row onto this note, so it is
re-claimed from scratch by the adopter.

Derived shadows are outside all of this. A shadow is nobody's independent claim — it is
a projection of its source — so the shadow sweep reads the SOURCE's status and never the
shadow's own set (`settle_note`). Its stamp exists only because the column is NOT NULL.

**What catches a writer that gets it wrong.** `settle_owner` is a required keyword with
no default on `commit_facts`, `commit_intent`, `apply_intent` and `settle_note`, so a new
producer that goes THROUGH those four seams and forgets it is a pyright error (that is
what caught all ten call sites the day the parameter landed). Nothing about the tables
themselves is type-checked: SQLAlchemy's declarative constructors take `**kw: Any`, so
`Fact(...)` with no stamp type-checks fine, and raw SQL is invisible to pyright
entirely. `tests/unit/test_settle_owner.py` covers what pyright cannot see — it fails on
a write site in `src/` (raw INSERT in any case or schema, the declarative constructor
under any name or alias, a Core `insert()`/`pg_insert()` chain, the ORM's bulk helpers)
with no `settle_owners` inside it, and pins the in-place paths to `_claimed_by`. It is a
grep with good manners, not a proof: its own docstring lists what it still misses (a
computed table name, a stamp it cannot follow into a variable, a wrong producer name).
The DB column does carry `DEFAULT ARRAY['analyzer']`, a divergence from
SETTLE_OWNERSHIP.md S1 argued in migration 0196's docstring.

**The conversation releases its claim now (S3) — and for one release between S1 and S3
it did not, which is worth remembering because it is the shape of what this key costs
when a producer has no settle.** A claim is released by a settle. When S1 shipped, the
conversation had none: `graphwritetools` calls `commit_facts` and nothing else, and the
only callers of `settle_note` were the analyzer's `apply_intent` and
`EmrNoteCommit.settle`. Every row carrying a `conversation` claim — the ones only the
conversation wrote AND the ones both producers assert — was therefore retractable by no
sweep at all, and the set of such rows only grew: edit a note to drop a claim both
producers wrote and the graph kept asserting it, which is a note no longer being the sole
source of truth for its own facts.

State that in trade terms, because it WAS a trade and it was made deliberately: before
S1, those rows were retracted by a producer that had not written them and could not tell
them from its own — which is how the owner's answers were disappearing. It exchanged "a
co-writer silently deletes what the owner said" for "the graph keeps asserting something
the note no longer says". The second is visible, correctable and recoverable; the first
is none of those. That did not make it free, which is why S3 followed immediately.

**What closed it.** `analysis/clarify.settle_conversation` runs at the end of a clean
pass, from both turn paths, and calls `sweep_note(settle_owner=CONVERSATION,
touched=NoteConversationRepo.writes().facts)`. `touched` is the whole-CONVERSATION union
across the unattended pass and the owner's reply turn (W4c/1); a per-turn share would
release the other turn's claim, and on a row only the conversation asserts that is the
last claim. It fires ONLY from the `settled` state, which is what keeps it off a
truncated turn, a turn still waiting on the owner, and a turn whose ledger did not record
— all three degrade to a state that is not `settled`.

**The MENTION half of that leak is still open, and is the bounded one.** The two tables
sit behind different foreign keys (migration 0006): `entity_mentions.chunk_id` is
`ON DELETE CASCADE`, while `facts.chunk_id` is `ON DELETE SET NULL`. The conversation's
ledger records fact and entity ids and no mention ids, so its sweep passes
`mentions=None` and skips the mention reconcile — running it against an empty set would
delete the spans of the facts it still asserts. An unreleased `conversation` mention
claim therefore survives at most until the next re-chunk of that note
(`ingest/pipeline.py`, `notes/repo.py` both replace the old chunks), where a fact
carrying such a claim would have kept its row and merely lost its chunk pointer. That
asymmetry is why the fact half was the urgent one.

**What can remove such a row without a producer.** None of these is producer-scoped,
which is why they are the escape hatches: `analysis/purge.purge_note_artifacts` (note
deletion, and the corpus rebuild sweep), `ON DELETE CASCADE` from the note, review-item
resolution/retraction, and the owner's own `correct_fact` — which supersedes rather than
sweeps, so a wrong value the owner notices stops being the current one even while the
stale row survives as history.

**The settle TAIL, which scoping the sweep did not give the conversation — closed by
S2.** Projection and reprojection (`_reproject_entities`, `_promote_corroborated`,
`project_appointments`, `project_emr`, …) run inside `AnalysisPipeline.settle_tail` and
NOWHERE else in a write path — verified: `analysis/pipeline.py` and `analysis/purge.py`
hold every call. The conversation calls `commit_facts` only, which does nothing
whole-note, so a conversation-written appointment landed in no projection and a
conversation-written `name.*` fact never refreshed `canonical_name`. That gap predated
this key and was MASKED by the bug: the analyzer's settle retracted the conversation's
facts and then projected the (now dead) rows away. Scoping the sweep made the facts
survive, which turned the mask into a fact the graph holds that the appointments view
does not — which is why S2 is the payment for S1's debt and not an improvement on it.
`analysis/clarify.settle_conversation` now runs the tail at the end of a clean pass, from
both turn paths. It does NOT run `stamp_analysis` (the conversation has no title or tags
verb, and the stamp is unconditional) and does not flip `integration_state`; both are
still `integrate_note`'s alone.

**Two residuals, both review cards rather than graph rows.** The settle's fact and
mention halves are producer-scoped; its review-card halves are still note-keyed and
producer-blind, so a settle by one producer deletes cards another filed:

- `_sweep_stale_ambiguous` retires open `ambiguous_mention` cards whole-note, sparing
  only those whose `payload->>'name'` the SETTLING producer still references — and any
  producer can file one (`_file_ambiguous_review` sits in `_resolve_entities`, which
  every commit path runs). When the analyzer settles, its names and the conversation's
  are both surface names off the same note, so the `NOT IN :names` clause spares most
  of the co-writer's cards and the loss is occasional. In the EMR direction it spares
  nothing: an EMR `Extraction`'s mentions and entity refs are SEMANTIC keys
  (`org:Quest`, `cond:E11.9`, `obs:<code>` — `ingest/emr/importer.py`), which share no
  surface with the names anyone else cards, so the clause excludes none of them and an
  EMR settle deletes essentially EVERY open `ambiguous_mention` card on that note. No
  graph row is lost and the filer refiles on its next run — but the re-enqueue paths
  that matter here do not give it one: `queue.backfill_pending_integration` and
  `analysis/rebuild.py` re-enqueue `integrate_note`/`emr_parse` only, so on those paths
  the card is deleted and never refiled, and the owner never gets to resolve that
  ambiguity.
- `_sync_truncation_review` (`analysis/pipeline.py`) is the same note-keyed,
  producer-blind shape: it deletes the note's open `extraction_truncated` card whenever
  the settling producer's own extraction did not truncate, so whichever of the two
  settlers runs second clears the card the other raised while that producer's tail is
  still dropped. Tracked as its own task, not fixed here.
"""

from __future__ import annotations

#: `integrate_note`'s facts and mentions — the `f"{provider}:{model}"` extractors,
#: whatever the live model is.
ANALYZER = "analyzer"

#: The note conversation's, from BOTH runs: `note_ingest` (unattended pass) and
#: `note_ingest_reply` (the owner's reply turn) are one producer.
CONVERSATION = "conversation"

#: The deterministic EMR parse (`ingest/emr/integrate.py`, extractor
#: `emr:deterministic`), which owns the write surface of an `emr_owned` note.
EMR = "emr"

#: The closed vocabulary — held here rather than as a DB `CHECK`, so a fourth producer
#: costs a constant and not a migration. Read by the tests that pin the mapping.
SETTLE_OWNERS = frozenset({ANALYZER, CONVERSATION, EMR})
