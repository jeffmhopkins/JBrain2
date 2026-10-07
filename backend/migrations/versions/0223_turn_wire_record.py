"""`agent_turns.wire` — an assistant turn exactly as the model was sent it.

A chat's next turn must be an exact extension of the last prompt, or a hybrid model (which
reuses its cache only from a checkpoint before the first differing token) re-reads the whole
conversation: measured 2026-10-07, a follow-up re-processed a 117k-token chat. Rebuilt from the
turn's prose and steps the replay diverged — no per-step thinking, rounds merged by text offset,
arguments reordered by JSONB, the turn's own volatile blocks gone. This column keeps what the
prose cannot: each round's text, thinking and model, its calls with the arguments as serialized,
the turn's own user-side messages, and the final round (docs/reference/PROMPT_CACHE.md).

Nullable: a turn stored before this, or one whose rounds went unrecorded, replays from its prose
as before. Rides the table's existing RLS policies and grants.

Revision ID: 0223
Revises: 0222
Create Date: 2026-10-07
"""

from alembic import op

revision = "0223"
down_revision = "0222"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE app.agent_turns ADD COLUMN wire jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE app.agent_turns DROP COLUMN wire")
