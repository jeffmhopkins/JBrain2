"""Phase 6 wiki — the graph-independent storage spine (docs/PHASE6_WIKI_PLAN.md Wave A).

The article/section/revision/index/exclusion tables + the `notes.wiki_built` dirty bit.
This is the parallel-safe slice: it depends only on the STABLE substrate (notes, chunks,
domains, the RLS firewall) — no FK into the in-flux fact/entity shape (those land in the
gated Wave C: `wiki_citations`, `wiki_links`, the `fact_id` exclusion FK).

Firewall (CLAUDE.md rule 3, enforced in Postgres):
- `wiki_articles` is the owner-visible cross-domain shell (display identity only — title,
  slug, image, lead summary — so a render never reads the single-domain-RLS entity row).
- `wiki_sections` is the firewall/RLS unit: owner + `has_domain_scope(domain_code)`, like
  `app.lists`. A subsection inherits its parent's domain (a BEFORE trigger enforces it), so
  a section subtree shares one domain and hides together.
- `wiki_revisions` carries no domain of its own; it inherits its section's visibility via an
  EXISTS policy (the `list_items` precedent).
- `wiki_index` is per-section, domain-scoped (the ANN match target; queries run scoped).
- `wiki_source_exclusions` is owner + domain-scoped editorial suppression.

Scoped non-owner readers (capability tokens) don't exist until Phase 7; sections are
owner-only-narrowed for now (tested with narrowed-owner sessions), which is the safe
over-restriction. The derived "scoped principal sees an article iff ≥1 in-scope section"
read policy is wired with capability tokens in Phase 7.

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-17
"""

