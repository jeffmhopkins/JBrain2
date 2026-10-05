"""A minimal MCP client over the streamable HTTP transport, for the `browser` sidecar.

The browse sub-agent (docs/plans/BROWSER_AGENT_PLAN.md) needs exactly four MCP operations
against one server it runs: open a session, list tools, call a tool, close the session. That
is a few JSON-RPC requests over the httpx the backend already ships, so this is written out
rather than pulling the `mcp` SDK (and its anyio/starlette/sse stack) into the api for one
caller — DEVELOPMENT.md's zero-new-dep default. The server may answer a request with plain
JSON or with a `text/event-stream` carrying the response among notifications; both are
handled, and nothing the server sends is ever treated as a request for us to act on.

Each `McpSession` is one MCP session, which playwright-mcp maps to its own isolated browser
context, so one browse run never sees another's tabs or cookies.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

PROTOCOL_VERSION = "2025-06-18"
_SESSION_HEADER = "mcp-session-id"
_ACCEPT = "application/json, text/event-stream"
# A tool result larger than this is cut before it reaches anything else. A busy page's
# accessibility tree can run to hundreds of kilobytes; the caller prunes it further.
MAX_RESULT_CHARS = 400_000


class McpError(RuntimeError):
    """The MCP server was unreachable, refused the session, or answered malformed."""


@dataclass(frozen=True)
class McpToolResult:
    """A tool call's text content and whether the server flagged it as an error."""

    text: str
    is_error: bool


def _parse_sse(body: str) -> list[dict[str, Any]]:
    """The JSON-RPC messages in a `text/event-stream` body (each event's `data:` lines)."""
    messages: list[dict[str, Any]] = []
    data: list[str] = []

    def flush() -> None:
        if data:
            try:
                parsed = json.loads("\n".join(data))
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                messages.append(parsed)
            data.clear()

    for line in body.splitlines():
        if not line.strip():
            flush()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    flush()
    return messages


def _response_for(resp: httpx.Response, request_id: int) -> dict[str, Any]:
    """Pick the JSON-RPC response to `request_id` out of a JSON or SSE reply."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        messages = _parse_sse(resp.text)
    else:
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise McpError("the browser server answered with something other than JSON") from exc
        messages = parsed if isinstance(parsed, list) else [parsed]
    for message in messages:
        if isinstance(message, dict) and message.get("id") == request_id:
            if "error" in message:
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else error
                raise McpError(f"the browser server refused the request: {detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise McpError("the browser server sent a response with no result")
            return result
    raise McpError("the browser server sent no response to the request")


def _tool_text(result: Mapping[str, Any]) -> McpToolResult:
    """Flatten a `tools/call` result to its text parts. Images are dropped: the browse loop
    is text-only, and the sidecar runs with `--image-responses omit` anyway."""
    parts = result.get("content") or []
    texts = [
        str(p.get("text", "")) for p in parts if isinstance(p, dict) and p.get("type") == "text"
    ]
    text = "\n".join(texts)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS]
    return McpToolResult(text=text, is_error=bool(result.get("isError")))


class McpSession:
    """One MCP session: `async with client.session() as s: await s.call_tool(...)`."""

    def __init__(self, http: httpx.AsyncClient, url: str) -> None:
        self._http = http
        self._url = url
        self._session_id: str | None = None
        self._next_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {"accept": _ACCEPT, "content-type": "application/json"}
        if self._session_id is not None:
            headers[_SESSION_HEADER] = self._session_id
            headers["mcp-protocol-version"] = PROTOCOL_VERSION
        return headers

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            resp = await self._http.post(self._url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise McpError(f"the browser server is unreachable: {exc!r}") from exc
        if resp.status_code >= 400:
            raise McpError(f"the browser server answered HTTP {resp.status_code}")
        return resp

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        resp = await self._post(payload)
        return _response_for(resp, request_id)

    async def open(self) -> None:
        self._next_id += 1
        request_id = self._next_id
        resp = await self._post(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "jbrain-browse", "version": "1"},
                },
            }
        )
        _response_for(resp, request_id)
        self._session_id = resp.headers.get(_SESSION_HEADER)
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def list_tools(self) -> list[str]:
        result = await self.request("tools/list")
        tools = result.get("tools") or []
        return [str(t.get("name")) for t in tools if isinstance(t, dict) and t.get("name")]

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> McpToolResult:
        result = await self.request("tools/call", {"name": name, "arguments": dict(arguments)})
        return _tool_text(result)

    async def close(self) -> None:
        """End the session so the server frees its browser context. Best-effort: a server
        that already dropped it (an idle timeout, a restart) is not an error here."""
        if self._session_id is None:
            return
        try:
            await self._http.delete(self._url, headers=self._headers())
        except httpx.HTTPError:
            log.info("browse.mcp_close_failed", exc_info=True)
        self._session_id = None


class McpHttpClient:
    """Opens sessions against one MCP server URL. `transport` is injected in tests."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self._timeout = timeout
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def session(self) -> _SessionScope:
        return _SessionScope(self)

    def _http(self) -> httpx.AsyncClient:
        # No proxy from the environment: the sidecar is on the box's own network, and the
        # api's HTTP(S)_PROXY (if any) is for the open web, not for reaching it.
        return httpx.AsyncClient(timeout=self._timeout, transport=self._transport, trust_env=False)


class _SessionScope:
    """The async context manager behind `McpHttpClient.session()`."""

    def __init__(self, client: McpHttpClient) -> None:
        self._client = client
        self._http: httpx.AsyncClient | None = None
        self._session: McpSession | None = None

    async def __aenter__(self) -> McpSession:
        if not self._client.configured:
            raise McpError("no browser server is configured")
        self._http = self._client._http()
        self._session = McpSession(self._http, self._client.url)
        try:
            await self._session.open()
        except BaseException:
            await self._http.aclose()
            raise
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._session is not None:
                await self._session.close()
        finally:
            if self._http is not None:
                await self._http.aclose()
