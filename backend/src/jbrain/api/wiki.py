"""GET /api/wiki/landing and /api/wiki/{id} — the read side of the machine-written wiki —
plus POST /api/wiki/{id}/corrections — the owner correction-note create path (Phase 6 §4).

Owner-only is implicit pre-P7 (every read query runs on the principal's RLS context, only the
owner holds a session today). The correction create path is EXPLICITLY owner-gated: minting an
`owner_correction` note is the one privileged write that force-supersedes the graph, so it must
never be reachable by a non-owner (capability) token. The response shapes are a frozen contract
with the frontend reader/landing; `landing` is declared before `{id}` so the static path wins.
"""

import uuid
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import OwnerDep, PrincipalDep
from jbrain.api.images import sniff_path
from jbrain.api.notes import BlobStoreDep, ctx_for
from jbrain.db.session import scoped_session
from jbrain.notes.repo import SqlNotesRepo
from jbrain.notes.service import UnknownDomain
from jbrain.wiki.readstore import WikiReadStore
from jbrain.workflow import events as wf_events

router = APIRouter()


def get_wiki_read_store(request: Request) -> WikiReadStore:
    return cast(WikiReadStore, request.app.state.wiki_read_store)


def get_notes_repo(request: Request) -> SqlNotesRepo:
    return cast(SqlNotesRepo, request.app.state.notes_repo)


def get_session_maker(request: Request) -> "async_sessionmaker[AsyncSession]":
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


@router.get("/wiki/landing")
async def wiki_landing(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    return await get_wiki_read_store(request).get_landing(ctx_for(principal))


@router.get("/wiki/{article_id}")
async def wiki_article(
    article_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    article = await get_wiki_read_store(request).get_article(ctx_for(principal), article_id)
    if article is None:
        raise HTTPException(status_code=404, detail="article not found")
    return article


@router.get("/wiki/{article_id}/image")
async def wiki_article_image(
    article_id: str, principal: PrincipalDep, request: Request, blobs: BlobStoreDep
) -> FileResponse:
    """Serve the article's owner profile image — the sha the builder copied onto the article row
    (so the bytes are never dereferenced across the firewall). RLS-scoped via the article shell."""
    try:
        aid = str(uuid.UUID(article_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="no image") from None
    async with scoped_session(get_session_maker(request), ctx_for(principal)) as session:
        sha = (
            await session.execute(
                text("SELECT image_sha FROM app.wiki_articles WHERE id = :a AND status = 'active'"),
                {"a": aid},
            )
        ).scalar()
    if sha is None or not await blobs.exists(sha):
        raise HTTPException(status_code=404, detail="no image")
    path = blobs.path_for(sha)
    # nosniff: served inline with a magic-byte-derived type — don't let the browser re-sniff.
    return FileResponse(
        path, media_type=sniff_path(path), headers={"X-Content-Type-Options": "nosniff"}
    )


class CorrectionRequest(BaseModel):
    body: str = Field(min_length=1)
    domain: str
    # The revision the correction disputes (the anchor); optional — a correction can also be a
    # standalone reassertion. Must be a revision the owner can see.
    revision_id: str | None = None


@router.post("/wiki/{article_id}/corrections", status_code=201)
async def file_correction(
    article_id: str, body: CorrectionRequest, owner: OwnerDep, request: Request
) -> dict[str, Any]:
    """Mint an owner-authored CORRECTION note (Phase 6 §4): provenance=owner_correction, anchored
    to the disputed revision, then drive ingestion. Its surface-attested facts extract at full
    weight and force-supersede + pin the conflicting head (Wave A+), and the changed entity is
    dirtied so the next builder run rewrites the article — the owner has out-argued the wiki."""
    ctx = ctx_for(owner)
    maker = get_session_maker(request)
    rev: uuid.UUID | None = None
    if body.revision_id is not None:
        try:
            rev = uuid.UUID(body.revision_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="bad revision_id") from None
        # The anchor must be a revision the owner can see (RLS-scoped); reject a dangling one
        # rather than let the FK raise an opaque IntegrityError mid-create.
        async with scoped_session(maker, ctx) as session:
            seen = (
                await session.execute(
                    text("SELECT 1 FROM app.wiki_revisions WHERE id = :r"), {"r": str(rev)}
                )
            ).first()
        if seen is None:
            raise HTTPException(status_code=404, detail="revision not found")

    try:
        note, created = await get_notes_repo(request).create_note(
            ctx,
            client_id=f"correction-{uuid.uuid4().hex}",
            domain=body.domain,
            destination=None,
            body=body.body,
            provenance="owner_correction",
            source_ref=f"wiki:{article_id}",
            wiki_revision_id=rev,
        )
    except UnknownDomain:
        raise HTTPException(status_code=400, detail="unknown domain") from None
    if created:
        # Drive ingestion via the note.created event (the dispatcher resolves it to ingest_note);
        # the correction then flows extract → integrate → force-supersede + pin → dirty → rebuild.
        await wf_events.emit_event(
            maker,
            ctx,
            type=wf_events.NOTE_CREATED,
            domain_code=note.domain,
            payload={"note_id": note.id},
            enqueued=wf_events.shadow_enqueued("ingest_note", {"note_id": note.id}),
            principal_id=ctx.principal_id,
        )
    return {"note_id": note.id, "created": created}
