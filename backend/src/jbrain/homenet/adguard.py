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
        if resp.status_code == 400:
            # AdGuard refusing the request itself (a client name already taken): the
            # owner's to read, not a fault in the box.
            raise HTTPException(status_code=400, detail=resp.text[:300])
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


def names_for(clients: Any) -> dict[str, str]:
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
    names = names_for(clients)
    return {
        "running": bool(st.get("running")),
        "protection_enabled": bool(st.get("protection_enabled")),
        # A timed pause counts down in AdGuard; the screen says when blocking comes back.
        "resumes_in_s": int(st.get("protection_disabled_duration") or 0) // 1000,
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


# The reasons AdGuard's own `response_status=blocked` filter returns: a list or the
# owner's rule, and blocked services. Counting more (safe browsing, parental) would put
# a name under "blocked" that the screen's Blocked log then leaves out.
_BLOCK_REASONS = frozenset({"FilteredBlackList", "FilteredBlockedService"})


def _blocked(reason: str) -> bool:
    return reason in _BLOCK_REASONS


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
                # -1 when AdGuard names no rule, so it is never read as the owner's (0).
                "list_id": int(rules[0].get("filter_list_id") or 0) if rules else -1,
            }
        )
    return {"rows": rows, "oldest": str(body.get("oldest") or "")}


def list_names(filtering: Any) -> dict[int, str]:
    """Blocklist id -> its name, so a blocked lookup can say which list caught it. The
    owner's own rules are list 0 in AdGuard's log."""
    body = cast(dict[str, Any], filtering or {})
    names = {0: "Your rules"}
    for f in body.get("filters") or []:
        names[int(f.get("id") or 0)] = str(f.get("name") or "")
    return names


def blocked_share(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {"recent": len(rows), "recent_blocked": sum(1 for r in rows if r["blocked"])}


def top_blocked(rows: list[dict[str, Any]], lists: dict[int, str]) -> list[dict[str, Any]]:
    """The names a device was refused, most-asked first, each with the list that caught it."""
    counts: dict[str, dict[str, Any]] = {}
    for r in rows:
        if not r["blocked"]:
            continue
        hit = counts.setdefault(
            r["domain"], {"domain": r["domain"], "count": 0, "list": lists.get(r["list_id"], "")}
        )
        hit["count"] += 1
    return sorted(counts.values(), key=lambda h: (-h["count"], h["domain"]))


# The owner's one-tap rules, in AdGuard's syntax: `||name^` blocks the name and every
# subdomain, `@@||name^` lets it through whatever a list says.
def block_rule(domain: str) -> str:
    return f"||{domain}^"


def allow_rule(domain: str) -> str:
    return f"@@||{domain}^"


def rules_view(user_rules: list[str]) -> dict[str, list[str]]:
    blocked: list[str] = []
    allowed: list[str] = []
    for rule in user_rules:
        if rule.startswith("@@||") and rule.endswith("^"):
            allowed.append(rule[4:-1])
        elif rule.startswith("||") and rule.endswith("^"):
            blocked.append(rule[2:-1])
    return {"blocked": blocked, "allowed": allowed}


def with_rule(user_rules: list[str], domain: str, action: str) -> list[str]:
    """`user_rules` with this screen's rule for `domain` set to `action` (block, allow or
    clear). Only the two exact rules this screen writes are touched; anything else the
    owner wrote by hand stays as it was."""
    mine = {block_rule(domain), allow_rule(domain)}
    kept = [r for r in user_rules if r not in mine]
    if action == "block":
        kept.append(block_rule(domain))
    elif action == "allow":
        kept.append(allow_rule(domain))
    return kept
