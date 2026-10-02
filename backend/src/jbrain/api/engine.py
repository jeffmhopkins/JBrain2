"""The owner-operable local engine switch: `GET`/`POST /api/settings/llm/engine`.

Owner auth (the PWA session), not the debug token. The debug console's `/api/debug/llm/engine`
routes are thin wrappers over the SAME two functions here (`engine_state`, `begin_switch`), so
there is exactly one switching orchestration (jbrain.llm.engine_switch) whichever surface
started it. docs/plans/FLASH_NEXT_ENGINE_PLAN.md F3a; the operator view is in
docs/runbooks/STRIX_HALO_SETUP.md.
"""

from __future__ import annotations

import contextlib
import time
from datetime import UTC
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from jbrain.api.deps import OwnerDep
from jbrain.api.notes import ctx_for
from jbrain.host_metrics import read_memory_gb
from jbrain.llm import drain, local_catalog
from jbrain.llm import engine as engines
from jbrain.llm.engine_switch import (
    EngineSwitcher,
    HoldRefused,
    SupervisorError,
    SwitchDeps,
    SwitchRefused,
)
from jbrain.workflow.scheduler import quiet_window_guard

router = APIRouter()

# Not provisioned: the supervisor has no container for the service.
MISSING = "missing"


class EngineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: engines.Engine
    # Switch even inside the nightly window or while a workflow run executes. Never overrides
    # a supervisor one-shot or a switch already in flight — those are not judgement calls.
    force: bool = False


class EngineServiceOut(BaseModel):
    service: str
    # Docker's container state, or "missing" when the service was never provisioned.
    state: str


class AdmissionOut(BaseModel):
    closed: bool
    reason: str | None = None
    # Epoch seconds after which the closure lapses on its own.
    until: float | None = None


class EngineMemoryOut(BaseModel):
    """GB; any field null when unreadable. GTT is the iGPU's pool — what an engine holds."""

    gtt_used_gb: float | None = None
    gtt_total_gb: float | None = None
    gtt_free_gb: float | None = None
    host_total_gb: float | None = None
    host_used_gb: float | None = None


class SmokeOut(BaseModel):
    probe: str
    ok: bool
    detail: str


class SwitchStageOut(BaseModel):
    stage: str
    at: str


class SwitchStatusOut(BaseModel):
    """One switch. `stage` walks draining → stopping → starting → loading → smoke and ends at
    `done`, `rolled_back` (the previous engine was put back) or `failed` (it could not be),
    with `reason` saying why for the last two."""

    id: str
    source: str
    target: engines.Engine
    previous: engines.Engine
    force: bool
    stage: str
    reason: str | None
    started_at: str
    updated_at: str
    ended_at: str | None
    stages: list[SwitchStageOut]
    # The served model the switch loaded and smoke-tested, when there was one.
    model: str | None
    smoke: list[SmokeOut]
    notes: list[str]


class EngineStateOut(BaseModel):
    # The owner's choice — what the update one-shot TRIES to bring up.
    desired: engines.Engine
    # The engine recorded as actually started — what every load, list and re-stamp follows.
    effective: engines.Engine
    services: dict[str, EngineServiceOut]
    # Engines whose container is actually up. Exactly `[effective]` on a healthy box.
    running: list[engines.Engine]
    consistent: bool
    # Whether each engine has weights installed to serve (Flash-Next: its one model).
    installed: dict[str, bool]
    # The supervisor one-shot in flight (update, refresh, perplexity, …) — a switch waits.
    oneshot: str | None
    perplexity_running: bool
    # A switch is running in this api process right now.
    switching: bool
    admission: AdmissionOut
    memory: EngineMemoryOut
    # Why a switch would be refused right now without `force` (a workflow run executing, the
    # nightly window), or null.
    guard: str | None
    # The in-flight switch, else the last one.
    switch: SwitchStatusOut | None
    # When the effective engine last became effective (ISO), wherever that was written — a
    # switch, or the update's deploy/local-engine.sh. Null before the first record.
    effective_since: str | None = None
    # Why the engine serving is not the one wanted, as recorded by whatever started it (a
    # deploy fallback: "the Flash-Next engine did not start on the last update"; a failed
    # switch). Only while desired != effective; null when they agree.
    fallback_reason: str | None = None
    # llama-server's own generation throughput (tokens/s, `llamacpp:predicted_tokens_seconds`
    # on /metrics) for the effective engine's resident model — its average since that model
    # loaded, read from the gateway. Null when nothing is resident or the read fails; never
    # estimated.
    decode_tps: float | None = None
    decode_model: str | None = None


