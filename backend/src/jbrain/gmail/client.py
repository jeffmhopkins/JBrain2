"""The Gmail client: the one place an HTTP request to Google is made for the
`archivist` persona (docs/EMAIL_ARCHIVIST_PLAN.md).

Thin over httpx, mirroring `jbrain.web.search.SearxngClient`: the base URL and the
OAuth token endpoint are pinned from config, never model-supplied; only typed query
text / message ids / label names flow in. The client holds a long-lived **refresh
token** and mints short-lived access tokens on demand (cached in memory, never
persisted) — so nothing about auth touches the DB. Its scope is `gmail.modify`:
read, label, archive; never delete. `transport` is injectable so tests run against a
MockTransport with no network (DEVELOPMENT.md "no network in tests").
"""

from __future__ import annotations

import base64
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 30.0
# Refresh a little before the stated expiry so an access token can't lapse mid-call.
_EXPIRY_MARGIN = 60.0
# Headers worth surfacing when we fetch a message as metadata-only (no body).
_METADATA_HEADERS = ("From", "To", "Subject", "Date")
# Pagination + bulk limits. A messages.list page is ≤500 ids; batchModify takes ≤1000
# ids per call. The count/bulk caps bound a runaway scan over a 20-year mailbox — a
# count beyond _COUNT_CAP reports "N+", a bulk beyond _BULK_CAP touches the first slice
# and says so, so the agent narrows the query rather than silently doing a partial job.
_PAGE_SIZE = 500
_BATCH_SIZE = 1000
_COUNT_CAP = 50_000
_BULK_CAP = 10_000


