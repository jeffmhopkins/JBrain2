"""Record WHAT a run was actually asked to do, on the run row itself.

The vitals detail surface (docs/mocks/vitals-detail/i-drilldown.html) lists the turns
running right now and, for each, answers "what triggered this and how was it set up".
Everything about the *shape* of a run is already on app.runs — kind, trigger, session,
steps, tokens, domain, ran_as, prompt_version — but nothing recorded the CALL: which
model answered, at what reasoning effort, against what context window, with which
tools, under which persona, and from which message. That is resolved at turn start and
was then thrown away, so the surface would have had to guess.

One JSONB column rather than seven typed ones: the stamp is display-only provenance
that differs by run kind (a workflow run has no user message; a sub-agent has a parent's
instead), it is never queried by field, and pinning each key as a column would mean a
migration every time a routing knob is added. NULL for every run that predates this and
for any run whose driver doesn't stamp — the surface renders what is there.

No RLS change: app.runs already carries its policy and this only widens an existing row.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0166"
down_revision = "0165"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("call_stamp", postgresql.JSONB(), nullable=True),
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("runs", "call_stamp", schema="app")
