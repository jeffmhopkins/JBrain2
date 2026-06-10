"""Attachment removal needs DELETE on app.attachments.

0002 granted the app role SELECT/INSERT/UPDATE only — notes delete softly
via UPDATE, but removing an attachment is a real row delete (the
content-addressed blob stays; only the link goes). RLS still scopes which
rows are deletable.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-10
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT DELETE ON app.attachments TO jbrain_app")


def downgrade() -> None:
    op.execute("REVOKE DELETE ON app.attachments FROM jbrain_app")
