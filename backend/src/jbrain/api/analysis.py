"""Analysis read/resolve endpoints: per-note extraction view, entity pages,
and the review inbox. Owner-only is implicit pre-P7: every query runs on the
principal's RLS context, and only the owner holds a session today.

The response shapes are a frozen contract with the frontend — change them
only with a coordinated frontend PR.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.agent.proposals import ProposalRepo, WaitingApproval
from jbrain.analysis.entities import MergeScopeError
from jbrain.analysis.repo import (
    REVIEW_STATUSES,
    AlreadyOpen,
    AlreadyResolved,
    SqlAnalysisRepo,
    UnknownAction,
)
from jbrain.api.deps import OwnerDep, PrincipalDep
from jbrain.api.images import MAX_IMAGE_BYTES, sniff_image_type, sniff_path
from jbrain.api.notes import BlobStoreDep, ctx_for
from jbrain.db.session import scoped_session
from jbrain.embed import EmbedClient
from jbrain.models.note_conversation import NoteConversationRepo, NotesInboxEntry
from jbrain.notes.repo import SqlNotesRepo
from jbrain.notes.service import UnknownDomain
from jbrain.workflow import events as wf_events

router = APIRouter()


def get_analysis_repo(request: Request) -> SqlAnalysisRepo:
    return cast(SqlAnalysisRepo, request.app.state.analysis_repo)


def get_notes_repo(request: Request) -> SqlNotesRepo:
    return cast(SqlNotesRepo, request.app.state.notes_repo)


def get_session_maker(request: Request) -> "async_sessionmaker":
    return cast("async_sessionmaker", request.app.state.session_maker)


def get_embed_client(request: Request) -> EmbedClient:
    return cast(EmbedClient, request.app.state.embed_client)


def get_proposals_repo(request: Request) -> ProposalRepo:
    return cast(ProposalRepo, request.app.state.agent_proposals)


@router.get("/notes/{note_id}/analysis")
async def note_analysis(note_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    view = await get_analysis_repo(request).note_analysis_view(ctx_for(principal), note_id)
    if view is None:
        raise HTTPException(status_code=404, detail="note not found")
    return view


@router.get("/entities")
async def entity_list(
    request: Request,
    principal: PrincipalDep,
    q: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    items = await get_analysis_repo(request).list_entities(ctx_for(principal), q=q, kind=kind)
    return {"items": items}


@router.get("/entities/{entity_id}")
async def entity_detail(
    entity_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    view = await get_analysis_repo(request).entity_view(ctx_for(principal), entity_id)
    if view is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return view


@router.get("/entities/{entity_id}/neighbors")
async def entity_neighbors(
    entity_id: str,
    request: Request,
    principal: PrincipalDep,
    depth: Annotated[int, Query(ge=1, le=2)] = 1,
) -> dict[str, Any]:
    """Ego subgraph for the graph view (nodes + directed edges to `depth`
    hops). RLS-scoped, so firewalled neighbours and their edges never leak."""
    view = await get_analysis_repo(request).ego_graph(ctx_for(principal), entity_id, depth=depth)
    if view is None:
        raise HTTPException(status_code=404, detail="entity not found")
    return view


@router.put("/entities/{entity_id}/image")
async def set_entity_image(
    entity_id: str, file: UploadFile, owner: OwnerDep, request: Request, blobs: BlobStoreDep
) -> dict[str, str]:
    """Set an entity's owner profile image. Owner-only; the media type is sniffed from the bytes
    (the client's Content-Type is not trusted), so a non-image is rejected before it is stored."""
    data = await file.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image too large")
    media_type = sniff_image_type(data[:16])
    if media_type is None:
        raise HTTPException(status_code=415, detail="unsupported image type")
    digest = await blobs.put(data)
    if not await get_analysis_repo(request).set_entity_image(ctx_for(owner), entity_id, digest):
        raise HTTPException(status_code=404, detail="entity not found")
    return {"image_sha": digest, "media_type": media_type}


@router.get("/entities/{entity_id}/image")
async def get_entity_image(
    entity_id: str, principal: PrincipalDep, request: Request, blobs: BlobStoreDep
) -> FileResponse:
    sha = await get_analysis_repo(request).entity_image_sha(ctx_for(principal), entity_id)
    if sha is None or not await blobs.exists(sha):
        raise HTTPException(status_code=404, detail="no image")
    path = blobs.path_for(sha)
    # nosniff: the bytes are served inline with a magic-byte-derived type, so a browser must not
    # re-sniff a chameleon file (image header + HTML tail) into something executable.
    return FileResponse(
        path, media_type=sniff_path(path), headers={"X-Content-Type-Options": "nosniff"}
    )


@router.get("/graph")
async def full_graph(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """The graph view's default: the whole graph (every visible entity, including
    disconnected ones, + all relationship edges), centered on the "Me" entity.
    RLS-scoped, so firewalled entities and their edges never leak. An empty
    knowledge base returns an empty graph rather than 404."""
    return await get_analysis_repo(request).full_graph(ctx_for(principal))


@router.get("/review")
async def review_list(
    request: Request,
    principal: PrincipalDep,
    status: Annotated[str, Query()] = "open",
) -> dict[str, Any]:
    if status not in REVIEW_STATUSES:
        raise HTTPException(status_code=400, detail="unknown status")
    items = await get_analysis_repo(request).list_review(ctx_for(principal), status)
    return {"items": items}


class NotesInboxRow(BaseModel):
    """One row of the review inbox's NOTES tab (D4). Deliberately verb-free: it names
    where to go and nothing to do, because the conversation is the only place note
    ingestion is decided. Nothing here addresses a decision endpoint."""

    kind: Literal["question", "approval"]
    # The conversation to open, and the persona hosting it. The row's entire purpose.
    session_id: str
    agent: str
    note_id: str | None
    domain: str
    # What the row quotes — the note's opening, or the owner's own staged request.
    quote: str
    # What is being asked. None on a conversation that has not asked yet (a first pass
    # still reading), which is why the row is listed but not counted.
    ask: str | None
    captured_at: datetime | None
    waiting_since: datetime
    # Graph writes this thread has already committed, so the row says how much of the
    # note is settled before the owner spends a tap on it.
    committed: int
    # A first pass still running: listed so the note is visibly in hand, NOT counted —
    # nothing is waiting on the owner yet.
    live: bool


# The sentence a staged standing-instruction change reads as in the inbox. Server-side
# so the row says the SAME thing everywhere it is read, and so the model never authors
# the copy on a row whose only job is to be trustworthy.
STAGED_APPROVAL_ASK = (
    "A change to your standing instructions is staged, waiting for your approval"
    " before it is written."
)


def merge_notes_inbox(
    waiting: Sequence[NotesInboxEntry], approvals: Sequence[WaitingApproval]
) -> list[NotesInboxRow]:
    """Interleave the two sources into ONE list, oldest wait first, so the tab drains
    from the top regardless of which producer a row came from. Pure, so the ordering
    that makes the tab usable is testable without a database."""
    rows = [
        NotesInboxRow(
            kind="question",
            session_id=w.session_id,
            agent=w.agent,
            note_id=w.note_id,
            domain=w.domain,
            quote=w.note_excerpt,
            ask=w.question,
            captured_at=w.captured_at,
            waiting_since=w.waiting_since,
            committed=w.committed,
            live=w.live,
        )
        for w in waiting
    ] + [
        NotesInboxRow(
            kind="approval",
            session_id=a.session_id,
            agent=a.agent,
            note_id=None,
            domain=a.domain,
            quote=a.title,
            ask=STAGED_APPROVAL_ASK,
            captured_at=None,
            waiting_since=a.staged_at,
            committed=0,
            live=False,
        )
        for a in approvals
    ]
    rows.sort(key=lambda r: r.waiting_since)
    return rows


@router.get("/review/notes")
async def notes_inbox(request: Request, principal: OwnerDep) -> dict[str, Any]:
    """The notes tab: ingestion questions and staged approvals waiting on the owner
    (D4/D5), oldest wait first so the list drains from the top.

    A REDIRECT list. It returns no item id a decision could be posted against and no
    action verb, because "the inbox only redirects" is a property of the contract, not
    an intention of the screen: there is no endpoint an inbox row could answer through,
    so no future screen can quietly grow one here.

    Owner-only explicitly (`OwnerDep`), not by the module's pre-P7 implicitness: the
    rows quote note bodies across every domain, including health.
    """
    ctx = ctx_for(principal)
    # Stateless over a caller-supplied session, the `PlanRepo`/`ArchivistMemoryRepo`
    # idiom — the transaction (and so the RLS scope) is this route's, not the repo's.
    async with scoped_session(get_session_maker(request), ctx) as session:
        waiting = await NoteConversationRepo().notes_inbox(session)
    approvals = await get_proposals_repo(request).list_waiting_approvals(ctx)
    rows = merge_notes_inbox(waiting, approvals)
    return {"items": [r.model_dump(mode="json") for r in rows]}


@router.get("/review/{item_id}/predicate-suggestions")
async def review_predicate_suggestions(
    item_id: str, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    """The weighted relation candidates the correct-in-place predicate picker
    offers for a held inference — computed on demand so any open card gets live
    suggestions. Embedder failures surface as an empty list (the picker falls
    back to manual entry); 404 only when the item is gone."""
    try:
        suggestions = await get_analysis_repo(request).predicate_suggestions(
            ctx_for(principal), item_id, embedder=get_embed_client(request)
        )
    except Exception:  # noqa: BLE001 — a flaky embedder must not break the picker
        return {"suggestions": []}
    if suggestions is None:
        raise HTTPException(status_code=404, detail="review item not found")
    return {"suggestions": suggestions}


class ResolveRequest(BaseModel):
    action: str = Field(min_length=1)
    payload: dict[str, Any] = {}


@router.post("/review/{item_id}/resolve")
async def resolve_review(
    item_id: str, body: ResolveRequest, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    repo = get_analysis_repo(request)
    try:
        item = await repo.resolve_review(ctx_for(principal), item_id, body.action, body.payload)
    except UnknownAction as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except AlreadyResolved:
        raise HTTPException(status_code=409, detail="review item is not open") from None
    except MergeScopeError as exc:
        # Unreachable over HTTP today (ctx_for is a full owner), but the guard exists
        # for a future narrowed surface and a 500 would read as a server fault rather
        # than the refusal it is.
        raise HTTPException(status_code=403, detail=str(exc)) from None
    if item is None:
        raise HTTPException(status_code=404, detail="review item not found")
    return item


class ReviewCorrectionRequest(BaseModel):
    body: str = Field(min_length=1)
    domain: str = "general"


@router.post("/review/{item_id}/correction", status_code=201)
async def file_review_correction(
    item_id: str, body: ReviewCorrectionRequest, owner: OwnerDep, request: Request
) -> dict[str, Any]:
    """Mint the owner CORRECTION note behind the review card's "correct it" flow —
    provenance=owner_correction, the #7 channel (docs/reference/DESIGN.md "Edit model").

    An owner correction "out-argues the graph": its surface-attested facts extract at
    full weight and force-supersede + pin the current head (supersession.decide), so a
    fix APPLIES instead of colliding with what it corrects. Filing it as a plain `human`
    note — the prior behaviour — routed it back through normal extraction, where a
    same-value restatement of a prose-valued attribute (value_json null) reads as a fresh
    conflict and files another attribute_collision card: the correction spawned reviews
    instead of resolving one. EXPLICITLY owner-gated like the wiki correction path: minting
    an owner_correction is the one privileged write that force-supersedes the graph. The
    card is resolved separately (action `correct`, carrying this note id) once the id is in
    hand, mirroring the wiki flow's create-then-drive shape.

    409 when the target card declares `correctable: false`. That flag also drops the
    footer's composer in the UI, but a rendering hint is not a gate: the one card that
    sets it (the EMR location firewall's) exists BECAUSE a value was held out of the
    domain the card sits in, and a correction lands pinned at full weight in that same
    domain — so the refusal has to hold for any caller, not just the shipped one."""
    ctx = ctx_for(owner)
    # Read the card on the caller's own scoped session — never a widened one — so the
    # gate sees exactly the card the caller can see.
    if not await get_analysis_repo(request).review_correctable(ctx, item_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "review item is not correctable: a correction would file the held"
                " value back into the domain it was kept out of"
            ),
        )
    maker = get_session_maker(request)
    try:
        note, created = await get_notes_repo(request).create_note(
            ctx,
            client_id=f"correction-{uuid.uuid4().hex}",
            domain=body.domain,
            destination=None,
            body=body.body,
            provenance="owner_correction",
            source_ref=f"review:{item_id}",
        )
    except UnknownDomain:
        raise HTTPException(status_code=400, detail="unknown domain") from None
    if created:
        # Drive ingestion via the note.created event exactly as POST /notes does — the
        # dispatcher resolves it to ingest_note; the correction then flows extract →
        # integrate → force-supersede + pin. Best-effort: a dropped emit is re-driven by
        # the pending-notes reconciler, so it never blocks the create.
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


class BatchDecision(BaseModel):
    id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    payload: dict[str, Any] = {}


class ResolveBatchRequest(BaseModel):
    decisions: list[BatchDecision] = Field(min_length=1, max_length=200)


@router.post("/review/resolve-batch")
async def resolve_review_batch(
    body: ResolveBatchRequest, request: Request, principal: PrincipalDep
) -> dict[str, Any]:
    """Bulk-apply the same-shaped per-item decisions in one transaction; the
    good ones commit and bad ones come back in `errors` (the UI rolls those
    rows back). Used by the inbox's select-and-approve / defer-all actions."""
    repo = get_analysis_repo(request)
    return await repo.resolve_review_batch(
        ctx_for(principal), [d.model_dump() for d in body.decisions]
    )


@router.post("/review/{item_id}/reopen")
async def reopen_review(item_id: str, request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """Full unwind: reverses the resolution's recorded graph effects and
    re-queues the item. Permanent distinct_from edges survive by doctrine;
    the response's reopen_note says so when one was kept."""
    repo = get_analysis_repo(request)
    try:
        item = await repo.reopen_review(ctx_for(principal), item_id)
    except AlreadyOpen:
        raise HTTPException(status_code=409, detail="review item is already open") from None
    except MergeScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    if item is None:
        raise HTTPException(status_code=404, detail="review item not found")
    return item
