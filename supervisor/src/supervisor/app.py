"""HTTP surface: a fixed command set over the Docker gateway.

Every route except /healthz requires the bearer token. The app is built by a
factory taking settings and a gateway so tests inject fakes — no docker
daemon, no real token in the environment.
"""

from __future__ import annotations

import hmac
import re
from contextlib import asynccontextmanager
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
from pydantic import BaseModel, ConfigDict, Field

from supervisor import host_metrics, usb_devices, watchdog
from supervisor.gateway import (
    ENGINE_SERVICES,
    FLASH_NEXT_SERVICE,
    ContainerInfo,
    DockerGateway,
    UnknownServiceError,
    UpdateInProgressError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from supervisor.config import Settings

DEFAULT_LOG_TAIL = 200
MAX_LOG_TAIL = 2000


class RestartRequest(BaseModel):
    service: str


class RestartResponse(BaseModel):
    restarting: list[str]


class ServiceRequest(BaseModel):
    service: str


class ServiceActionResponse(BaseModel):
    service: str
    action: str  # "start" | "stop"


class ContainerStatus(BaseModel):
    service: str
    state: str
    health: str | None
    started_at: str | None
    image: str


class StatusResponse(BaseModel):
    containers: list[ContainerStatus]
    # `docker compose run` containers (an update's `run api`, the perplexity run). They
    # carry their service's label, so they are listed apart rather than in
    # `containers`, where one would read as the service itself.
    oneoffs: list[ContainerStatus] = []


class OneshotRunningResponse(BaseModel):
    # The kind in flight ("update", "refresh", "perplexity", ...), or None.
    running: str | None


class ContainerMemoryOut(BaseModel):
    service: str
    mem_bytes: int


class GpuMemOut(BaseModel):
    # iGPU unified-memory usage/ceilings (bytes); gtt_used is the model device
    # footprint the per-process RSS table can't attribute — see host_metrics.GpuMem.
    gtt_used_bytes: int
    gtt_total_bytes: int
    vram_used_bytes: int
    vram_total_bytes: int


class NetCountersOut(BaseModel):
    # Monotonic since-boot byte counters over physical interfaces; the sampler
    # turns their delta into a throughput rate for the Ops history graph.
    rx_bytes: int
    tx_bytes: int


class DiskCountersOut(BaseModel):
    # Monotonic since-boot byte counters over whole block devices; the sampler
    # turns their delta into a read/write throughput rate for Ops history.
    read_bytes: int
    write_bytes: int


class MetricsResponse(BaseModel):
    mem_total_bytes: int
    mem_available_bytes: int
    swap_total_bytes: int
    swap_free_bytes: int
    disk_total_bytes: int
    disk_free_bytes: int
    load_1m: float
    load_5m: float
    load_15m: float
    uptime_seconds: int
    gpu_busy_percent: float | None
    fan_rpm: dict[str, int] | None
    apu_power_w: float | None
    # iGPU RAM the meter's total includes but no process shows as RSS; None off AMD.
    gpu_mem: GpuMemOut | None = None
    # Curated /proc/meminfo lines (bytes) attributing "used" to a kind; None if
    # meminfo is unreadable.
    mem_breakdown: dict[str, int] | None = None
    # Cumulative rx/tx byte counters (physical interfaces); None if unreadable.
    net: NetCountersOut | None = None
    # Cumulative read/write byte counters (whole block devices); None if unreadable.
    disk_io: DiskCountersOut | None = None
    containers: list[ContainerMemoryOut]


class ProcessMemoryOut(BaseModel):
    service: str
    pid: int
    rss_bytes: int
    command: str


class ProcessesResponse(BaseModel):
    processes: list[ProcessMemoryOut]


class UsbDeviceOut(BaseModel):
    name: str
    usb_id: str
    manufacturer: str | None
    product: str | None
    serial: str | None
    device_node: str | None
    drivers: list[str]
    is_sdr: bool
    sdr_name: str | None


class UsbResponse(BaseModel):
    """Every USB device sysfs reports, plus the SDR-family subset called out.

    `sysfs_readable` distinguishes "the box has no USB devices" (impossible in
    practice) from "this container cannot see /sys" — without it an empty list
    reads as a missing dongle when the real fault is the mount."""

    sysfs_readable: bool
    devices: list[UsbDeviceOut]
    sdrs: list[UsbDeviceOut]


class UpdateStartResponse(BaseModel):
    updater: str


class UpdateStatusResponse(BaseModel):
    state: str
    exit_code: int | None
    log_tail: str


class OneshotStartResponse(BaseModel):
    oneshot: str


class ImportStartRequest(BaseModel):
    archive: str


class RebuildRequest(BaseModel):
    service: str


# Bounds on the perplexity job's one numeric knob. 200 chunks of 512 tokens is ~100k
# tokens — enough for a stable estimate to compare against a published reference, and a
# ceiling so a token cannot park a ~60 GiB process on the box for hours.
PERPLEXITY_CHUNKS_MAX = 200


class PerplexityRequest(BaseModel):
    """Everything a caller may say about the perplexity job — and it is not much.

    `model_path` is resolved by the api from the catalog (it owns the models mount and
    the shard naming); here it must be a .gguf under /models with plain path segments,
    so it can never name a file outside the weights tree or carry a flag.
    extra="forbid": an unexpected field is a 422, not a silently ignored argument."""

    model_config = ConfigDict(extra="forbid")

    model_path: str
    chunks: int | None = Field(default=None, ge=1, le=PERPLEXITY_CHUNKS_MAX)


# /models/<catalog id>/[<quant dir>/]<file>.gguf — every segment starts alphanumeric, so
# none can be `..` or begin with `-`.
PERPLEXITY_MODEL_RE = re.compile(
    r"^/models/[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,127})?"
    r"/[A-Za-z0-9][A-Za-z0-9._-]{0,191}\.gguf$"
)

# Import archives are api-named uploads; anything else is rejected before the
# name reaches a shell command line.
IMPORT_ARCHIVE_RE = re.compile(r"^import-\d{8}-\d{6}\.jbrain\.tar$")


def create_app(
    settings: Settings, gateway: DockerGateway, *, watch_api: bool = True
) -> FastAPI:
    """Build the supervisor app around an injected gateway.

    `watch_api` off is for tests: the watchdog is a background task that probes over the
    network, which a route test has no business starting."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # The API's recovery path cannot live in the API (see watchdog.py). The
        # supervisor
        # already holds the docker socket and is already always-up, so watching from
        # here
        # adds no externally reachable surface and no new credential.
        task = watchdog.start(gateway) if watch_api else None
        try:
            yield
        finally:
            if task is not None:
                await watchdog.stop(task)

    app = FastAPI(title="jbrain-supervisor", lifespan=lifespan)

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
        def out(c: ContainerInfo) -> ContainerStatus:
            return ContainerStatus(
                service=c.service,
                state=c.state,
                health=c.health,
                started_at=c.started_at,
                image=c.image,
            )

        return StatusResponse(
            containers=[out(c) for c in gateway.list_containers()],
            oneoffs=[out(c) for c in gateway.list_oneoffs()],
        )

    @authed.get("/oneshot")
    def oneshot_running() -> OneshotRunningResponse:
        # One read for "is anything in flight", so a caller deciding whether it may
        # touch the engines does not have to know every one-shot kind there is.
        return OneshotRunningResponse(running=gateway.running_oneshot())

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

    def _guard_engine_start(service: str) -> None:
        # One engine at a time (FLASH_NEXT_ENGINE_PLAN §4d), enforced HERE because every
        # caller that starts an engine - the debug switch, the Ops toggle, jcode's power
        # on - comes through this route, and the two engines together freeze the box.
        if gateway.running_oneshot() == "perplexity":
            raise HTTPException(
                status_code=409, detail="a perplexity run holds the box"
            )
        for c in gateway.list_containers():
            other = c.service in ENGINE_SERVICES and c.service != service
            if other and c.state == "running":
                raise HTTPException(
                    status_code=409,
                    detail=f"{c.service} is running; stop it before {service}",
                )

    @authed.post("/start", status_code=202)
    def start_service(body: ServiceRequest) -> ServiceActionResponse:
        # Toggle an existing-but-stopped service on (the comfyui profile service).
        # An unknown/never-created service raises UnknownServiceError -> 404.
        if body.service in ENGINE_SERVICES:
            _guard_engine_start(body.service)
        gateway.start(body.service)
        return ServiceActionResponse(service=body.service, action="start")

    @authed.post("/stop", status_code=202)
    def stop_service(body: ServiceRequest) -> ServiceActionResponse:
        gateway.stop(body.service)
        return ServiceActionResponse(service=body.service, action="stop")

    @authed.get("/metrics")
    def metrics() -> MetricsResponse:
        host = host_metrics.read_host_metrics()
        return MetricsResponse(
            mem_total_bytes=host.mem_total_bytes,
            mem_available_bytes=host.mem_available_bytes,
            swap_total_bytes=host.swap_total_bytes,
            swap_free_bytes=host.swap_free_bytes,
            disk_total_bytes=host.disk_total_bytes,
            disk_free_bytes=host.disk_free_bytes,
            load_1m=host.load_1m,
            load_5m=host.load_5m,
            load_15m=host.load_15m,
            uptime_seconds=host.uptime_seconds,
            gpu_busy_percent=host.gpu_busy_percent,
            fan_rpm=host.fan_rpm,
            apu_power_w=host.apu_power_w,
            gpu_mem=(
                GpuMemOut(
                    gtt_used_bytes=host.gpu_mem.gtt_used_bytes,
                    gtt_total_bytes=host.gpu_mem.gtt_total_bytes,
                    vram_used_bytes=host.gpu_mem.vram_used_bytes,
                    vram_total_bytes=host.gpu_mem.vram_total_bytes,
                )
                if host.gpu_mem is not None
                else None
            ),
            mem_breakdown=host.mem_breakdown,
            net=(
                NetCountersOut(rx_bytes=host.net.rx_bytes, tx_bytes=host.net.tx_bytes)
                if host.net is not None
                else None
            ),
            disk_io=(
                DiskCountersOut(
                    read_bytes=host.disk_io.read_bytes,
                    write_bytes=host.disk_io.write_bytes,
                )
                if host.disk_io is not None
                else None
            ),
            containers=[
                ContainerMemoryOut(service=c.service, mem_bytes=c.mem_bytes)
                for c in gateway.container_memory()
            ],
        )

    @authed.get("/usb")
    def usb() -> UsbResponse:
        # Enumeration only — this reads sysfs, so it needs no /dev/bus/usb
        # passthrough and no privileges. Naming a device is a read; opening
        # one is not.
        found = usb_devices.scan()
        out = [
            UsbDeviceOut(
                name=d.name,
                usb_id=d.usb_id,
                manufacturer=d.manufacturer,
                product=d.product,
                serial=d.serial,
                device_node=d.device_node,
                drivers=list(d.drivers),
                is_sdr=d.is_sdr,
                sdr_name=d.sdr_name,
            )
            for d in found.devices
        ]
        return UsbResponse(
            sysfs_readable=found.sysfs_readable,
            devices=out,
            sdrs=[d for d in out if d.is_sdr],
        )

    @authed.get("/processes")
    def processes() -> ProcessesResponse:
        # Per-process RSS via `docker top` — the breakdown /metrics' per-container
        # total can't show (one container can run several heavy processes).
        return ProcessesResponse(
            processes=[
                ProcessMemoryOut(
                    service=p.service,
                    pid=p.pid,
                    rss_bytes=p.rss_bytes,
                    command=p.command,
                )
                for p in gateway.container_processes()
            ]
        )

    @authed.post("/update", status_code=202)
    def start_update() -> UpdateStartResponse:
        try:
            return UpdateStartResponse(updater=gateway.start_update())
        except UpdateInProgressError:
            raise HTTPException(
                status_code=409, detail="update already running"
            ) from None

    @authed.get("/update/status")
    def update_status(
        tail: Annotated[int, Query(ge=1)] = 80,
    ) -> UpdateStatusResponse:
        status = gateway.update_status(min(tail, MAX_LOG_TAIL))
        return UpdateStatusResponse(
            state=status.state, exit_code=status.exit_code, log_tail=status.log_tail
        )

    @authed.post("/export", status_code=202)
    def start_export() -> OneshotStartResponse:
        try:
            return OneshotStartResponse(oneshot=gateway.start_export())
        except UpdateInProgressError:
            raise HTTPException(
                status_code=409, detail="another one-shot is running"
            ) from None

    @authed.get("/export/status")
    def export_status(
        tail: Annotated[int, Query(ge=1)] = 80,
    ) -> UpdateStatusResponse:
        status = gateway.oneshot_status("export", min(tail, MAX_LOG_TAIL))
        return UpdateStatusResponse(
            state=status.state, exit_code=status.exit_code, log_tail=status.log_tail
        )

    @authed.post("/import", status_code=202)
    def start_import(body: ImportStartRequest) -> OneshotStartResponse:
        if not IMPORT_ARCHIVE_RE.fullmatch(body.archive):
            raise HTTPException(status_code=400, detail="bad archive name")
        try:
            return OneshotStartResponse(oneshot=gateway.start_import(body.archive))
        except UpdateInProgressError:
            raise HTTPException(
                status_code=409, detail="another one-shot is running"
            ) from None

    @authed.get("/import/status")
    def import_status(
        tail: Annotated[int, Query(ge=1)] = 80,
    ) -> UpdateStatusResponse:
        status = gateway.oneshot_status("import", min(tail, MAX_LOG_TAIL))
        return UpdateStatusResponse(
            state=status.state, exit_code=status.exit_code, log_tail=status.log_tail
        )

    @authed.post("/reset", status_code=202)
    def start_reset() -> OneshotStartResponse:
        try:
            return OneshotStartResponse(oneshot=gateway.start_reset())
        except UpdateInProgressError:
            raise HTTPException(
                status_code=409, detail="another one-shot is running"
            ) from None

    @authed.get("/reset/status")
    def reset_status(
        tail: Annotated[int, Query(ge=1)] = 80,
    ) -> UpdateStatusResponse:
        status = gateway.oneshot_status("reset", min(tail, MAX_LOG_TAIL))
        return UpdateStatusResponse(
            state=status.state, exit_code=status.exit_code, log_tail=status.log_tail
        )

    @authed.post("/provision", status_code=202)
    def start_provision() -> OneshotStartResponse:
        # The PWA "Download" action: sync local-model weights on demand (no git pull,
        # no rebuild). Shares the one-shot mutual-exclusion guard, so it 409s during
        # an update/export/import/reset rather than racing over .env and the weights.
        try:
            return OneshotStartResponse(oneshot=gateway.start_provision())
        except UpdateInProgressError:
            raise HTTPException(
                status_code=409, detail="another one-shot is running"
            ) from None

    @authed.get("/provision/status")
    def provision_status(
        tail: Annotated[int, Query(ge=1)] = 80,
    ) -> UpdateStatusResponse:
        status = gateway.oneshot_status("provision", min(tail, MAX_LOG_TAIL))
        return UpdateStatusResponse(
            state=status.state, exit_code=status.exit_code, log_tail=status.log_tail
        )

    @authed.post("/rebuild", status_code=202)
    def start_rebuild(body: RebuildRequest) -> OneshotStartResponse:
        # Rebuild ONE service (compose build + up -d) — the PWA's per-service Rebuild
        # button, applying a code/Dockerfile change already on the box without a full
        # update. Validate against the live service set so only a real compose service
        # reaches the shell-quoted command; shares the one-shot mutual-exclusion guard.
        if body.service not in {c.service for c in gateway.list_containers()}:
            raise UnknownServiceError(body.service)
        try:
            return OneshotStartResponse(oneshot=gateway.start_rebuild(body.service))
        except UpdateInProgressError:
            raise HTTPException(
                status_code=409, detail="another one-shot is running"
            ) from None

    @authed.get("/rebuild/status")
    def rebuild_status(
        tail: Annotated[int, Query(ge=1)] = 80,
    ) -> UpdateStatusResponse:
        status = gateway.oneshot_status("rebuild", min(tail, MAX_LOG_TAIL))
        return UpdateStatusResponse(
            state=status.state, exit_code=status.exit_code, log_tail=status.log_tail
        )

    @authed.post("/refresh", status_code=202)
    def start_refresh(body: RebuildRequest) -> OneshotStartResponse:
        # Pull main and rebuild ONE service — `rebuild` never pulls and `update`
        # rebuilds the world, so iterating on one sidecar had no route between a stale
        # box and ten minutes. Takes NO ref (it resets to the tracked upstream), so a
        # token cannot choose the code it deploys, only ask for merged `main`. Same
        # live-service validation and one-shot mutual exclusion as rebuild.
        if body.service not in {c.service for c in gateway.list_containers()}:
            raise UnknownServiceError(body.service)
        try:
            return OneshotStartResponse(oneshot=gateway.start_refresh(body.service))
        except UpdateInProgressError:
            raise HTTPException(
                status_code=409, detail="another one-shot is running"
            ) from None

    @authed.get("/refresh/status")
    def refresh_status(
        tail: Annotated[int, Query(ge=1)] = 80,
    ) -> UpdateStatusResponse:
        status = gateway.oneshot_status("refresh", min(tail, MAX_LOG_TAIL))
        return UpdateStatusResponse(
            state=status.state, exit_code=status.exit_code, log_tail=status.log_tail
        )

    @authed.post("/perplexity", status_code=202)
    def start_perplexity(body: PerplexityRequest) -> OneshotStartResponse:
        # WikiText-2 perplexity inside the flash-next image (FLASH_NEXT_ENGINE_PLAN F2,
        # check 6) — a FIXED job: the binary, text file and flags are constants in the
        # gateway; the request picks only a validated model path and a bounded count.
        # The service must exist (it is the image the job runs), so a box that never
        # provisioned Flash-Next 404s instead of compose building one on the spot.
        if not PERPLEXITY_MODEL_RE.fullmatch(body.model_path):
            raise HTTPException(status_code=400, detail="bad model path")
        if FLASH_NEXT_SERVICE not in {c.service for c in gateway.list_containers()}:
            raise UnknownServiceError(FLASH_NEXT_SERVICE)
        try:
            return OneshotStartResponse(
                oneshot=gateway.start_perplexity(body.model_path, body.chunks)
            )
        except UpdateInProgressError:
            raise HTTPException(
                status_code=409, detail="another one-shot is running"
            ) from None

    @authed.get("/perplexity/status")
    def perplexity_status(
        tail: Annotated[int, Query(ge=1)] = 80,
    ) -> UpdateStatusResponse:
        status = gateway.oneshot_status("perplexity", min(tail, MAX_LOG_TAIL))
        return UpdateStatusResponse(
            state=status.state, exit_code=status.exit_code, log_tail=status.log_tail
        )

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