_TPS_METRIC = "llamacpp:predicted_tokens_seconds"


def parse_decode_tps(metrics: str) -> float | None:
    """The generation-throughput gauge out of llama-server's Prometheus text, or None."""
    for line in metrics.splitlines():
        if line.startswith(_TPS_METRIC):
            parts = line.split()
            with contextlib.suppress(ValueError, IndexError):
                value = float(parts[-1])
                return round(value, 1) if value > 0 else None
    return None


async def _decode_tps(request: Request) -> tuple[float | None, str | None]:
    """The first ready resident model's throughput gauge. Reads only what is RESIDENT (the
    gateway client refuses a cold model's /metrics rather than loading it)."""
    gateway = getattr(request.app.state, "local_gateway", None)
    if gateway is None:
        return None, None
    try:
        states = await gateway.running_states()
        for served, state in sorted((states or {}).items()):
            if state in ("", "ready"):
                return parse_decode_tps(await gateway.metrics(served)), served
    except Exception:  # noqa: BLE001 — a gauge, never a failure
        return None, None
    return None, None


class HttpSupervisor:
    """The supervisor's container API, as the switch needs it."""

    def __init__(self, client: Any, token: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}"}

    async def states(self) -> dict[str, str]:
        try:
            resp = await self._client.get("/status", headers=self._headers)
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise SupervisorError(str(exc)) from exc
        return {
            str(c["service"]): str(c.get("state", ""))
            for c in payload.get("containers", [])
            if isinstance(c, dict) and "service" in c
        }

    async def perplexity_running(self) -> bool:
        try:
            resp = await self._client.get(
                "/perplexity/status", params={"tail": 1}, headers=self._headers
            )
            # A supervisor that predates the job has never run one.
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            return cast(dict[str, Any], resp.json()).get("state") == "running"
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise SupervisorError(str(exc)) from exc

    async def oneshot(self) -> str | None:
        try:
            resp = await self._client.get("/oneshot", headers=self._headers)
            if resp.status_code == 404:
                # Predates the route: the perplexity read is the most it can say.
                return "perplexity" if await self.perplexity_running() else None
            resp.raise_for_status()
            running = cast(dict[str, Any], resp.json()).get("running")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise SupervisorError(str(exc)) from exc
        return str(running) if running else None

    async def toggle(self, action: str, service: str, switch_id: str | None = None) -> int:
        body: dict[str, str] = {"service": service}
        if switch_id is not None and action == "start":
            body["switch_id"] = switch_id
        try:
            resp = await self._client.post(f"/{action}", json=body, headers=self._headers)
        except (httpx.HTTPError, AttributeError) as exc:
            raise SupervisorError(str(exc)) from exc
        if resp.status_code not in (202, 404):
            raise SupervisorError(f"{action} {service}: HTTP {resp.status_code}")
        return int(resp.status_code)

    async def hold(self, switch_id: str, ttl_s: float) -> bool:
        try:
            resp = await self._client.post(
                "/engine-switch/hold",
                json={"id": switch_id, "ttl_s": ttl_s},
                headers=self._headers,
            )
        except (httpx.HTTPError, AttributeError) as exc:
            raise SupervisorError(str(exc)) from exc
        if resp.status_code == 404:
            return False  # a supervisor that predates the hold
        if resp.status_code == 409:
            detail = ""
            with contextlib.suppress(ValueError, AttributeError):
                detail = str(resp.json().get("detail", ""))
            raise HoldRefused(detail or "another engine operation holds the box")
        if resp.status_code >= 400:
            raise SupervisorError(f"engine-switch hold: HTTP {resp.status_code}")
        return True

    async def release(self, switch_id: str) -> None:
        with contextlib.suppress(httpx.HTTPError, AttributeError):
            await self._client.post(
                "/engine-switch/release", json={"id": switch_id}, headers=self._headers
            )


