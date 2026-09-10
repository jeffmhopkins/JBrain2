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
  supersession; `correct_fact` supersedes an ACTIVE head and PINS the new value rather
  than retracting (against a `pending_review` head it holds BESIDE rather than
  superseding, which is O15 in `AGENT_INGEST_REWRITE.md` and does not change this); and a
  re-assert refreshes the SAME row in place, returning `ALREADY` when that row is live and
  `HELD` when it was already held. No path retracts and every one yields a row id, so its
  ledger never SHRINKS;
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

**The review cards carry the key too, and singular.** The settle's card halves
(`_sweep_stale_ambiguous`, `_sync_truncation_review`, both in `analysis/pipeline.py`)
were the last note-keyed, producer-blind destructive paths — S1's two residuals — and
they are closed by `review_items.settle_owner` (migration 0197). The EMR direction was
the deterministic one: on a health `Records` note `note.ingested` fans out to
`integrate_note` and `emr_parse`, EMR cannot truncate (`ingest/emr/integrate.py` says
why, at the seam), so its settle always took `_sync_truncation_review`'s clear branch
and deleted the analyzer's open `extraction_truncated` card — the owner never told that
the tail of their medical records had been dropped. `_sweep_stale_ambiguous` was worse
in that direction and quieter: an EMR `Extraction`'s refs are semantic keys
(`org:Quest`, `cond:E11.9` — `ingest/emr/importer.py`) sharing no surface with anyone
else's names, so its `NOT IN :names` clause spared nothing and an EMR settle deleted
essentially every open `ambiguous_mention` card on the note, on re-enqueue paths
(`queue.backfill_pending_integration`, `analysis/rebuild.py`) that re-run
`integrate_note`/`emr_parse` and so never give the filer a chance to re-file.

**One filer, not a claim set — the one place the card model differs from the row
model.** A fact states something about the world, which is why two producers reading one
note land the same identity key and the column above has to be a SET. A card states
something about a READING — *this producer could not resolve this name*, *this
producer's extraction hit the cap* — and a reading has one reader. `_file_ambiguous_review`
keeps its dedup whole-note (one card per name per note, so the inbox never stacks
duplicates), so a second producer that hits the same ambiguity files nothing and the row
keeps its first filer. First-writer-keeps-it is unsound for a fact and sound here for
two reasons that do not transfer: the scoped delete is evidence-backed — a card is
retired only by the producer that filed it, only on a settle where that producer re-read
the note and no longer has the problem — and a missing card costs no citable truth,
since resolving either kind is a dismissal that writes no graph state
(`analysis/repo.py`). A piggybacking producer that still cannot resolve the name
re-files on its own next run.

The column is nullable with no default, deliberately unlike `settle_owners` above:
`review_items` holds a dozen kinds and only two are swept, so NULL states the true thing
for the rest — *no settling producer claims this card* — and a swept-kind filer that
forgets to stamp gets a card no sweep can retire rather than one silently joining
someone else's claim. Migration 0197 argues both choices in full.

**The conversation's cards inherit the row leak, and for the same reason.** An
`ambiguous_mention` card is filed by whichever producer reaches `_file_ambiguous_review`
first, and on an ordinary note both fan out of one `note.ingested`, so that is a race the
conversation sometimes wins. A card it filed is stamped `conversation`, the analyzer's
sweep is now scoped away from it, and the conversation has no sweep at all (S3, dropped) —
so it stands until the owner dismisses it or `purge.purge_note_artifacts` runs. That is
the same permanent leak the rows carry, in the same safe direction: before the scoping,
the analyzer would have retired it, and a co-writer retiring a card on evidence it does
not have is what this key exists to stop. Read "a piggybacking producer re-files on its
own next run" above with that caveat — it is true of the analyzer and the importer, and
not of the conversation, which never re-files because it never releases.
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
