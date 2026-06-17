"""Phase 6 wiki — the graph-coupled write layer (docs/PHASE6_WIKI_PLAN.md Wave C1).

The gated half of the wiki storage: the FKs into the (now-stable) fact/entity shape, plus
the `entities.wiki_built` dirty bit and its mark-and-sweep propagation. No LLM, no builder
brain — just the schema + the Postgres-enforced firewall the builder writes through.

What lands here:
- `entities.wiki_built` — the entity half of the mark-and-sweep (the note half shipped in
  0045). Default false; flipped back to false in Postgres on ANY change to an entity's facts,
  mentions, or identity, so the builder (Wave C2) can never miss a mutation regardless of the
  write path that caused it (non-negotiable: the firewall/maintenance lives in the DB).
- `wiki_citations` — a revision's per-clause citation: a hard FK to the cited chunk (and a
  nullable FK to the fact when fact-backed; SET NULL, not RESTRICT, so a fact purge can't
  abort). A BEFORE trigger enforces the contract firewall in Postgres: the citation's domain
  equals its section's AND its chunk's (AND its fact's, when fact-backed), and its
  denormalized `note_id` equals the chunk's — so the two CASCADE paths can't diverge and a
  cross-domain citation can neither be created nor read.
- `wiki_links` — wiki↔wiki links + the "what links here" back-index. `to_entity_id` is a SOFT
  ref (no FK): `entities` is single-domain RLS, so a cross-domain back-link query can't carry
  an FK readable by a scoped principal; it's resolved system-scoped at build, like
  `wiki_articles.entity_ref`. Its domain must equal its source section's (BEFORE trigger).
- the `wiki_source_exclusions.fact_id` FK (left FK-less in 0045) → `app.facts` ON DELETE
  CASCADE: an exclusion of a purged fact is moot, so it goes with the fact.

Dirty propagation (mark): three SECURITY-DEFINER triggers, pinned search_path, so they fire
regardless of the writing session's scope and can dirty a cross-domain entity row a scoped
session can't see (they write a boolean, they never read into the session — no leak):
- on `facts` (INSERT/UPDATE/DELETE): dirty the subject + object entity. Covers create/edit,
  the in-place `valid_to`/`superseded_by` close, refresh, `pinned` toggle, `status→retracted`,
  and the note-purge fact delete.
- on `entity_mentions` (INSERT/DELETE): dirty the mentioned entity. Covers a note edit
  re-resolving mentions (so chunk-only context is picked up with no fact change) and the
  purge mention delete.
- on `entities` (BEFORE UPDATE): self-dirty when an identity column changes (canonical_name,
  kind, summary, status, merged_into_id, domain_code, subject_id) — merge, split re-key,
  rename. It does NOT fire when only `wiki_built` flips, so the builder marking an entity
  CLEAN (`wiki_built=true`) is never immediately re-dirtied.

Purge needs no explicit `wiki_rebuild` enqueue here: a purge deletes the note's facts and
mentions, and those deletes dirty every surviving entity the note touched (the candidate set
in analysis/purge.py), so the next builder run re-derives the affected articles. The explicit
cite-but-not-mentioned enqueue rides Wave C2 with the builder action.

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-17
"""

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- the entity half of the wiki_built dirty bit ---------------------------------------
    op.execute("ALTER TABLE app.entities ADD COLUMN wiki_built boolean NOT NULL DEFAULT false")
    op.execute(
        "CREATE INDEX entities_wiki_unbuilt_idx ON app.entities (created_at) WHERE NOT wiki_built"
    )

    # Dirty an entity when its facts change. SECURITY DEFINER + pinned search_path: the writer
    # is a domain-scoped session and the entity may sit in a different domain than the fact
    # (facts ratchet above their source), so an INVOKER update would be RLS-filtered and miss
    # it. The `AND wiki_built` guard makes the common case (already-dirty entity) a no-op write.
    op.execute(
        """
        CREATE FUNCTION app.wiki_dirty_entity_from_fact() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, pg_temp AS $$
        DECLARE ids uuid[] := ARRAY[]::uuid[];
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                ids := ids || OLD.entity_id;
                IF OLD.object_entity_id IS NOT NULL THEN ids := ids || OLD.object_entity_id; END IF;
            END IF;
            IF TG_OP <> 'DELETE' THEN
                ids := ids || NEW.entity_id;
                IF NEW.object_entity_id IS NOT NULL THEN ids := ids || NEW.object_entity_id; END IF;
            END IF;
            UPDATE app.entities SET wiki_built = false WHERE id = ANY(ids) AND wiki_built;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER wiki_dirty_entity_from_fact
        AFTER INSERT OR UPDATE OR DELETE ON app.facts
        FOR EACH ROW EXECUTE FUNCTION app.wiki_dirty_entity_from_fact()
        """
    )

    # Dirty an entity when its mention set changes (a re-analyzed note rewrites mentions; a
    # purge deletes them) — this is how a note edit with no fact change still rebuilds. UPDATE
    # is covered too: an entity merge re-points mentions in place (UPDATE ... SET entity_id),
    # and a mention-only absorbed entity has no facts to dirty the survivor through the fact
    # trigger, so without dirtying both OLD and NEW here the survivor's article goes stale.
    op.execute(
        """
        CREATE FUNCTION app.wiki_dirty_entity_from_mention() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, pg_temp AS $$
        DECLARE ids uuid[] := ARRAY[]::uuid[];
        BEGIN
            IF TG_OP <> 'INSERT' THEN ids := ids || OLD.entity_id; END IF;
            IF TG_OP <> 'DELETE' THEN ids := ids || NEW.entity_id; END IF;
            UPDATE app.entities SET wiki_built = false WHERE id = ANY(ids) AND wiki_built;
            RETURN NULL;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER wiki_dirty_entity_from_mention
        AFTER INSERT OR UPDATE OR DELETE ON app.entity_mentions
        FOR EACH ROW EXECUTE FUNCTION app.wiki_dirty_entity_from_mention()
        """
    )

    # Self-dirty on an identity change. BEFORE UPDATE mutating NEW (no recursive write); it
    # deliberately ignores a `wiki_built`-only update so the builder's mark-clean sticks.
    op.execute(
        """
        CREATE FUNCTION app.wiki_entity_self_dirty() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.canonical_name IS DISTINCT FROM OLD.canonical_name
               OR NEW.kind IS DISTINCT FROM OLD.kind
               OR NEW.summary IS DISTINCT FROM OLD.summary
               OR NEW.status IS DISTINCT FROM OLD.status
               OR NEW.merged_into_id IS DISTINCT FROM OLD.merged_into_id
               OR NEW.domain_code IS DISTINCT FROM OLD.domain_code
               OR NEW.subject_id IS DISTINCT FROM OLD.subject_id THEN
                NEW.wiki_built := false;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER wiki_entity_self_dirty
        BEFORE UPDATE ON app.entities
        FOR EACH ROW EXECUTE FUNCTION app.wiki_entity_self_dirty()
        """
    )

    # --- citations: a revision clause → the cited chunk (+ fact when fact-backed) -----------
    op.execute(
        """
        CREATE TABLE app.wiki_citations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            revision_id uuid NOT NULL REFERENCES app.wiki_revisions(id) ON DELETE CASCADE,
            fact_id uuid REFERENCES app.facts(id) ON DELETE SET NULL,   -- null = chunk-only claim
            chunk_id uuid NOT NULL REFERENCES app.chunks(id) ON DELETE CASCADE,
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,  -- denormalized
            seq int NOT NULL DEFAULT 0,            -- the [n] order within the revision
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX wiki_citations_revision_idx ON app.wiki_citations (revision_id, seq)")
    op.execute("CREATE INDEX wiki_citations_chunk_idx ON app.wiki_citations (chunk_id)")
    op.execute("CREATE INDEX wiki_citations_note_idx ON app.wiki_citations (note_id)")
    op.execute("CREATE INDEX wiki_citations_fact_idx ON app.wiki_citations (fact_id)")
    op.execute("ALTER TABLE app.wiki_citations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.wiki_citations FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY wiki_citations_owner ON app.wiki_citations
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.wiki_citations TO jbrain_app")
    # The contract firewall in Postgres: a citation's domain must equal its section's AND its
    # chunk's (AND its fact's, when fact-backed), and the denormalized note must equal the
    # chunk's note. SECURITY DEFINER so the section/chunk/fact lookups see the true rows even
    # under a narrowed session.
    op.execute(
        """
        CREATE FUNCTION app.wiki_citation_firewall() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, pg_temp AS $$
        DECLARE section_domain text; chunk_domain text; chunk_note uuid; fact_domain text;
        BEGIN
            SELECT s.domain_code INTO section_domain
                FROM app.wiki_revisions r JOIN app.wiki_sections s ON s.id = r.section_id
                WHERE r.id = NEW.revision_id;
            IF section_domain IS NULL OR NEW.domain_code IS DISTINCT FROM section_domain THEN
                RAISE EXCEPTION 'wiki_citation domain % must equal its section''s domain',
                    NEW.domain_code;
            END IF;
            SELECT domain_code, note_id INTO chunk_domain, chunk_note
                FROM app.chunks WHERE id = NEW.chunk_id;
            IF chunk_domain IS NULL OR NEW.domain_code IS DISTINCT FROM chunk_domain THEN
                RAISE EXCEPTION 'wiki_citation domain % must equal its chunk''s domain',
                    NEW.domain_code;
            END IF;
            IF NEW.note_id IS DISTINCT FROM chunk_note THEN
                RAISE EXCEPTION 'wiki_citation note_id must equal its chunk''s note_id';
            END IF;
            IF NEW.fact_id IS NOT NULL THEN
                SELECT domain_code INTO fact_domain FROM app.facts WHERE id = NEW.fact_id;
                IF fact_domain IS NULL OR NEW.domain_code IS DISTINCT FROM fact_domain THEN
                    RAISE EXCEPTION 'wiki_citation domain % must equal its fact''s domain',
                        NEW.domain_code;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER wiki_citation_firewall
        BEFORE INSERT OR UPDATE ON app.wiki_citations
        FOR EACH ROW EXECUTE FUNCTION app.wiki_citation_firewall()
        """
    )

    # --- links: wiki↔wiki + "what links here" (back-index) ----------------------------------
    op.execute(
        """
        CREATE TABLE app.wiki_links (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            from_section_id uuid NOT NULL REFERENCES app.wiki_sections(id) ON DELETE CASCADE,
            to_entity_id uuid,          -- SOFT ref; resolved system-scoped at build, no FK
            to_article_id uuid REFERENCES app.wiki_articles(id) ON DELETE SET NULL,
            anchor text,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX wiki_links_from_idx ON app.wiki_links (from_section_id)")
    op.execute("CREATE INDEX wiki_links_to_entity_idx ON app.wiki_links (to_entity_id)")
    op.execute("CREATE INDEX wiki_links_to_article_idx ON app.wiki_links (to_article_id)")
    op.execute("ALTER TABLE app.wiki_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.wiki_links FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY wiki_links_owner ON app.wiki_links
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.wiki_links TO jbrain_app")
    # A link's domain must equal its source section's, so a scoped back-link query
    # ("what links here") only ever totals in-scope links. SECURITY DEFINER for the lookup.
    op.execute(
        """
        CREATE FUNCTION app.wiki_link_firewall() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = app, pg_temp AS $$
        DECLARE section_domain text;
        BEGIN
            SELECT domain_code INTO section_domain
                FROM app.wiki_sections WHERE id = NEW.from_section_id;
            IF section_domain IS NULL OR NEW.domain_code IS DISTINCT FROM section_domain THEN
                RAISE EXCEPTION 'wiki_link domain % must equal its source section''s domain',
                    NEW.domain_code;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER wiki_link_firewall
        BEFORE INSERT OR UPDATE ON app.wiki_links
        FOR EACH ROW EXECUTE FUNCTION app.wiki_link_firewall()
        """
    )

    # --- the fact_id exclusion FK (the note_id half shipped in 0045) ------------------------
    op.execute(
        "ALTER TABLE app.wiki_source_exclusions ADD CONSTRAINT wiki_source_exclusions_fact_fk"
        " FOREIGN KEY (fact_id) REFERENCES app.facts(id) ON DELETE CASCADE"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE app.wiki_source_exclusions DROP CONSTRAINT IF EXISTS"
        " wiki_source_exclusions_fact_fk"
    )
    op.execute("DROP TABLE IF EXISTS app.wiki_links")  # drops its firewall trigger
    op.execute("DROP FUNCTION IF EXISTS app.wiki_link_firewall")
    op.execute("DROP TABLE IF EXISTS app.wiki_citations")  # drops its firewall trigger
    op.execute("DROP FUNCTION IF EXISTS app.wiki_citation_firewall")
    op.execute("DROP TRIGGER IF EXISTS wiki_entity_self_dirty ON app.entities")
    op.execute("DROP FUNCTION IF EXISTS app.wiki_entity_self_dirty")
    op.execute("DROP TRIGGER IF EXISTS wiki_dirty_entity_from_mention ON app.entity_mentions")
    op.execute("DROP FUNCTION IF EXISTS app.wiki_dirty_entity_from_mention")
    op.execute("DROP TRIGGER IF EXISTS wiki_dirty_entity_from_fact ON app.facts")
    op.execute("DROP FUNCTION IF EXISTS app.wiki_dirty_entity_from_fact")
    op.execute("DROP INDEX IF EXISTS app.entities_wiki_unbuilt_idx")
    op.execute("ALTER TABLE app.entities DROP COLUMN wiki_built")
