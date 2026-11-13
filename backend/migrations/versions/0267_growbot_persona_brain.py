"""GrowBot brain tools: which phone, if any, may let a persona's creature use the owner's notes.

docs/plans/GROWBOT_PLAN.md "Brain tools". The owner asked (2026-11-12, 2026-11-13) for a grown-up
persona that can search his notes and file new ones, opened only on his own phone. One column
holds both the switch and the lock: `brain_phone_id` is the phone the owner turned brain access
on for (the one holding the persona at that moment), NULL when it is off. The api grants a
brain call only when the persona is held by that same phone right now, so a link opened on any
other device, or the persona moved to another phone, gets nothing. Only the owner's PATCH writes
it; no route the phone reaches does. A deleted phone row turns it off.

The call log learns a `brain` kind, so every brain search and note is in the owner's log.

The column rides `growbot_personas`' owner-only RLS; no new table, so no new isolation test.

Revision ID: 0267
Revises: 0266
Create Date: 2026-11-13
"""

from alembic import op

revision = "0267"
down_revision = "0266"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.growbot_personas ADD COLUMN brain_phone_id text"
        " REFERENCES app.growbot_phones (id) ON DELETE SET NULL"
    )
    op.execute("ALTER TABLE app.growbot_calls DROP CONSTRAINT growbot_calls_kind_check")
    op.execute(
        "ALTER TABLE app.growbot_calls ADD CONSTRAINT growbot_calls_kind_check"
        " CHECK (kind IN ('llm', 'stt', 'tts', 'brain'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.growbot_calls WHERE kind = 'brain'")
    op.execute("ALTER TABLE app.growbot_calls DROP CONSTRAINT growbot_calls_kind_check")
    op.execute(
        "ALTER TABLE app.growbot_calls ADD CONSTRAINT growbot_calls_kind_check"
        " CHECK (kind IN ('llm', 'stt', 'tts'))"
    )
    op.execute("ALTER TABLE app.growbot_personas DROP COLUMN brain_phone_id")