class GmailError(RuntimeError):
    """A Gmail call could not be completed — auth refused, a non-2xx response, or a
    malformed body. Surfaced to the agent as a recoverable tool error, never a crash.
    `status` carries the HTTP code when there was one (the 401 retry / 409 idempotent-
    create paths branch on it)."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GmailMessage:
    """One message, flattened to what a triage agent reads. `body` is the decoded
    text/plain part (empty for a metadata-only fetch)."""

    id: str
    thread_id: str
    sender: str
    to: str
    subject: str
    date: str
    snippet: str
    body: str


@dataclass(frozen=True)
class GmailLabel:
    """One Gmail label: its id (what `modify` targets) and its display name (what a
    Parent/Child path reads as)."""

    id: str
    name: str


class GmailApi(Protocol):
    """The surface the gmail_* tool handlers depend on — implemented by `GmailClient`
    (live) and `FakeGmail` (tests), so the handlers never know which they hold."""

    async def search(self, query: str, *, max_results: int = ...) -> list[str]: ...

    async def get(self, message_id: str, *, metadata_only: bool = ...) -> GmailMessage: ...

    async def list_labels(self) -> list[GmailLabel]: ...

    async def create_label(self, name: str) -> GmailLabel: ...

    async def modify(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str] = (),
        remove_label_ids: Sequence[str] = (),
    ) -> None: ...

    async def count(self, query: str, *, cap: int = ...) -> tuple[int, bool]: ...

    async def search_all(self, query: str, *, cap: int = ...) -> tuple[list[str], bool]: ...

    async def batch_modify(
        self,
        message_ids: Sequence[str],
        *,
        add_label_ids: Sequence[str] = (),
        remove_label_ids: Sequence[str] = (),
    ) -> None: ...


def _header(headers: list[dict[str, Any]], name: str) -> str:
    """The value of a message header by case-insensitive name, or ""."""
    lname = name.lower()
    for h in headers:
        if str(h.get("name", "")).lower() == lname:
            return str(h.get("value", "")).strip()
    return ""


def _decode_b64url(data: str) -> str:
    """Decode a Gmail base64url body part, tolerating missing padding."""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _extract_text(payload: dict[str, Any]) -> str:
    """Walk a message payload for its text/plain body, falling back to text/html
    stripped of tags only as a last resort. Returns "" if there is no text part."""
    mime = str(payload.get("mimeType", ""))
    body_data = (payload.get("body") or {}).get("data")
    if mime == "text/plain" and body_data:
        return _decode_b64url(body_data)
    html_fallback = ""
    for part in payload.get("parts") or []:
        text = _extract_text(part)
        if text and str(part.get("mimeType", "")) == "text/plain":
            return text
        if text and not html_fallback:
            html_fallback = text
    if mime == "text/html" and body_data:
        return _decode_b64url(body_data)
    return html_fallback


def _message_from_payload(raw: dict[str, Any]) -> GmailMessage:
    payload = raw.get("payload") or {}
    headers = payload.get("headers") or []
    return GmailMessage(
        id=str(raw.get("id", "")),
        thread_id=str(raw.get("threadId", "")),
        sender=_header(headers, "From"),
        to=_header(headers, "To"),
        subject=_header(headers, "Subject"),
        date=_header(headers, "Date"),
        snippet=str(raw.get("snippet", "")).strip(),
        body=_extract_text(payload),
    )


class GmailClient:
    """Talk to a pinned Gmail API as the authenticated owner (`users/me`). Reads and
    the one write (`modify`) all run server-side under a `gmail.modify` token."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        *,
        base_url: str,
        token_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._base_url = base_url.rstrip("/")
        self._token_url = token_url
        self._transport = transport
        self._token: str | None = None
        self._token_expiry = 0.0

    # --- auth --------------------------------------------------------------

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        if not self._refresh_token:
            raise GmailError("Gmail is not configured on this instance")
        body = await self._send(
            "POST",
            self._token_url,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
        )
        token = str(body.get("access_token") or "")
        if not token:
            raise GmailError("Gmail rejected the refresh token — re-run the bootstrap")
        self._token = token
        self._token_expiry = time.monotonic() + float(body.get("expires_in", 3600)) - _EXPIRY_MARGIN
        return token

    # --- transport ---------------------------------------------------------

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport) as client:
                resp = await client.request(
                    method, url, params=params, data=data, json=json, headers=headers
                )
                resp.raise_for_status()
                return resp.json() if resp.content else {}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            log.warning("gmail.request_failed", status=status, error=repr(exc))
            raise GmailError("Gmail refused the request", status=status) from exc
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("gmail.request_failed", error=repr(exc))
            raise GmailError("the Gmail service is unavailable right now") from exc

    async def _api(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """An authenticated call, retrying once on a 401 (a token that lapsed early
        despite the margin) with a freshly minted access token."""
        for attempt in range(2):
            token = await self._access_token()
            headers = {"Authorization": f"Bearer {token}"}
            try:
                return await self._send(
                    method, f"{self._base_url}{path}", params=params, json=json, headers=headers
                )
            except GmailError as exc:
                if attempt == 0 and exc.status == 401:
                    self._token = None  # force a refresh, then retry the call once
                    continue
                raise
        return {}  # unreachable; the loop returns or raises

    # --- operations --------------------------------------------------------

    async def search(self, query: str, *, max_results: int = 25) -> list[str]:
        body = await self._api(
            "GET",
            "/users/me/messages",
            params={"q": query, "maxResults": max(1, max_results)},
        )
        return [str(m.get("id", "")) for m in (body.get("messages") or []) if m.get("id")]

    async def get(self, message_id: str, *, metadata_only: bool = False) -> GmailMessage:
        params: dict[str, Any] = {"format": "metadata" if metadata_only else "full"}
        if metadata_only:
            params["metadataHeaders"] = list(_METADATA_HEADERS)
        raw = await self._api("GET", f"/users/me/messages/{message_id}", params=params)
        return _message_from_payload(raw)

    async def list_labels(self) -> list[GmailLabel]:
        body = await self._api("GET", "/users/me/labels")
        return [
            GmailLabel(id=str(label.get("id", "")), name=str(label.get("name", "")))
            for label in (body.get("labels") or [])
            if label.get("id") and label.get("name")
        ]

    async def create_label(self, name: str) -> GmailLabel:
        """Create a label (Parent/Child for nesting). Idempotent: if Gmail reports the
        name already exists (409), resolve and return the existing label instead."""
        try:
            body = await self._api(
                "POST",
                "/users/me/labels",
                json={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
            return GmailLabel(id=str(body.get("id", "")), name=str(body.get("name", name)))
        except GmailError:
            existing = next((lbl for lbl in await self.list_labels() if lbl.name == name), None)
            if existing is None:
                raise
            return existing

    async def modify(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str] = (),
        remove_label_ids: Sequence[str] = (),
    ) -> None:
        await self._api(
            "POST",
            f"/users/me/messages/{message_id}/modify",
            json={
                "addLabelIds": list(add_label_ids),
                "removeLabelIds": list(remove_label_ids),
            },
        )

    async def _list_ids(self, query: str, *, cap: int) -> tuple[list[str], bool]:
        """Paginate messages.list for `query`, collecting ids until exhausted or `cap`.
        Returns (ids, capped): capped is True when more pages exist beyond the cap."""
        ids: list[str] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"q": query, "maxResults": _PAGE_SIZE}
            if page_token:
                params["pageToken"] = page_token
            body = await self._api("GET", "/users/me/messages", params=params)
            ids.extend(str(m["id"]) for m in (body.get("messages") or []) if m.get("id"))
            page_token = body.get("nextPageToken")
            if not page_token:
                return ids, False  # exhausted — an exact set
            if len(ids) >= cap:
                return ids[:cap], True  # more pages remain, but we stop at the cap

    async def count(self, query: str, *, cap: int = _COUNT_CAP) -> tuple[int, bool]:
        ids, capped = await self._list_ids(query, cap=cap)
        return len(ids), capped

    async def search_all(self, query: str, *, cap: int = _BULK_CAP) -> tuple[list[str], bool]:
        return await self._list_ids(query, cap=cap)

    async def batch_modify(
        self,
        message_ids: Sequence[str],
        *,
        add_label_ids: Sequence[str] = (),
        remove_label_ids: Sequence[str] = (),
    ) -> None:
        """Apply label changes to many messages in ≤1000-id batchModify calls."""
        ids = list(message_ids)
        for start in range(0, len(ids), _BATCH_SIZE):
            await self._api(
                "POST",
                "/users/me/messages/batchModify",
                json={
                    "ids": ids[start : start + _BATCH_SIZE],
                    "addLabelIds": list(add_label_ids),
                    "removeLabelIds": list(remove_label_ids),
                },
            )
