"""Give the jmolt persona its own `jmolt` firewall domain (docs/plans/JMOLT_PLAN.md).

jmolt's data — its scratchpad, its outbox, its action ledger (landing in W2/W3) — is a
third-party-adjacent corpus that jerv reads but never owns: one hop from attacker-authorable
Moltbook text. Give it a dedicated `jmolt` domain, like the `external` corpus domain (0136):
jmolt's nightly session runs `domain_scopes=('jmolt',)` under `auth_context='jmolt'`, and each
jmolt-owned table (added in later waves) splits its RLS so a jerv-scoped session may SELECT but
only jmolt's own launcher-set auth context may write (the M19 matrix). This migration lands only
the domain row; the tables and their policies arrive with the features that need them.

`jmolt` is a corpus-only domain, deliberately NOT an owner-knowledge one: notes, tasks,
extraction, and the wiki never target it (their allow-lists exclude it). The owner sees it for
free — `app.has_domain_scope` returns true for an unrestricted owner session — so owner review
and observation just work.
"""

from alembic import op

revision = "0172"
down_revision = "0171"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("INSERT INTO app.domains (code, name) VALUES ('jmolt', 'Jmolt')")


def downgrade() -> None:
    op.execute("DELETE FROM app.domains WHERE code = 'jmolt'")
