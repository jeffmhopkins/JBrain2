"""Index app.facts (object_entity_id) for inbound ref-edge expansion (W3).

The neighborhood/ego traversals walk relationship edges both ways; the inbound
arm filters `WHERE f.object_entity_id IN (...)` and today seq-scans facts — a
3-hop frontier multiplies that scan per hop. Partial (`IS NOT NULL`) because
object_entity_id is set only on ref-shaped relationship edges and stays NULL on
the attribute/measurement/event/state/preference majority, and every consumer
queries it with non-null equality — the planner still uses the partial index
for `= x` / `IN (...)`, and the null majority never bloats it.

Pure index on an existing table — no RLS change, no new isolation test.
"""

from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX facts_object_entity_idx ON app.facts (object_entity_id)"
        " WHERE object_entity_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.facts_object_entity_idx")
