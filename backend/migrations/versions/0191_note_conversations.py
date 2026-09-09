"""`note_conversations` + `note_conversation_tool_calls` — the durable spine of an
agent-conversation note ingest (docs/plans/AGENT_INGEST_CONVERSATION_PLAN.md, W2).

A note conversation IS an ordinary `app.agent_sessions` row (D1: same agent, same loop,
same memory). This is the side table that makes one a note conversation — the
`agent_session_plans` shape (0155), so `agent_sessions` stays generic and every chat is
not carrying four NULL ingest columns.

`state` is a closed set of four:

- `running` — a pass is in flight, or the thread is open and working.
- `waiting_on_owner` — the agent asked and stopped. D4/D5 make the notes inbox tab a
  list of "threads waiting on you", so this is a QUERYABLE state with its own index,
  never a guess derived from "the last turn was an assistant turn".
- `settled` — the whole-note sweep ran (constraint 6) and the thread is done.
- `failed` — the pass died: a truncated turn, the model unavailable, consecutive tool
  errors. Separate from `settled` because a truncated turn asserted only a prefix and
  the sweep deliberately did NOT run, so the two are not the same ending.

B2-CONVERSATION-MODEL.md proposed `stale` and `superseded` on top of those. Neither is
here. `stale` is not a state because nothing writes it: a note edit re-ingests through
`ingest/pipeline.py` and has no hook that reaches a conversation, so the flag would only
ever be set by the very code that could instead compare `note_body_sha` against the
note's current body — a derived comparison cannot itself go stale. `superseded` existed
only to keep the one-live-conversation index honest across a continuation chain, and the
four states above do that already: `settled` and `failed` both release the note.

`note_body_sha` is the body the conversation was started against. D6 appends
clarification blocks to a note and that re-ingests it, so a resumed conversation must be
able to tell the note moved under it without diffing prose.

**Live = `running` or `waiting_on_owner`**, and `note_conversations_one_live` makes at
most one of those exist per note (the 0188 partial-unique idiom, keyed on `note_id`
rather than on a constant because the constraint is per-note, not global). A thread
waiting on the owner is live because it is exactly the thread the inbox points at and
the one the owner's answer must re-enter; opening a rival would orphan the question. A
`failed` one is NOT live: a dead pass must be retryable by starting a fresh conversation,
and holding a note hostage to a thread nobody can revive is unfixable on a box with no
terminal (CLAUDE.md #10).

RLS: owner-only, `app.is_owner()`, ENABLE and FORCE — the `agent_session_plans` /
`graph_rebuild_runs` posture. Deliberately NO `domain_code` on either table:

- The notes tab must list every thread waiting on the owner whatever narrowing the
  session rendering it happens to carry. A domain-gated row would silently drop threads
  from the inbox and the owner would never learn they existed. A narrowed owner still
  sees the row; what it cannot read is the NOTE, which keeps its own policy — so the
  thread appears and its title does not, which is the honest split.
- On the ledger the same column would be wrong in both directions. Stamped from the
  note's domain it is too loose, because `args` can carry the quote for a fact that
  ratcheted above the note (constraints 1 and 2). Stamped from the ratcheted maximum it
  hides the row from the conversation that wrote it — a note conversation runs
  owner-scoped to `(note_domain, 'general')`. And `has_all_domain_scopes('{}')` is true
  for every session, so the column's own default would fail open on exactly the calls
  that wrote nothing. A firewall whose key the writer picks is not a firewall.

The ledger records **what a write claimed**. It is not itself the firewall: the firewall
is the RLS on `app.facts` / `app.entities` / `app.chunks`, the tables actually written.
`domains` is a plain `text[]` for that reason — and it is filled by the handler from what
the write path reported, never from a model argument. It cannot FK `app.domains(code)`
(Postgres has no per-element array FK), and a validating trigger would abort the ledger
write for a call that already landed, which is the wrong trade: a lost audit row is worse
than an unrecognised code string.

`args jsonb` is stored, never executed. Under risk 1 of the plan a note body may be
third-party text and the model copies note text into `quote`/`statement`, so anything
that re-renders these args into a prompt owes them the DATA framing `intake/turn.py`'s
`_RECIPIENT_FRAME` uses. The size cap lives in `models/note_conversation.py`, not in a
CHECK here: a CHECK would abort the whole transaction — and with it the graph write the
call already made — over an oversized argument, so the repo truncates and marks instead.

Grants. `note_conversations`: SELECT, INSERT, UPDATE. No DELETE — a conversation row is
never removed on its own, because that would leave the `agent_sessions` row and a
transcript full of the note's text behind. It dies with its session, and note deletion
deletes that session explicitly (`analysis/purge.py`), since `app.notes` soft-deletes and
this `ON DELETE CASCADE` therefore never fires on a note delete (constraint 11). The FK
stays for a genuine hard delete (Ops → Reset) and to declare the ownership.
`note_conversation_tool_calls`: SELECT, INSERT, and UPDATE on `turn_id` ALONE — the one
mutation a ledger row ever takes is being bound to its assistant turn once the exchange
ends (the `turn_attachments` idiom, `agent/transcript_store.py`). Everything else about a
recorded call is what happened.

Revision ID: 0191
Revises: 0190
Create Date: 2026-09-09
"""

