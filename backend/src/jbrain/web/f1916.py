"""The 1f916.ai client — jerv's forum citizenship (docs/plans/F1916_CITIZENSHIP_PLAN.md).

1f916.ai is a public forum whose members are AI agents. This client is the ONE
place any byte travels to it: a pinned base host (never model-supplied), one typed
method per whitelisted route, redirects off, bounded JSON bodies, and the bearer
secret injected here from a live settings provider — the tool layer passes only
typed fields (a post id, a query), never a URL and never a credential.

The route whitelist is structural (threat model §2.1): key-operation routes
(`/api/keys`, `/api/keys/revoke`), every economic/treasury/payout route, and the
site's MCP doors have no method here and cannot be reached through this module.
The two POSTs that exist — register and rotate — are credential lifecycle, called
ONLY by the owner-only PWA settings routes, never by an agent tool handler; W1
ships zero agent-reachable writes.

Every response opens with the server's own clock (`now`/`now_utc`); cap accounting
(a W2 concern) must use that, never the local clock. The platform's register
response is deliberately undocumented ("read the door"), so the secret is found by
scanning the response for the documented `1f916_sk_` prefix rather than trusting a
field name — a wrong guess here would orphan a just-minted citizen.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx
import structlog

log = structlog.get_logger()

DEFAULT_BASE_URL = "https://1f916.ai"
_TIMEOUT = 20.0
# The forum's JSON pages are small (feeds cap at 100 rows, threads at 1000 comments);
# anything past this is out of contract and refused rather than buffered.
_MAX_BODY_BYTES = 1_048_576
_UA = "jbrain-jerv/1.0"
# The documented shape of a citizen bearer secret — used to FIND the secret in the
# register/rotate responses (their field names are undocumented) and by the tool-layer
# scrubber to redact any occurrence from model-facing text.
SECRET_PREFIX = "1f916_sk_"


class F1916Error(RuntimeError):
    """A 1f916 request could not be completed. `status` carries the upstream HTTP
    status when there was one (the platform's `{"error": …}` prose rides in the
    message — it is written to be read, and often contains the fix)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class F1916Creds:
    """The live citizen state, read from owner-only app.settings per call — never
    cached in the client, so a rotation or toggle takes effect immediately."""

    enabled: bool
    handle: str
    secret: str


# Kept as a callable, not a store handle, so the client stays DB-free (the Tavily
# settings-provider precedent in web/fetch.py).
F1916CredsProvider = Callable[[], Awaitable[F1916Creds]]


def find_secret(body: object) -> str:
    """The citizen secret in a register/rotate response: the first string value
    anywhere in the JSON carrying the documented `1f916_sk_` prefix. Field names are
    undocumented by design, so scanning by shape is the robust read."""
    if isinstance(body, str):
        return body if body.startswith(SECRET_PREFIX) else ""
    if isinstance(body, dict):
        for value in body.values():
            if found := find_secret(value):
                return found
    if isinstance(body, list):
        for value in body:
            if found := find_secret(value):
                return found
    return ""


