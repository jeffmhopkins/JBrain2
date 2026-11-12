"""GrowBot call log: every model turn, transcription and voice clip the creature makes.

docs/plans/GROWBOT_PLAN.md §2 "Call log". The owner asked (2026-11-12) for every prompt and
reply kept so the creature's slowness and its answers can be analysed from the debug API,
which reverses the earlier "passes through and is not kept" rule for this one table.

- `kind` 'llm': the api's model lane writes it (the request as sent, its pictures replaced by
  their size, and the reply it streamed back); 'stt' and 'tts': the sidecar posts the
  transcript and the spoken line after each call.
- `timings` holds the per-stage milliseconds (lock wait, admission, first byte, first word,
  total, the engine's own prefill and decode), so a turn's latency splits by stage.

Kept a week, pruned by the writer (`jbrain.growbot.calls`). Owner-only RLS like 0257's: the
sidecar's writes run under the system owner context after `growbot.token` is checked, and no
share, member, device or capability session, agent tool, RAG or wiki ingest reads it.

Revision ID: 0260
Revises: 0259
Create Date: 2026-11-12
"""

from alembic import op

revision = "0260"
down_revision = "0259"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.growbot_calls (
            id bigserial PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now(),
            kind text NOT NULL CHECK (kind IN ('llm', 'stt', 'tts')),
            persona_id uuid,
            robot text NOT NULL DEFAULT '',
            model text NOT NULL DEFAULT '',
            ok boolean NOT NULL,
            error text NOT NULL DEFAULT '',
            request jsonb NOT NULL DEFAULT '{}'::jsonb,
            reply text NOT NULL DEFAULT '',
            reasoning text NOT NULL DEFAULT '',
            images integer NOT NULL DEFAULT 0,
            image_bytes integer NOT NULL DEFAULT 0,
            prompt_tokens integer,
            cached_tokens integer,
            completion_tokens integer,
            total_ms integer NOT NULL,
            timings jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute("CREATE INDEX growbot_calls_created_idx ON app.growbot_calls (created_at)")
    op.execute("ALTER TABLE app.growbot_calls ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.growbot_calls FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY growbot_calls_owner ON app.growbot_calls
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, DELETE ON app.growbot_calls TO jbrain_app")
    op.execute("GRANT USAGE ON SEQUENCE app.growbot_calls_id_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.growbot_calls")
