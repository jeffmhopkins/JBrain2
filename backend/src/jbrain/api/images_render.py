"""The direct, owner-only render API (Wave L3): the image launcher's non-agent path.

`GET /images/generated` lists the owner's gallery; `POST /images/generate` and
`POST /images/edit` drive ONE render through `app.state.image_render` (the shared
`ImageRenderService`, Wave L2) so the screen renders WITHOUT waking jerv — the LLM stays
unloaded (docs/IMAGE_LAUNCHER_PLAN.md). Every route is `OwnerDep` + RLS-scoped: the
`generated_images` table is owner-only (a non-owner principal sees zero rows, can't insert),
so the surface is unreachable by a scoped/capability token.

generate/edit are gated on configuration in main.py — the render router mounts only when
`comfyui_url` is set, mirroring the tool-registry omission, so an unconfigured box 404s them.
The list route reads existing rows and is always available.

This module does NOT touch the LLM router (CLAUDE.md rule 1 n/a — image gen isn't an LLM
call); upload/source bytes ride the `BlobStore` (rule 2); the insert/list run on an RLS-scoped
session (rule 3). A render failure maps to a clean HTTP detail — never a leaked stack trace."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import OwnerDep
from jbrain.api.images import MAX_IMAGE_BYTES, sniff_image_type
from jbrain.api.notes import ctx_for
from jbrain.db.session import scoped_session
from jbrain.image_gen.comfyui import MAX_EDIT_IMAGES, ImageGenError, ImageGenInterrupted
from jbrain.image_gen.render import (
    ImageRenderService,
    ModelNotInstalledError,
    RenderValidationError,
)
from jbrain.models.images import GeneratedImage, GeneratedImageRepo
from jbrain.storage import BlobStore

# The gallery list cap — generous (one owner, content-addressed dedup) but bounded so the
# response never grows without limit. The screen pages nothing yet; newest-first is enough.
_LIST_LIMIT = 200

# The gallery list reads existing rows, so it is ALWAYS mounted (an unconfigured box still has
# a gallery of past renders). generate/edit drive ComfyUI, so their router mounts only when
# image hosting is configured (main.py, mirroring the tool-registry omission) — an unconfigured
# box 404s them.
list_router = APIRouter(prefix="/images")
router = APIRouter(prefix="/images")


class GeneratedImageOut(BaseModel):
    """One render in the gallery — the by-id summary the screen lists and reveals. Matches the
    frontend `GeneratedImageOut` (client.ts): the component builds the `<img>` src from `id`."""

    id: str
    kind: str
    prompt: str
    width: int
    height: int
    model: str
    seed: int | None
    created_at: str


def _summary(row: GeneratedImage) -> GeneratedImageOut:
    return GeneratedImageOut(
        id=str(row.id),
        kind=row.kind,
        prompt=row.prompt,
        width=row.width,
        height=row.height,
        model=row.model,
        seed=row.seed,
        created_at=row.created_at.isoformat(),
    )


# The client's request bodies arrive camelCase (negativePrompt, sourceImageId); accept them by
# alias while keeping snake-case fields, so the backend convention holds and the wire matches.
class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class GenerateImageRequest(_CamelModel):
    prompt: str
    speed: str = "quality"
    aspect: str = "square"
    resolution: str = "medium"
    steps: int | None = None
    seed: int | None = None
    negative_prompt: str = ""


class EditImageRequest(_CamelModel):
    prompt: str
    speed: str = "quality"
    resolution: str = "medium"
    steps: int | None = None
    seed: int | None = None
    negative_prompt: str = ""
    # A prior render to edit, when the source isn't an uploaded file part.
    source_image_id: str | None = None


def _repo(request: Request) -> GeneratedImageRepo:
    return cast(GeneratedImageRepo, request.app.state.generated_image_repo)


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


def _blobs(request: Request) -> BlobStore:
    return cast(BlobStore, request.app.state.blob_store)


def _render(request: Request) -> ImageRenderService:
    # Present whenever this router is mounted (main.py wires both on the comfyui_url gate).
    return cast(ImageRenderService, request.app.state.image_render)


@list_router.get("/generated")
async def list_generated_images(owner: OwnerDep, request: Request) -> list[GeneratedImageOut]:
    """The owner's gallery, newest-first. RLS-scoped to the owner; a non-owner is refused at
    `OwnerDep` and would in any case see no rows (the owner-only firewall)."""
    async with scoped_session(_maker(request), ctx_for(owner)) as session:
        rows = await _repo(request).list(session, limit=_LIST_LIMIT)
    return [_summary(row) for row in rows]


@router.post("/generate")
async def generate_image(
    body: GenerateImageRequest, owner: OwnerDep, request: Request
) -> GeneratedImageOut:
    """Text→image through the shared render service. RLS-scoped; the render frees any resident
    local LLM first (the point — the brain stays unloaded). The typed render errors map to a
    clean 400/409/502, never a stack trace."""
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    try:
        row = await _render(request).generate(
            ctx_for(owner),
            prompt=prompt,
            aspect=body.aspect,
            resolution=body.resolution,
            steps=body.steps,
            seed=body.seed,
            speed=body.speed,
            negative_prompt=body.negative_prompt,
        )
    except (RenderValidationError, ModelNotInstalledError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ImageGenInterrupted as exc:
        raise HTTPException(status_code=409, detail="the render was stopped") from exc
    except ImageGenError as exc:
        raise HTTPException(status_code=502, detail="the image model did not respond") from exc
    return _summary(row)


async def _upload_bytes(upload: UploadFile) -> bytes:
    """Read an uploaded image, enforcing the magic-byte allowlist + size cap (the api/images.py
    guards, reused). A non-image or an oversize file is a clean 400 — the client's Content-Type
    is never trusted."""
    data = await upload.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="image is too large")
    if sniff_image_type(data[:16]) is None:
        raise HTTPException(status_code=400, detail="unsupported image type")
    return data


async def _source_by_id(request: Request, owner: OwnerDep, image_id: str) -> tuple[bytes, str]:
    """Resolve a prior render's bytes + sha by id, RLS-scoped to the owner. A row the owner
    can't see (or a vanished blob) is a clean 400, never a 403/oracle."""
    async with scoped_session(_maker(request), ctx_for(owner)) as session:
        row = await _repo(request).get(session, image_id)
    if row is None:
        raise HTTPException(status_code=400, detail="source image not found")
    try:
        return await _blobs(request).get(row.blob_sha256), row.blob_sha256
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail="source image is no longer available") from exc


