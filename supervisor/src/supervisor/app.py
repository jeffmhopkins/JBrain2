"""HTTP surface: a fixed command set over the Docker gateway.

Every route except /healthz requires the bearer token. The app is built by a
factory taking settings and a gateway so tests inject fakes — no docker
daemon, no real token in the environment.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from supervisor.gateway import DockerGateway, UnknownServiceError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from supervisor.config import Settings

DEFAULT_LOG_TAIL = 200
MAX_LOG_TAIL = 2000


class RestartRequest(BaseModel):
    service: str


class RestartResponse(BaseModel):
    restarting: list[str]


class ContainerStatus(BaseModel):
    service: str
    state: str
    health: str | None
    started_at: str | None
    image: str


class StatusResponse(BaseModel):
    containers: list[ContainerStatus]


def create_app(settings: Settings, gateway: DockerGateway) -> FastAPI:
    """Build the supervisor app around an injected gateway."""
    app = FastAPI(title="jbrain-supervisor")

    expected = f"Bearer {settings.supervisor_token}".encode()

    def require_token(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        # compare_digest keeps the check constant-time; comparing the whole
        # header value means a wrong scheme fails the same way as a wrong token.
        provided = (authorization or "").encode()
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.exception_handler(UnknownServiceError)
    async def _unknown_service(
        request: Request, exc: UnknownServiceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404, content={"detail": f"Unknown service: {exc.service}"}
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Unauthenticated by design: the compose healthcheck carries no token.
        return {"status": "ok"}

    authed = APIRouter(dependencies=[Depends(require_token)])

    @authed.get("/status")
    def status() -> StatusResponse:
        return StatusResponse(
            containers=[
                ContainerStatus(
                    service=c.service,
                    state=c.state,
                    health=c.health,
                    started_at=c.started_at,
                    image=c.image,
                )
                for c in gateway.list_containers()
            ]
        )

    @authed.post("/restart", status_code=202)
    def restart(body: RestartRequest, background: BackgroundTasks) -> RestartResponse:
        known = {c.service for c in gateway.list_containers()}

        if body.service == "all":
            peers = sorted(known - {settings.self_service})
            for service in peers:
                gateway.restart(service)
            order = list(peers)
            if settings.self_service in known:
                # Self-restart kills this process, so it must run after the
                # response is sent — and after every peer is already bounced.
                background.add_task(gateway.restart, settings.self_service)
                order.append(settings.self_service)
            return RestartResponse(restarting=order)

        if body.service not in known:
            raise UnknownServiceError(body.service)
        if body.service == settings.self_service:
            background.add_task(gateway.restart, body.service)
        else:
            gateway.restart(body.service)
        return RestartResponse(restarting=[body.service])

    @authed.get("/logs/{service}", response_class=PlainTextResponse)
    def logs(
        service: str,
        tail: Annotated[int, Query(ge=1)] = DEFAULT_LOG_TAIL,
    ) -> str:
        return gateway.logs(service, min(tail, MAX_LOG_TAIL))

    @authed.get("/logs/{service}/stream")
    def stream_logs(service: str) -> StreamingResponse:
        # Resolve the service before streaming so unknown names still 404.
        lines = gateway.stream_logs(service)

        def sse() -> Iterator[str]:
            for line in lines:
                yield f"data: {line}\n\n"

        # Sync iterator: starlette drives it in a threadpool, so the blocking
        # docker log follow never stalls the event loop.
        return StreamingResponse(
            sse(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    app.include_router(authed)
    return app
