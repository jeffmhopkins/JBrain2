"""Per-session interview state for the guided-intake chat path (W3).

The intake chat runs the agent loop over many turns under the per-session non-owner
principal. The running conversation, plus the per-session cumulative caps the plan
makes a hard backstop (§5: turn + cost ceilings, since the loop's guardrails are
per-TURN only), live on `intake_sessions`:

  * `transcript`      — the full running conversation (copied onto the submission at
                        capture); also what the owner browses for an in-progress or
                        abandoned session (#15).
  * `turns_used`      — cumulative turns this session (a stranger can drive many turns
                        within per-turn caps; this is the per-session backstop).
  * `cost_tokens_used`— cumulative model cost this session (the cost ceiling).
  * `last_turn_at`    — drives the reaper that transitions stale `drafting` sessions to
                        `abandoned` (an abandoned open holds its opens_used slot, §6).

Purely additive columns; the RLS policies (0108) are unchanged — these are read/written
under the same per-session principal pin.
"""

from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app.intake_sessions"
        " ADD COLUMN transcript jsonb NOT NULL DEFAULT '[]',"
        " ADD COLUMN turns_used int NOT NULL DEFAULT 0 CHECK (turns_used >= 0),"
        " ADD COLUMN cost_tokens_used bigint NOT NULL DEFAULT 0 CHECK (cost_tokens_used >= 0),"
        " ADD COLUMN last_turn_at timestamptz"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app.intake_sessions"
        " DROP COLUMN last_turn_at,"
        " DROP COLUMN cost_tokens_used,"
        " DROP COLUMN turns_used,"
        " DROP COLUMN transcript"
    )
