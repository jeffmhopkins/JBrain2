"""Phase-6 Talk board (Wave T1): per-article discussion topics + an auto Build-log.

Two owner-only tables backing the threaded Talk board (`docs/mocks/wiki-talk-b-topics.html`):
`wiki_talk_topics` (per-article threads; ≤1 `build_log` topic per article) and `wiki_talk_posts`
(append-only signed posts — voices `owner` / `editor` / `builder`). Owner-only RLS mirrors
`wiki_articles` (the cross-domain shell): Talk is editorial metadata *about* the article, not the
underlying domain facts, so it is NOT domain-scoped — the firewall is enforced where it matters (the
T2 Editor's tool reads are domain-scoped, and Build-log summaries are written domain-neutral). Posts
carry no `seq` (append-only, ordered by created_at,id) and no principals FK on author (the builder
writes as the string-id system principal). `run_id` is forward-looking and unused in T1.

Revision ID: 0053
Revises: 0052
Create Date: 2026-06-17
"""

from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.wiki_talk_topics (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            article_id uuid NOT NULL REFERENCES app.wiki_articles(id) ON DELETE CASCADE,
            kind text NOT NULL DEFAULT 'discussion'
                CHECK (kind IN ('discussion', 'build_log')),
            title text NOT NULL,
            status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
            last_post_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # At most one Build-log topic per article (the find-or-create arbiter; the partial predicate is
    # REQUIRED in the builder's ON CONFLICT clause for Postgres to infer this index).
    op.execute(
        "CREATE UNIQUE INDEX wiki_talk_one_build_log ON app.wiki_talk_topics (article_id)"
        " WHERE kind = 'build_log'"
    )
    op.execute(
        "CREATE INDEX wiki_talk_topics_article_idx ON app.wiki_talk_topics"
        " (article_id, last_post_at DESC)"
    )
    op.execute("ALTER TABLE app.wiki_talk_topics ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.wiki_talk_topics FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY wiki_talk_topics_owner ON app.wiki_talk_topics
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.wiki_talk_topics TO jbrain_app")

    op.execute(
        """
        CREATE TABLE app.wiki_talk_posts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            topic_id uuid NOT NULL REFERENCES app.wiki_talk_topics(id) ON DELETE CASCADE,
            author text NOT NULL CHECK (author IN ('owner', 'editor', 'builder')),
            body text NOT NULL,
            source_json jsonb,
            outcome text,
            run_id uuid REFERENCES app.runs(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX wiki_talk_posts_topic_idx ON app.wiki_talk_posts (topic_id, created_at, id)"
    )
    op.execute("ALTER TABLE app.wiki_talk_posts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.wiki_talk_posts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY wiki_talk_posts_owner ON app.wiki_talk_posts
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.wiki_talk_posts TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS app.wiki_talk_posts")
    op.execute("DROP TABLE IF EXISTS app.wiki_talk_topics")
