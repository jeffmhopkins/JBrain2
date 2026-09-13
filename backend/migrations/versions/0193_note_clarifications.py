"""Appended clarification blocks for a note (D6/D7 of AGENT_INGEST_CONVERSATION_PLAN).

D6: a note keeps the body its author wrote FROZEN and gains appended, timestamped
clarification blocks as the owner answers the agent's questions. The naive storage —
append onto `app.notes.body` — loses every block the first time the owner edits the
note, because `PATCH /notes/{id}` is a whole-value overwrite. So the blocks live here
and are composed onto the body at READ time (`jbrain.notes.compose`), which also keeps
the original body's character offsets untouched. Precisely: the offsets that exist are
`app.chunks.char_start`/`char_end` into the note's composed text, and the CHUNK-RELATIVE
spans on `app.entity_mentions` that `analysis.pipeline._locate` derives from them.
`app.facts` carries no span — it cites a chunk by id — so appending after the body is
what keeps the chunk boundaries stable, and `ingest.carryover` is what keeps the ids
stable across the re-ingest that follows.

D7: the composed text is what ingest chunks, so a block is a chunk of the same note and
a fact extracted from it has a real chunk to cite.

Three shapes here are deliberate and depart from the obvious precedents:

* `domain_code`, and the `has_domain_scope` policy the notes/chunks/facts tables use —
  NOT the owner-only policy of `graph_rebuild_runs` (0188) or `archivist_memory` (0094).
  Those hold sweep metadata and agent scratchpad; a clarification is the owner's own
  words about a health or finance note, and the same sentence is already firewalled by
  domain in `app.chunks`. Owner-only here would let a domain-narrowed owner session —
  which is exactly what a note conversation runs as (constraint 2) — read a finance
  note's clarification, and would make a capability token see a different note text
  than its own search results. The column is duplicated rather than joined for the
  reason 0002 gives for attachments: the policy then needs no join.
* `GRANT UPDATE (domain_code)` and nothing wider. A clarification is an immutable
  record of what the owner said; the one legitimate write after insert is the domain
  carry when `update_note` moves the note (the 0002 attachment invariant). A
  column-level grant makes "the text is never rewritten" a Postgres property instead
  of a code convention, which is the D6 freeze stated in the only place that holds.

* A trigger, not just the RLS policy, ties `domain_code` to the note's — the 0045
  subsection rule. The policy validates the domain the writer NAMES; nothing in it
  reaches the note, and the FK bypasses RLS the way FK checks always do. See the comment
  on the trigger for what that let through.

No UPDATE on the rest and no route: the append path is a repo method (W3's `ask_owner`
calls it), so there is no way to edit a block through the API at all.

Revision ID: 0193
Revises: 0192
Create Date: 2026-09-09
"""

from alembic import op

revision = "0193"
down_revision = "0192"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.note_clarifications (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            -- A global identity, not a per-note counter: ordering within a note is all
            -- composition needs, and `max(seq)+1` would race two concurrent answers
            -- onto the same number.
            seq bigint GENERATED ALWAYS AS IDENTITY,
            question text NOT NULL,
            answer text NOT NULL,
            -- The conversation the answer came from (D1: `agent_sessions` IS the
            -- conversation's identity). SET NULL, not CASCADE: purging a conversation
            -- must never silently delete the owner's answer, which is source text the
            -- graph re-derives from.
            session_id uuid REFERENCES app.agent_sessions(id) ON DELETE SET NULL,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT note_clarifications_question_nonblank
                CHECK (length(btrim(question)) > 0),
            CONSTRAINT note_clarifications_answer_nonblank
                CHECK (length(btrim(answer)) > 0)
        )
        """
    )
    # Every read is "this note's blocks, in order" — the composition helper's only query.
    op.execute(
        "CREATE INDEX note_clarifications_note_idx ON app.note_clarifications (note_id, seq)"
    )
    op.execute("ALTER TABLE app.note_clarifications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.note_clarifications FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY note_clarifications_domain ON app.note_clarifications
        USING (app.has_domain_scope(domain_code))
        WITH CHECK (app.has_domain_scope(domain_code))
        """
    )
    # A block's domain must EQUAL its note's, enforced in Postgres — the 0045 subsection
    # rule, for the same reason it was put there rather than in app code (non-negotiable
    # #3). The policy above only checks the domain the WRITER supplies, and the FK to
    # app.notes bypasses RLS as every FK check does, so without this a general-scoped
    # capability token can stamp `general` on a clarification of a HEALTH note: the text
    # then composes into the health note's body (jbrain.notes.compose), and D7 turns it
    # into a health chunk and health facts. The app path already reads the note under
    # RLS first, so this is the backstop, not the mechanism.
    #
    # SECURITY DEFINER + pinned search_path for the same reason 0045 needs it: the
    # writers are narrowed sessions under FORCE RLS, and an INVOKER lookup of a note
    # outside their scope returns NULL rather than the true row. A NULL note domain is a
    # hard failure (IS DISTINCT FROM is NULL-safe), so a clarification can never be
    # written against a note the checker cannot see.
    #
    # UPDATE is covered as well as INSERT: the one granted update is `update_note`'s
    # domain carry when a note MOVES, and the trigger is exactly what makes that carry
    # mandatory rather than conventional — the block's new domain has to be the note's.
    op.execute(
        """
        CREATE FUNCTION app.note_clarification_domain_matches() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, pg_temp AS $$
        DECLARE note_domain text;
        BEGIN
            SELECT domain_code INTO note_domain FROM app.notes WHERE id = NEW.note_id;
            IF note_domain IS NULL OR NEW.domain_code IS DISTINCT FROM note_domain THEN
                RAISE EXCEPTION
                    'note_clarification domain % must equal its note''s domain',
                    NEW.domain_code;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER note_clarification_domain_matches
        BEFORE INSERT OR UPDATE ON app.note_clarifications
        FOR EACH ROW EXECUTE FUNCTION app.note_clarification_domain_matches()
        """
    )
    # DELETE is the note-deletion privacy purge (analysis/purge.py). It has to be
    # explicit: the note delete is SOFT, so the ON DELETE CASCADE above never fires on
    # it — the cascade only covers a genuine hard delete of the row.
    op.execute("GRANT SELECT, INSERT, DELETE ON app.note_clarifications TO jbrain_app")
    op.execute("GRANT UPDATE (domain_code) ON app.note_clarifications TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.note_clarifications")
    op.execute("DROP FUNCTION IF EXISTS app.note_clarification_domain_matches()")
