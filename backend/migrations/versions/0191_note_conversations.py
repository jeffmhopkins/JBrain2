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
able to tell the note moved under it without diffing prose. NOTHING READS IT YET: the
comparison lands in W3 with the resume path, and until it does the column is stored
evidence with no live mechanism behind it. That is also the honest correction to the
`stale` argument above — a comparison cannot rot, but one nobody performs answers
nothing either, so W3 owes the reader, not merely the column.

**Live = `running` or `waiting_on_owner`**, and `note_conversations_one_live` makes at
most one of those exist per note (the 0188 partial-unique idiom, keyed on `note_id`
rather than on a constant because the constraint is per-note, not global). A thread
waiting on the owner is live because it is exactly the thread the inbox points at and
the one the owner's answer must re-enter; opening a rival would orphan the question. A
`failed` one is NOT live: a dead pass must be retryable by starting a fresh conversation,
and holding a note hostage to a thread nobody can revive is unfixable on a box with no
terminal (CLAUDE.md #10).

RLS: owner-only, `app.is_owner()`, ENABLE and FORCE — the `agent_session_plans` /
`graph_rebuild_runs` posture, with NO domain gate on either table.

The counter-precedent is `app.agent_episodes` (0017:110-117), and it is the closer one:
agent-written, domain-tagged, content-bearing, and gated on
`app.is_owner() AND app.has_all_domain_scopes(domain_scopes)` over a `NOT NULL` column
with no default. The answer is not that a domain gate would fail open — that would only
be true of a `DEFAULT '{}'` this migration would have inflicted on itself, and it does
not: `domains` is `NOT NULL` with NO DEFAULT, so a writer states where the write landed
even when the answer is "nowhere". The answer is that gating the LEDGER on
`has_all_domain_scopes(domains)` breaks D3 for exactly the writes that matter most. A
fact on a `general` note that the deterministic domain floor ratchets to `health`
produces a row stamped `{health}`; the conversation that wrote it runs owner-scoped to
`(note_domain, 'general')` under constraint 2, so it could not read back its own write to
render the chip. A chip that goes blank precisely on the sensitive writes is worse than
no chip at all.

Owner-only is defensible here rather than merely convenient, on two counts. First, it
adds no exposure for the note it is about: `app.agent_turns` already holds that same
note's BODY in this same session, owner-only and with no domain gate — the ledger's
`args` are a capped echo of text the transcript beside it keeps in full. Second, every
read path is scoped to one session (`tool_calls`, `writes`), so what a narrowed
conversation can reach is its own thread, not the corpus of them. That scoping is
load-bearing for this whole argument, and `test_note_conversations_pg.py` proves it
rather than asserting it.

The conversation row's own lack of a domain is a separate, easier call: the notes tab
must list every thread waiting on the owner whatever narrowing the session rendering it
carries, or a domain-gated row silently drops a question from the inbox and the owner
never learns it existed. A narrowed owner still sees the row; what it cannot read is the
NOTE, which keeps its own policy — the thread appears and its title does not, which is
the honest split.

The ledger records **what a write claimed**. It is not itself the firewall: the firewall
is the RLS on `app.facts` / `app.entities` / `app.chunks`, the tables actually written.
`domains` is a plain `text[]` for that reason — and it is filled by the handler from what
the write path reported, never from a model argument. It cannot FK `app.domains(code)`
(Postgres has no per-element array FK) and it gets no validating trigger, because a
trigger would abort the ledger write for a call that already landed — the same trade the
size cap makes. The codes are instead checked at the repo boundary
(`models/note_conversation.py:validate_domains`) against the four owner-knowledge
domains, so an unrecognised code is REFUSED rather than stored: a docstring contract is
not a contract, and a ledger that records a domain the firewall does not have is a
ledger that lies about where a write went.

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
            -- No DEFAULT: a writer states where the write landed, even when the
            -- answer is the empty array. The default is what would have made a
            -- domain gate here fail open, and nothing needs one.
            domains text[] NOT NULL,
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
