"""A clarification block the owner wrote UNPROMPTED (O16 of AGENT_INGEST_REWRITE, option 1).

0193 built one block shape: a question the agent asked paired with the owner's answer,
`question NOT NULL` and non-blank. That is the only way text has ever reached a note it
did not start with — so the owner opening a note he saved yesterday and typing "actually
the dentist is Dr. Ashcote" had nowhere for those words to land, and the reply path's
answer was to REFUSE the write and tell him so (`agents.narrow_for_unlanded_reply`). The
same dead end was reached on a thread that IS waiting, by the designed send: the taps
carry the answers, the box carries a sentence beside them, and the prose was dropped.

This column is the shape that was missing. `kind` says which of the two a row is, and
the CHECK ties the pair together in Postgres:

* `answer` — 0193's row, a question and its answer. `question` stays non-null and
  non-blank for exactly these, so the constraint the old column carried is not weakened
  for the rows it was about, it is narrowed to them.
* `addition` — the owner's own unprompted words, with NO question at all.

**Why a discriminator and not a bare nullable column.** Three readers render a block
(`notes.compose.clarification_block`, the PWA's eraser list, the search leg's composed
preview) and each has to pick a rendering; a NULL is an absence they would each have to
interpret, and a reader that forgets reads `None` into the owner's own note text. `kind`
makes the two shapes nameable — a writer states which it is writing, a reader switches on
it — and the CHECK makes "an answer has a question, an addition has none" a fact of the
schema rather than a convention four modules keep separately. The DEFAULT is `answer`, so
a writer that forgets the column writes the old shape and a writer that forgets it while
passing no question fails the CHECK loudly instead of storing a questionless "answer".

Everything else about the table is deliberately untouched: the RLS policy, the
domain-match trigger, the immutability grant (`UPDATE (domain_code)` and nothing wider)
and the `(note_id, seq)` index all apply to an addition exactly as they do to an answer,
because an addition is the same thing — the owner's words, composed onto the note at read
time and chunked with it.

Revision ID: 0203
Revises: 0202
Create Date: 2026-09-14
"""

from alembic import op

revision = "0203"
down_revision = "0202"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.note_clarifications ALTER COLUMN question DROP NOT NULL")
    op.execute("ALTER TABLE app.note_clarifications ADD COLUMN kind text NOT NULL DEFAULT 'answer'")
    # Replaced rather than kept beside the new one: `question NOT NULL` plus a non-blank
    # CHECK is the answer shape, and the new constraint states it for the rows it is true
    # of instead of for every row.
    op.execute(
        "ALTER TABLE app.note_clarifications"
        " DROP CONSTRAINT note_clarifications_question_nonblank"
    )
    op.execute(
        """
        ALTER TABLE app.note_clarifications ADD CONSTRAINT note_clarifications_kind_shape
        CHECK (
            (kind = 'answer' AND question IS NOT NULL AND length(btrim(question)) > 0)
            OR (kind = 'addition' AND question IS NULL)
        )
        """
    )


def downgrade() -> None:
    # LOSSY, in the one direction that matters: an `addition` is the owner's own text and
    # the old schema has no shape that can hold it, so going back DELETES those rows —
    # the note's composed body loses the sentences he typed into it, and the graph rows
    # citing their chunks lose the only text behind them on the next re-ingest. There is
    # no reconstruction on the way back up (0196's asymmetry, sharper: there is no other
    # column to read them out of). Down-migrate to ABANDON this shape, never as one leg
    # of a round trip.
    op.execute("DELETE FROM app.note_clarifications WHERE kind = 'addition'")
    op.execute("ALTER TABLE app.note_clarifications DROP CONSTRAINT note_clarifications_kind_shape")
    op.execute("ALTER TABLE app.note_clarifications DROP COLUMN kind")
    op.execute(
        """
        ALTER TABLE app.note_clarifications ADD CONSTRAINT note_clarifications_question_nonblank
            CHECK (length(btrim(question)) > 0)
        """
    )
    op.execute("ALTER TABLE app.note_clarifications ALTER COLUMN question SET NOT NULL")
