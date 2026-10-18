"""JShare: files the owner publishes, each at its own public link.

docs/runbooks/JSHARE.md. One row per published item — a picture, a sound, a video, an HTML
page, a zipped self-contained website or any other file — with its bytes in the
content-addressed blob store (`blob_sha256`) and its public address `slug`, the unguessable
id in `/s/<slug>/`.

Unlike the other share links the slug is stored as itself rather than as a hash: the owner
re-copies an item's link from the PWA whenever they like, so it must be recoverable, and it
grants nothing but this row's own bytes, which the owner chose to publish.

The capability is enforced in RLS. A visitor runs under `jshare_context(slug)`
(db/session.py): a non-owner kind with no scopes, so every owner-only table denies it, with
`auth_context = 'jshare'` and the slug as `principal_id`. `jshare_items_public_read` admits
exactly the row whose slug that is, compared as text so an empty or junk pin matches nothing.
The visitor has no write policy anywhere; deleting the row is what unpublishes it.

Revision ID: 0252
Revises: 0251
Create Date: 2026-10-18
"""

from alembic import op

revision = "0252"
down_revision = "0251"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jshare_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            -- 128 random bits, base32 lowercase: one DNS label, so the same id could later
            -- name its own subdomain.
            slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z2-7]{26}$'),
            title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
            kind text NOT NULL
                CHECK (kind IN ('image', 'audio', 'video', 'html', 'site', 'file')),
            filename text NOT NULL,
            media_type text NOT NULL,
            size_bytes bigint NOT NULL,
            blob_sha256 text NOT NULL,
            -- A site's folder inside its zip that holds index.html ('' for the zip's root).
            site_root text NOT NULL DEFAULT '',
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            last_viewed_at timestamptz,
            view_count integer NOT NULL DEFAULT 0
        )
        """
    )
    op.execute("ALTER TABLE app.jshare_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jshare_items FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY jshare_items_owner ON app.jshare_items
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute(
        """
        CREATE POLICY jshare_items_public_read ON app.jshare_items FOR SELECT
        USING (
            app.auth_ctx() = 'jshare'
            AND slug = current_setting('app.principal_id', true)
        )
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.jshare_items TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.jshare_items")
