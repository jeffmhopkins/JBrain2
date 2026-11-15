"""GrowBot body tests: the guided real-robot trial's runs and their logs, kept for review.

The owner asked (2026-11-15) for a Body test button that walks him through a scripted trial on
the robot and logs everything "so you can review after". The creature's phone runs it
(growbot/patches/growbot-bodytest.js) and posts its log every couple of seconds: samples (gyro,
accelerometer, tilt, the walk's heading and command, leg poses, the face it sees), events
(steps, asks, commands, skill swaps, falls, "stop stop") and a small camera still per step.

`growbot_bodytests` is one row per run: its number (`id`, what the owner calls "run 7"), the
phone's own run id, the robot, its status, and `summary`, the run's latest state, which the
Body tab mirrors while it runs and which holds the verdicts at the end. `growbot_bodytest_chunks`
is the log, one row per post, kept once by `(run_id, seq)`. Only each robot's newest 50 runs are
kept, pruned by the writer (`jbrain.growbot.bodytests`). Owner-only RLS like 0262's.

Revision ID: 0274
Revises: 0273
Create Date: 2026-11-15
"""

from alembic import op

revision = "0274"
down_revision = "0273"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.growbot_bodytests (
            id bigserial PRIMARY KEY,
            run text NOT NULL UNIQUE,
            robot text NOT NULL,
            persona_id uuid,
            started_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            status text NOT NULL DEFAULT 'running',
            skills jsonb NOT NULL DEFAULT '{}'::jsonb,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            row_count integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute("CREATE INDEX growbot_bodytests_robot_idx ON app.growbot_bodytests (robot, id DESC)")
    op.execute(
        """
        CREATE TABLE app.growbot_bodytest_chunks (
            run_id bigint NOT NULL REFERENCES app.growbot_bodytests (id) ON DELETE CASCADE,
            seq integer NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            body jsonb NOT NULL,
            PRIMARY KEY (run_id, seq)
        )
        """
    )
    for table in ("growbot_bodytests", "growbot_bodytest_chunks"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_owner ON app.{table}"
            " FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())"
        )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.growbot_bodytests TO jbrain_app")
    # Chunks go only with their run (ON DELETE CASCADE).
    op.execute("GRANT SELECT, INSERT ON app.growbot_bodytest_chunks TO jbrain_app")
    op.execute("GRANT USAGE ON SEQUENCE app.growbot_bodytests_id_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.growbot_bodytest_chunks")
    op.execute("DROP TABLE app.growbot_bodytests")