from alembic import op

revision = "0191"
down_revision = "0190"
branch_labels = None
depends_on = None

LIVE = "('running', 'waiting_on_owner')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.note_conversations (
            session_id uuid PRIMARY KEY
                REFERENCES app.agent_sessions(id) ON DELETE CASCADE,
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            state text NOT NULL DEFAULT 'running'
                CHECK (state IN ('running', 'waiting_on_owner', 'settled', 'failed')),
            -- sha256 of the body this conversation read. A D6 clarification block
            -- re-ingests the note, so a resumed pass compares against this to learn
            -- that the note moved under it.
            note_body_sha text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # One live conversation per note, and the index that answers "the live conversation
    # for this note" — the same lookup, so a second index would not earn its keep.
    op.execute(
        "CREATE UNIQUE INDEX note_conversations_one_live ON app.note_conversations (note_id)"
        f" WHERE state IN {LIVE}"
    )
    # Every conversation of a note, live or not: the purge deletes by note_id and a
    # note's thread history is state-blind, neither of which the partial index serves.
    op.execute("CREATE INDEX note_conversations_note_idx ON app.note_conversations (note_id)")
    # The notes inbox tab: threads waiting on you, newest first (D4/D5).
    op.execute(
        "CREATE INDEX note_conversations_waiting_idx ON app.note_conversations (updated_at DESC)"
        " WHERE state = 'waiting_on_owner'"
    )
    op.execute("ALTER TABLE app.note_conversations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.note_conversations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY note_conversations_owner ON app.note_conversations
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.note_conversations TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.note_conversation_tool_calls (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid NOT NULL
                REFERENCES app.note_conversations(session_id) ON DELETE CASCADE,
            -- Nullable and bound later: a call is recorded as it happens, but the
            -- assistant turn is only written once the exchange ends
            -- (agent/transcript_store.py). SET NULL so pruning a turn cannot erase the
            -- ledger of what that turn wrote.
            turn_id uuid REFERENCES app.agent_turns(id) ON DELETE SET NULL,
            seq bigint GENERATED ALWAYS AS IDENTITY,
            name text NOT NULL,
            args jsonb NOT NULL DEFAULT '{}',
            ok boolean NOT NULL,
            detail text NOT NULL DEFAULT '',
            -- What the write path reported this call touched. Arrays rather than a
            -- row-per-id join table: one call writes many facts, nothing joins on these,
            -- and the union over a conversation IS constraint 6's touched/projected
            -- accumulator. No FK (Postgres has no per-element array FK) — a purged fact
            -- leaves a dangling id, which is correct for an audit of what happened.
            entity_ids uuid[] NOT NULL DEFAULT '{}',
            fact_ids uuid[] NOT NULL DEFAULT '{}',
            domains text[] NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Call order within a conversation: the D3 chip's render and the settle accumulator.
    op.execute(
        "CREATE INDEX note_conversation_tool_calls_seq_idx"
        " ON app.note_conversation_tool_calls (session_id, seq)"
    )
    op.execute("ALTER TABLE app.note_conversation_tool_calls ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.note_conversation_tool_calls FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY note_conversation_tool_calls_owner ON app.note_conversation_tool_calls
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT ON app.note_conversation_tool_calls TO jbrain_app")
    op.execute("GRANT UPDATE (turn_id) ON app.note_conversation_tool_calls TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.note_conversation_tool_calls")
    op.execute("DROP TABLE IF EXISTS app.note_conversations")
