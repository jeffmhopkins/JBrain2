"""`untrusted_origin` note provenance + the intake Proposal kinds (W4).

A captured intake submission is stranger-authored content. On owner approval its
per-claim leaves become notes provenance-tagged **`untrusted_origin`** (net-new, §5)
— so the integration backfill can drain trusted owner notes ahead of them (the
Phase-7 `(1=0)` seam in queue.py is swapped to read this provenance), and the
content's origin is auditable. Two Proposal kinds are admitted: `intake-link` (the
mint-time editable Proposal) and `intake-submission` (the owner-side materialization
of a captured submission into a summary + per-claim leaves).
"""

from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None

_PROV_OLD = "('human', 'agent', 'owner_correction')"
_PROV_NEW = "('human', 'agent', 'owner_correction', 'untrusted_origin')"

_KIND_OLD = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'predicate-canon', 'egress')"
)
_KIND_NEW = (
    "('correction', 'knowledge', 'appointment', 'wiki-restructure',"
    " 'prompt-edit', 'skill-promotion', 'predicate-canon', 'egress',"
    " 'intake-link', 'intake-submission')"
)


def _set_provenance(values: str) -> None:
    op.execute("ALTER TABLE app.notes DROP CONSTRAINT notes_provenance_check")
    op.execute(
        "ALTER TABLE app.notes ADD CONSTRAINT notes_provenance_check"
        f" CHECK (provenance IN {values})"
    )


def _set_kind(values: str) -> None:
    op.execute("ALTER TABLE app.proposals DROP CONSTRAINT proposals_kind_check")
    op.execute(
        f"ALTER TABLE app.proposals ADD CONSTRAINT proposals_kind_check CHECK (kind IN {values})"
    )


def upgrade() -> None:
    _set_provenance(_PROV_NEW)
    _set_kind(_KIND_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM app.notes WHERE provenance = 'untrusted_origin'")
    _set_provenance(_PROV_OLD)
    op.execute("DELETE FROM app.proposals WHERE kind IN ('intake-link', 'intake-submission')")
    _set_kind(_KIND_OLD)
