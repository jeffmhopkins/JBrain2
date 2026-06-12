"""Lists — `lists` / `list_items`, the agent-managed structured records
(docs/ARCHITECTURE.md "Lists", docs/ROADMAP.md Phase 4).

A list is user-managed data the owner asks the agent to maintain — a shopping
list, a packing list, a watchlist. Unlike facts/entities (the citable graph that
goes through extraction + review), lists are corrected directly: the agent's
tools write them under the RLS-scoped session, the firewall is Postgres, and
there is no Proposal round-trip on a checkbox tap (the memory-scratchpad
category, not the citable-truth category — invariant #7 carves them out).

Both tables are owner-only and domain-firewalled: a list targets a single
in-scope domain (you cannot make a list in a domain the session cannot read), and
a non-owner principal sees none (#8). Items inherit their list's visibility, and
both get true DELETE (a projection of the owner's intent, not citable history).

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-12
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.lists (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            domain_code text NOT NULL REFERENCES app.domains(code),
            title text NOT NULL,
            archived_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX lists_open_idx ON app.lists (domain_code, updated_at DESC)"
        " WHERE archived_at IS NULL"
    )
    op.execute("ALTER TABLE app.lists ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.lists FORCE ROW LEVEL SECURITY")
    # Owner-only AND domain-narrowed: a list lives in exactly one in-scope domain,
    # and a non-owner principal sees/creates none (#7/#8).
    op.execute(
        """
        CREATE POLICY lists_owner ON app.lists
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.lists TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.list_items (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            list_id uuid NOT NULL REFERENCES app.lists(id) ON DELETE CASCADE,
            body text NOT NULL,
            checked_at timestamptz,
            position int NOT NULL DEFAULT 0,
            source_note_id uuid REFERENCES app.notes(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX list_items_list_idx ON app.list_items (list_id, position)")
    op.execute("ALTER TABLE app.list_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.list_items FORCE ROW LEVEL SECURITY")
    # An item is visible exactly when its parent list is — the lists RLS filters
    # this subquery, so items carry no firewall column of their own.
    op.execute(
        """
        CREATE POLICY list_items_via_list ON app.list_items
        USING (EXISTS (SELECT 1 FROM app.lists l WHERE l.id = list_id))
        WITH CHECK (EXISTS (SELECT 1 FROM app.lists l WHERE l.id = list_id))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.list_items TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.list_items")
    op.execute("DROP TABLE app.lists")
