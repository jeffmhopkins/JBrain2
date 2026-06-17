from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import PrincipalDep
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.notes.service import NoteInfo, NotesRepo, NoteUpdate, UnknownDomain
from jbrain.queue import JobEnqueuer
from jbrain.storage import BlobStore
from jbrain.workflow import events as wf_events

router = APIRouter()

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024


def get_notes_repo(request: Request) -> NotesRepo:
    return cast(NotesRepo, request.app.state.notes_repo)


def get_blob_store(request: Request) -> BlobStore:
    return cast(BlobStore, request.app.state.blob_store)


def get_job_queue(request: Request) -> JobEnqueuer:
    return cast(JobEnqueuer, request.app.state.job_queue)


def get_session_maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


NotesRepoDep = Annotated[NotesRepo, Depends(get_notes_repo)]
BlobStoreDep = Annotated[BlobStore, Depends(get_blob_store)]
JobQueueDep = Annotated[JobEnqueuer, Depends(get_job_queue)]
SessionMakerDep = Annotated["async_sessionmaker[AsyncSession]", Depends(get_session_maker)]


def ctx_for(principal: PrincipalInfo) -> SessionContext:
    # Owner sessions see every domain; scoped principals arrive in Phase 7.
    return SessionContext(principal_id=principal.id, principal_kind=principal.kind)


class AttachmentOut(BaseModel):
    id: str
    filename: str
    media_type: str
    size_bytes: int
    # True once the vision pipeline cached OCR/caption text for this file —
    # the client derives the OCR status chip from it.
    has_extracts: bool = False
    # True once a non-empty description is cached (full image analysis) —
    # flips the chip to "text + description".
    has_description: bool = False


class NoteOut(BaseModel):
    id: str
    client_id: str
    domain: str
    destination: str | None
    body: str
    created_at: datetime
    tz_offset_minutes: int | None
    ingest_state: str
    # Hidden from the home stream (still searchable); see POST /notes/{id}/hide.
    hidden: bool
    # True once the integrate_note job has written the note_analysis row —
    # the client's lifecycle chip disappears on it.
    analyzed: bool
    # 'human' or 'agent' — the stream tags agent-authored (Proposal-enacted)
    # notes without polluting the body with attribution prose (ASSISTANT.md #7).
    provenance: str
    attachments: list[AttachmentOut]
    # Location fields are owner-eyes metadata: Phase 7 scoped-token
    # serialization must exclude them from non-owner responses.
    latitude: float | None
    longitude: float | None
    accuracy_m: float | None


def note_out(n: NoteInfo, *, include_location: bool) -> NoteOut:
    # Note lat/lon/accuracy are owner-eyes metadata living on the note row across
    # ALL domains, so the domain RLS firewall does not strip them — this
    # serializer is the sole defense. HTTP request principals are never
    # owner-narrowed (ctx_for sets no owner_scoped), so `kind == 'owner'` is the
    # correct gate here; the owner-narrowed agent/tool layer guards separately.
    return NoteOut(
        id=n.id,
        client_id=n.client_id,
        domain=n.domain,
        destination=n.destination,
        body=n.body,
        created_at=n.created_at,
        tz_offset_minutes=n.tz_offset_minutes,
        ingest_state=n.ingest_state,
        hidden=n.hidden,
        analyzed=n.analyzed,
        provenance=n.provenance,
        attachments=[
            AttachmentOut(
                id=a.id,
                filename=a.filename,
                media_type=a.media_type,
                size_bytes=a.size_bytes,
                has_extracts=a.has_extracts,
                has_description=a.has_description,
            )
            for a in n.attachments
        ],
        latitude=n.latitude if include_location else None,
        longitude=n.longitude if include_location else None,
        accuracy_m=n.accuracy_m if include_location else None,
    )


class CreateNoteRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    domain: str = "general"
    destination: str | None = None
    body: str = Field(min_length=1)
    # Capture time and the client's UTC offset (minutes east of UTC), recorded
    # at write time so an offline note flushed later keeps its true local
    # capture instant — the anchor extraction resolves relative dates against.
    created_at: datetime | None = None
    tz_offset_minutes: int | None = Field(default=None, ge=-1080, le=1080)
    # Capture location, stored verbatim. Owner-eyes metadata: Phase 7
    # scoped-token serialization must exclude these fields.
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    accuracy_m: float | None = Field(default=None, ge=0)


class UpdateNoteRequest(BaseModel):
    body: str | None = Field(default=None, min_length=1)
    domain: str | None = None
    destination: str | None = None


class NoteListOut(BaseModel):
    notes: list[NoteOut]
    next_cursor: datetime | None


