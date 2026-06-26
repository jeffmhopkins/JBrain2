"""Image-gen ORM + repo (Wave G1, migration 0077). `generated_images` is an owner-only
chat-artifact table (mirrors `wiki_*`): one immutable row per generation/edit recording the
result blob, the resolved seed/dims (for repeatability), and — for edits — the source blob.

`GeneratedImageRepo` takes the caller's already-RLS-scoped `AsyncSession` directly (the
handler owns the session/transaction), so the owner-only firewall is Postgres', not these
methods'. It deliberately does NOT import the image_gen package — the model row is pure
metadata, decoupled from the ComfyUI client.
"""

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import BigInteger, CursorResult, DateTime, Integer, Text, delete, func, select
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from jbrain.models.core import Base


class GeneratedImage(Base):
    """One generated (or edited) image — a chat artifact, never a note/RAG source. `seed` is the
    resolved value (a random seed is recorded so a result is reproducible); `source_sha256` is
    the input blob for an edit and NULL for a fresh generation."""

    __tablename__ = "generated_images"
    __table_args__ = {"schema": "app"}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    blob_sha256: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)  # 'generate' | 'edit'
    model: Mapped[str] = mapped_column(Text)
    prompt: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    steps: Mapped[int] = mapped_column(Integer)
    seed: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GeneratedImageRepo:
    """Persists/reads generated-image rows on a caller-supplied RLS-scoped session."""

    async def insert(
        self,
        session: AsyncSession,
        *,
        blob_sha256: str,
        kind: str,
        model: str,
        prompt: str,
        source_sha256: str | None,
        width: int,
        height: int,
        steps: int,
        seed: int,
    ) -> GeneratedImage:
        row = GeneratedImage(
            blob_sha256=blob_sha256,
            kind=kind,
            model=model,
            prompt=prompt,
            source_sha256=source_sha256,
            width=width,
            height=height,
            steps=steps,
            seed=seed,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return row

    async def get(self, session: AsyncSession, image_id: str) -> GeneratedImage | None:
        try:
            key = uuid.UUID(image_id)
        except (ValueError, AttributeError):
            return None
        return (
            await session.execute(select(GeneratedImage).where(GeneratedImage.id == key))
        ).scalar_one_or_none()

    async def list(self, session: AsyncSession, *, limit: int) -> list[GeneratedImage]:
        """The owner's rows newest-first (the gallery). RLS-scoped: an owner session sees its
        own artifacts, a non-owner session sees none (the owner-only firewall does the hiding,
        not this query)."""
        rows = await session.execute(
            select(GeneratedImage).order_by(GeneratedImage.created_at.desc()).limit(limit)
        )
        return list(rows.scalars().all())

    async def delete(self, session: AsyncSession, image_id: str) -> bool:
        """Delete the owner's row by id, returning whether one was removed. RLS-scoped: a
        foreign/missing id matches nothing, so a non-owner can't delete (and gets no oracle).
        The blob is intentionally NOT touched — blobs are content-addressed/keep-all and may be
        shared by another row's result or an edit's source, so there is no BlobStore.delete."""
        try:
            key = uuid.UUID(image_id)
        except (ValueError, AttributeError):
            return False
        result = await session.execute(delete(GeneratedImage).where(GeneratedImage.id == key))
        return (cast("CursorResult[Any]", result).rowcount or 0) > 0
