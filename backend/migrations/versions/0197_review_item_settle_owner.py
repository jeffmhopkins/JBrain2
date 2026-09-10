"""`settle_owner` on `app.review_items` — which producer FILED a card, so only it
may retire one.

The settle's two review-card halves (`_sweep_stale_ambiguous` and
`_sync_truncation_review`, `analysis/pipeline.py`) were keyed on the note alone: any
producer settling the note deleted the open `ambiguous_mention` and
`extraction_truncated` cards every OTHER producer had filed. On the owner's box that
fires deterministically — `note.ingested` fans out to `integrate_note` and `emr_parse`
on a health `Records` note, EMR cannot truncate, so its settle always took the delete
branch and the analyzer's "the tail of your medical records was dropped" card went with
it. 0196 scoped the fact and mention halves; this is the third key, for the cards.

**Singular, not a set — the one real difference from `facts.settle_owners`.** A fact
states something about the world, so two producers reading one note assert the same
identity key and `decide()` refreshes ONE row; the set exists because "whose row is
this?" has two right answers at once. A card states something about a READING — *this
producer could not resolve this name*, *this producer's extraction hit the cap* — and a
reading has exactly one reader. The dedup in `_file_ambiguous_review` keeps one card per
(name, note) so the inbox does not stack duplicates, which means a second producer
hitting the same ambiguity files nothing and the row keeps its first filer. That is
sound where a first-writer-keeps-it FACT would not be, because the scoped delete is
evidence-backed in a way the note-keyed one never was: a card is now removed only by the
producer that filed it, on a settle where that producer RE-READ the note and no longer
has the problem. If a co-writer was piggybacking on the card, its own next run re-files
— and unlike a retracted fact, a missing card costs no citable truth (resolving either
kind is a dismissal that writes no graph state, `analysis/repo.py`).

**Nullable with NO default, deliberately unlike 0196.** `review_items` holds a dozen
kinds and only two are swept; a `DEFAULT 'analyzer'` would stamp every firewall hold,
merge proposal and wiki contradiction with a producer that did not file it and could not
retire it. NULL says the true thing — *no settling producer claims this card* — and it
is also the safe failure: a future filer of a swept kind that forgets to stamp gets a
card no sweep can retire, i.e. one the owner must dismiss by hand. Visible and
dismissible, where 0196's default direction (silently joining someone else's claim) is
the failure mode the key exists to stop. The loudness 0196 bought with a grep test is
bought here by the column's own shape.

Backfill: the two swept kinds get `analyzer`, every other kind stays NULL. Nothing in
the corpus records a filer, and the analyzer is the only producer that has ever filed an
`extraction_truncated` card (it owns the only path that runs the cap) — for
`ambiguous_mention` it is the overwhelming majority and the same assumption 0196 made
for `entity_mentions`. All statuses, not just `open`: a resolved card can be reopened
(`analysis/repo.py`), and it should come back owned rather than immortal.

Revision ID: 0197
Revises: 0196
Create Date: 2026-09-10
"""

from alembic import op

revision = "0197"
down_revision = "0196"
branch_labels = None
depends_on = None

#: The kinds the settle sweeps, spelled out rather than imported: a migration must keep
#: meaning what it meant on the day it ran.
_SWEPT = "('ambiguous_mention', 'extraction_truncated')"


def upgrade() -> None:
    op.execute("ALTER TABLE app.review_items ADD COLUMN settle_owner text")
    op.execute(f"UPDATE app.review_items SET settle_owner = 'analyzer' WHERE kind IN {_SWEPT}")
    # No index: both sweeps filter `kind` and `payload->>'note_id'` first and a note's
    # open cards number in the single digits, so the column is a residual predicate on
    # an already-tiny row set.


def downgrade() -> None:
    # Losing the column restores the note-keyed sweeps, i.e. the bug: a producer that
    # cannot name a card's filer deletes every one on the note. Nothing here is
    # recoverable from another field the way 0196's backfill reads `extractor` — cards
    # carry no writer at all — so a re-upgrade re-guesses `analyzer` for both kinds and
    # hands the analyzer retirement rights over the EMR importer's and the
    # conversation's cards. Down-migrate to ABANDON, never as one leg of a round trip.
    op.execute("ALTER TABLE app.review_items DROP COLUMN IF EXISTS settle_owner")
