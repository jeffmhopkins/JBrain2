"""Allow analysis to derive a per-domain copy of a chunk for fact citations.

A note is captured in ONE domain, so all its chunks carry that domain. When a
fact ratchets UP (a health reading written in a `general` note), its citation
would point at a chunk in the capture domain that the fact's own RLS scope
cannot see — a citation crossing the firewall (docs/reference/ANALYSIS.md "Mixed-domain
notes"). Analysis now derives a `source_kind = 'derived'` copy of the cited
chunk in the fact's domain and the fact cites that instead, so a citation never
leaves the fact's scope. The original note stays the source of truth in its
capture domain; the derived chunk only references the same span.

Derived chunks are citation backing, not primary sources: they carry no
embedding (so dense search skips them already) and search excludes them by
source_kind so the same text is not surfaced twice. They ride the existing
app.chunks RLS policy; the analysis RLS isolation test proves a derived health
chunk is invisible to a general scope and visible to a health one.

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-11
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

_KINDS_WITH = "('note', 'text-layer', 'ocr', 'transcript', 'caption', 'derived')"
_KINDS_WITHOUT = "('note', 'text-layer', 'ocr', 'transcript', 'caption')"


def upgrade() -> None:
    op.execute("ALTER TABLE app.chunks DROP CONSTRAINT chunks_source_kind_check")
    op.execute(
        "ALTER TABLE app.chunks ADD CONSTRAINT chunks_source_kind_check"
        f" CHECK (source_kind IN {_KINDS_WITH})"
    )


def downgrade() -> None:
    # Derived rows are not representable without the kind: drop them first.
    op.execute("DELETE FROM app.chunks WHERE source_kind = 'derived'")
    op.execute("ALTER TABLE app.chunks DROP CONSTRAINT chunks_source_kind_check")
    op.execute(
        "ALTER TABLE app.chunks ADD CONSTRAINT chunks_source_kind_check"
        f" CHECK (source_kind IN {_KINDS_WITHOUT})"
    )
