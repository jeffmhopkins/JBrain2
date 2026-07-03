"""The Proposal primitive: staged, owner-approved work as a dependency-ordered
tree (docs/reference/ASSISTANT.md "Staging & approval", invariant #7).

The agent never writes citable truth or behaviour directly — it stages a
*Proposal* and the owner enacts it. A proposal is a tree: a root intent, grouping
nodes, and atomic leaf ops, each with its own rendered `preview` (what the owner
judges) and per-node `status`. Leaves declare `deps` (prerequisite node ids) so
partial approval stays dependency-safe and fail-closed — a leaf enacts only when
every prerequisite it depends on is also approved.

Both tables are owner-only and domain-firewalled: a proposal targets a single
in-scope domain (you cannot stage a write to a domain the session cannot read),
and a non-owner principal can stage nothing (#8). Nodes inherit their proposal's
visibility. `notes` also gains `provenance`/`source_ref` so an agent-authored note
(the sanctioned promotion path, #7) is flagged and source-attributed while
re-entering at NORMAL extraction weight.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-12
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.proposals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid REFERENCES app.agent_sessions(id) ON DELETE SET NULL,
            principal_id uuid NOT NULL REFERENCES app.principals(id),
            kind text NOT NULL CHECK (kind IN (
                'correction', 'knowledge', 'wiki-restructure',
                'prompt-edit', 'skill-promotion', 'egress'
            )),
            status text NOT NULL DEFAULT 'staged'
                CHECK (status IN ('staged', 'approved', 'enacted', 'rejected', 'expired')),
            title text NOT NULL DEFAULT '',
            provenance jsonb NOT NULL DEFAULT '{}',
            domain_code text NOT NULL REFERENCES app.domains(code),
            subject_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX proposals_open_idx ON app.proposals (status, created_at DESC)")
    op.execute("ALTER TABLE app.proposals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.proposals FORCE ROW LEVEL SECURITY")
    # Owner-only AND domain-narrowed: a proposal can target only an in-scope
    # domain, and a non-owner principal stages none (#7/#8).
    op.execute(
        """
        CREATE POLICY proposals_owner ON app.proposals
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.proposals TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.proposal_nodes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            proposal_id uuid NOT NULL REFERENCES app.proposals(id) ON DELETE CASCADE,
            parent_id uuid REFERENCES app.proposal_nodes(id) ON DELETE CASCADE,
            type text NOT NULL CHECK (type IN ('group', 'leaf')),
            op text NOT NULL DEFAULT '',
            label text NOT NULL DEFAULT '',
            preview jsonb NOT NULL DEFAULT '{}',
            deps uuid[] NOT NULL DEFAULT '{}',
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected', 'enacted', 'held')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX proposal_nodes_proposal_idx ON app.proposal_nodes (proposal_id)")
    op.execute("ALTER TABLE app.proposal_nodes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.proposal_nodes FORCE ROW LEVEL SECURITY")
    # A node is visible exactly when its parent proposal is — the proposals RLS
    # filters this subquery, so nodes carry no firewall column of their own.
    op.execute(
        """
        CREATE POLICY proposal_nodes_via_proposal ON app.proposal_nodes
        USING (EXISTS (SELECT 1 FROM app.proposals p WHERE p.id = proposal_id))
        WITH CHECK (EXISTS (SELECT 1 FROM app.proposals p WHERE p.id = proposal_id))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.proposal_nodes TO jbrain_app")

    # Agent-authored notes are provenance-flagged and source-attributed; they
    # re-enter at NORMAL extraction weight (#7 — elevated weight is reserved for
    # owner-authored corrections). Existing notes are human-authored.
    op.execute(
        "ALTER TABLE app.notes ADD COLUMN provenance text NOT NULL DEFAULT 'human'"
        " CHECK (provenance IN ('human', 'agent'))"
    )
    op.execute("ALTER TABLE app.notes ADD COLUMN source_ref text")


def downgrade() -> None:
    op.execute("ALTER TABLE app.notes DROP COLUMN source_ref")
    op.execute("ALTER TABLE app.notes DROP COLUMN provenance")
    op.execute("DROP TABLE app.proposal_nodes")
    op.execute("DROP TABLE app.proposals")
