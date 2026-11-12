"""GrowBot persona voices: which Kokoro voice a persona's creature speaks in.

docs/plans/GROWBOT_PLAN.md §2c. Every creature spoke in Kokoro's default voice (the page sent
an empty voice id), whichever persona it was. The owner asked (2026-11-12) for a voice per
persona, picked from a dropdown in JBrain: the phone reads it with the soul and its poll
carries a change, like the face (0259). '' is the box's default voice, the existing personas
included; anything else is a tts-stt voice id, the shape the sidecar's /api/fish-tts accepts.

The column rides `growbot_personas`' owner-only RLS; no new table, so no new isolation test.

Revision ID: 0263
Revises: 0262
Create Date: 2026-11-12
"""

from alembic import op

revision = "0263"
down_revision = "0262"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.growbot_personas ADD COLUMN voice text NOT NULL DEFAULT ''"
        " CHECK (voice = '' OR voice ~ '^kokoro-[a-z0-9_]{1,40}$')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.growbot_personas DROP COLUMN voice")
