"""Fetch a URL and extract its readable text (docs/ASSISTANT.md "Agent
selection").

This is the one genuinely outbound leg of the jerv sandbox: it GETs a
model-supplied URL and returns the page's main text as plain prose the agent can
read. It is bounded deliberately — only the jerv agent (no knowledge-base access,
no owner data in context) can reach it, the response is size-capped, and the
extractor is a dependency-free HTML-to-text pass (scripts/styles dropped,
whitespace collapsed) rather than a full browser. Non-HTML text bodies pass
through; binary content is refused.

SSRF guard: the URL is model-supplied, and api/worker share an internal Docker
network with Postgres, the embedder, SearXNG, and the MQTT auth endpoints — so a
fetch that resolved to a private/loopback/link-local/reserved address would be a
read primitive into the box's own services (and the cloud metadata endpoint). We
resolve the host first and refuse any such target, disable automatic redirects,
and re-validate every redirect hop's host the same way — so an allowlisted public
host cannot 30x its way to `db:5432` or `169.254.169.254`. The body is read as a
bounded stream, so an oversized response cannot be buffered whole into memory.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 20.0
_MAX_BYTES = 2_000_000  # cap the download; a page beyond this is truncated
_MAX_CHARS = 20_000  # cap the extracted text handed to the model
_MAX_REDIRECTS = 4
_DROP_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
_BLOCK_TAGS = frozenset(
    {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}
)


class WebFetchError(RuntimeError):
    """A URL could not be fetched or read — a bad scheme, an unreachable host, a
    non-2xx response, or a non-text body. Surfaced as a recoverable tool error."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    title: str
    text: str


class _Extractor(HTMLParser):
    """Collect visible text, dropping script/style and breaking on block tags so
    the output reads as paragraphs rather than one run-on line."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self.title = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        # Collapse runs of blank lines and trailing spaces into tidy paragraphs.
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        out: list[str] = []
        for line in lines:
            if line or (out and out[-1]):
                out.append(line)
        return "\n".join(out).strip()


def _extract(html: str) -> tuple[str, str]:
    parser = _Extractor()
    parser.feed(html)
    return parser.title.strip(), parser.text()


class WebFetcher:
    """Fetch and extract a single URL. `transport` is injectable so tests run
    without network; when a transport is supplied (tests) the SSRF host check is
    skipped, since there is no real network to reach."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    async def fetch(self, url: str) -> FetchResult:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport, follow_redirects=False
            ) as client:
                resp = await self._get_following_safe_redirects(client, url)
                content_type = resp.headers.get("content-type", "")
                final_url = str(resp.url)
                if not _is_textual(content_type):
                    await resp.aclose()
                    kind = content_type or "unknown"
                    raise WebFetchError(f"that URL is not a text page ({kind})")
                body = await _read_capped(resp)
                html = body.decode(resp.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            log.warning("web.fetch_failed", error=repr(exc))
            raise WebFetchError("that URL could not be fetched right now") from exc
        title, text = _extract(html) if "html" in content_type.lower() else ("", html.strip())
        return FetchResult(url=final_url, title=title, text=text[:_MAX_CHARS])

    async def _get_following_safe_redirects(
        self, client: httpx.AsyncClient, url: str
    ) -> httpx.Response:
        """GET `url`, validating the host of every hop against the SSRF blocklist
        and following up to `_MAX_REDIRECTS` redirects by hand (httpx auto-redirect
        is off, so a 30x to a private address can't slip past the per-hop check)."""
        for _hop in range(_MAX_REDIRECTS + 1):
            self._guard_host(url)
            resp = await client.send(
                client.build_request("GET", url, headers={"User-Agent": "JBrain2 jerv/1.0"}),
                stream=True,
            )
            if resp.is_redirect and resp.headers.get("location"):
                await resp.aclose()
                url = urljoin(url, resp.headers["location"])
                continue
            if resp.is_error:
                await resp.aclose()
                resp.raise_for_status()
            return resp
        raise WebFetchError("that URL redirected too many times")

    def _guard_host(self, url: str) -> None:
        """Refuse a non-http(s) URL, or one whose host resolves to a private,
        loopback, link-local, or otherwise non-public address (the SSRF guard).
        Skipped under an injected transport (tests have no real network)."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise WebFetchError("only http(s) URLs can be fetched")
        if self._transport is not None:
            return
        try:
            infos = socket.getaddrinfo(parsed.hostname, parsed.port or 0, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise WebFetchError("that host could not be resolved") from exc
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not _is_public(ip):
                raise WebFetchError("that URL points at a non-public address")


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether an address is a routable public one — the SSRF allow-condition. Maps
    an IPv4-mapped IPv6 address (::ffff:10.0.0.1) back to its v4 form first so a
    private target can't hide behind the v6 representation."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _read_capped(resp: httpx.Response) -> bytes:
    """Read a streamed response body up to `_MAX_BYTES` and stop — so an oversized
    or endless response is truncated, never buffered whole into memory (DoS guard)."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= _MAX_BYTES:
            break
    await resp.aclose()
    return b"".join(chunks)[:_MAX_BYTES]


def _is_textual(content_type: str) -> bool:
    ct = content_type.lower()
    return not ct or ct.startswith("text/") or "html" in ct or "json" in ct or "xml" in ct
