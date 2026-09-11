"""Who still asserts a note's rows — the whole-note settle's producer key.

`settle_note` retracts every unpinned, non-derived, active/pending_review fact of a
note it was not handed in `touched`, and `_reconcile_mentions` deletes that note's
mentions the same whole-note way. THREE producers have written one note — the analyzer
(`integrate_note`, deleted in R4 but the stamp on rows it left behind), the note
conversation (`agent/graphwritetools.py`, both runs) and the EMR importer
(`ingest/emr/integrate.py`) — and while the first two fanned out of a single
`note.ingested` event, a note-keyed sweep retracted whatever the co-writer had just
committed. That is shipped loss, not a race: the conversation asserts
once and revises by supersession, so it never re-asserts and loses again on EVERY later
settle of the note (re-ingest, `queue.backfill_pending_integration`,
`analysis/rebuild.py`). The reasoning and the sequencing are
`docs/plans/SETTLE_OWNERSHIP.md` (S1); this module is its vocabulary.

**Why a stamped column and not `Fact.extractor`.** `facts.extractor` is `text NOT NULL`
(migration 0006) and every writer sets one, so it does distinguish the writers — but
equality on it is the wrong key, twice:

- The analyzer's value was `f"{provider}:{model}"`, read live from the router. A PWA
  task override, an on-box model swap or a provider fallback changes it with nobody
  asking, and an extractor-scoped sweep would then stop
  retracting the note's OWN stale rows from the previous model — they match no future
  sweep and stay `active`, i.e. citable, forever. That breaks the sweep's primary job.
- `note_ingest` (unattended pass) and `note_ingest_reply` (the owner's reply turn) are
  ONE producer across two runs. Split by string, the unattended pass's settle would eat
  the owner's own reply-turn writes.

A `settle_owners` stamp survives both: the analyzer keeps claiming `analyzer` across any
model change, and `NoteGraphWriter` claims `conversation` whichever extractor string the
run carries — the grouping is structural rather than a naming convention.

⟲ **Re-derive the second bullet before you lean on it, because R3 inverted what it
protects.** Under the LEDGER sweep it removed the whole problem: one claim across both
runs meant the unattended pass could not release what the reply turn wrote. Under the
READING sweep it is the opposite — the unattended pass releases the shared claim against
a reading of the NOTE's text, so a reply-turn write whose words are nowhere in that text
is stripped of the only claim it ever had. The bullet is still right about the KEY (a
string split here would be strictly worse: the unattended pass's settle would eat the
reply turn's writes outright, and no reading would re-absorb them). What it no longer
covers is a reply-turn write that never becomes note text, and that is narrowed at the
source rather than here: `assert_fact` is off a reply turn whose words did not land on
the note as source text (`agents.narrow_for_unprompted_reply`, keyed on
`clarify.owner_words_reached_note`), `correct_fact`'s empty-address arm — the one
that MINTS a pinned row — refuses on the same condition, and `close_reading`'s
correction-note elevation is withheld on it (both `replytools`).

⟲ **This used to close with "so every fact this producer commits has words on the note
behind it, and the next reading re-states it." That is false, on two paths at once**,
which makes it a comment stating a property the code does not have (CLAUDE.md #4):

- `_assert_one` commits a fact whose `quote` is NOT in the note. It caps the weight and
  says so in the result line — held for review rather than overwriting a confident value
  — but it commits, on the unattended pass as much as on a reply turn, because D2 has
  inferred facts commit. Such a row is kept alive from pass to pass only by being
  restated; one the model invents once and never restates is retracted by the next clean
  pass, which is the sweep working rather than failing;
- and on the reply turn the narrowing bounds the ordinary path, not every path: an
  unattested element inside a reply-turn `close_reading` reaches the same commit. That
  row is unpinned, low-weight and falsifiable, and the reply path licenses no sweep of
  its own (`api/agent.py` settles with `reading=None`), so what it costs is the O16 loss
  and never a permanent wrong row.

⟲ **This used to close with an ABSOLUTE — "no reply turn may mint a PINNED row out of
words the note never received" — and three consecutive reviews found it false at one more
site each.** The third round found the third site (`_assert_one`'s CORRECTION-NOTE
ELEVATION: `_attests` is a string check, so on an `owner_correction` note a reply-turn
`close_reading` element pairing a real line of the note with a value the owner had only
typed in the thread committed `correction=True` → active, pinned, confidence 1.0; it is
gated now on the same condition the other two are). The fourth round found the fourth, and
the fourth is NOT a gap to close: `correct_fact` at an address the graph DOES hold
something for supersedes that head and commits the owner's new value active and PINNED —
on an unprompted reply turn as much as on an answering one, and the value may be one he
typed only into the thread. So the absolute is retired rather than patched a fourth time.
What the three gates enforce is narrow, and it is this:

**No reply turn whose words did not reach the note may mint a pinned row at an address the
graph holds nothing for.**

All three read the same condition — `ASSERT_FACT not in ctx.agent_tools`, taken at the
reply registry where it is exact. `assert_fact` is off such a turn entirely
(`agents.narrow_for_unprompted_reply`); `correct_fact`'s EMPTY-ADDRESS arm refuses
(`replytools`); `close_reading`'s correction-note elevation is withheld
(`graphwritetools`, `words_reached_note`, which since R3's fourth review governs a
`correction=True` passed as a parameter too). Those are every route into the only branch
that pins: `decide()` sets `insert_pinned` on `candidate.correction` and nothing else
(`analysis/supersession.py`). "The address" is the address AS THE TURN CAN SEE IT —
`entity_view` under the turn's read scopes — so a head in a domain the conversation is not
scoped to reads as empty and the arm refuses: wrong in the direction of refusing, which is
the direction to be wrong in.

The residue is the reason the sentence is worth having:

- **a pinned row at an OCCUPIED address is still mintable on that turn, and that is
  deliberate.** By the elevation's own standard — `sweep_note` spares a pinned fact, no
  later note supersedes one, no correction note addresses one — such a row is exactly as
  permanent as the one the third round closed, and the note may never say it. It stays
  because plan §3's ⟲ argues it: correcting a fact that IS on file is the owner's repair
  path on a settled thread, and pinning is the designed mechanism there. What the gate
  buys is that the turn can only REPAIR an address, never open one;
- **an UNPINNED row the note does not say is still mintable on that turn**, and that is
  deliberate too: it is O16's loss shape, falsifiable by the next reading and released by
  the first clean pass that reads the note without it. Closing it needs the `ToolContext`
  flag threaded through three `AgentLoop` sites and is not R3's;
- **"swept eventually" is not "swept" — on two of its three cases.** ⟲ This bullet used to
  say an unprompted reply changes nothing about the note, so `integration_state` stays
  `integrated` and nothing re-enqueues it. That is true of a reply into a SETTLED thread
  and of one whose append failed, and false of the commonest case there is: the designed
  §3b I7 send, where the structured answers land and the prose beside them is dropped.
  There `append_clarifications` sets `ingest_state='pending'` and enqueues `ingest_note` in
  the same transaction (`notes/repo.py`), and the re-ingest flips `integrated → stale`
  (`ingest/pipeline.py`), so the note IS re-enqueued and the row IS swept promptly. On the
  other two the row stands until the note is next edited or `analysis/rebuild.py` runs;
- **and on a THIRD-PARTY note nothing is ever swept at all.** `PassReading.third_party`
  refuses the sweep permanently (`clarify.settle_conversation`) — a stranger's words may
  cause a fact and never a retraction — so such a row persists by D10's design rather than
  by any of this. (The elevation itself cannot fire there: `is_correction` and
  `is_third_party` read the same field in opposite directions.)

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
no default on `commit_facts`, `commit_intent` and `settle_note`, so a new producer that
goes THROUGH those three seams and forgets it is a pyright error (that is what caught all ten
call sites the day the parameter landed). Nothing about the tables
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

**The conversation RELEASES its claim, and has since R3.** ⟲ This section said the
opposite — "never releases, permanently and by decision" — and it was true of the settle
that existed when it was written. `analysis/clarify.settle_conversation` now runs
`sweep_note` as well as the tail, so a `conversation` claim is released from every row of
the note the pass's closing reading no longer names, and a row whose claim set empties is
retracted. Edit a note to drop a claim both producers wrote and the graph stops asserting
it on the next pass, which is what the column was always supposed to buy.

**What licenses it is the READING, and the distinction is the whole wave.** A sweep for
this producer was built once (S3), reviewed and removed, on a proof rather than a bug
count — and that proof is not repealed, because it is about a different instrument:

- a release is justified only when a producer RE-DERIVED the note and dropped X;
- within one session this producer never drops anything — it asserts once and revises by
  supersession; `correct_fact` supersedes an ACTIVE head and PINS the new value rather
  than retracting (against a `pending_review` head it holds BESIDE rather than
  superseding, which is O15 in `AGENT_INGEST_REWRITE.md` and does not change this); and a
  re-assert refreshes the SAME row in place, returning `ALREADY` when that row is live and
  `HELD` when it was already held. No path retracts and every one yields a row id, so its
  LEDGER never shrinks;
- therefore the only claims a ledger-derived release could remove are OTHER sessions';
- and judging those needs a complete current READING of the note, which
  `NoteConversationRepo.writes()` — a record of what a pass WROTE — structurally is not.
  Its silence is the designed output: the agent holds `find_entity`/`read_entity`, is
  told to read before it writes, and is rewarded by its own tool surface for not
  restating what the graph already holds.

R3 did not answer that argument; it built the thing the last bullet names. `close_reading`
is a verb whose whole job is to restate what the note says NOW, every restated identity key
comes back `ALREADY` carrying the SAME `fact_id`, and `Reading.fact_ids` is therefore the
complete current reading — so the sweep is keyed on a claim about the NOTE and never on a
record of writes. ⟲ The sentence that used to close this section — *a sound conversation
sweep is empty; a non-empty one is unsound* — was a statement about a LEDGER sweep and is
false of this one: a reading sweep is soundest exactly when it is non-empty, because that
is the note having stopped saying something.

S3's failure 4 is the one to check against and it cannot recur here: the owner answers,
the clarification block is appended so the note's text only GROWS, and nothing is keyed on
a generation at all — the reading states what the note says now, a fact the answer did not
remove is re-stated, and its id lands in `touched`. `note_body_sha` keeps its one honest
question (*did the note move under me?*) and gains no second job. SETTLE_OWNERSHIP.md's S3
section is still the argument in full; read it before re-deriving a LEDGER sweep, which
remains unsound.

**What the sweep will not do, and what removes those rows.** Four endings refuse it
(`settle_conversation`): a pass that did not end cleanly, a CLAMPED reading (a prefix of
the note — and a refused element clamps it too, `graphwritetools.close_reading`), a
THIRD-PARTY reading, and a pass that closed no reading at all. Each lands on the pre-R3
behaviour — commit, project, retract nothing — so the residue this section used to
describe as permanent is now the residue of a DEGRADED pass, cleared by the note's next
clean one. What removes a row with no sweep at all, none of it producer-scoped:
`analysis/purge.purge_note_artifacts` (note deletion, and the corpus rebuild sweep),
`ON DELETE CASCADE` from the note, review-item resolution/retraction, ordinary
supersession (any later fact on the same identity key retires the head), and the owner's
own `correct_fact` — which supersedes rather than sweeps, so a wrong value the owner
notices stops being the current one even while the stale row survives as history.

State the cost in trade terms, because the S1 trade is what got us here. Before S1 those
rows were retracted by a producer that had not written them and could not tell them from
its own — which is how the owner's answers were disappearing. S1 bought "the graph keeps
asserting something the note no longer says" with it, and R3 is what pays that back: the
producer that wrote the rows is the one that releases them, on evidence about the note
rather than on a note-keyed guess.

**The mention half is bounded on its own, which is why the fact half was the loud one.**
The two tables sit behind different foreign keys (migration 0006):
`entity_mentions.chunk_id` is `ON DELETE CASCADE`, while `facts.chunk_id` is
`ON DELETE SET NULL`. Re-ingesting a note replaces its chunks (`ingest/pipeline.py`,
`notes/repo.py` both delete the old ones), so a `conversation` mention claim the settle
cannot release survives at most until that note's next re-chunk, while a fact carrying one
keeps its row and merely loses its chunk pointer. It still matters after R3: the reading
sweep passes `mentions=None` and so reconciles no mentions at all
(`settle_conversation` says why), which leaves the mention half exactly where this
paragraph found it.

**A future remedy, recorded and NOT scheduled.** That same asymmetry names an
evidence-based retraction needing no producer: `ingest.carryover` keeps a rebuilt chunk's
row when it comes back byte-identical, so a fact whose note has been re-ingested and whose
`chunk_id` is now NULL is a fact WHOSE CITED TEXT IS GONE. That is real evidence about the
note, it is producer-agnostic, and it applies to the analyzer's rows exactly as to the
conversation's — so it satisfies the invariant S3 could not, without reconstructing
anybody's reading from a write ledger. R3's reading sweep is a different answer to the
same invariant and is the one that shipped, which leaves this a remedy for the FACT the
reading never names because no pass read the note cleanly again. It is a different
mechanism from the settle and it needs its own care (a fact can lose its chunk for reasons
other than its text disappearing). Nothing depends on it. Do not read this paragraph as
planned work.

**The review cards carry the key too, and singular.** The settle's card halves
(`_sweep_stale_ambiguous`, `_sync_truncation_review`, both in `analysis/pipeline.py`)
were the last note-keyed, producer-blind destructive paths — S1's two residuals — and
they are closed by `review_items.settle_owner` (migration 0197). The EMR direction was
the deterministic one: on a health `Records` note `note.ingested` fanned out to
`integrate_note` and `emr_parse`, EMR cannot truncate (`ingest/emr/integrate.py` says
why, at the seam), so its settle always took `_sync_truncation_review`'s clear branch
and deleted the analyzer's open `extraction_truncated` card — the owner never told that
the tail of their medical records had been dropped. `_sweep_stale_ambiguous` was worse
in that direction and quieter: an EMR `Extraction`'s refs are semantic keys
(`org:Quest`, `cond:E11.9` — `ingest/emr/importer.py`) sharing no surface with anyone
else's names, so its `NOT IN :names` clause spared nothing and an EMR settle deleted
essentially every open `ambiguous_mention` card on the note, on re-enqueue paths
(`queue.backfill_pending_integration`, `analysis/rebuild.py`) that re-ran both producers
and so never gave the filer a chance to re-file. R4 deleted the analyzer, which narrows
the live pairing to one card filer (EMR) — and leaves every card the analyzer filed
standing under its own key, which is exactly what this column is now protecting.

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

**The conversation's cards leak where its ROWS no longer do, and the reason is now a
different one.** An `ambiguous_mention` card is filed by whichever producer reaches
`_file_ambiguous_review` first — it sits in `_resolve_entities`, which `commit_facts`
runs, so the conversation files them too — and on an ordinary note both producers fan out
of one `note.ingested`, so that is a race the conversation sometimes wins. A card it filed
is stamped `conversation` and the analyzer's sweep is scoped away from it. ⟲ This used to
say "and the conversation has no sweep at all (S3, dropped)", which is no longer why: the
conversation's settle sweeps FACTS off its closing reading, and simply runs no card half —
`settle_conversation` is three steps, not five, because the two card halves want the
`Extraction` this producer does not have (`analysis/pipeline.py`). So the card stands
until the owner dismisses it or `purge.purge_note_artifacts` runs, in the same safe
direction as before: a co-writer retiring a card on evidence it does not have is what this
key exists to stop. Read "a piggybacking producer re-files on its own next run" above with
that caveat — it is true of the analyzer and the importer, and not of the conversation,
whose settle never reaches a card at all.
"""

from __future__ import annotations

#: The deleted `integrate_note` producer's facts, mentions and cards — the
#: `f"{provider}:{model}"` extractors, whatever the live model was. Nothing writes this
#: stamp any more (R4); it still NAMES the rows that producer left on the box, which is
#: what keeps a surviving producer's sweep off them.
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
