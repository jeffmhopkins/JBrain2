"""`cast_notice` — notices cast to the house's Chromecast TVs, and how each TV took one.

docs/plans/JPANEL_CAST_PLAN.md (wave K1). One send is one `cast_notice` row: who sent it,
what it said, and the 1920x1080 frame the box drew from that (`frame_sha256`, a blob digest
registered in `jbrain.blob_refs` in the same change). Each TV it was aimed at gets one
`cast_notice_tv` row with its outcome: `shown` over an idle screen, `woke` a TV that was off,
`skipped` with the gate's reason (in use, not reachable), or `failed` with the sidecar's.

The gate runs on the sidecar at the instant of casting, so a TV the PWA listed as ready can
still be `skipped`; that is the record, not an error.

Owner-only on both tables: only the box writes them, as the owner (CLAUDE.md #3).

Revision ID: 0250
Revises: 0249
Create Date: 2026-10-15
"""

from alembic import op

revision = "0250"
down_revision = "0249"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.cast_notice (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            sender text NOT NULL
                CHECK (sender ~ '^(jerv|ops|owner|debug|workflow:[a-z0-9][a-z0-9_.-]{0,47})$'),
            title text NOT NULL CHECK (octet_length(title) BETWEEN 1 AND 120),
            body text NOT NULL DEFAULT '' CHECK (octet_length(body) <= 600),
            frame_sha256 text NOT NULL CHECK (frame_sha256 ~ '^[0-9a-f]{64}$'),
            seconds integer NOT NULL CHECK (seconds BETWEEN 5 AND 300),
            created_at timestamptz NOT NULL DEFAULT now(),
            stopped_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX cast_notice_sent ON app.cast_notice (sender, created_at DESC)")
    op.execute(
        """
        CREATE TABLE app.cast_notice_tv (
            id bigserial PRIMARY KEY,
            notice_id uuid NOT NULL REFERENCES app.cast_notice(id) ON DELETE CASCADE,
            tv_id text NOT NULL,
            tv_name text NOT NULL,
            outcome text NOT NULL CHECK (outcome IN ('shown', 'woke', 'skipped', 'failed')),
            why text NOT NULL DEFAULT '',
            cast_id text,
            at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (notice_id, tv_id)
        )
        """
    )
    for table in ("cast_notice", "cast_notice_tv"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_owner ON app.{table}"
            " USING (app.is_owner()) WITH CHECK (app.is_owner())"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")
    op.execute("GRANT USAGE ON SEQUENCE app.cast_notice_tv_id_seq TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.cast_notice_tv")
    op.execute("DROP TABLE app.cast_notice")
