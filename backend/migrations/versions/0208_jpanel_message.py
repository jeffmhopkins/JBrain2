"""`jpanel_message` — voice post between the twins' panels and the owner (JPANEL_PLAN.md).

**THE POLICIES ARE THE FEATURE.** These rows are recordings of small children in their
bedrooms, made on a device that lives on a wall and authenticates with a key a four-year-old
could hand to a visitor. The isolation that matters is not owner-versus-panel — it is
**panel-versus-panel**, and there is no route-shaped way to get it right: "the handler filters
by recipient" is a code-review convention, which `0178_settings_deny_jmolt` exists to argue is
not a mechanism. So a panel's reach is bounded in Postgres, under the exact context a flashed
panel runs as, and the integration test asserts the sibling case rather than only the owner
case.

Three separate panel policies, because the three verbs want different bounds:

- **SELECT** — rows a panel SENT or rows addressed TO it. Not "all panel rows": a panel that
  could read its sibling's inbox is a baby monitor pointed the wrong way.
- **INSERT** — only with `sender_device` equal to its own principal. This is the one that stops
  impersonation: without it a panel could post a message that appears to come from its sister,
  which on a device aimed at four-year-olds is worse than eavesdropping.
- **UPDATE** — only rows addressed to it. The route writes `played_at` and nothing else.

**What this deliberately does NOT do.** RLS bounds rows, not columns, and both the owner and
the panels reach the table as `jbrain_app`, so a column grant narrow enough to pin the panel's
UPDATE to `played_at` would pin the owner's too. The residual is that a panel could rewrite the
transcript of a message *already addressed to it* — it cannot forge a sender, cannot reach its
sibling's rows, and cannot touch the audio, which is content-addressed in blob storage. That is
a contained and low-value corruption of a row the panel already possesses, and the alternative
(a trigger comparing OLD and NEW) buys little for its weight. Stated here rather than left for
someone to discover.

**`played_at IS NULL` is the entire inbox query**, which is why it is a nullable timestamp
rather than a boolean: it answers "is there anything waiting" and "when did they hear it" with
one column, and the second question is the one a parent asks.

**The audio is a `blob_sha256`, never a path** (`CLAUDE.md` #2). Content-addressed, so the same
clip stored twice costs once, and a row can never name a file outside the store.

Revision ID: 0208
Revises: 0207
Create Date: 2026-09-22
"""

from alembic import op

revision = "0208"
down_revision = "0207"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.jpanel_message (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            sender_kind text NOT NULL,
            sender_device text,
            recipient_kind text NOT NULL,
            recipient_device text,
            blob_sha256 text NOT NULL,
            transcript text NOT NULL DEFAULT '',
            composed text NOT NULL,
            duration_ms integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            played_at timestamptz,
            CONSTRAINT jpanel_message_sender_kind
                CHECK (sender_kind IN ('panel', 'owner')),
            CONSTRAINT jpanel_message_recipient_kind
                CHECK (recipient_kind IN ('panel', 'owner')),
            CONSTRAINT jpanel_message_composed
                CHECK (composed IN ('voice', 'text')),
            -- A device id is present exactly when the party is a panel. Without this a row
            -- could claim to be from a panel and name no panel, which the SELECT policy would
            -- then match against nobody and quietly hide from everyone.
            CONSTRAINT jpanel_message_sender_device
                CHECK ((sender_kind = 'panel') = (sender_device IS NOT NULL)),
            CONSTRAINT jpanel_message_recipient_device
                CHECK ((recipient_kind = 'panel') = (recipient_device IS NOT NULL))
        )
        """
    )
    # The inbox poll runs every ~30 s per panel forever, so it gets the index rather than the
    # owner's list, which is read by one person occasionally.
    op.execute(
        """
        CREATE INDEX jpanel_message_waiting
        ON app.jpanel_message (recipient_device, created_at)
        WHERE played_at IS NULL
        """
    )

    op.execute("ALTER TABLE app.jpanel_message ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.jpanel_message FORCE ROW LEVEL SECURITY")

    op.execute(
        """
        CREATE POLICY jpanel_message_owner ON app.jpanel_message
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    # Sent BY me or addressed TO me. `app.principal_id` is the panel's own device id, set by
    # `device_context` on every scoped session.
    op.execute(
        """
        CREATE POLICY jpanel_message_panel_read ON app.jpanel_message
        FOR SELECT
        USING (
            current_setting('app.principal_kind', true) = 'device_key'
            AND (
                sender_device = current_setting('app.principal_id', true)
                OR recipient_device = current_setting('app.principal_id', true)
            )
        )
        """
    )
    # THE ANTI-IMPERSONATION ONE. A panel may only author rows that say they came from it.
    op.execute(
        """
        CREATE POLICY jpanel_message_panel_send ON app.jpanel_message
        FOR INSERT
        WITH CHECK (
            current_setting('app.principal_kind', true) = 'device_key'
            AND sender_kind = 'panel'
            AND sender_device = current_setting('app.principal_id', true)
        )
        """
    )
    # Marking a message played. Bounded to rows addressed to this panel on both sides, so it
    # cannot move a row out of its own inbox either.
    op.execute(
        """
        CREATE POLICY jpanel_message_panel_played ON app.jpanel_message
        FOR UPDATE
        USING (
            current_setting('app.principal_kind', true) = 'device_key'
            AND recipient_device = current_setting('app.principal_id', true)
        )
        WITH CHECK (
            current_setting('app.principal_kind', true) = 'device_key'
            AND recipient_device = current_setting('app.principal_id', true)
        )
        """
    )
    # RLS decides which rows; the grant decides whether the app role may reach the table at
    # all. Without it every query is `permission denied` before a policy is consulted.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.jpanel_message TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.jpanel_message")
