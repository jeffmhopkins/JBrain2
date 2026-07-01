"""The non-owner `intake_link` principal kind (guided-intake share links, W1).

A guided-intake session runs the agent loop under a NON-owner principal: a
stranger holding a share link drives a scoped interviewer that must read nothing
of the owner's brain. That principal is a new `principals.kind = 'intake_link'`,
deliberately distinct from `owner` so it fails BOTH `app.is_owner()` and
`app.is_full_owner()` — the owner-bypass trap (GUIDED_INTAKE_PLAN.md §5): were it
stored as `kind='owner'` with an "empty scope", every `USING(app.is_owner())`
table would leak the whole brain. One per intake SESSION (minted at redeem, not at
mint), so its `principal_id` is the per-session isolation pin on the intake tables.

Purely additive — a widened `principals_kind_check` (mirrors 0100/0104); no new
column, no RLS policy change here (principals visibility is already owner-or-self).
The intake tables themselves arrive in 0108.
"""

from alembic import op

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None

_KINDS = "'owner', 'capability_token', 'device_key', 'jcode_share_link', 'external_llm'"


def upgrade() -> None:
    op.execute("ALTER TABLE app.principals DROP CONSTRAINT principals_kind_check")
    op.execute(
        "ALTER TABLE app.principals ADD CONSTRAINT principals_kind_check "
        f"CHECK (kind IN ({_KINDS}, 'intake_link'))"
    )


def downgrade() -> None:
    # Drop any intake-link principals first, or the narrowed CHECK would reject them.
    op.execute("DELETE FROM app.principals WHERE kind = 'intake_link'")
    op.execute("ALTER TABLE app.principals DROP CONSTRAINT principals_kind_check")
    op.execute(
        "ALTER TABLE app.principals ADD CONSTRAINT principals_kind_check "
        f"CHECK (kind IN ({_KINDS}))"
    )