@router.post("/edit")
async def edit_image(
    owner: OwnerDep,
    request: Request,
    spec: Annotated[str, Form()],
    source: UploadFile | None = None,
    references: list[UploadFile] | None = None,
) -> GeneratedImageOut:
    """Image→image through the shared render service. The `spec` part is the JSON edit config;
    the primary source is EITHER an uploaded `source` file OR `spec.sourceImageId` (exactly
    one). `references` are extra uploaded image parts; generated-id references ride the spec's
    none here (the screen only attaches uploaded refs). Enforces one primary + the
    MAX_EDIT_IMAGES total, then drives one render under the owner's RLS scope."""
    try:
        body = EditImageRequest.model_validate_json(spec)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid spec") from exc
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    source_id = (body.source_image_id or "").strip()
    has_upload = source is not None and bool(source.filename)
    if has_upload == bool(source_id):
        raise HTTPException(
            status_code=400,
            detail="provide exactly one source: an uploaded file or a sourceImageId",
        )
    if has_upload:
        assert source is not None  # narrowed by has_upload
        source_bytes = await _upload_bytes(source)
        # Persist the uploaded source so the edit row's source_sha256 resolves later (the
        # before/after frame fetches it by the edit's id) — content-addressed dedup.
        source_sha = await _blobs(request).put(source_bytes)
    else:
        source_bytes, source_sha = await _source_by_id(request, owner, source_id)

    ref_uploads = [r for r in (references or []) if r.filename]
    if len(ref_uploads) > MAX_EDIT_IMAGES - 1:
        raise HTTPException(
            status_code=400,
            detail=f"at most {MAX_EDIT_IMAGES} images — the one to edit plus "
            f"{MAX_EDIT_IMAGES - 1} references",
        )
    extra_sources = [await _upload_bytes(r) for r in ref_uploads]

    try:
        row = await _render(request).edit(
            ctx_for(owner),
            prompt=prompt,
            source_bytes=source_bytes,
            source_sha=source_sha,
            resolution=body.resolution,
            extra_sources=extra_sources,
            steps=body.steps,
            seed=body.seed,
            speed=body.speed,
            negative_prompt=body.negative_prompt,
        )
    except (RenderValidationError, ModelNotInstalledError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ImageGenInterrupted as exc:
        raise HTTPException(status_code=409, detail="the render was stopped") from exc
    except ImageGenError as exc:
        raise HTTPException(status_code=502, detail="the image model did not respond") from exc
    return _summary(row)
