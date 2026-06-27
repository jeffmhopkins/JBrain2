"""External LLM sessions: a token-gated public proxy to the on-box coder.

An external session lets the owner expose the loaded coder model over an
Anthropic-compatible endpoint to a remote Claude (it sets ANTHROPIC_BASE_URL +
ANTHROPIC_AUTH_TOKEN to the minted URL + secret). It reuses the capability-token
machinery (a `principals` row with `key_hash` + `revoked_at`, and `suspended_at`
as the on/off toggle), adding three cumulative usage counters so the owner sees
token consumption. Purely additive (three NOT NULL DEFAULT 0 columns + a widened
kind CHECK); no RLS policy change — principals visibility is already owner-or-self.
"""

from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None

_KINDS = "'owner', 'capability_token', 'device_key', 'jcode_share_link'"


def upgrade() -> None:
    op.execute("ALTER TABLE app.principals ADD COLUMN ext_in_tokens bigint NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE app.principals ADD COLUMN ext_out_tokens bigint NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE app.principals ADD COLUMN ext_requests bigint NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE app.principals DROP CONSTRAINT principals_kind_check")
    op.execute(
        "ALTER TABLE app.principals ADD CONSTRAINT principals_kind_check "
        f"CHECK (kind IN ({_KINDS}, 'external_llm'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM app.principals WHERE kind = 'external_llm'")
    op.execute("ALTER TABLE app.principals DROP CONSTRAINT principals_kind_check")
    op.execute(
        "ALTER TABLE app.principals ADD CONSTRAINT principals_kind_check "
        f"CHECK (kind IN ({_KINDS}))"
    )
    op.execute("ALTER TABLE app.principals DROP COLUMN ext_requests")
    op.execute("ALTER TABLE app.principals DROP COLUMN ext_out_tokens")
    op.execute("ALTER TABLE app.principals DROP COLUMN ext_in_tokens")
