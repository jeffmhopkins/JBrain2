from datetime import datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from jbrain.api.deps import PrincipalDep
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.notes.service import NoteInfo, NotesRepo, NoteUpdate, UnknownDomain
from jbrain.queue import JobEnqueuer
from jbrain.storage import BlobStore

router = APIRouter()

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024


def get_notes_repo(request: Request) -> NotesRepo:
    return cast(NotesRepo, request.app.state.notes_repo)


def get_blob_store(request: Request) -> BlobStore:
    return cast(BlobStore, request.app.state.blob_store)


def get_job_queue(request: Request) -> JobEnqueuer:
    return cast(JobEnqueuer, request.app.state.job_queue)


NotesRepoDep = Annotated[NotesRepo, Depends(get_notes_repo)]
BlobStoreDep = Annotated[BlobStore, Depends(get_blob_store)]
JobQueueDep = Annotated[JobEnqueuer, Depends(get_job_queue)]


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
    # True once the analyze_note job has written the note_analysis row —
    # the client's lifecycle chip disappears on it.
    analyzed: bool
    attachments: list[AttachmentOut]
    # Location fields are owner-eyes metadata: Phase 7 scoped-token
    # serialization must exclude them from non-owner responses.
    latitude: float | None
    longitude: float | None
    accuracy_m: float | None


def note_out(n: NoteInfo) -> NoteOut:
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
        latitude=n.latitude,
        longitude=n.longitude,
        accuracy_m=n.accuracy_m,
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
    body: CreateNoteRequest, principal: PrincipalDep, repo: NotesRepoDep, jobs: JobQueueDep
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
        await jobs.enqueue(ctx, "ingest_note", {"note_id": note.id})
    return note_out(note)


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
    return NoteListOut(notes=[note_out(n) for n in notes], next_cursor=next_cursor)


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
    return note_out(note)


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
    return note_out(note)


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
    """On-demand re-analysis of one note: a plain analyze_note job, the same
    incremental upsert + retraction sweep an edit triggers — no special re-run
    job kind. Refused while the pipeline would run it anyway (ingest pending
    or OCR outstanding): the ingest gate owns that sequencing."""
    ctx = ctx_for(principal)
    note = await repo.get_note(ctx, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="note not found")
    if await jobs.has_active(ctx, "analyze_note", payload_field="note_id", value=note_id):
        raise HTTPException(status_code=409, detail="analysis already queued or running")
    if note.ingest_state in ("pending", "processing") or await jobs.has_active_ocr_for_note(
        ctx, note_id
    ):
        raise HTTPException(
            status_code=409,
            detail="note is still being processed; analysis will run automatically",
        )
    job_id = await jobs.enqueue(ctx, "analyze_note", {"note_id": note_id})
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
