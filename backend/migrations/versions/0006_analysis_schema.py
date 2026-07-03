"""Phase 3 analysis schema: entities, facts, temporal tokens, review inbox.

Implements docs/reference/ANALYSIS.md. Every table carries its own domain_code and the
standard app.has_domain_scope policy from 0002 — facts always carry their own
domain regardless of their entity's domain, so no policy ever needs a join.

Deletion philosophy (drives the FK actions and the grants below):

- Facts are never deleted: the supersession chain IS the revision history.
- Entities are never deleted: merge = tombstone via merged_into_id.
- distinct_from edges are permanent negative knowledge: insert-only.
- Mentions and aliases ARE deleted: re-extraction rebuilds them wholesale,
  the same pattern as chunks in 0003.
- Re-ingestion deletes a note's chunks wholesale (0003), so durable rows
  that cite a chunk (facts, temporal tokens) use ON DELETE SET NULL — a
  chunk rebuild must not destroy facts; re-extraction re-points citations.
  Mentions instead CASCADE with their chunk: their spans are meaningless
  without the text they anchor to, and re-extraction recreates them.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-10
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

TEMPORAL_PRECISION = "('instant', 'day', 'month', 'year', 'era', 'unknown')"


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.entities (
            id uuid PRIMARY KEY,
            -- schema.org-guided free text (Person, Organization, Place...),
            -- deliberately not an enum: models converge on the vocabulary,
            -- and nightly consolidation normalizes drift.
            kind text NOT NULL,
            canonical_name text NOT NULL,
            summary text,
            summary_embedding vector(384),
            embedding_model text,
            -- Set when the entity is also a security subject ("Mom" the
            -- entity IS Mom the subject); fact->subject attribution is a
            -- security field.
            subject_id uuid REFERENCES app.subjects(id),
            status text NOT NULL DEFAULT 'provisional'
                CHECK (status IN ('provisional', 'confirmed', 'merged')),
            merged_into_id uuid REFERENCES app.entities(id),
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX entities_kind_idx ON app.entities (kind)")
    # Resolution layer 1 is case-insensitive exact match.
    op.execute("CREATE INDEX entities_name_idx ON app.entities (lower(canonical_name))")
    op.execute(
        "CREATE INDEX entities_summary_embedding_idx"
        " ON app.entities USING hnsw (summary_embedding vector_cosine_ops)"
    )

    op.execute(
        """
        CREATE TABLE app.entity_aliases (
            id uuid PRIMARY KEY,
            entity_id uuid NOT NULL REFERENCES app.entities(id) ON DELETE CASCADE,
            alias text NOT NULL,
            -- App-maintained (not a generated column): dediacritization
            -- needs unaccent(), which Postgres marks only STABLE because it
            -- reads a dictionary, and generated columns require IMMUTABLE
            -- expressions. The repo layer owns the normalization.
            alias_norm text NOT NULL,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (entity_id, alias_norm)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE app.entity_mentions (
            id uuid PRIMARY KEY,
            entity_id uuid NOT NULL REFERENCES app.entities(id),
            chunk_id uuid NOT NULL REFERENCES app.chunks(id) ON DELETE CASCADE,
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            -- Span anchoring is what makes entity merges reversible:
            -- un-merge = re-resolve these spans, not archaeology.
            surface_text text NOT NULL,
            char_start int NOT NULL,
            char_end int NOT NULL,
            link_method text NOT NULL
                CHECK (link_method IN ('exact_alias', 'embedding', 'llm', 'human')),
            confidence real,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX entity_mentions_entity_idx ON app.entity_mentions (entity_id)")
    op.execute("CREATE INDEX entity_mentions_note_idx ON app.entity_mentions (note_id)")

    op.execute(
        """
        CREATE TABLE app.entity_distinctions (
            id uuid PRIMARY KEY,
            entity_a uuid NOT NULL REFERENCES app.entities(id),
            entity_b uuid NOT NULL REFERENCES app.entities(id),
            reason text NOT NULL DEFAULT '',
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now(),
            -- Ordered pair + unique = one canonical edge per entity pair, so
            -- a rejected merge can never be re-proposed in either direction.
            CHECK (entity_a < entity_b),
            UNIQUE (entity_a, entity_b)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE app.temporal_tokens (
            id uuid PRIMARY KEY,
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            chunk_id uuid REFERENCES app.chunks(id) ON DELETE SET NULL,
            surface_phrase text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('point', 'range', 'recurrence')),
            resolved_start timestamptz NOT NULL,
            resolved_end timestamptz,
            temporal_precision text NOT NULL
                CHECK (temporal_precision IN {TEMPORAL_PRECISION}),
            -- The anchor "last Tuesday" was resolved against; an anchor
            -- correction makes re-resolution a targeted update over tokens.
            capture_anchor timestamptz NOT NULL,
            -- iCalendar RRULE, only for kind = 'recurrence'.
            rrule text,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX temporal_tokens_note_idx ON app.temporal_tokens (note_id)")

    op.execute(
        f"""
        CREATE TABLE app.facts (
            id uuid PRIMARY KEY,
            -- Structural identity key: (subject, entity, predicate,
            -- qualifier) is the property-graph address; re-extraction
            -- upserts on it and the supersession chain hangs off it.
            subject_id uuid REFERENCES app.subjects(id),
            entity_id uuid NOT NULL REFERENCES app.entities(id),
            predicate text NOT NULL,
            qualifier text NOT NULL DEFAULT '',
            kind text NOT NULL CHECK (kind IN
                ('event', 'measurement', 'state', 'attribute', 'preference', 'relationship')),
            statement text NOT NULL,
            value_json jsonb,
            -- For relationship-kind edges pointing at another entity.
            object_entity_id uuid REFERENCES app.entities(id),
            assertion text NOT NULL CHECK (assertion IN
                ('asserted', 'negated', 'hypothetical', 'reported', 'question', 'expected')),
            -- Bi-temporal: valid_* is truth-in-the-world, reported_at is the
            -- client capture time (server receipt time is wrong under the
            -- offline outbox). Supersession compares validity time, never
            -- capture time.
            valid_from timestamptz,
            valid_to timestamptz,
            reported_at timestamptz NOT NULL,
            temporal_precision text NOT NULL DEFAULT 'unknown'
                CHECK (temporal_precision IN {TEMPORAL_PRECISION}),
            temporal_token_id uuid REFERENCES app.temporal_tokens(id),
            status text NOT NULL DEFAULT 'active' CHECK (status IN
                ('active', 'superseded', 'pending_review', 'retracted')),
            -- Human overrides survive reprocessing; auto-supersession may
            -- only re-flag a pinned fact, never flip it.
            pinned boolean NOT NULL DEFAULT false,
            superseded_by uuid REFERENCES app.facts(id),
            note_id uuid NOT NULL REFERENCES app.notes(id) ON DELETE CASCADE,
            chunk_id uuid REFERENCES app.chunks(id) ON DELETE SET NULL,
            extractor text NOT NULL,
            prompt_version text NOT NULL,
            confidence real,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # Identity-key lookups, conflict-candidate retrieval, and chain walks.
    op.execute("CREATE INDEX facts_identity_idx ON app.facts (entity_id, predicate, qualifier)")
    op.execute("CREATE INDEX facts_note_idx ON app.facts (note_id)")
    op.execute(
        "CREATE INDEX facts_pending_review_idx ON app.facts (created_at)"
        " WHERE status = 'pending_review'"
    )

    op.execute(
        """
        CREATE TABLE app.review_items (
            id uuid PRIMARY KEY,
            kind text NOT NULL CHECK (kind IN
                ('fact_conflict', 'attribute_collision', 'merge_proposal', 'ambiguous_mention',
                 'domain_promotion', 'low_confidence', 'split_proposal')),
            -- Row references only (fact/entity/mention ids), never note
            -- content: the referenced rows carry their own RLS.
            payload jsonb NOT NULL DEFAULT '{}',
            status text NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'resolved', 'dismissed')),
            resolution jsonb,
            domain_code text NOT NULL REFERENCES app.domains(code),
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX review_items_open_idx ON app.review_items (created_at) WHERE status = 'open'"
    )

    tables = (
        "entities",
        "entity_aliases",
        "entity_mentions",
        "entity_distinctions",
        "temporal_tokens",
        "facts",
        "review_items",
    )
    for table in tables:
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY {table}_domain ON app.{table}
            USING (app.has_domain_scope(domain_code))
            WITH CHECK (app.has_domain_scope(domain_code))
            """
        )

    # Grants follow the deletion philosophy in the module docstring:
    # - entities: UPDATE for merges/status/summary refresh; never DELETE.
    # - entity_aliases: re-extraction rebuilds (delete + insert); no UPDATE —
    #   an alias correction is a rebuild, there is nothing to edit in place.
    # - entity_mentions: UPDATE to repoint on merge/un-merge, DELETE for
    #   re-extraction rebuilds (the chunks pattern from 0003/0005).
    # - entity_distinctions: insert-only permanent negative knowledge.
    # - temporal_tokens: UPDATE for targeted re-resolution after an anchor
    #   correction; never DELETE while facts cite them.
    # - facts: UPDATE for supersession/pinning/retraction; never DELETE —
    #   the chain is the revision history.
    # - review_items: UPDATE writes resolutions; history is kept, no DELETE.
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.entities TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, DELETE ON app.entity_aliases TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.entity_mentions TO jbrain_app")
    op.execute("GRANT SELECT, INSERT ON app.entity_distinctions TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.temporal_tokens TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.facts TO jbrain_app")
    op.execute("GRANT SELECT, INSERT, UPDATE ON app.review_items TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.review_items")
    op.execute("DROP TABLE app.facts")
    op.execute("DROP TABLE app.temporal_tokens")
    op.execute("DROP TABLE app.entity_distinctions")
    op.execute("DROP TABLE app.entity_mentions")
    op.execute("DROP TABLE app.entity_aliases")
    op.execute("DROP TABLE app.entities")