@router.post("/notes", status_code=201)
async def create_note(
    body: CreateNoteRequest,
    principal: PrincipalDep,
    repo: NotesRepoDep,
    maker: SessionMakerDep,
) -> NoteOut:
    ctx = ctx_for(principal)
    try:
        note, created = await repo.create_note(
            ctx,
            client_id=body.client_id,
            domain=body.domain,
            destination=body.destination,
            body=body.body,
            created_at=body.created_at,
            tz_offset_minutes=body.tz_offset_minutes,
            latitude=body.latitude,
            longitude=body.longitude,
            accuracy_m=body.accuracy_m,
        )
    except UnknownDomain:
        raise HTTPException(status_code=400, detail="unknown domain") from None
    # Only a fresh insert needs ingestion; an idempotent retry already has a
    # job (or finished one). Payload carries the id only, never note content.
    if created:
        # W2·C cutover: emit the note.created event that DRIVES ingestion — the engine
        # dispatcher resolves it to the ingest pipeline and enqueues the ingest_note
        # job (with the queued / past-'pending' dedup now in dispatcher._already_active).
        # The direct ingest enqueue is gone; only the event remains. Best-effort — a
        # failed emit never blocks note creation, and the recurring pending reconciler
        # (backfill_pending_notes, keyed on ingest_state='pending') is the safety net
        # that re-drives ingestion for any note whose event was dropped. The
        # `_shadow_enqueued` baseline rides along for the dispatcher's observability
        # diff, not a second enqueue.
        await wf_events.emit_event(
            maker,
            ctx,
            type=wf_events.NOTE_CREATED,
            domain_code=note.domain,
            payload={"note_id": note.id},
            enqueued=wf_events.shadow_enqueued("ingest_note", {"note_id": note.id}),
            principal_id=ctx.principal_id,
        )
    return note_out(note, include_location=principal.kind == "owner")


@router.get("/notes")
async def list_notes(
    principal: PrincipalDep,
    repo: NotesRepoDep,
    limit: int = 50,
    before: datetime | None = None,
) -> NoteListOut:
    limit = max(1, min(limit, 200))
    notes = await repo.list_notes(ctx_for(principal), limit=limit, before=before)
    next_cursor = notes[-1].created_at if len(notes) == limit else None
    include_location = principal.kind == "owner"
    return NoteListOut(
        notes=[note_out(n, include_location=include_location) for n in notes],
        next_cursor=next_cursor,
    )


@router.patch("/notes/{note_id}")
async def update_note(
    note_id: str,
    body: UpdateNoteRequest,
    principal: PrincipalDep,
    repo: NotesRepoDep,
    jobs: JobQueueDep,
) -> NoteOut:
    ctx = ctx_for(principal)
    changes = NoteUpdate(
        body=body.body,
        domain=body.domain,
        destination=body.destination,
        # An explicit `"destination": null` clears it; an absent key leaves it.
        clear_destination="destination" in body.model_fields_set and body.destination is None,
    )
    try:
        note = await repo.update_note(ctx, note_id, changes)
    except UnknownDomain:
        raise HTTPException(status_code=400, detail="unknown domain") from None
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")
    # Re-chunk under the (possibly new) domain — chunks always derive domain
    # from the note at ingest time; ingest then re-enqueues embedding.
    await jobs.enqueue(ctx, "ingest_note", {"note_id": note_id})
    return note_out(note, include_location=principal.kind == "owner")


@router.delete("/notes/{note_id}", status_code=204)
async def delete_note(note_id: str, principal: PrincipalDep, repo: NotesRepoDep) -> None:
    if not await repo.delete_note(ctx_for(principal), note_id):
        raise HTTPException(status_code=404, detail="note not found")


# Hide/unhide only flip home-stream visibility — no re-ingest, so unlike a
# PATCH they leave the search index alone and the note stays findable.
@router.post("/notes/{note_id}/hide", status_code=204)
async def hide_note(note_id: str, principal: PrincipalDep, repo: NotesRepoDep) -> None:
    if not await repo.set_hidden(ctx_for(principal), note_id, True):
        raise HTTPException(status_code=404, detail="note not found")


@router.post("/notes/{note_id}/unhide", status_code=204)
async def unhide_note(note_id: str, principal: PrincipalDep, repo: NotesRepoDep) -> None:
    if not await repo.set_hidden(ctx_for(principal), note_id, False):
        raise HTTPException(status_code=404, detail="note not found")


