"""How far into a model load the box actually is.

`app.box_events` (0167) says WHAT the box is doing and since when, and both surfaces that
read it — the vitals list and the chat status line — render an elapsed count from `at`.
Elapsed answers "how long has this been going". On a load that reads tens of GB it is the
other question that is being asked: how much longer.

The fraction has to live on the row rather than beside it because the reader and the
writer are usually different processes — the worker loads a model, the api serves the
page — which is the same argument that made 0167 a table instead of a ring. It is also
what keeps the percent and the model name from ever disagreeing: they are one row.

NULL, never 0, when there is no reading: no device probe on the box, or a load whose first
sample has not landed yet. A bar that renders 0% claims "started and stuck", which is the
one thing the surface must not say while weights are streaming in.

Revision ID: 0169
Revises: 0168
Create Date: 2026-08-20
"""

from alembic import op

revision = "0169"
down_revision = "0168"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No CHECK on the 0..1 range: this is narration, and a write that fails a constraint
    # would raise inside the load it is describing. The writer clamps instead.
    op.execute("ALTER TABLE app.box_events ADD COLUMN progress double precision")


def downgrade() -> None:
    op.execute("ALTER TABLE app.box_events DROP COLUMN progress")
