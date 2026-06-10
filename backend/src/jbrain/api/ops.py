"""Owner-only proxy to the supervisor container.

The supervisor is never exposed through Caddy; this proxy is the single
authenticated path from the outside world to host control, and it forwards
only the supervisor's fixed command set.
"""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from jbrain.api.deps import PrincipalDep, SettingsDep, owner_only
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.db.stats import database_stats
from jbrain.storage import BackupShelf, BlobStore
from jbrain.usage import usage_summary

router = APIRouter(prefix="/ops", dependencies=[Depends(owner_only)])


def _client(request: Request) -> httpx.AsyncClient:
    return cast(httpx.AsyncClient, request.app.state.supervisor_client)


def _headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.supervisor_token}"}


@router.get("/status")
async def status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get("/status", headers=_headers(settings))
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


class RestartRequest(BaseModel):
    service: str


@router.post("/restart", status_code=202)
async def restart(
    body: RestartRequest, request: Request, settings: SettingsDep
) -> dict[str, object]:
    resp = await _client(request).post(
        "/restart", json={"service": body.service}, headers=_headers(settings)
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="unknown service")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/metrics")
async def metrics(
    request: Request, principal: PrincipalDep, settings: SettingsDep
) -> dict[str, object]:
    resp = await _client(request).get("/metrics", headers=_headers(settings))
    resp.raise_for_status()
    merged = cast(dict[str, object], resp.json())

    # DB/blob stats are best-effort: host metrics still render if the
    # database is mid-restart.
    try:
        maker = cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)
        ctx = SessionContext(principal_id=principal.id, principal_kind=principal.kind)
        db = await database_stats(maker, ctx)
        merged["db"] = {
            "db_size_bytes": db.db_size_bytes,
            "note_count": db.note_count,
            "attachment_count": db.attachment_count,
            "attachment_bytes": db.attachment_bytes,
        }
    except Exception:  # noqa: BLE001
        merged["db"] = None

    try:
        blobs = cast(BlobStore, request.app.state.blob_store)
        count, total = blobs.usage()
        merged["blobs"] = {"file_count": count, "total_bytes": total}
    except Exception:  # noqa: BLE001
        merged["blobs"] = None

    return merged


@router.get("/llm-usage")
async def llm_usage(
    request: Request, principal: PrincipalDep, settings: SettingsDep
) -> dict[str, object]:
    """The AI usage card: today/month totals, per-task breakdown, last 30
    days — costs estimated at query time from the config price table
    (docs/ANALYSIS.md "Token accounting" / "Cost estimates")."""
    maker = cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)
    ctx = SessionContext(principal_id=principal.id, principal_kind=principal.kind)
    return await usage_summary(maker, ctx, settings.llm_prices)


@router.post("/update", status_code=202)
async def start_update(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).post("/update", headers=_headers(settings))
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="update already running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/update/status")
async def update_status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get(
        "/update/status", params={"tail": 80}, headers=_headers(settings)
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


# --- Data export/import -------------------------------------------------
# Heavy lifting happens in supervisor-launched one-shots (they have docker;
# the api deliberately has neither superuser DB access nor pg_dump). The api
# proxies start/status and moves archive bytes via the shared backups mount.


def _shelf(request: Request) -> BackupShelf:
    return cast(BackupShelf, request.app.state.backup_shelf)


@router.post("/export", status_code=202)
async def start_export(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).post("/export", headers=_headers(settings))
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another operation is running")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/export/status")
async def export_status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get(
        "/export/status", params={"tail": 80}, headers=_headers(settings)
    )
    resp.raise_for_status()
    status = cast(dict[str, object], resp.json())
    # A finished export's filename comes from the shelf, not the log text.
    status["filename"] = (
        _shelf(request).latest_export()
        if status.get("state") == "exited" and status.get("exit_code") == 0
        else None
    )
    return status


@router.get("/export/file/{name}")
async def download_export(name: str, request: Request) -> FileResponse:
    try:
        path = _shelf(request).export_path(name)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such export") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="no such export")
    return FileResponse(path, media_type="application/x-tar", filename=name)


@router.post("/import/upload", status_code=201)
async def upload_import(request: Request, file: UploadFile) -> dict[str, str]:
    async def chunks() -> AsyncIterator[bytes]:
        while data := await file.read(1 << 20):
            yield data

    name = await _shelf(request).save_import(chunks())
    return {"archive": name}


class ImportStartRequest(BaseModel):
    archive: str


@router.post("/import/start", status_code=202)
async def start_import(
    body: ImportStartRequest, request: Request, settings: SettingsDep
) -> dict[str, object]:
    resp = await _client(request).post(
        "/import", json={"archive": body.archive}, headers=_headers(settings)
    )
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail="another operation is running")
    if resp.status_code == 400:
        raise HTTPException(status_code=400, detail="bad archive name")
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/import/status")
async def import_status(request: Request, settings: SettingsDep) -> dict[str, object]:
    resp = await _client(request).get(
        "/import/status", params={"tail": 80}, headers=_headers(settings)
    )
    resp.raise_for_status()
    return cast(dict[str, object], resp.json())


@router.get("/logs/{service}")
async def logs(
    service: str,
    request: Request,
    settings: SettingsDep,
    tail: int = 200,
) -> PlainTextResponse:
    resp = await _client(request).get(
        f"/logs/{service}", params={"tail": tail}, headers=_headers(settings)
    )
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="unknown service")
    resp.raise_for_status()
    return PlainTextResponse(resp.text)


@router.get("/logs/{service}/stream")
async def logs_stream(service: str, request: Request, settings: SettingsDep) -> StreamingResponse:
    client = _client(request)

    async def relay() -> AsyncIterator[bytes]:
        async with client.stream(
            "GET", f"/logs/{service}/stream", headers=_headers(settings), timeout=None
        ) as upstream:
            if upstream.status_code != 200:
                return
            async for chunk in upstream.aiter_bytes():
                yield chunk

    return StreamingResponse(relay(), media_type="text/event-stream")
