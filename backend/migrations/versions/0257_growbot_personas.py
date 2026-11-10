"""GrowBot personas: souls that live on the box, the phones that hold them, and the body's rider.

docs/plans/GROWBOT_PLAN.md §2c, wave G4a. A persona is a creature's soul (upstream's whole
`pb2_soul` document) with a name; it stands apart from the robot (owner, 2026-11-10: one
body, several personas).

- `growbot_phones` — one row per phone browser that has opened GrowBot. `id` is the random
  phone id the sidecar keeps in that browser's `gb_session`, so a phone stays the same phone
  across tickets. `label` is what the Phones tab calls it.
- `growbot_personas` — the soul and its `version`, bumped by every write, so a phone that
  pushes against an older version is refused instead of overwriting a newer soul.
  `phone_id` is the phone holding it now; the partial unique index is "a phone holds one
  persona" (upstream keeps one soul per browser).
- `growbot_checkpoints` — named, dated copies of a persona's soul; they go with it.
- `growbot_body` — the robot's rider: which persona wears it. The phone that drives it is
  that persona's phone, so a hand-over moves both at once.
- `growbot_tickets` gains `persona_id` (the persona the ticket opens, NULL for G2's tickets,
  which open the creature with whatever soul the phone has).

All five are owner-only under RLS, like 0254's: the sidecar's `/internal/growbot/*` calls run
under the system owner context after `growbot.token` has been checked, and no share, member,
device or capability session can read or write them. Agent tools, RAG and the wiki read none
of them.

Revision ID: 0257
Revises: 0256
Create Date: 2026-11-10
"""

from alembic import op

revision = "0257"
down_revision = "0256"
branch_labels = None
depends_on = None

TABLES = ("growbot_phones", "growbot_personas", "growbot_checkpoints", "growbot_body")


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.growbot_phones (
            id text PRIMARY KEY CHECK (id ~ '^[A-Za-z0-9_-]{16,43}$'),
            label text NOT NULL DEFAULT '' CHECK (char_length(label) <= 40),
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE app.growbot_personas (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 40),
            soul jsonb NOT NULL CHECK (jsonb_typeof(soul) = 'object'),
            version integer NOT NULL DEFAULT 1 CHECK (version > 0),
            made_from text NOT NULL DEFAULT '' CHECK (char_length(made_from) <= 120),
            phone_id text REFERENCES app.growbot_phones (id) ON DELETE SET NULL,
            last_phone_id text REFERENCES app.growbot_phones (id) ON DELETE SET NULL,
            opened_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX growbot_personas_one_per_phone"
        " ON app.growbot_personas (phone_id) WHERE phone_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE TABLE app.growbot_checkpoints (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            persona_id uuid NOT NULL REFERENCES app.growbot_personas (id) ON DELETE CASCADE,
            name text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 80),
            auto boolean NOT NULL DEFAULT false,
            why text NOT NULL DEFAULT '' CHECK (char_length(why) <= 80),
            soul jsonb NOT NULL CHECK (jsonb_typeof(soul) = 'object'),
            created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    op.execute(
        "CREATE INDEX growbot_checkpoints_persona"
        " ON app.growbot_checkpoints (persona_id, created_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE app.growbot_body (
            code text PRIMARY KEY REFERENCES app.growbot_robots (code) ON DELETE CASCADE,
            persona_id uuid REFERENCES app.growbot_personas (id) ON DELETE SET NULL,
            since timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        ALTER TABLE app.growbot_tickets
            ADD COLUMN persona_id uuid REFERENCES app.growbot_personas (id) ON DELETE CASCADE
        """
    )
    for table in TABLES:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_owner ON app.{table}
            FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
            """
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")


def downgrade() -> None:
    op.execute("ALTER TABLE app.growbot_tickets DROP COLUMN persona_id")
    for table in reversed(TABLES):
        op.execute(f"DROP TABLE app.{table}")