from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- articles: owner-visible cross-domain shell (display identity only) ---------------
    op.execute(
        """
        CREATE TABLE app.wiki_articles (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            entity_ref uuid,             -- soft anchor; resolved system-scoped at build, no FK
            title text NOT NULL,
            slug text NOT NULL UNIQUE,
            image_sha text,              -- copied from the entity at build (no entity-row read)
            lead_summary text,           -- the 1-2 sentence blurb for landing + search
            lead_embedding vector(384),
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'merged', 'archived')),
            merged_into_id uuid REFERENCES app.wiki_articles(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX wiki_articles_status_idx ON app.wiki_articles (status, updated_at DESC)"
    )
    op.execute("ALTER TABLE app.wiki_articles ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.wiki_articles FORCE ROW LEVEL SECURITY")
    # Owner-only: the cross-domain shell is non-sensitive display identity; the firewall
    # lives on sections. (Phase 7 relaxes to "owner OR a visible in-scope section".)
    op.execute(
        """
        CREATE POLICY wiki_articles_owner ON app.wiki_articles
        USING (app.is_owner()) WITH CHECK (app.is_owner())
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.wiki_articles TO jbrain_app")

    # --- sections: the firewall/RLS unit (single-domain; subsections inherit) -------------
    op.execute(
        """
        CREATE TABLE app.wiki_sections (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            article_id uuid NOT NULL REFERENCES app.wiki_articles(id) ON DELETE CASCADE,
            domain_code text NOT NULL REFERENCES app.domains(code),
            parent_section_id uuid REFERENCES app.wiki_sections(id) ON DELETE CASCADE,
            current_revision_id uuid,    -- FK added after wiki_revisions exists (circular)
            seq int NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX wiki_sections_article_idx ON app.wiki_sections (article_id, seq)")
    op.execute("CREATE INDEX wiki_sections_parent_idx ON app.wiki_sections (parent_section_id)")
    # A subsection must share its parent's domain, so the whole subtree is one firewall unit
    # and hides together. Enforced in Postgres, not app code (non-negotiable #3).
    op.execute(
        """
        CREATE FUNCTION app.wiki_section_domain_inherit() RETURNS trigger AS $$
        BEGIN
            IF NEW.parent_section_id IS NOT NULL
               AND NEW.domain_code <> (
                   SELECT domain_code FROM app.wiki_sections WHERE id = NEW.parent_section_id
               ) THEN
                RAISE EXCEPTION
                    'wiki subsection domain % must equal its parent''s domain', NEW.domain_code;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER wiki_section_domain_inherit
        BEFORE INSERT OR UPDATE ON app.wiki_sections
        FOR EACH ROW EXECUTE FUNCTION app.wiki_section_domain_inherit()
        """
    )
    op.execute("ALTER TABLE app.wiki_sections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.wiki_sections FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY wiki_sections_owner ON app.wiki_sections
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.wiki_sections TO jbrain_app")

    # --- revisions: append-only per section; inherits section visibility -------------------
    op.execute(
        """
        CREATE TABLE app.wiki_revisions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            section_id uuid NOT NULL REFERENCES app.wiki_sections(id) ON DELETE CASCADE,
            seq int NOT NULL,
            run_id uuid REFERENCES app.runs(id) ON DELETE SET NULL,
            body text NOT NULL,
            summary text,
            body_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', body)) STORED,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX wiki_revisions_section_idx ON app.wiki_revisions (section_id, seq)")
    op.execute("CREATE INDEX wiki_revisions_tsv_idx ON app.wiki_revisions USING gin (body_tsv)")
    op.execute("ALTER TABLE app.wiki_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.wiki_revisions FORCE ROW LEVEL SECURITY")
    # A revision is visible exactly when its section is — the sections RLS filters this
    # subquery, so revisions (incl. the FTS over body_tsv) carry no firewall column.
    op.execute(
        """
        CREATE POLICY wiki_revisions_via_section ON app.wiki_revisions
        USING (EXISTS (SELECT 1 FROM app.wiki_sections s WHERE s.id = section_id))
        WITH CHECK (EXISTS (SELECT 1 FROM app.wiki_sections s WHERE s.id = section_id))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.wiki_revisions TO jbrain_app")

    # close the circular ref now that revisions exists
    op.execute(
        "ALTER TABLE app.wiki_sections ADD CONSTRAINT wiki_sections_current_revision_fk"
        " FOREIGN KEY (current_revision_id) REFERENCES app.wiki_revisions(id) ON DELETE SET NULL"
    )

    # --- index: per-section summary embedding (the domain-scoped ANN match target) --------
    op.execute(
        """
        CREATE TABLE app.wiki_index (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            section_id uuid NOT NULL UNIQUE REFERENCES app.wiki_sections(id) ON DELETE CASCADE,
            domain_code text NOT NULL REFERENCES app.domains(code),
            summary text NOT NULL,
            summary_embedding vector(384),
            embedding_model text,
            last_updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX wiki_index_embedding_idx ON app.wiki_index"
        " USING hnsw (summary_embedding vector_cosine_ops)"
    )
    op.execute("ALTER TABLE app.wiki_index ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.wiki_index FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY wiki_index_owner ON app.wiki_index
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.wiki_index TO jbrain_app")

    # --- source exclusions: owner editorial suppression (note-id half; fact_id FK is Wave C)
    op.execute(
        """
        CREATE TABLE app.wiki_source_exclusions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            note_id uuid REFERENCES app.notes(id) ON DELETE CASCADE,
            fact_id uuid,                          -- no FK in Wave A; the FK lands in Wave C
            article_id uuid REFERENCES app.wiki_articles(id) ON DELETE CASCADE,  -- NULL = global
            reason text,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK ((note_id IS NULL) <> (fact_id IS NULL))   -- exactly one target
        )
        """
    )
    op.execute("ALTER TABLE app.wiki_source_exclusions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.wiki_source_exclusions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY wiki_source_exclusions_owner ON app.wiki_source_exclusions
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.wiki_source_exclusions TO jbrain_app")

    # --- the wiki_built dirty bit on notes (graph-independent half of the mark-and-sweep) --
    op.execute("ALTER TABLE app.notes ADD COLUMN wiki_built boolean NOT NULL DEFAULT false")
    op.execute("CREATE INDEX notes_wiki_unbuilt_idx ON app.notes (created_at) WHERE NOT wiki_built")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS app.notes_wiki_unbuilt_idx")
    op.execute("ALTER TABLE app.notes DROP COLUMN wiki_built")
    op.execute("DROP TABLE app.wiki_source_exclusions")
    op.execute("DROP TABLE app.wiki_index")
    op.execute("ALTER TABLE app.wiki_sections DROP CONSTRAINT wiki_sections_current_revision_fk")
    op.execute("DROP TABLE app.wiki_revisions")
    op.execute("DROP TABLE app.wiki_sections")
    op.execute("DROP FUNCTION app.wiki_section_domain_inherit")
    op.execute("DROP TABLE app.wiki_articles")
