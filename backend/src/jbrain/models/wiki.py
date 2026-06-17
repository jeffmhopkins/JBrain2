"""Phase-6 wiki ORM models (docs/PHASE6_WIKI_PLAN.md §2). The graph-independent spine:
articles (the owner-visible cross-domain shell — display identity only), domain-scoped
sections (the firewall/RLS unit; subsections inherit their parent's domain), append-only
per-section revisions, the per-section embedding index, and owner source-exclusions.

The pgvector columns (`wiki_articles.lead_embedding`, `wiki_index.summary_embedding`) and
the generated `wiki_revisions.body_tsv` are deliberately UNMAPPED — written/cosine-queried
via raw SQL exactly like `Entity.summary_embedding` / `Chunk.embedding`; the ORM is for the
relational columns. `wiki_citations` / `wiki_links` and the `fact_id` exclusion FK are NOT
here — they FK into the in-flux fact/entity shape and land in the gated Wave C.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class WikiArticle(Base):
    """One cross-domain article per subject/entity — the owner-visible shell. Display
    identity (title/slug/image/lead) lives here so a render never reads the single-domain-
    RLS entity row; `entity_ref` is a soft anchor resolved system-scoped at build. A merged
    article becomes a reversible redirect (`status='merged'`, `merged_into_id`)."""

    __tablename__ = "wiki_articles"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entity_ref: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    title: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True)
    image_sha: Mapped[str | None] = mapped_column(Text, nullable=True)
    lead_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.wiki_articles.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WikiSection(Base):
    """A domain-scoped section — the firewall/RLS/revision/index unit. `parent_section_id`
    nests subsections (which inherit the parent's domain, enforced by a Postgres trigger);
    `current_revision_id` points at the live revision."""

    __tablename__ = "wiki_sections"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    article_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.wiki_articles.id", ondelete="CASCADE")
    )
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    parent_section_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.wiki_sections.id", ondelete="CASCADE"), nullable=True
    )
    # FK to wiki_revisions is added in the migration (circular ref); kept unmapped-FK here.
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WikiRevision(Base):
    """Append-only per-section revision. The full `body` is kept so any diff is
    reconstructable; `body_tsv` (generated tsvector) is unmapped (FTS via raw SQL)."""

    __tablename__ = "wiki_revisions"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.wiki_sections.id", ondelete="CASCADE")
    )
    seq: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.runs.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WikiIndexEntry(Base):
    """Per-section summary + embedding — the domain-scoped ANN match target for the builder
    and the search wiki-leg. `summary_embedding` is unmapped (raw-SQL cosine, like chunks)."""

    __tablename__ = "wiki_index"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    section_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.wiki_sections.id", ondelete="CASCADE"), unique=True
    )
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    summary: Mapped[str] = mapped_column(Text)
    embedding_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class WikiSourceExclusion(Base):
    """Owner editorial suppression: a note (Wave A) or fact (`fact_id`, FK added in Wave C)
    the builder skips when sourcing — global, or scoped to one `article_id`. Not deletion
    (still searchable) and not retraction (still true)."""

    __tablename__ = "wiki_source_exclusions"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    note_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.notes.id", ondelete="CASCADE"), nullable=True
    )
    fact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    article_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("app.wiki_articles.id", ondelete="CASCADE"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain_code: Mapped[str] = mapped_column(Text, ForeignKey("app.domains.code"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
