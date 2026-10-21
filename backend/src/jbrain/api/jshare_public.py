"""The PUBLIC side of JShare: `/s/<slug>/` serves what the owner published, to anyone.

Unauthenticated by design: the unguessable slug is the whole credential, and RLS limits a
visitor to that one item (`store.fetch_public`). This router carries NO owner dependency
and reads no cookie; Caddy strips the Cookie header before these requests reach the api.

The bytes are the owner's but the CODE in them is anyone's — a zipped site or an HTML page
can run whatever script it carries — and they are served from the app's own origin. What
keeps that script away from the owner's session is the `sandbox` CSP on every response
here: the browser gives the page an opaque origin, so it cannot read the app's cookies or
storage, its requests to /api are cross-site (the Lax session cookie stays home, and the
same-site guard refuses an Origin of `null` besides), and it cannot register a service
worker. Mounted at the root, not under /api, so a site's relative links stay inside its
own `/s/<slug>/` folder.
"""

from __future__ import annotations

import asyncio
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from jbrain.api.client_ip import client_ip
from jbrain.jshare import store
from jbrain.jshare.content import PDF_MEDIA_TYPE, SLUG_RE, is_pdf, lookup, site_media_type

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.locations.ratelimit import TokenBucket
    from jbrain.storage import BlobStore

log = structlog.get_logger()

router = APIRouter()

#: Scripts, forms, popups and downloads work; `allow-same-origin` is the one thing a shared
#: page never gets, and it is what would hand it the app's origin. No `allow-modals` either: a
#: `prompt()` on the app's own host, in the installed PWA's chromeless window, is a convincing
#: place to ask for a passphrase.
SANDBOX = (
    "sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox"
    " allow-downloads allow-pointer-lock allow-presentation allow-orientation-lock"
)

_HEADERS = {
    "Content-Security-Policy": SANDBOX,
    "X-Content-Type-Options": "nosniff",
    # The slug is the credential: no click may carry it off in a Referer, and no crawler
    # should keep it.
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    # A sandboxed page has an opaque origin, so its own fetches of its own files (JSON, module
    # scripts, fonts) are cross-origin. Everything here is public already.
    "Access-Control-Allow-Origin": "*",
    # Revalidate every time, and keep Cloudflare's edge from caching: unpublishing must
    # take the item down at once, not when an edge copy expires.
    "Cache-Control": "private, no-cache",
}

# Browsers refuse to render a PDF in a sandboxed document (their viewers count as plugins),
# so a PDF goes out without the sandbox. That is safe where an HTML page would not be: the
# type is fixed to application/pdf by the name and nosniff holds it there, and a PDF's own
# script runs inside the viewer, never on the app's origin.
_PDF_HEADERS = {k: v for k, v in _HEADERS.items() if k != "Content-Security-Policy"}

# Zip members up to this size are answered whole; bigger ones are streamed.
_BUFFER_LIMIT = 1024**2
_CHUNK = 1 << 20
# Every zip read opens the archive and parses its directory on the shared thread pool. The
# per-client limit cannot bound that across many clients; this does, so a crowd on one share
# queues here instead of starving the api's other blocking work.
_SITE_READS = 16


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast("async_sessionmaker[AsyncSession]", request.app.state.session_maker)


def _blobs(request: Request) -> BlobStore:
    return cast("BlobStore", request.app.state.blob_store)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="not found", headers=_HEADERS)


def _rate_limit(request: Request) -> None:
    limiter = cast("TokenBucket", request.app.state.jshare_rate_limiter)
    if not limiter.allow(client_ip(request) or "unknown"):
        raise HTTPException(status_code=429, detail="rate limited", headers=_HEADERS)


@router.api_route("/s/{slug}", methods=["GET", "HEAD"])
async def share_folder(slug: str, request: Request) -> RedirectResponse:
    """Into the item's folder, so a site's relative links resolve under it."""
    if not SLUG_RE.fullmatch(slug):
        raise _not_found()
    return RedirectResponse(f"/s/{slug}/{_query(request)}", status_code=308, headers=_HEADERS)


