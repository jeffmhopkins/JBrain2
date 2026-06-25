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

import asyncio
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
# A sender/domain breakdown samples recent mail (Gmail has no server-side group-by) and
# fetches each From header; the sample is bounded so the scan stays quick, and metadata
# fetches run in small concurrent chunks rather than one slow id-at-a-time loop.
_SENDER_SAMPLE_MAX = 500
_SENDER_FETCH_CHUNK = 10


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
    text/plain part (empty for a metadata-only fetch). `label_ids` / `internal_date_ms`
    ride along from the message resource (present on metadata fetches too) and feed the
    metadata index — the From/Date/labels the breakdown aggregates exactly."""

    id: str
    thread_id: str
    sender: str
    to: str
    subject: str
    date: str
    snippet: str
    body: str
    label_ids: tuple[str, ...] = ()
    internal_date_ms: int = 0


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

    async def get_profile(self) -> tuple[int, str]: ...

    async def list_page(
        self, query: str, *, page_token: str | None = ..., page_size: int = ...
    ) -> tuple[list[str], str | None]: ...

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

    async def sender_sample(self, query: str, *, sample: int = ...) -> tuple[list[str], bool]: ...

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
    try:
        internal_ms = int(raw.get("internalDate") or 0)
    except (TypeError, ValueError):
        internal_ms = 0
    return GmailMessage(
        id=str(raw.get("id", "")),
        thread_id=str(raw.get("threadId", "")),
        sender=_header(headers, "From"),
        to=_header(headers, "To"),
        subject=_header(headers, "Subject"),
        date=_header(headers, "Date"),
        snippet=str(raw.get("snippet", "")).strip(),
        body=_extract_text(payload),
        label_ids=tuple(str(x) for x in (raw.get("labelIds") or [])),
        internal_date_ms=internal_ms,
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

    async def get_profile(self) -> tuple[int, str]:
        """(messagesTotal, historyId) from users.getProfile — the exact mailbox size for
        the index progress denominator, and the history cursor incremental sync resumes
        from. One cheap call (no paging)."""
        body = await self._api("GET", "/users/me/profile")
        try:
            total = int(body.get("messagesTotal") or 0)
        except (TypeError, ValueError):
            total = 0
        return total, str(body.get("historyId") or "")

    async def list_page(
        self, query: str, *, page_token: str | None = None, page_size: int = _PAGE_SIZE
    ) -> tuple[list[str], str | None]:
        """One page of messages.list ids + the next page token (None when exhausted).
        Unlike `search_all`, this hands pagination to the caller, so the metadata index
        can checkpoint its page cursor and resume an interrupted full-mailbox scan."""
        params: dict[str, Any] = {"q": query, "maxResults": max(1, min(page_size, _PAGE_SIZE))}
        if page_token:
            params["pageToken"] = page_token
        body = await self._api("GET", "/users/me/messages", params=params)
        ids = [str(m["id"]) for m in (body.get("messages") or []) if m.get("id")]
        return ids, body.get("nextPageToken")

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

    async def sender_sample(self, query: str, *, sample: int = 200) -> tuple[list[str], bool]:
        """The From headers of up to `sample` recent messages matching `query` — the raw
        material for a sender/domain breakdown. Gmail has no server-side group-by, so a
        breakdown samples recent mail (then the agent confirms exact totals with count).
        Returns (froms, capped): capped is True when the sample came back full, so more
        matched than were read. Fetches metadata in bounded-concurrency chunks to stay
        quick rather than one slow id-at-a-time loop."""
        want = min(max(1, sample), _SENDER_SAMPLE_MAX)
        ids = await self.search(query, max_results=want)
        froms: list[str] = []
        for start in range(0, len(ids), _SENDER_FETCH_CHUNK):
            chunk = ids[start : start + _SENDER_FETCH_CHUNK]
            msgs = await asyncio.gather(*(self.get(mid, metadata_only=True) for mid in chunk))
            froms.extend(m.sender for m in msgs)
        return froms, len(ids) >= want

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


async def exchange_authorization_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    token_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Exchange an OAuth authorization code for a long-lived refresh token (the
    in-app Connect flow's one-shot, docs/EMAIL_ARCHIVIST_PLAN.md). `redirect_uri` must
    match the one the consent URL used. Raises GmailError if Google returns no refresh
    token (e.g. a re-consent without prompt=consent / access_type=offline)."""
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, transport=transport) as client:
            resp = await client.post(token_url, data=data)
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("gmail.code_exchange_failed", error=repr(exc))
        raise GmailError("Google rejected the authorization — try connecting again.") from exc
    token = str(body.get("refresh_token") or "")
    if not token:
        raise GmailError(
            "Google did not return a refresh token. Revoke prior access at "
            "myaccount.google.com and connect again."
        )
    return token
