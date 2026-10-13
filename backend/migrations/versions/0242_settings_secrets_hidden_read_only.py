"""Hide credential rows of app.settings from read-only transactions (SECURITY S3.3, F12).

The debug console runs owner-level SQL inside `SET TRANSACTION READ ONLY`, and its old
redaction worked by column NAME: `SELECT value AS v FROM app.settings WHERE key =
'tavily_api_key'`, or `substr(value::text, 1, 8)`, walked past it. Redaction by relation
has to happen where the relation is resolved, so this is a RESTRICTIVE policy: in a
read-only transaction, the rows whose key is a credential do not exist — through any alias,
expression, subquery, join or SPI-executing function, because RLS applies to all of them.

Why `transaction_read_only` rather than a GUC the route stamps: a GUC is one `set_config`
away from off, and a text blocklist on the statement is the only thing between the console
and set_config. Read-only, by contrast, is a one-way door — Postgres refuses to make a
transaction read-write once it has run a query — so the statement can't open it. Nothing in
the app reads a credential inside a read-only transaction (the debug routes are the only
ones that open one, and none of them reads these keys); one that ever needs to must open an
ordinary transaction.

The trade-off: the console can't see these rows at all, not even that they're set. The PWA
Settings panels already say whether each key is set, so nothing is lost there.

The key list is frozen here because a migration can't follow live code; it mirrors
`settings_store.SECRET_SETTING_KEYS` and `tests/integration/test_debug_sql_secrets_pg.py`
walks that set against this policy, so a secret added there and not here fails CI.

Revision ID: 0242
Revises: 0241
Create Date: 2026-10-12
"""

from alembic import op

revision = "0242"
down_revision = "0241"
branch_labels = None
depends_on = None

SECRET_KEYS = (
    "appointments_feed_token",
    "gmail_client_secret",
    "gmail_refresh_token",
    "tavily_api_key",
    "brave_api_key",
    "moltbook_api_key",
    "endpoint_wifi",
    "gmail_oauth_state",
)


def upgrade() -> None:
    keys = ", ".join(f"'{k}'" for k in SECRET_KEYS)
    # FOR SELECT: writes can't happen in a read-only transaction anyway, and ordinary
    # transactions never match the first arm, so the app's own reads and writes are untouched.
    op.execute(
        f"""
        CREATE POLICY settings_secrets_not_read_only ON app.settings
        AS RESTRICTIVE FOR SELECT
        USING (current_setting('transaction_read_only') <> 'on' OR key NOT IN ({keys}))
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY settings_secrets_not_read_only ON app.settings")