def _query(request: Request) -> str:
    return f"?{request.url.query}" if request.url.query else ""


# HEAD too: link unfurlers, download managers and some media players ask it first.
@router.api_route("/s/{slug}/{path:path}", methods=["GET", "HEAD"])
async def share_file(slug: str, path: str, request: Request) -> Response:
    _rate_limit(request)
    if not SLUG_RE.fullmatch(slug):
        raise _not_found()
    maker = _maker(request)
    item = await store.fetch_public(maker, slug)
    if item is None:
        raise _not_found()
    blob = _blobs(request).path_for(item.blob_sha256)
    if not blob.is_file():
        log.warning("jshare_blob_missing", item_id=item.id)
        raise _not_found()
    # A page view, not each thumbnail or file the page pulls in: the owner's own list loads a
    # picture's link as an <img>.
    if (
        path == ""
        and request.method == "GET"
        and request.headers.get("sec-fetch-dest", "document") == "document"
    ):
        try:
            await store.count_view(maker, item.id)
        except Exception as exc:  # noqa: BLE001 - a counter must never take the page down
            log.warning("jshare_count_failed", error=repr(exc))
    if item.kind == "site":
        return await _site_file(request, item, blob, path)
    if path not in ("", item.filename):
        raise _not_found()
    # By the name, not the stored type: PDFs shared before they opened inline are stored as
    # octet-stream.
    pdf = item.kind == "file" and is_pdf(item.filename)
    # A file of no kind the browser shows is a download; the rest open in the tab, unless
    # the link asks for the download.
    inline = (item.kind != "file" or pdf) and "download" not in request.query_params
    return FileResponse(
        blob,
        media_type=PDF_MEDIA_TYPE if pdf else item.media_type,
        filename=item.filename,
        content_disposition_type="inline" if inline else "attachment",
        headers=_PDF_HEADERS if pdf else _HEADERS,
    )


async def _site_file(request: Request, item: store.ShareItem, blob: Path, path: str) -> Response:
    # Made on first use, inside the app's own loop: a semaphore binds to the loop it first
    # waits in.
    state = request.app.state
    if getattr(state, "jshare_site_reads", None) is None:
        state.jshare_site_reads = asyncio.Semaphore(_SITE_READS)
    async with state.jshare_site_reads:
        return await _site_file_locked(request, item, blob, path)


async def _site_file_locked(
    request: Request, item: store.ShareItem, blob: Path, path: str
) -> Response:
    zf = await asyncio.to_thread(zipfile.ZipFile, blob)
    try:
        found = await asyncio.to_thread(lookup, zf, item.site_root, path)
    except Exception:
        zf.close()
        raise
    if found.redirect_to_folder:
        zf.close()
        return RedirectResponse(
            f"/s/{item.slug}/{quote(path)}/{_query(request)}", status_code=308, headers=_HEADERS
        )
    info = found.info
    if info is None:
        zf.close()
        raise _not_found()
    media = site_media_type(info.filename)
    etag = f'"{item.blob_sha256[:16]}-{info.CRC:08x}-{info.file_size:x}"'
    headers = {**_HEADERS, "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        zf.close()
        return Response(status_code=304, headers=headers)
    if request.method == "HEAD":
        zf.close()
        return Response(
            status_code=200,
            media_type=media,
            headers={**headers, "Content-Length": str(info.file_size)},
        )
    if info.file_size <= _BUFFER_LIMIT:
        try:
            body = await asyncio.to_thread(zf.read, info)
        finally:
            zf.close()
        return Response(body, media_type=media, headers=headers)

    async def stream() -> AsyncIterator[bytes]:
        try:
            member = await asyncio.to_thread(zf.open, info)
            with member:
                while chunk := await asyncio.to_thread(member.read, _CHUNK):
                    yield chunk
        finally:
            zf.close()

    # The generator's `finally` never runs if the client leaves before the body starts; the
    # background close covers that (closing twice is harmless).
    return StreamingResponse(
        stream(), media_type=media, headers=headers, background=BackgroundTask(zf.close)
    )
