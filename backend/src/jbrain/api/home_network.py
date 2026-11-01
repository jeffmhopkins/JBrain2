"""`/api/home-network/*`: the house's DNS, what it blocks, and who asked.

docs/archive/HOME_DNS_PLAN.md. Two routers over one client (`jbrain.homenet`):

- `router` — the owner (`OwnerDep`), for the Home network screen (wave B, binding mock
  docs/mocks/home-network/a-devices.html): `GET /home-network` (the summary, each device
  with how much of its recent traffic was blocked), `GET /home-network/devices/{ip}` (one
  device: the names it was refused and which list caught each), `GET
  /home-network/queries` (the log), `POST /home-network/protection` (blocking on or off,
  optionally for a while), `GET`/`POST /home-network/rules` (the owner's block and allow
  rules, one name at a time) and `POST /home-network/devices/{ip}/name`.
- `debug_router` — a debug token (`DebugDep`, mounted only with debug access on): the same
  two reads, so a session holding the token can check the house's DNS with no terminal
  (CLAUDE.md #10).

A box with no AdGuard, or one not up yet, answers 503 with why, never a 500.
"""

from __future__ import annotations

import asyncio
import ipaddress
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, field_validator

from jbrain.api.deps import DebugDep, OwnerDep, SettingsDep
from jbrain.homenet import adguard

router = APIRouter(prefix="/home-network", tags=["home-network"])
debug_router = APIRouter(prefix="/debug/home-network")

# An IPv4 or IPv6 literal, or a client name AdGuard knows; quotes would end the strict
# match AdGuard's `search` takes, so they are refused rather than escaped.
Client = Annotated[str | None, Query(max_length=128, pattern=r'^[^"\s]+$')]
Limit = Annotated[int, Query(ge=1, le=500)]
DeviceIp = Annotated[str, Path(max_length=45)]

#: How many of a device's latest lookups its blocked share is read from. AdGuard counts
#: lookups per device but not blocks per device, so the share is a sample of the log.
SAMPLE = 200
#: The detail screen reads further back for the names a device was refused.
DETAIL_SAMPLE = 500
#: The summary samples the busiest devices only; each is one query-log read.
SAMPLED_DEVICES = 24


def _ip(value: str) -> str:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="not an IP address") from exc
    # An IPv6 zone (`fe80::1%eth0`) takes any text after `%`, which would reach the
    # query log's quoted search and AdGuard's client ids unchecked.
    if isinstance(addr, ipaddress.IPv6Address) and addr.scope_id:
        raise HTTPException(status_code=422, detail="not an IP address")
    return str(addr)


async def _device_rows(dns: adguard.AdGuard, ip: str, limit: int) -> list[dict[str, Any]]:
    log = await dns.call("GET", "/control/querylog", params={"search": f'"{ip}"', "limit": limit})
    return adguard.query_rows(log)["rows"]


async def _summary(request: Request, settings: Any) -> dict[str, Any]:
    dns = adguard.for_request(request, settings)
    status = await dns.call("GET", "/control/status")
    stats = await dns.call("GET", "/control/stats")
    clients = await dns.call("GET", "/control/clients")
    out = adguard.summary(status, stats, clients)
    sampled = out["devices"][:SAMPLED_DEVICES]
    # One slow or failed device read leaves that device unsampled; it must not turn the
    # whole screen into "the DNS is down" when AdGuard answered everything else.
    samples = await asyncio.gather(
        *(_device_rows(dns, d["ip"], SAMPLE) for d in sampled), return_exceptions=True
    )
    for device, rows in zip(sampled, samples, strict=True):
        if isinstance(rows, BaseException):
            device.update({"recent": 0, "recent_blocked": 0, "sampled": False})
        else:
            device.update({**adguard.blocked_share(rows), "sampled": True})
    for device in out["devices"][SAMPLED_DEVICES:]:
        device.update({"recent": 0, "recent_blocked": 0, "sampled": False})
    return out


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


@router.get("/devices/{ip}")
async def home_network_device(
    request: Request, settings: SettingsDep, _o: OwnerDep, ip: DeviceIp
) -> dict[str, Any]:
    """One device: its name, its week's lookups, and the names it was refused among its
    latest lookups, each with the list that caught it."""
    ip = _ip(ip)
    dns = adguard.for_request(request, settings)
    stats = await dns.call("GET", "/control/stats")
    clients = await dns.call("GET", "/control/clients")
    filtering = await dns.call("GET", "/control/filtering/status")
    rows = await _device_rows(dns, ip, DETAIL_SAMPLE)
    known = adguard.summary({}, stats, clients)
    device = next((d for d in known["devices"] if d["ip"] == ip), None)
    named = adguard.names_for(clients)
    return {
        "ip": ip,
        "name": named.get(ip, ""),
        "queries": device["queries"] if device else 0,
        **adguard.blocked_share(rows),
        "top_blocked": adguard.top_blocked(rows, adguard.list_names(filtering)),
    }


async def _user_rules(dns: adguard.AdGuard) -> list[str]:
    status = await dns.call("GET", "/control/filtering/status")
    return [str(r) for r in status.get("user_rules") or [] if isinstance(r, str)]


@router.get("/rules")
async def home_network_rules(
    request: Request, settings: SettingsDep, _o: OwnerDep
) -> dict[str, list[str]]:
    """The names the owner blocked or allowed from this screen."""
    return adguard.rules_view(await _user_rules(adguard.for_request(request, settings)))


_DOMAIN = r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$"


class RuleIn(BaseModel):
    domain: str = Field(max_length=253, pattern=_DOMAIN)
    # `clear` drops this screen's rule and leaves the name to the lists.
    action: Literal["block", "allow", "clear"]

    @field_validator("domain", mode="before")
    @classmethod
    def _lower(cls, v: Any) -> Any:
        return v.strip().lower().rstrip(".") if isinstance(v, str) else v


@router.post("/rules")
async def home_network_set_rule(
    request: Request, settings: SettingsDep, _o: OwnerDep, body: RuleIn
) -> dict[str, list[str]]:
    """Block or allow one name for every device, or clear the rule. AdGuard takes the
    whole list at once, so this reads it, changes the one rule, and writes it back."""
    dns = adguard.for_request(request, settings)
    rules = adguard.with_rule(await _user_rules(dns), body.domain, body.action)
    await dns.call("POST", "/control/filtering/set_rules", json={"rules": rules})
    return adguard.rules_view(rules)


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


@router.post("/devices/{ip}/name")
async def home_network_name_device(
    request: Request, settings: SettingsDep, _o: OwnerDep, ip: DeviceIp, body: NameIn
) -> dict[str, str]:
    """Name a device. AdGuard keeps it as a persistent client, so the name sticks across
    restarts and shows in its own log; a device it already knows by name is renamed."""
    ip = _ip(ip)
    dns = adguard.for_request(request, settings)
    clients = await dns.call("GET", "/control/clients")
    existing = next(
        (c for c in clients.get("clients") or [] if ip in (c.get("ids") or []) and c.get("name")),
        None,
    )
    if existing is not None:
        await dns.call(
            "POST",
            "/control/clients/update",
            json={"name": existing["name"], "data": {**existing, "name": body.name}},
        )
    else:
        await dns.call(
            "POST",
            "/control/clients/add",
            json={
                "name": body.name,
                "ids": [ip],
                "use_global_settings": True,
                "use_global_blocked_services": True,
                "filtering_enabled": True,
            },
        )
    return {"ip": ip, "name": body.name}


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