@router.get("/notes/{note_id}")
async def get_note(note_id: str, principal: PrincipalDep, repo: NotesRepoDep) -> NoteOut:
    note = await repo.get_note(ctx_for(principal), note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")
    return note_out(note, include_location=principal.kind == "owner")


@router.post("/notes/{note_id}/attachments", status_code=201)
async def upload_attachment(
    note_id: str,
    file: UploadFile,
    principal: PrincipalDep,
    repo: NotesRepoDep,
    blobs: BlobStoreDep,
    jobs: JobQueueDep,
) -> AttachmentOut:
    data = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")
    ctx = ctx_for(principal)
    digest = await blobs.put(data)
    attachment = await repo.add_attachment(
        ctx,
        note_id=note_id,
        sha256=digest,
        filename=file.filename or "attachment",
        media_type=file.content_type or "application/octet-stream",
        size_bytes=len(data),
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail="note not found")
    # Re-ingest the whole note: the pipeline rebuilds all chunks, now
    # including this attachment's extracted segments.
    await jobs.enqueue(ctx, "ingest_note", {"note_id": note_id})
    return AttachmentOut(
        id=attachment.id,
        filename=attachment.filename,
        media_type=attachment.media_type,
        size_bytes=attachment.size_bytes,
        has_extracts=attachment.has_extracts,
        has_description=attachment.has_description,
    )


@router.delete("/attachments/{attachment_id}", status_code=204)
async def remove_attachment(
    attachment_id: str,
    principal: PrincipalDep,
    repo: NotesRepoDep,
    jobs: JobQueueDep,
) -> None:
    ctx = ctx_for(principal)
    note_id = await repo.remove_attachment(ctx, attachment_id)
    if note_id is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    # Re-ingest rebuilds the note's chunks without the removed file's text.
    await jobs.enqueue(ctx, "ingest_note", {"note_id": note_id})


class ExtractOut(BaseModel):
    kind: str
    text: str
    tool: str
    confidence: float | None
    created_at: datetime


class ExtractsOut(BaseModel):
    extracts: list[ExtractOut]


@router.get("/attachments/{attachment_id}/extracts")
async def attachment_extracts(
    attachment_id: str, principal: PrincipalDep, repo: NotesRepoDep
) -> ExtractsOut:
    """The vision-cache rows for one attachment — fetched lazily when a
    manifest row expands, never inlined into note payloads."""
    rows = await repo.list_extracts(ctx_for(principal), attachment_id)
    if rows is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    return ExtractsOut(
        extracts=[
            ExtractOut(
                kind=r.kind,
                text=r.text,
                tool=r.tool,
                confidence=r.confidence,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )


@router.post("/notes/{note_id}/analyze", status_code=202)
async def analyze_note(
    note_id: str,
    principal: PrincipalDep,
    repo: NotesRepoDep,
    jobs: JobQueueDep,
) -> dict[str, str]:
    """On-demand re-analysis of one note: the integrate_note pipeline, the same
    incremental upsert + retraction sweep an edit triggers — no special re-run
    job kind. Refused while the pipeline would run it anyway (ingest pending
    or OCR outstanding): the ingest gate owns that sequencing."""
    ctx = ctx_for(principal)
    note = await repo.get_note(ctx, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")
    # A 409 if integration is already in flight, so the note can't be raced
    # into double-processing.
    if await jobs.has_active_analysis(ctx, note_id):
        raise HTTPException(status_code=409, detail="analysis already queued or running")
    if note.ingest_state in ("pending", "processing") or await jobs.has_active_ocr_for_note(
        ctx, note_id
    ):
        raise HTTPException(
            status_code=409,
            detail="note is still being processed; analysis will run automatically",
        )
    job_id = await jobs.enqueue(ctx, "integrate_note", {"note_id": note_id})
    return {"job_id": job_id}


@router.post("/attachments/{attachment_id}/analyze", status_code=202)
async def analyze_attachment(
    attachment_id: str,
    principal: PrincipalDep,
    repo: NotesRepoDep,
    jobs: JobQueueDep,
) -> dict[str, str]:
    """On-demand full analysis for one attachment, regardless of the global
    image-analysis mode (also the re-run path). The handler re-describes —
    delete+insert of the caption row — runs OCR only if missing, then
    re-ingests, so the note's lifecycle chip walks again."""
    ctx = ctx_for(principal)
    if await repo.get_attachment(ctx, attachment_id) is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    if await jobs.has_active(
        ctx, "ocr_attachment", payload_field="attachment_id", value=attachment_id
    ):
        raise HTTPException(status_code=409, detail="analysis already queued or running")
    job_id = await jobs.enqueue(
        ctx, "ocr_attachment", {"attachment_id": attachment_id, "mode": "full"}
    )
    return {"job_id": job_id}


@router.get("/attachments/{attachment_id}")
async def download_attachment(
    attachment_id: str, principal: PrincipalDep, repo: NotesRepoDep, blobs: BlobStoreDep
) -> FileResponse:
    info = await repo.get_attachment(ctx_for(principal), attachment_id)
    if info is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    return FileResponse(
        blobs.path_for(info.sha256), media_type=info.media_type, filename=info.filename
    )
