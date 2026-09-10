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

**The conversation NEVER releases its claim, permanently and by decision.** A claim is
released by a settle, and the conversation's settle (`analysis/clarify.settle_conversation`)
runs the tail and no sweep. So every row carrying a `conversation` claim — the ones only
it wrote AND the ones both producers assert — is retractable by no sweep at all. Edit a
note to drop a claim both producers wrote and the graph keeps asserting it.

That is a decision, not an omission. A sweep for this producer was built (S3), reviewed
and removed, on a proof rather than a bug count:

- a release is justified only when a producer RE-DERIVED the note and dropped X;
- within one session this producer never drops anything — it asserts once and revises by
  supersession, `correct_fact` supersedes and PINS rather than retracting, and a
  re-assert returns `ALREADY` with the same `fact_id` — so its ledger never SHRINKS;
- therefore the only claims a release could remove are OTHER sessions';
- and judging those needs a complete current READING of the note, which
  `NoteConversationRepo.writes()` — a record of what a pass WROTE — structurally is not.
  Its silence is the designed output: the agent holds `find_entity`/`read_entity`, is
  told to read before it writes, and is rewarded by its own tool surface for not
  restating what the graph already holds.

**A sound conversation sweep is empty; a non-empty one is unsound.** The built version
failed four ways, ending on the feature's happy path: the owner answers a question, the
clarification block is appended so the note's text only GROWS, `record_owner_reply`
re-stamps the `note_body_sha` the sweep was keyed on, and an earlier conversation's facts
are retracted. SETTLE_OWNERSHIP.md's S3 section is the argument in full; read it before
re-deriving a sweep from "nothing ever releases a `conversation` claim", which is true and
is not a reason.

State the cost in trade terms, because it IS a trade and it was made with the numbers in
front of us. Before S1 those rows were retracted by a producer that had not written them
and could not tell them from its own — which is how the owner's answers were disappearing.
The exchange is "a co-writer silently deletes what the owner said" for "the graph keeps
asserting something the note no longer says". The second is visible, correctable and
recoverable; the first is none of those. That does not make it free.

**What removes such a row.** None of these is producer-scoped, which is why they are the
escape hatches: `analysis/purge.purge_note_artifacts` (note deletion, and the corpus
rebuild sweep), `ON DELETE CASCADE` from the note, review-item resolution/retraction,
ordinary supersession (any later fact on the same identity key retires the head), and the
owner's own `correct_fact` — which supersedes rather than sweeps, so a wrong value the
owner notices stops being the current one even while the stale row survives as history.
The unbounded residue is a fact whose identity key is never re-asserted, on a note never
purged or rebuilt.

**The mention half is bounded on its own, which is why the fact half was the loud one.**
The two tables sit behind different foreign keys (migration 0006):
`entity_mentions.chunk_id` is `ON DELETE CASCADE`, while `facts.chunk_id` is
`ON DELETE SET NULL`. Re-ingesting a note replaces its chunks (`ingest/pipeline.py`,
`notes/repo.py` both delete the old ones), so an unreleasable `conversation` mention claim
survives at most until that note's next re-chunk, while a fact carrying one keeps its row
and merely loses its chunk pointer.

**A future remedy, recorded and NOT scheduled.** That same asymmetry names an
evidence-based retraction needing no producer: `ingest.carryover` keeps a rebuilt chunk's
row when it comes back byte-identical, so a fact whose note has been re-ingested and whose
`chunk_id` is now NULL is a fact WHOSE CITED TEXT IS GONE. That is real evidence about the
note, it is producer-agnostic, and it applies to the analyzer's rows exactly as to the
conversation's — so it satisfies the invariant S3 could not, without reconstructing
anybody's reading from a write ledger. It is a different mechanism from the settle and it
needs its own care (a fact can lose its chunk for reasons other than its text
disappearing). Nothing depends on it. Do not read this paragraph as planned work.

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
