"""`/api/home-network/*`: the house's DNS, what it blocks, and who asked.

docs/plans/HOME_DNS_PLAN.md (wave A). Two routers over one client (`jbrain.homenet`):

- `router` — the owner (`OwnerDep`): `GET /home-network` (the summary), `GET
  /home-network/queries` (the log, filterable to one device or to blocks only) and `POST
  /home-network/protection` (blocking on or off, optionally for a while). The screen that
  shows them is wave B, behind the GUI gate.
- `debug_router` — a debug token (`DebugDep`, mounted only with debug access on): the same
  two reads, so a session holding the token can check the house's DNS with no terminal
  (CLAUDE.md #10).

A box with no AdGuard, or one not up yet, answers 503 with why, never a 500.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from jbrain.api.deps import DebugDep, OwnerDep, SettingsDep
from jbrain.homenet import adguard

router = APIRouter(prefix="/home-network", tags=["home-network"])
debug_router = APIRouter(prefix="/debug/home-network")

# An IPv4 or IPv6 literal, or a client name AdGuard knows; quotes would end the strict
# match AdGuard's `search` takes, so they are refused rather than escaped.
Client = Annotated[str | None, Query(max_length=128, pattern=r'^[^"\s]+$')]
Limit = Annotated[int, Query(ge=1, le=500)]


async def _summary(request: Request, settings: Any) -> dict[str, Any]:
    dns = adguard.for_request(request, settings)
    status = await dns.call("GET", "/control/status")
    stats = await dns.call("GET", "/control/stats")
    clients = await dns.call("GET", "/control/clients")
    return adguard.summary(status, stats, clients)


async def _queries(
    request: Request,
    settings: Any,
    client: str | None,
    blocked_only: bool,
    limit: int,
    older_than: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if client:
        # Double quotes make AdGuard match the client exactly instead of as a substring,
        # so 192.168.1.2 does not also return 192.168.1.20.
        params["search"] = f'"{client}"'
    if blocked_only:
        params["response_status"] = "blocked"
    if older_than:
        params["older_than"] = older_than
    log = await adguard.for_request(request, settings).call(
        "GET", "/control/querylog", params=params
    )
    return adguard.query_rows(log)


@router.get("")
async def home_network(request: Request, settings: SettingsDep, _o: OwnerDep) -> dict[str, Any]:
    """Blocking on or off, queries and blocks over the stats window, the busiest devices
    and the most-blocked names."""
    return await _summary(request, settings)


@router.get("/queries")
async def home_network_queries(
    request: Request,
    settings: SettingsDep,
    _o: OwnerDep,
    client: Client = None,
    blocked: bool = False,
    limit: Limit = 100,
    older_than: Annotated[str | None, Query(max_length=40)] = None,
) -> dict[str, Any]:
    """The query log, newest first. `client` narrows it to one device, `blocked` to what
    was blocked, and `older_than` (the previous page's `oldest`) pages back."""
    return await _queries(request, settings, client, blocked, limit, older_than)


class ProtectionIn(BaseModel):
    enabled: bool
    # Turning blocking off for a while (a site that breaks behind a list) comes back on
    # by itself; AdGuard keeps the timer. 0 is "until turned back on".
    minutes: int = Field(default=0, ge=0, le=24 * 60)


@router.post("/protection")
async def home_network_protection(
    request: Request, settings: SettingsDep, _o: OwnerDep, body: ProtectionIn
) -> dict[str, Any]:
    payload: dict[str, Any] = {"enabled": body.enabled}
    if not body.enabled and body.minutes:
        payload["duration"] = body.minutes * 60_000
    dns = adguard.for_request(request, settings)
    await dns.call("POST", "/control/protection", json=payload)
    status = await dns.call("GET", "/control/status")
    return {"protection_enabled": bool(status.get("protection_enabled"))}


@debug_router.get("")
async def debug_home_network(
    request: Request, settings: SettingsDep, _p: DebugDep
) -> dict[str, Any]:
    request.state.debug_detail = "home network"
    return await _summary(request, settings)


@debug_router.get("/queries")
async def debug_home_network_queries(
    request: Request,
    settings: SettingsDep,
    _p: DebugDep,
    client: Client = None,
    blocked: bool = False,
    limit: Limit = 100,
    older_than: Annotated[str | None, Query(max_length=40)] = None,
) -> dict[str, Any]:
    request.state.debug_detail = f"home network queries {client or 'all'}"
    return await _queries(request, settings, client, blocked, limit, older_than)
