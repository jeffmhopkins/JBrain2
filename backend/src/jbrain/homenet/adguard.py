"""The api's one way to AdGuard Home, and the shapes the house's DNS is shown in.

AdGuard's UI and REST API listen on the docker bridge gateway (deploy/adguard/pin.awk),
so the LAN never reaches them; the api does, as `host.docker.internal`, with basic auth
as the `jbrain` user whose password is `adguard.token`. The token is read per call, so a
re-mint is picked up as soon as AdGuard restarts with its new hash.

AdGuard's answers are reshaped here rather than passed through: its stats encode every
top-N as a list of one-key objects, and its query log nests what matters three deep. The
routes and the screen get flat rows instead, and the log rows carry only what a person
reads (when, which device, which name, blocked or not, and by which list).
"""

from __future__ import annotations

from typing import Any, cast

import httpx
from fastapi import HTTPException, Request

from jbrain.api import internal_auth
from jbrain.storage import InternalSecrets

TIMEOUT_S = 10.0
USER = "jbrain"
# Tests swap in an httpx.MockTransport; None is the real network.
_transport: httpx.AsyncBaseTransport | None = None


class AdGuard:
    """AdGuard Home at `base_url`, as the `jbrain` user."""

    def __init__(self, secrets: InternalSecrets, base_url: str) -> None:
        self._secrets = secrets
        self._base = base_url.strip().rstrip("/")

    async def call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if not self._base:
            raise HTTPException(status_code=503, detail="No house DNS on this box.")
        token = self._secrets.read(internal_auth.ADGUARD)
        if not token:
            raise HTTPException(status_code=503, detail="adguard.token is not minted yet")
        try:
            async with httpx.AsyncClient(
                base_url=self._base,
                timeout=TIMEOUT_S,
                transport=_transport,
                auth=(USER, token.decode()),
            ) as client:
                resp = await client.request(method, path, json=json, params=params)
        except httpx.TransportError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"house DNS unreachable ({type(exc).__name__}) — is it running?",
            ) from exc
        if resp.status_code in (401, 403):
            # The token was re-minted and AdGuard has not restarted onto its new hash.
            raise HTTPException(status_code=502, detail="house DNS refused the api's password")
        if resp.status_code >= 400:
            raise HTTPException(status_code=502, detail=resp.text[:300])
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}


def for_request(request: Request, settings: Any) -> AdGuard:
    secrets = cast(InternalSecrets, request.app.state.internal_secrets)
    return AdGuard(secrets, str(settings.adguard_url or ""))


def _pairs(rows: Any) -> list[tuple[str, int]]:
    """AdGuard's top-N lists are `[{"key": count}, ...]`; this is `[(key, count), ...]`."""
    out: list[tuple[str, int]] = []
    for row in rows or []:
        if isinstance(row, dict):
            for key, count in cast(dict[str, Any], row).items():
                out.append((str(key), int(count or 0)))
    return out


def _names(clients: Any) -> dict[str, str]:
    """IP -> the best name AdGuard knows (a named client, else what rDNS/ARP found)."""
    names: dict[str, str] = {}
    body = cast(dict[str, Any], clients or {})
    for client in body.get("auto_clients") or []:
        ip, name = client.get("ip"), client.get("name")
        if ip and name:
            names[str(ip)] = str(name)
    # A client the owner named wins over a discovered one.
    for client in body.get("clients") or []:
        for ident in client.get("ids") or []:
            if client.get("name"):
                names[str(ident)] = str(client["name"])
    return names


def summary(status: Any, stats: Any, clients: Any) -> dict[str, Any]:
    """The house at a glance: is blocking on, how much was asked and blocked over the
    stats window, which devices ask the most, and which names were blocked most."""
    st = cast(dict[str, Any], status or {})
    sx = cast(dict[str, Any], stats or {})
    names = _names(clients)
    return {
        "running": bool(st.get("running")),
        "protection_enabled": bool(st.get("protection_enabled")),
        "dns_addresses": list(st.get("dns_addresses") or []),
        "version": str(st.get("version") or ""),
        "queries": int(sx.get("num_dns_queries") or 0),
        "blocked": int(sx.get("num_blocked_filtering") or 0),
        "devices": [
            {"ip": ip, "name": names.get(ip, ""), "queries": n}
            for ip, n in _pairs(sx.get("top_clients"))
        ],
        "top_blocked": [
            {"domain": d, "count": n} for d, n in _pairs(sx.get("top_blocked_domains"))
        ],
        "top_queried": [
            {"domain": d, "count": n} for d, n in _pairs(sx.get("top_queried_domains"))
        ],
    }


def _blocked(reason: str) -> bool:
    # AdGuard's reasons for an answer it did not pass through unchanged. Rewrites and
    # allow-listed names are not blocks.
    return reason.startswith("Filtered") and reason != "FilteredSafeSearch"


def query_rows(log: Any) -> dict[str, Any]:
    """The query log as flat rows, newest first, with the cursor for the next page."""
    body = cast(dict[str, Any], log or {})
    rows: list[dict[str, Any]] = []
    for entry in body.get("data") or []:
        question = cast(dict[str, Any], entry.get("question") or {})
        info = cast(dict[str, Any], entry.get("client_info") or {})
        reason = str(entry.get("reason") or "")
        rules = cast(list[dict[str, Any]], entry.get("rules") or [])
        rows.append(
            {
                "time": str(entry.get("time") or ""),
                "client": str(entry.get("client") or ""),
                "client_name": str(info.get("name") or ""),
                "domain": str(question.get("name") or ""),
                "type": str(question.get("type") or ""),
                "blocked": _blocked(reason),
                "reason": reason,
                "rule": str(rules[0].get("text") or "") if rules else "",
            }
        )
    return {"rows": rows, "oldest": str(body.get("oldest") or "")}
