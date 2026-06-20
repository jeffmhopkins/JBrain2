"""Chat-turn attachments API (Stage-2 attachments): pre-upload the image/PDF/text a
user drags into a Full Brain chat, then reference the returned ids when the turn is
sent (Wave 2). Owner-only — chat is an owner surface.

Upload links a file to the SESSION and stamps it with a firewall domain computed from
the session's scopes (agent.attachments.domain_for_session). Download/delete go through
a DISTINCT `/chat-attachments` prefix so they never collide with note attachments'
`/attachments/{id}` nor with the `/sessions/{session_id}` param routes.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jbrain.agent.attachments import (
    TurnAttachmentRepo,
    domain_for_session,
    is_allowed_media_type,
)
from jbrain.agent.session import AgentSessionRepo
from jbrain.api.deps import PrincipalDep, SettingsDep, owner_only
from jbrain.api.notes import MAX_ATTACHMENT_BYTES, ctx_for
from jbrain.llm.providers import supports_vision_for_spec
from jbrain.llm.router import TASK_DEFAULTS
from jbrain.settings_store import SqlSettingsStore
from jbrain.storage import BlobStore


def get_turn_attachments(request: Request) -> TurnAttachmentRepo:
    return cast(TurnAttachmentRepo, request.app.state.turn_attachments)


def get_agent_sessions(request: Request) -> AgentSessionRepo:
    return cast(AgentSessionRepo, request.app.state.agent_sessions)


def get_blob_store(request: Request) -> BlobStore:
    return cast(BlobStore, request.app.state.blob_store)


def get_settings_store(request: Request) -> SqlSettingsStore:
    return cast(SqlSettingsStore, request.app.state.settings_store)


TurnAttachmentsDep = Annotated[TurnAttachmentRepo, Depends(get_turn_attachments)]
AgentSessionsDep = Annotated[AgentSessionRepo, Depends(get_agent_sessions)]
BlobStoreDep = Annotated[BlobStore, Depends(get_blob_store)]
SettingsStoreDep = Annotated[SqlSettingsStore, Depends(get_settings_store)]


class AttachmentOut(BaseModel):
    id: str
    filename: str
    media_type: str
    size_bytes: int
    has_extracts: bool = False
    has_description: bool = False


# Owner-only, like the sessions router. Two routers in one module: chat uploads hang
# off the session (so the path mirrors note attachments), while get/delete-by-id live
# under their own prefix to avoid the collisions noted above.
sessions_router = APIRouter(prefix="/sessions", dependencies=[Depends(owner_only)])
router = APIRouter(prefix="/chat-attachments", dependencies=[Depends(owner_only)])


@sessions_router.post("/{session_id}/attachments", status_code=201)
async def upload_chat_attachment(
    session_id: str,
    file: UploadFile,
    principal: PrincipalDep,
    repo: TurnAttachmentsDep,
    sessions: AgentSessionsDep,
    blobs: BlobStoreDep,
) -> AttachmentOut:
    media_type = file.content_type or "application/octet-stream"
    # Only the conversion-allowlist types may be stored: anything else has no
    # chat-send conversion path, so reject it at the door (shared allowlist).
    if not is_allowed_media_type(media_type):
        raise HTTPException(status_code=415, detail=f"unsupported media type: {media_type}")
    data = await file.read(MAX_ATTACHMENT_BYTES + 1)
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail="attachment too large")
    owner_ctx = ctx_for(principal)
    info = await sessions.get(owner_ctx, session_id)
    if info is None:
        raise HTTPException(status_code=404, detail="session not found")
    # The firewall scope is the session's, decided here (security choice, Decision 1).
    domain_code = domain_for_session(info.domain_scopes)
    # Write under the session's narrowed firewall so the insert's WITH CHECK can never
    # stamp a domain the session can't reach.
    ctx = await repo.session_read_context(owner_ctx, session_id)
    assert ctx is not None  # session existence already confirmed above
    digest = await blobs.put(data)
    att = await repo.add(
        ctx,
        session_id,
        sha256=digest,
        filename=file.filename or "attachment",
        media_type=media_type,
        size_bytes=len(data),
        domain_code=domain_code,
    )
    return AttachmentOut(
        id=att.id,
        filename=att.filename,
        media_type=att.media_type,
        size_bytes=att.size_bytes,
        has_extracts=att.has_extracts,
        has_description=att.has_description,
    )


@router.get("/{attachment_id}")
async def download_chat_attachment(
    attachment_id: str,
    principal: PrincipalDep,
    repo: TurnAttachmentsDep,
    blobs: BlobStoreDep,
) -> FileResponse:
    info = await repo.get(ctx_for(principal), attachment_id)
    if info is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    return FileResponse(
        blobs.path_for(info.sha256), media_type=info.media_type, filename=info.filename
    )


@router.delete("/{attachment_id}", status_code=204)
async def delete_chat_attachment(
    attachment_id: str, principal: PrincipalDep, repo: TurnAttachmentsDep
) -> None:
    if await repo.remove(ctx_for(principal), attachment_id) is None:
        raise HTTPException(status_code=404, detail="attachment not found")


class ChatCapabilities(BaseModel):
    # Whether the model the agent.turn task resolves to can accept images — the PWA
    # gates the attach-image affordance on it. A model capability, not per-session.
    supports_vision: bool


capabilities_router = APIRouter(prefix="/chat", dependencies=[Depends(owner_only)])


@capabilities_router.get("/capabilities")
async def chat_capabilities(
    principal: PrincipalDep, settings: SettingsDep, store: SettingsStoreDep
) -> ChatCapabilities:
    """Whether the agent.turn model supports vision, after live per-task overrides —
    so the chat composer offers image upload only when the model can read it."""
    overrides = await store.llm_task_overrides(ctx_for(principal))
    spec = (overrides.get("agent.turn") or {}).get("spec") or TASK_DEFAULTS["agent.turn"]
    return ChatCapabilities(supports_vision=supports_vision_for_spec(settings, spec))