class F1916Client:
    """Typed access to the whitelisted 1f916 surface. `transport` is injectable so
    tests run with no network (DEVELOPMENT.md "no network in tests")."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        creds: F1916CredsProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._creds = creds
        self._transport = transport

    # --- keyless reads ----------------------------------------------------------

    async def front(self, *, limit: int = 20, tag: str = "") -> dict:
        params: dict[str, str | int] = {"limit": max(1, min(limit, 100))}
        if tag.strip():
            params["tag"] = tag.strip()
        return await self._get("/api/front", params)

    async def new(self, *, before: str = "", limit: int = 30) -> dict:
        params: dict[str, str | int] = {"limit": max(1, min(limit, 100))}
        if before.strip():
            params["before"] = before.strip()
        return await self._get("/api/new", params)

    async def post(self, post_id: int, *, since: int = 0) -> dict:
        params: dict[str, str | int] = {}
        if since > 0:
            params["since"] = since
        return await self._get(f"/api/post/{int(post_id)}", params)

    async def search(self, query: str, *, limit: int = 20) -> dict:
        return await self._get("/api/search", {"q": query.strip(), "limit": max(1, min(limit, 50))})

    async def citizen(self, handle: str) -> dict:
        cleaned = handle.strip().lstrip("@")
        if not cleaned:
            raise F1916Error("a citizen handle is required")
        return await self._get(f"/api/citizen/{quote(cleaned, safe='')}")

    async def changes(self, *, since: int) -> dict:
        return await self._get("/api/changes", {"since": max(0, since)})

    async def events(self, *, kind: str = "", since: int | None = None) -> dict:
        params: dict[str, str | int] = {}
        if kind.strip():
            params["kind"] = kind.strip()
        if since is not None:
            params["since"] = max(0, since)
        return await self._get("/api/events", params)

    # --- keyed reads (bearer injected here, from the live provider) -------------

    async def pulse(self) -> dict:
        # Authenticated when a secret exists (the `you` block), keyless otherwise.
        secret = (await self._live_creds()).secret if self._creds else ""
        return await self._get("/api/pulse", secret=secret)

    async def me(self) -> dict:
        return await self._get("/api/me", secret=await self._require_secret())

    # --- credential lifecycle (owner-only PWA routes; never an agent tool) ------

    async def register(
        self, *, handle: str, model: str, public_key: str = "", signature: str = ""
    ) -> dict:
        """POST /api/register — mint a citizen, optionally binding an Ed25519 key in
        the same call (an invalid key refuses the WHOLE registration). Returns the
        platform's full response; the caller extracts and stores the one-time secret
        (`find_secret`) before doing anything else with it."""
        body: dict[str, str] = {"handle": handle.strip(), "model": model.strip()}
        if public_key:
            body["public_key"] = public_key
            body["signature"] = signature
        return await self._post("/api/register", body)

    async def rotate(self) -> dict:
        """POST /api/rotate — the old secret dies, identity and karma stay. Returns
        the platform's full response carrying the new secret."""
        return await self._post("/api/rotate", {}, secret=await self._require_secret())

    # --- plumbing ---------------------------------------------------------------

    async def _live_creds(self) -> F1916Creds:
        if self._creds is None:
            return F1916Creds(enabled=False, handle="", secret="")
        return await self._creds()

    async def _require_secret(self) -> str:
        secret = (await self._live_creds()).secret
        if not secret:
            raise F1916Error("no 1f916 citizen is registered on this box")
        return secret

    async def _get(
        self, path: str, params: dict[str, str | int] | None = None, *, secret: str = ""
    ) -> dict:
        return await self._request("GET", path, params=params, secret=secret)

    async def _post(self, path: str, body: dict, *, secret: str = "") -> dict:
        return await self._request("POST", path, body=body, secret=secret)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict | None = None,
        secret: str = "",
    ) -> dict:
        url = f"{self._base_url}{path}"
        headers = {"User-Agent": _UA, "Accept": "application/json"}
        if secret:
            # The one place the credential is attached — server-side, never logged.
            headers["Authorization"] = f"Bearer {secret}"
        try:
            async with (
                httpx.AsyncClient(
                    timeout=_TIMEOUT,
                    transport=self._transport,
                    # Redirects stay off (threat model §2.1): a 3xx from a pinned host
                    # is out of contract and must never carry the bearer elsewhere.
                    follow_redirects=False,
                ) as client,
                client.stream(method, url, params=params, json=body, headers=headers) as resp,
                # (json=None sends no body; httpx omits the header for GETs.)
            ):
                raw, truncated = await _read_capped(resp)
        except httpx.HTTPError as exc:
            log.warning("f1916.request_failed", path=path, error=repr(exc))
            raise F1916Error("1f916.ai is unreachable right now") from exc
        if truncated:
            log.warning("f1916.body_too_large", path=path)
            raise F1916Error("1f916.ai returned an oversized response; refusing to read it")
        if resp.status_code >= 300:
            # A redirect or error never gets its body trusted: parse best-effort for the
            # platform's error prose (written for an LLM, often names the fix) and raise
            # with the real status either way.
            detail = ""
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
                if isinstance(parsed, dict):
                    detail = str(parsed.get("error") or "").strip()
            except ValueError:
                pass
            log.warning("f1916.http_error", path=path, status=resp.status_code)
            raise F1916Error(
                detail or f"1f916.ai answered HTTP {resp.status_code}",
                status=resp.status_code,
            )
        parsed = _parse_json(raw, path)
        if not isinstance(parsed, dict):
            raise F1916Error("1f916.ai returned an unexpected response shape")
        return parsed


async def _read_capped(resp: httpx.Response) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            return b"", True
    return b"".join(chunks), False


def _parse_json(raw: bytes, path: str) -> object:
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError as exc:
        log.warning("f1916.non_json", path=path, error=repr(exc))
        raise F1916Error("1f916.ai returned a non-JSON response") from exc
