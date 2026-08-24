"""Pinned typed client for the Moltbook API (docs/plans/JMOLT_PLAN.md).

Moltbook (https://moltbook.com) is a social network whose members are AI agents. jmolt
is a sandboxed persona that reads and — later — writes there. This is the ONLY path
any byte reaches moltbook.com: a pinned base URL (never model-supplied), typed methods
per whitelisted route, redirects off, a hard response-size cap, and response-list
truncation. Key-op / owner-email / moderation / submolt-create / delete routes are
structurally absent — the client cannot be asked to hit them (JMOLT_PLAN §2 M-list).

Security invariants wired here, not left to the persona (the model runs on a local
120B and will breach any textual rule — JMOLT_PLAN §2):

- **M4 — local-clock cap accounting.** A client-side `RateLedger` counts reads/writes
  against the *local monotonic clock*; the platform's own rate-limit headers and any
  `now_utc` it returns are advisory only. A compromised platform cannot rewind our
  clock to make us over-post into a suspension.
- **M12 — response truncation.** Every response body is size-capped on read and every
  returned list is length-capped and each item body truncated, so one 256 KB reply
  can't smuggle hundreds of injection payloads or blow the token ceiling.
- **M17/M18 — secret custody.** The bearer key is injected as `Authorization` by the
  client from a live provider callable; it is NEVER logged (we log route + status
  only) and `scrub_secret()` redacts any `moltbook_`-shaped token from any string
  before it can reach a transcript or the PWA. `register()`/`rotate()` consume the
  platform's secret from the HTTP response and hand it straight to the store — the
  agent loop never sees them.

Modeled on `web/grokipedia.py` (pinned client) and reusing `web/fetch.py`'s SSRF
guard and streamed size cap.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from jbrain.web.fetch import BROWSER_HEADERS, _read_capped, guard_public_host

log = structlog.get_logger()

# The one pinned origin. Model input never supplies a URL; only path + typed params.
BASE_URL = "https://www.moltbook.com/api/v1"
_TIMEOUT = 20.0
_MAX_BYTES = 512_000  # hard body cap — Moltbook's own JSON cap is ~256 KB; this is slack.

# M12 truncation defaults — a feed/thread/search reply cannot deliver an unbounded
# number of injection payloads. Callers may pass tighter caps.
_MAX_LIST_ITEMS = 25
_MAX_ITEM_CHARS = 2_000

# A Moltbook bearer key looks like `moltbook_<base62…>`; the scrubber redacts it and
# any verification code of the same family, defense-in-depth on top of never logging it.
_SECRET_RE = re.compile(r"moltbook_[A-Za-z0-9_\-]{6,}")


def scrub_secret(text: str, *extra: str) -> str:
    """Redact any `moltbook_`-shaped token (and any explicitly-passed secret) from a
    string before it can land in a transcript, log line, tool output, or the PWA.

    M17/M18: the key is never supposed to be in model-facing text at all; this is the
    belt-and-braces backstop that holds even if a handler echoes a raw response.
    """
    scrubbed = _SECRET_RE.sub("moltbook_[redacted]", text)
    for secret in extra:
        if secret:
            scrubbed = scrubbed.replace(secret, "[redacted]")
    return scrubbed


class MoltbookError(RuntimeError):
    """A Moltbook API failure. `status` carries the upstream HTTP status when the error
    is an HTTP error (so callers can special-case 401/403/410/429), else None.
    `retry_after_s` carries a 429's Retry-After when present (advisory)."""

    def __init__(
        self, message: str, *, status: int | None = None, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


@dataclass
class RateLedger:
    """M4 — client-side rate accounting on the LOCAL monotonic clock.

    A sliding-window counter per kind ("read"/"write"). The platform's rate-limit
    headers and `now_utc` are advisory; THIS is what decides whether we spend a call,
    so a lying or compromised platform clock cannot trick jmolt into over-posting past
    a suspension line. Not frozen — the windows mutate in place; one ledger per client.
    """

    read_per_min: int = 55  # a touch under the platform's 60 — headroom, local-clock.
    write_per_min: int = 25  # under 30.
    clock: Callable[[], float] = time.monotonic
    _reads: deque[float] = field(default_factory=deque)
    _writes: deque[float] = field(default_factory=deque)

    def _window(self, kind: str) -> deque[float]:
        return self._writes if kind == "write" else self._reads

    def _limit(self, kind: str) -> int:
        return self.write_per_min if kind == "write" else self.read_per_min

    def _evict(self, dq: deque[float], now: float) -> None:
        while dq and now - dq[0] >= 60.0:
            dq.popleft()

    def allow(self, kind: str) -> bool:
        """True if a call of this kind fits the local-clock window right now."""
        now = self.clock()
        dq = self._window(kind)
        self._evict(dq, now)
        return len(dq) < self._limit(kind)

    def charge(self, kind: str) -> None:
        """Record that a call of this kind was spent, stamped on the local clock."""
        dq = self._window(kind)
        dq.append(self.clock())


# A provider callable returns (key, handle) live from the settings store; "" key means
# unregistered — the client refuses to call rather than sending a blank Authorization.
KeyProvider = Callable[[], Awaitable[tuple[str, str]]]


@dataclass(frozen=True)
class RegisterResult:
    """The registration handshake outcome. The secret is deliberately absent from this
    object — register() hands it straight to the store via a sink callback and returns
    only the non-secret claim material the owner needs (M2/M17: no secret in any value
    that could be logged or surfaced)."""

    claim_url: str
    verification_code: str
    handle: str


class MoltbookClient:
    """Typed Moltbook API client. Base URL pinned in the constructor; transport
    injectable for tests. Reads are wired in W1; writes arrive in W3 through additional
    whitelisted methods on this same client (never a general POST surface)."""

    def __init__(
        self,
        key_provider: KeyProvider,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        ledger: RateLedger | None = None,
        max_list_items: int = _MAX_LIST_ITEMS,
        max_item_chars: int = _MAX_ITEM_CHARS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._key_provider = key_provider
        self._transport = transport
        self._ledger = ledger or RateLedger()
        self._max_list_items = max_list_items
        self._max_item_chars = max_item_chars

    # ---- request plumbing -------------------------------------------------

    async def _auth_header(self) -> tuple[dict[str, str], str]:
        """Build the Authorization header from the live provider. Returns (headers,
        key) — the key is returned only so the caller can pass it to scrub_secret, never
        to log it."""
        key, _handle = await self._key_provider()
        if not key:
            raise MoltbookError("jmolt is not registered on Moltbook yet (no API key set).")
        headers = dict(BROWSER_HEADERS)
        headers["Authorization"] = f"Bearer {key}"
        headers["Accept"] = "application/json"
        return headers, key

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        authed: bool = True,
    ) -> Any:
        """Issue one request to a whitelisted path and return parsed JSON. Redirects
        OFF (a 3xx is an error — a compromised platform cannot bounce us to another
        host); body size-capped; the key never logged. M4: on a 429 we raise with the
        advisory Retry-After but our own RateLedger is the real gate."""
        if not path.startswith("/"):
            raise MoltbookError(f"refusing non-rooted path {path!r}")
        url = f"{self._base_url}{path}"
        guard_public_host(url)  # the base is pinned, but re-guard defensively.
        headers: dict[str, str] = dict(BROWSER_HEADERS)
        key = ""
        if authed:
            headers, key = await self._auth_header()
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport, follow_redirects=False
            ) as client:
                resp = await client.send(
                    client.build_request(
                        method, url, params=params, json=json_body, headers=headers
                    ),
                    stream=True,
                )
                if resp.is_redirect:
                    await resp.aclose()
                    raise MoltbookError(
                        "Moltbook redirected the request — refusing", status=resp.status_code
                    )
                body, _truncated = await _read_capped(resp, max_bytes=_MAX_BYTES)
                status = resp.status_code
        except httpx.HTTPError as exc:
            # Log route + status shape only — NEVER the key or the body.
            log.warning("moltbook.request_failed", path=path, error=type(exc).__name__)
            raise MoltbookError("Moltbook is unreachable right now", status=None) from exc

        if status >= 400:
            retry_after = _retry_after(resp)
            log.warning("moltbook.http_error", path=path, status=status)
            raise MoltbookError(_error_message(status), status=status, retry_after_s=retry_after)
        text = body.decode("utf-8", errors="replace")
        try:
            return httpx.Response(200, text=text).json()
        except Exception as exc:  # noqa: BLE001 — any parse failure is one class to us
            log.warning("moltbook.non_json", path=path)
            raise MoltbookError("Moltbook returned an unexpected (non-JSON) response") from exc

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """A rate-ledgered GET. M4: refuse locally before spending a read we don't have."""
        if not self._ledger.allow("read"):
            raise MoltbookError("local read rate window is full — backing off", status=429)
        self._ledger.charge("read")
        return await self._request("GET", path, params=params)

    # ---- registration (owner-only, never the agent loop) ------------------

    async def register(
        self,
        name: str,
        description: str,
        *,
        secret_sink: Callable[[str], Awaitable[None]],
    ) -> RegisterResult:
        """Register a new agent account. Owner-only flow (an API route, not a tool):
        the platform returns the API key in the HTTP response; we hand it straight to
        `secret_sink` (which stores it) and return only the non-secret claim material.
        The secret appears in no return value, log line, or transcript (M2/M17)."""
        data = await self._request(
            "POST",
            "/agents/register",
            json_body={"name": name, "description": description},
            authed=False,
        )
        api_key = str(data.get("api_key", ""))
        if not api_key:
            raise MoltbookError("Moltbook registration returned no api_key")
        await secret_sink(api_key)
        return RegisterResult(
            claim_url=str(data.get("claim_url", "")),
            verification_code=str(data.get("verification_code", "")),
            handle=name,
        )

    async def status(self) -> str:
        """Claim status of the current account: 'pending_claim' | 'claimed' | ...."""
        data = await self._get("/agents/status")
        return str(data.get("status", "unknown"))

    # ---- reads (W1) -------------------------------------------------------

    async def home(self) -> dict[str, Any]:
        """The /home dashboard. NOTE: the caller (the prologue assembler) must strip the
        platform's imperative channels — `suggested_actions`, announcements, `what_to_do_next`
        — before this reaches the model (M3). This method returns the raw dict; stripping is
        `strip_home_imperatives()` below so it can be unit-tested in isolation."""
        return dict(await self._get("/home"))

    async def feed(
        self, *, sort: str = "hot", limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sort": _clean_sort(sort)}
        params["limit"] = self._page_limit(limit)
        if cursor:
            params["cursor"] = str(cursor)
        return self._cap_feed(await self._get("/feed", params))

    async def submolt_feed(
        self, name: str, *, sort: str = "hot", limit: int | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"submolt": _slug(name), "sort": _clean_sort(sort)}
        params["limit"] = self._page_limit(limit)
        if cursor:
            params["cursor"] = str(cursor)
        return self._cap_feed(await self._get("/posts", params))

    async def post(self, post_id: str) -> dict[str, Any]:
        return self._cap_item(dict(await self._get(f"/posts/{_id(post_id)}")))

    async def comments(
        self,
        post_id: str,
        *,
        sort: str = "best",
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"sort": _clean_sort(sort, {"best", "new", "old"})}
        params["limit"] = self._page_limit(limit)
        if cursor:
            params["cursor"] = str(cursor)
        data = dict(await self._get(f"/posts/{_id(post_id)}/comments", params))
        if isinstance(data.get("comments"), list):
            data["comments"] = [
                self._cap_item(dict(c)) for c in data["comments"][: self._max_list_items]
            ]
        return data

    async def search(
        self, query: str, *, kind: str = "all", limit: int | None = None
    ) -> dict[str, Any]:
        params = {
            "q": str(query)[:500],
            "type": _clean_sort(kind, {"posts", "comments", "all"}),
            "limit": self._page_limit(limit),
        }
        data = dict(await self._get("/search", params))
        if isinstance(data.get("results"), list):
            data["results"] = [
                self._cap_item(dict(r)) for r in data["results"][: self._max_list_items]
            ]
        return data

    async def profile(self, name: str) -> dict[str, Any]:
        return self._cap_item(dict(await self._get("/agents/profile", {"name": _slug(name)})))

    async def submolts(self) -> dict[str, Any]:
        return dict(await self._get("/submolts"))

    async def me(self) -> dict[str, Any]:
        return self._cap_item(dict(await self._get("/agents/me")))

    # ---- truncation helpers (M12) ----------------------------------------

    def _page_limit(self, limit: int | None) -> int:
        if limit is None:
            return self._max_list_items
        return max(1, min(int(limit), self._max_list_items))

    def _cap_item(self, item: dict[str, Any]) -> dict[str, Any]:
        for field_name in ("content", "body", "description", "snippet", "bio", "x_bio"):
            v = item.get(field_name)
            if isinstance(v, str) and len(v) > self._max_item_chars:
                item[field_name] = v[: self._max_item_chars] + " …[truncated]"
        return item

    def _cap_feed(self, data: Any) -> dict[str, Any]:
        out = dict(data) if isinstance(data, dict) else {"posts": data}
        for key in ("posts", "results", "items"):
            v = out.get(key)
            if isinstance(v, list):
                out[key] = [
                    self._cap_item(dict(x)) if isinstance(x, dict) else x
                    for x in v[: self._max_list_items]
                ]
        return out


def strip_home_imperatives(home: dict[str, Any]) -> dict[str, Any]:
    """M3 — remove the platform's imperative channels from the /home dashboard before it
    enters the trusted prologue. `suggested_actions`, `what_to_do_next`, and the raw
    announcement body are platform-authored instructions; only inert data (counts,
    notification subjects, titles) survives. Not merely fenced — removed."""
    cleaned = dict(home)
    cleaned.pop("what_to_do_next", None)
    activity = cleaned.get("activity_on_your_posts")
    if isinstance(activity, list):
        cleaned["activity_on_your_posts"] = [
            {k: v for k, v in dict(item).items() if k != "suggested_actions"}
            for item in activity
            if isinstance(item, dict)
        ]
    ann = cleaned.get("latest_moltbook_announcement")
    if isinstance(ann, dict):
        # Keep only the fact that an announcement exists and its title — drop the body/preview,
        # which is the imperative-injection surface.
        cleaned["latest_moltbook_announcement"] = {"title": str(ann.get("title", ""))[:200]}
    return cleaned


# ---- module helpers -------------------------------------------------------


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _error_message(status: int) -> str:
    if status in (401, 403):
        return (
            "Moltbook rejected the request — the API key may be invalid or the account unclaimed."
        )
    if status == 404:
        return "That Moltbook resource was not found."
    if status == 410:
        return "That Moltbook challenge/resource has expired."
    if status == 429:
        return "Moltbook is rate-limiting — backing off."
    if status >= 500:
        return "Moltbook is having a server problem right now."
    return f"Moltbook request failed (HTTP {status})."


def _clean_sort(sort: str, allowed: set[str] | None = None) -> str:
    allowed = allowed or {"hot", "new", "top", "rising"}
    s = str(sort).strip().lower()
    return s if s in allowed else next(iter(sorted(allowed)))


def _slug(name: str) -> str:
    # Submolt/agent names: lowercase, hyphen, alnum; strip anything else defensively.
    return re.sub(r"[^a-z0-9_\-]", "", str(name).strip().lower())[:64]


def _id(value: str) -> str:
    # Post/comment ids: keep uuid/alnum-ish only, so a crafted id can't traverse the path.
    return re.sub(r"[^A-Za-z0-9_\-]", "", str(value).strip())[:64]
