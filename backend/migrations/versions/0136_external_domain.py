"""Give the ingested-video corpus its own `external` domain.

The corpus (`external_sources` + `external_source_chunks`) is third-party, attacker-authorable
content the assistant searches but never treats as owner knowledge (#7). It shipped in the
`general` domain with jerv reading it through a purpose-built `general`-scoped context — but that
context grants `general`, the owner's *own* knowledge domain. Give the corpus a dedicated
`external` domain instead: jerv's corpus context is re-scoped to `external` only (jbrain.external
.corpus), so the sandbox can reach the video corpus and NOTHING owner-authored — strictly tighter.

`external` is a corpus-only domain, deliberately NOT an owner-knowledge one: notes, tasks,
extraction, and the wiki never target it (their 4-domain allow-lists correctly exclude it). The
owner sees it for free — `app.has_domain_scope` returns true for any domain on an unrestricted
owner session (migration 0015), so reviewing a removal proposal and reading the corpus just work.
The tables' existing `has_domain_scope(domain_code)` RLS is generic, so no policy change is needed.

Revision ID: 0136
Revises: 0135
Create Date: 2026-07-18
"""

from alembic import op

revision = "0136"
down_revision = "0135"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("INSERT INTO app.domains (code, name) VALUES ('external', 'External')")
    # Flip the default and re-domain every existing corpus row (the corpus is external by nature).
    op.execute("ALTER TABLE app.external_sources ALTER COLUMN domain_code SET DEFAULT 'external'")
    op.execute(
        "ALTER TABLE app.external_source_chunks ALTER COLUMN domain_code SET DEFAULT 'external'"
    )
    op.execute("UPDATE app.external_sources SET domain_code = 'external'")
    op.execute("UPDATE app.external_source_chunks SET domain_code = 'external'")


def downgrade() -> None:
    op.execute("UPDATE app.external_source_chunks SET domain_code = 'general'")
    op.execute("UPDATE app.external_sources SET domain_code = 'general'")
    op.execute(
        "ALTER TABLE app.external_source_chunks ALTER COLUMN domain_code SET DEFAULT 'general'"
    )
    op.execute("ALTER TABLE app.external_sources ALTER COLUMN domain_code SET DEFAULT 'general'")
    op.execute("DELETE FROM app.domains WHERE code = 'external'")
