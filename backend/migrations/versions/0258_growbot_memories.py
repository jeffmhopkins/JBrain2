"""GrowBot visual memories on the box: the saved views a persona's creature keeps of the people
and things it has met.

docs/plans/GROWBOT_PLAN.md §2c, wave G4d. Upstream keeps them in the phone's IndexedDB
(`v3/visual-memory.js`); here each one is a row, so it moves with the persona from phone to
phone, and its pictures are blobs in their own namespace of the storage abstraction (rule 2),
never the attachment store, so nothing else on the box can address a face.

- `growbot_memories` — one saved view per row, keyed by the creature's own id (`r…`).
  `meta` is the record exactly as the creature wrote it (kind, name, desc, when, who
  confirmed it), because the phone syncs by comparing it field for field; `views` is
  `[{sha, t}]`, the pictures' digests in the namespace and when each was taken.
- `growbot_personas.memories_version` — bumped by every change to its memories, so the phone
  holding it notices one made from the PWA (a delete, a restore) within a poll.
- `growbot_checkpoints.memories` — the memories as they were, the same `[{id, meta, views}]`
  shape. NULL for a checkpoint made before this migration: it never captured them, so
  restoring it keeps the memories the persona has instead of clearing them. A checkpoint
  shares its pictures' digests with the persona instead of copying them, so a blob goes only
  once no row of either table names it.

Owner-only under RLS like 0257's: the sidecar's calls run under the system owner context after
`growbot.token` is checked; no share, member, device or capability session can read them, and
no agent tool, RAG or wiki ingest does.

Revision ID: 0258
Revises: 0257
Create Date: 2026-11-10
"""

from alembic import op

revision = "0258"
down_revision = "0257"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.growbot_memories (
            persona_id uuid NOT NULL REFERENCES app.growbot_personas (id) ON DELETE CASCADE,
            id text NOT NULL CHECK (id ~ '^r[0-9a-f]{1,32}$'),
            meta jsonb NOT NULL CHECK (jsonb_typeof(meta) = 'object'),
            views jsonb NOT NULL CHECK (
                jsonb_typeof(views) = 'array' AND jsonb_array_length(views) BETWEEN 1 AND 3
            ),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (persona_id, id)
        )
        """
    )
    op.execute(
        "ALTER TABLE app.growbot_personas ADD COLUMN memories_version integer NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE app.growbot_checkpoints ADD COLUMN memories jsonb"
        " CHECK (memories IS NULL OR jsonb_typeof(memories) = 'array')"
    )
    op.execute("ALTER TABLE app.growbot_memories ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.growbot_memories FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY growbot_memories_owner ON app.growbot_memories
        FOR ALL USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.growbot_memories TO jbrain_app")


def downgrade() -> None:
    op.execute("ALTER TABLE app.growbot_checkpoints DROP COLUMN memories")
    op.execute("ALTER TABLE app.growbot_personas DROP COLUMN memories_version")
    op.execute("DROP TABLE app.growbot_memories")
