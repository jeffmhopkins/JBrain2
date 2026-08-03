"""The PUBLIC jlaunch results-share surface — unauthenticated by design (the token is the
credential), the read side of the owner mint/list/revoke in `jlaunch.py`.

A visitor GETs `/jlaunch-share/{token}`: the token resolves to a live link, then the run is
read PINNED to the link id so the `jlaunch_runs_share` RLS policy (0153) admits exactly that
link's run and nothing else. `/jlaunch-share/{token}/artifact` streams the collected tarball
from the content-addressed blob store (addressed by the run's stored sha). Revoked or unknown
tokens 404. Every response is rate-limited, `no-referrer`, and `noindex`. This router carries
NO owner dependency — do not add owner-only reads here. Mirrors `research_share.py`.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jbrain.external import jlaunch_shares
from jbrain.locations.ratelimit import TokenBucket

router = APIRouter()


def get_maker(request: Request):
    return request.app.state.session_maker


def get_blobs(request: Request):
    return request.app.state.blob_store


def get_share_limiter(request: Request) -> TokenBucket:
    return cast(TokenBucket, request.app.state.jlaunch_share_rate_limiter)


MakerDep = Annotated[Any, Depends(get_maker)]
BlobsDep = Annotated[Any, Depends(get_blobs)]
ShareLimiterDep = Annotated[TokenBucket, Depends(get_share_limiter)]


class PublicRunOut(BaseModel):
    """A shared run's public results — the fields the results page renders, no owner metadata."""

    title: str
    spec_name: str
    repo: str
    state: str
    verify_ok: bool | None
    headline: list[Any]
    machine: dict[str, Any]
    phase_times: list[Any]
    artifact_name: str
    artifact_bytes: int | None
    has_artifact: bool
    created_at: str
    finished_at: str


def _harden(response: Response) -> None:
    # A share URL carries a bearer token in the path; a click must not send it in the
    # Referer, and crawlers must not index the token URL.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"


def _rate_limit(request: Request, limiter: TokenBucket) -> None:
    client = request.client.host if request.client else "unknown"
    if not limiter.allow(client):
        raise HTTPException(status_code=429, detail="rate limited")


@router.get("/jlaunch-share/{token}")
async def view_share(
    token: str,
    request: Request,
    response: Response,
    maker: MakerDep,
    limiter: ShareLimiterDep,
) -> PublicRunOut:
    """Resolve a share link to its run's public results. 404 on an unknown / revoked token."""
    _rate_limit(request, limiter)
    _harden(response)
    link_id = await jlaunch_shares.resolve_share(maker, token)
    if link_id is None:
        raise HTTPException(status_code=404, detail="unknown or revoked share link")
    run = await jlaunch_shares.fetch_shared_run(maker, link_id)
    if run is None:
        raise HTTPException(status_code=404, detail="unknown or revoked share link")
    return PublicRunOut(
        title=run.title,
        spec_name=run.spec_name,
        repo=run.repo,
        state=run.state,
        verify_ok=run.verify_ok,
        headline=run.headline,
        machine=run.machine,
        phase_times=run.phase_times,
        artifact_name=run.artifact_name,
        artifact_bytes=run.artifact_bytes,
        has_artifact=bool(run.artifact_sha256),
        created_at=run.created_at,
        finished_at=run.last_active_at,
    )


@router.get("/jlaunch-share/{token}/artifact")
async def download_artifact(
    token: str,
    request: Request,
    maker: MakerDep,
    blobs: BlobsDep,
    limiter: ShareLimiterDep,
) -> FileResponse:
    """Stream the shared run's collected artifact from the content-addressed blob store. 404
    on an unknown / revoked token or a run with no collected artifact."""
    _rate_limit(request, limiter)
    link_id = await jlaunch_shares.resolve_share(maker, token)
    if link_id is None:
        raise HTTPException(status_code=404, detail="unknown or revoked share link")
    run = await jlaunch_shares.fetch_shared_run(maker, link_id)
    if run is None or not run.artifact_sha256:
        raise HTTPException(status_code=404, detail="no artifact for this run")
    path = blobs.path_for(run.artifact_sha256)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file is missing")
    return FileResponse(
        path,
        media_type="application/gzip",
        filename=run.artifact_name or "artifact.tar.gz",
        headers={"Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"},
    )
