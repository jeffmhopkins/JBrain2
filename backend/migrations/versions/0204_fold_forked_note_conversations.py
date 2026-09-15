"""Fold a note's forked conversations back to the one the owner talked in.

Until `note_converse` learned to resume, every re-reading of a note opened a SECOND
conversation: the owner added to a note, the re-ingest ran, and `thread_for_note` —
which returns the NEWEST — pointed the note screen at the new thread. The one he had
typed into, holding the first pass and his own reply, was still in the database and
unreachable from the app. He reported it as *"I then said the size was different, and
went back into the conversation, and the original conversation was gone?"*

The code fix stops new forks. This heals the ones already on the box.

**What it deletes, and what it refuses to.** For each note with more than one
conversation it keeps the OLDEST — the one holding the note's history — and deletes
newer ones ONLY where the ledger shows they committed nothing: no `fact_ids` on any
successful call. A fork that DID write is left exactly where it is. Deleting it would
strand the facts it committed from the conversation that vouches for them, and a
duplicate thread the owner can still reach is a far smaller problem than a fact whose
provenance has been deleted out from under it. Those, if any exist, stay for a human
decision.

Deletion is of the `agent_sessions` row, not the `note_conversations` side row, which
is `analysis/purge.py:_purge_conversations`' rule and for its reason: erasing the side
row alone strands the transcript holding the note's body. The session cascades the side
row, the ledger, the turns and the runs.
"""

from alembic import op

revision = "0204"
down_revision = "0203"
branch_labels = None
depends_on = None


# The forks that may go: not the oldest for their note, and nothing on their ledger ever
# committed a fact. `fact_ids` is a uuid[]; `coalesce(array_length(...), 0)` counts an
# empty array and a NULL alike, so a call that wrote nothing does not protect a fork.
_DOOMED = """
    WITH ranked AS (
        SELECT session_id, note_id,
               row_number() OVER (
                   PARTITION BY note_id ORDER BY created_at ASC, session_id ASC
               ) AS rank
          FROM app.note_conversations
    )
    SELECT r.session_id
      FROM ranked r
     WHERE r.rank > 1
       AND NOT EXISTS (
           SELECT 1 FROM app.note_conversation_tool_calls t
            WHERE t.session_id = r.session_id
              AND t.ok
              AND coalesce(array_length(t.fact_ids, 1), 0) > 0
       )
"""


def upgrade() -> None:
    op.execute(f"DELETE FROM app.agent_sessions WHERE id IN ({_DOOMED})")


def downgrade() -> None:
    raise RuntimeError(
        "0204 deletes duplicate conversations that wrote nothing; there is nothing to"
        " restore them from. Restore from a backup (Data -> Back up everything)."
    )