def refuse_while_switching(request: Request, what: str) -> None:
    """409 for an engine-affecting operation (an update, a refresh, an engine start or
    restart, jcode's power-on, a perplexity run) while an engine switch runs in this api.
    The supervisor's switch hold refuses the same operations for any caller; this is the
    api's own, immediate answer with the sentence the PWA shows."""
    existing = getattr(request.app.state, "engine_switcher", None)
    if isinstance(existing, EngineSwitcher) and existing.busy:
        raise HTTPException(
            status_code=409,
            detail=f"an engine switch is in progress; {what} once it has finished",
        )


def switcher(request: Request) -> EngineSwitcher:
    state = request.app.state
    existing = getattr(state, "engine_switcher", None)
    if existing is None:
        existing = EngineSwitcher()
        state.engine_switcher = existing
    return cast(EngineSwitcher, existing)


def switch_deps(request: Request, ctx: Any) -> SwitchDeps:
    state = request.app.state
    settings = state.settings
    supervisor = HttpSupervisor(
        getattr(state, "supervisor_client", None), getattr(settings, "supervisor_token", "")
    )
    maker = getattr(state, "session_maker", None)
    probe = getattr(state, "gpu_probe", None)
    router_ = getattr(state, "llm_router", None)

    async def _guard() -> str | None:
        if maker is None:
            return None
        zone = await state.settings_store.owner_timezone(ctx)
        return await quiet_window_guard(maker, tz=ZoneInfo(zone) if zone else UTC)

    async def _gtt_used() -> float | None:
        sample = await probe.sample() if probe is not None else None
        return sample.gtt_used_gb if sample is not None else None

    async def _primary() -> str | None:
        return None if router_ is None else await router_.primary_local_served_model()

    return SwitchDeps(
        supervisor=supervisor,
        gateway=getattr(state, "local_gateway", None),
        store=state.settings_store,
        ctx=ctx,
        local_models=list(getattr(settings, "local_models", []) or []),
        quiet_guard=_guard,
        memory_used_gb=_gtt_used,
        primary_model=_primary,
    )


async def _memory(request: Request) -> EngineMemoryOut:
    out = EngineMemoryOut()
    probe = getattr(request.app.state, "gpu_probe", None)
    if probe is not None:
        try:
            sample = await probe.sample()
        except Exception:  # noqa: BLE001 — a gauge, never a failure
            sample = None
        if sample is not None:
            out.gtt_used_gb = round(sample.gtt_used_gb, 2)
            out.gtt_total_gb = round(sample.gtt_total_gb, 2)
            out.gtt_free_gb = round(sample.gtt_free_gb, 2)
    host = read_memory_gb()
    if host is not None:
        out.host_total_gb, out.host_used_gb = round(host[0], 2), round(host[1], 2)
    return out


def _installed(local_models: list[str]) -> dict[str, bool]:
    out: dict[str, bool] = {}
    for engine in engines.ENGINES:
        out[engine] = any(
            engines.parse(m.engine) == engine for m in local_catalog.selected(local_models)
        )
    return out


