"""Resolve a chat image source (a generated image, or a turn attachment) to bytes —
RLS-scoped — for the agent tools that read an image by id: image-gen edits,
analyze_image, and identify_fish.

The boundary is the session: the lookup runs under `ctx.session`, so a foreign or
out-of-scope id is a clean MISS (the artifact simply isn't visible), never a leaked
error. A non-uuid id (a model guessing "latest") is treated as a miss before it can
hand the DB a bad argument. Exactly one of the two ids must be given; both/neither is
a clean tool-error string the caller returns verbatim. Extracted so the resolution —
and its RLS guarantees — live in one place rather than re-implemented per tool.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.attachments import TurnAttachmentRepo
from jbrain.agent.loop import ToolContext
from jbrain.db.session import scoped_session
from jbrain.models.images import GeneratedImageRepo
from jbrain.storage import BlobStore


def is_uuid(value: str) -> bool:
    """Whether a string is a parseable uuid — the form every image/attachment id
    takes. A non-uuid id is rejected here so the lookup never hands the DB a bad
    argument and leaks a raw error to the model."""
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class ImageSourceResolver:
    """Resolve an image source by id to (bytes, sha256) under the caller's RLS-scoped
    session. `maker` opens the scoped transaction each read runs under; the firewall
    is Postgres', applied from `ctx.session`."""

    def __init__(
        self,
        repo: GeneratedImageRepo,
        blob_store: BlobStore,
        attachments: TurnAttachmentRepo,
        maker: async_sessionmaker[AsyncSession],
    ) -> None:
        self._repo = repo
        self._blob_store = blob_store
        self._attachments = attachments
        self._maker = maker

    async def source_bytes(
        self, arguments: dict, ctx: ToolContext, *, tool: str
    ) -> tuple[bytes, str] | str:
        """Resolve EXACTLY ONE source to (bytes, sha) or a clean error string (naming
        the calling `tool`). Both/neither is rejected before any work; an unknown or
        out-of-scope id is a clean miss (RLS-scoped — a foreign artifact isn't visible)."""
        image_id = str(arguments.get("source_image_id", "")).strip()
        attachment_id = str(arguments.get("source_attachment_id", "")).strip()
        if bool(image_id) == bool(attachment_id):
            return (
                f"{tool} needs exactly one source: source_image_id (an image you generated)"
                " or source_attachment_id (an image the owner attached) — not both, not neither."
            )
        return await self.resolve(image_id, attachment_id, ctx)

    async def resolve(
        self, image_id: str, attachment_id: str, ctx: ToolContext
    ) -> tuple[bytes, str] | str:
        """Resolve a single source — exactly one of the two ids non-empty — to
        (bytes, sha) or a clean error string. Shared by the primary source and (for
        the image tools) each reference image."""
        if image_id:
            # A non-uuid (e.g. the model guessing "latest") would make the lookup raise
            # a raw DB DataError that leaks to the model — treat it as a clean miss.
            if not is_uuid(image_id):
                return "No generated image with that id is in this chat."
            # generated_images is owner-only, so an empty-scope owner context reads it.
            async with scoped_session(self._maker, ctx.session) as session:
                row = await self._repo.get(session, image_id)
            if row is None:
                return "No generated image with that id is in this chat."
            try:
                return await self._blob_store.get(row.blob_sha256), row.blob_sha256
            except FileNotFoundError:
                return "That source image is no longer available."
        # A chat attachment is DOMAIN-scoped (stamped 'general' for a jerv session), so it
        # is read under the attachment context (the session's scopes + that stamped domain),
        # not jerv's empty read scopes — the same widening the chat turn uses to load
        # attachments. RLS still hides a foreign-domain id, which reads as a clean miss.
        if ctx.agent_session_id is None or not is_uuid(attachment_id):
            return "No attached image with that id is in this chat."
        att_ctx = await self._attachments.session_read_context(ctx.session, ctx.agent_session_id)
        if att_ctx is None:
            return "No attached image with that id is in this chat."
        info = await self._attachments.get(att_ctx, attachment_id)
        if info is None:
            return "No attached image with that id is in this chat."
        try:
            return await self._blob_store.get(info.sha256), info.sha256
        except FileNotFoundError:
            return "That source image is no longer available."