async def engine_state(request: Request, ctx: Any) -> EngineStateOut:
    """Desired + effective engine, both services, memory, admission, the guard and the last
    switch. 502 when the supervisor cannot be read: every field here is about what runs."""
    deps = switch_deps(request, ctx)
    supervisor = cast(HttpSupervisor, deps.supervisor)
    try:
        states = await supervisor.states()
        oneshot = await supervisor.oneshot()
        perplexity = await supervisor.perplexity_running()
    except SupervisorError as exc:
        raise HTTPException(status_code=502, detail=f"supervisor unreachable: {exc}") from exc
    store = deps.store
    effective = await store.llm_local_engine_effective(ctx)
    services = {
        e: EngineServiceOut(
            service=engines.SERVICE[e], state=states.get(engines.SERVICE[e], MISSING)
        )
        for e in engines.ENGINES
    }
    running: list[engines.Engine] = [
        e for e in engines.ENGINES if engines.holds_memory(services[e].state)
    ]
    closure = drain.closure_from(
        await request.app.state.settings_store.llm_local_admission(ctx), time.time()
    )
    guard: str | None = None
    if deps.quiet_guard is not None:
        try:
            guard = await deps.quiet_guard()
        except Exception as exc:  # noqa: BLE001 — reported; the switch itself fails closed
            guard = f"could not check the nightly window or running workflows ({exc})"
    sw = switcher(request)
    last = await sw.status(deps)
    desired = await store.llm_local_engine(ctx)
    meta = await request.app.state.settings_store.llm_local_engine_effective_meta(ctx)
    since = meta.get("since")
    reason = meta.get("reason")
    tps, tps_model = await _decode_tps(request)
    return EngineStateOut(
        desired=desired,
        effective_since=since if isinstance(since, str) else None,
        fallback_reason=reason if isinstance(reason, str) and desired != effective else None,
        decode_tps=tps,
        decode_model=tps_model if tps is not None else None,
        effective=effective,
        services=services,
        running=running,
        consistent=running == [effective],
        installed=_installed(list(deps.local_models)),
        oneshot=oneshot,
        perplexity_running=perplexity,
        switching=sw.busy,
        admission=AdmissionOut(
            closed=closure is not None,
            reason=closure.reason if closure else None,
            until=closure.until if closure else None,
        ),
        memory=await _memory(request),
        guard=guard,
        switch=SwitchStatusOut.model_validate(last) if last is not None else None,
    )


async def begin_switch(
    request: Request, body: EngineIn, *, source: str, ctx: Any
) -> SwitchStatusOut:
    """Start (or refuse) a switch — the one entry both surfaces use."""
    try:
        status = await switcher(request).begin(
            switch_deps(request, ctx), body.engine, source=source, force=body.force
        )
    except SwitchRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return SwitchStatusOut.model_validate(status)


def cancel_switch(request: Request) -> SwitchStatusOut:
    """Cancel the in-flight switch while it is draining — the one entry both surfaces use."""
    try:
        status = switcher(request).cancel()
    except SwitchRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return SwitchStatusOut.model_validate(status)


@router.get("/settings/llm/engine")
async def read_engine(request: Request, owner: OwnerDep) -> EngineStateOut:
    """Which local engine is wanted and which serves, both containers' state, whether each is
    installed, a busy one-shot, device memory, local admission, the nightly guard, and the
    in-flight or last switch (stage, outcome, reason, smoke results)."""
    return await engine_state(request, ctx_for(owner))


@router.post("/settings/llm/engine", status_code=202)
async def switch_engine(body: EngineIn, request: Request, owner: OwnerDep) -> SwitchStatusOut:
    """Switch the local engine: drain → stop → start → load → smoke → done, with automatic
    rollback to the previous engine on any failure. 202 with the switch's status; poll
    `GET /settings/llm/engine` for its stage. Refused (nothing touched) with 409 while a
    switch or a supervisor one-shot runs, inside the nightly window or during a workflow run
    (unless `force`), when the target is not provisioned or its weights are not installed;
    502 when the supervisor is unreachable. Switching to the engine already serving alone
    re-persists the setting and answers `done`."""
    return await begin_switch(request, body, source="owner", ctx=ctx_for(owner))


@router.post("/settings/llm/engine/cancel", status_code=202)
async def cancel_engine_switch(request: Request, _owner: OwnerDep) -> SwitchStatusOut:
    """Cancel the switch in flight — only while it is DRAINING, before anything has been
    unloaded or stopped. 202 with the status (poll for `cancelled`; admission reopens and a
    box event is recorded); 409 at any other stage or with no switch running."""
    return cancel_switch(request)
