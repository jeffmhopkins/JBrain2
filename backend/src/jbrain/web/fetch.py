"""Fetch a URL and extract its readable text (docs/reference/ASSISTANT.md "Agent
selection").

This is the one genuinely outbound leg of the jerv sandbox: it GETs a
model-supplied URL and returns the page's main content as clean **markdown**
(headings, lists, emphasis, inline links, fenced code), PLUS the links the page
points at (resolved to absolute URLs) so the agent can navigate — follow a link, a
"next page", a file in a repository — by fetching one of them, not just read a
single page. It is bounded deliberately — only the jerv agent (no knowledge-base
access, no owner data in context) can reach it, the response and the link list are
size-capped, and extraction is on-box: trafilatura isolates the page's main content as
markdown (with the dependency-free HTML→markdown pass as the fallback for the title,
the navigation links, and any page trafilatura declines). A linked PDF is read for its
text layer (PyMuPDF) rather than refused; other binary content is refused. Non-HTML
text bodies pass through.

Two things keep the agent from routing a blocked URL through a third-party reader on
its own: the request presents as an ordinary browser (BROWSER_HEADERS), so a bot-wall
is far less likely to 403 it; and when a direct fetch IS blocked or comes back empty (a
JS-rendered shell), an owner-configured reader endpoint (`reader_url`, an on-box reader
shipped with the stock stack by default) is used as a sanctioned fallback — the target
URL is the only thing that travels off-box, and it does so through a pinned endpoint the
owner controls rather than an unmonitored `r.jina.ai/<url>` the model built. Empty
`reader_url` disables the fallback.

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
from typing import cast
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
import structlog

from jbrain.htmltext import extract_page

log = structlog.get_logger()

_TIMEOUT = 20.0
_MAX_BYTES = 2_000_000  # cap the download; a page beyond this is truncated
# A larger cap for a binary image fetch (fetch_bytes) — a product photo is often a
# few MB, well over the text page cap, but still bounded so an endless/huge body can't
# be buffered whole. The decoded-pixel cap (agent.chat_images) is the memory bound;
# this bounds the encoded transfer.
_MAX_IMAGE_BYTES = 10_000_000
_MAX_CHARS = 20_000  # cap the extracted text handed to the model
_MAX_LINKS = 40  # cap the links surfaced for navigation; a link-heavy page is trimmed
_MAX_REDIRECTS = 4

# Present as an ordinary browser, not a bot. A bare custom User-Agent (and httpx's
# minimal default headers) is the single biggest reason a fetch comes back 403/429
# from bot-walled sites — which is what pushes the model to route the URL through a
# third-party reader instead. A realistic header set unblocks the common case; the
# fetch is still bounded by the SSRF guard, the size caps, and the jerv sandbox.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class WebFetchError(RuntimeError):
    """A URL could not be fetched or read — a bad scheme, an unreachable host, a
    non-2xx response, or a non-text body. Surfaced as a recoverable tool error."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    title: str
    text: str
    # The page's outbound links, resolved to absolute http(s) URLs, deduped and
    # capped — what lets the agent navigate (fetch one to follow it) instead of being
    # stuck on a single page. Empty for a non-HTML body.
    links: tuple[str, ...] = ()


def _collect_links(hrefs: list[str], *, base: str) -> tuple[str, ...]:
    """Resolve raw hrefs against the page's final URL into absolute http(s) links,
    dropping fragments, non-http(s) schemes (mailto:, javascript:, …), the page's own
    URL, and duplicates — order preserved, capped at `_MAX_LINKS`. This is what turns
    a single fetch into navigable browsing without a headless browser."""
    base_clean = urldefrag(base)[0]
    seen: set[str] = set()
    out: list[str] = []
    for href in hrefs:
        absolute = urldefrag(urljoin(base, href.strip()))[0]
        if urlparse(absolute).scheme not in ("http", "https"):
            continue
        if absolute == base_clean or absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
        if len(out) >= _MAX_LINKS:
            break
    return tuple(out)


class WebFetcher:
    """Fetch and extract a single URL. `transport` is injectable so tests run
    without network; when a transport is supplied (tests) the SSRF host check is
    skipped, since there is no real network to reach. `reader_url`, if configured,
    is a pinned reader endpoint the fetch falls back to when a direct fetch is
    blocked or comes back empty (see `_fetch_via_reader`)."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        reader_url: str = "",
    ):
        self._transport = transport
        self._reader_url = reader_url.rstrip("/")

    async def fetch(self, url: str) -> FetchResult:
        try:
            result = await self._fetch_direct(url)
        except WebFetchError:
            # A bot-wall (403/429) or unreachable host: the reader, if configured,
            # renders the page from a real browser and gets past what blocked us.
            if self._reader_url:
                reader = await self._fetch_via_reader(url)
                if reader is not None:
                    return reader
            raise
        if not result.text.strip() and self._reader_url:
            # A JS-rendered shell our static extractor can't see — the reader runs the
            # page's scripts and returns the content that wasn't in the served HTML.
            reader = await self._fetch_via_reader(url)
            if reader is not None and reader.text.strip():
                return reader
        return result

    async def fetch_bytes(
        self, url: str, *, max_bytes: int = _MAX_IMAGE_BYTES
    ) -> tuple[str, bytes]:
        """Fetch a URL's RAW bytes (a binary resource — an image) following redirects
        through the SAME per-hop SSRF guard as `fetch` (`_get_following_safe_redirects`:
        httpx auto-redirect is off, every hop's host is re-checked), capped at `max_bytes`.
        Returns (content_type, body); does NOT interpret the body — the caller validates it
        is really an image. Raises `WebFetchError` on a bad scheme/host/hop or an
        unreachable/errored response. No reader fallback (that endpoint returns markdown,
        not image bytes)."""
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport, follow_redirects=False
            ) as client:
                resp = await self._get_following_safe_redirects(client, url)
                content_type = resp.headers.get("content-type", "")
                body = await _read_capped(resp, max_bytes=max_bytes)
        except httpx.HTTPError as exc:
            log.warning("web.fetch_bytes_failed", error=repr(exc))
            raise WebFetchError("that URL could not be fetched right now") from exc
        return content_type, body

    async def _fetch_direct(self, url: str) -> FetchResult:
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport, follow_redirects=False
            ) as client:
                resp = await self._get_following_safe_redirects(client, url)
                content_type = resp.headers.get("content-type", "")
                final_url = str(resp.url)
                if not _is_textual(content_type) and "pdf" not in content_type.lower():
                    await resp.aclose()
                    kind = content_type or "unknown"
                    raise WebFetchError(f"that URL is not a text page ({kind})")
                body = await _read_capped(resp)
        except httpx.HTTPError as exc:
            log.warning("web.fetch_failed", error=repr(exc))
            raise WebFetchError("that URL could not be fetched right now") from exc
        if "pdf" in content_type.lower():
            # A linked PDF (common in research) is real text, not a dead end: pull its
            # text layer with PyMuPDF rather than refusing the way a binary body is.
            title, text, links = "", _extract_pdf_text(body), ()
        elif "html" in content_type.lower():
            html = body.decode(_charset(content_type) or "utf-8", errors="replace")
            title, text, hrefs = _extract_html(html, base=final_url)
            links = _collect_links(hrefs, base=final_url)
        else:
            text = body.decode(_charset(content_type) or "utf-8", errors="replace").strip()
            title, links = "", ()
        return FetchResult(url=final_url, title=title, text=text[:_MAX_CHARS], links=links)

    async def _fetch_via_reader(self, url: str) -> FetchResult | None:
        """Re-fetch `url` through the pinned reader endpoint, which renders the page
        and returns clean markdown. The reader base URL is owner-configured and trusted
        (like SearXNG), so it is NOT run through the SSRF guard — a self-hosted reader
        legitimately lives on an internal host (`http://reader:3000`). Only the public
        target URL travels in the path. Returns None on any reader failure, so the
        caller falls back to its original error or empty result."""
        guard_public_host(url, skip_dns=self._transport is not None)  # the TARGET must be public
        target = f"{self._reader_url}/{url}"
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport, follow_redirects=True
            ) as client:
                resp = await client.get(
                    target, headers={**BROWSER_HEADERS, "X-Respond-With": "markdown"}
                )
                resp.raise_for_status()
                text = (await _read_capped(resp)).decode(
                    _charset(resp.headers.get("content-type", "")) or "utf-8", errors="replace"
                )
        except (httpx.HTTPError, WebFetchError) as exc:
            log.warning("web.reader_failed", error=repr(exc))
            return None
        return FetchResult(url=url, title="", text=text.strip()[:_MAX_CHARS], links=())

    async def _get_following_safe_redirects(
        self, client: httpx.AsyncClient, url: str
    ) -> httpx.Response:
        """GET `url`, validating the host of every hop against the SSRF blocklist
        and following up to `_MAX_REDIRECTS` redirects by hand (httpx auto-redirect
        is off, so a 30x to a private address can't slip past the per-hop check)."""
        for _hop in range(_MAX_REDIRECTS + 1):
            self._guard_host(url)
            resp = await client.send(
                client.build_request("GET", url, headers=BROWSER_HEADERS),
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
        """Refuse a non-http(s) URL, or one whose host resolves to a non-public
        address (the SSRF guard). Skipped under an injected transport (tests have no
        real network). Delegates to the shared `guard_public_host`."""
        guard_public_host(url, skip_dns=self._transport is not None)


def guard_public_host(url: str, *, skip_dns: bool = False) -> None:
    """Refuse a non-http(s) URL, or one whose host resolves to a private, loopback,
    link-local, or otherwise non-public address — the shared SSRF guard, reused by
    `WebFetcher` and the favicon fetcher (both GET a host derived from untrusted
    content). `skip_dns` bypasses the resolution check for tests with an injected
    transport (no real network to reach). Raises `WebFetchError` on a refused host."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise WebFetchError("only http(s) URLs can be fetched")
    if skip_dns:
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


async def _read_capped(resp: httpx.Response, *, max_bytes: int = _MAX_BYTES) -> bytes:
    """Read a streamed response body up to `max_bytes` and stop — so an oversized
    or endless response is truncated, never buffered whole into memory (DoS guard)."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            break
    await resp.aclose()
    return b"".join(chunks)[:max_bytes]


def _is_textual(content_type: str) -> bool:
    ct = content_type.lower()
    return not ct or ct.startswith("text/") or "html" in ct or "json" in ct or "xml" in ct


def _charset(content_type: str) -> str | None:
    """The charset declared in a Content-Type header (`text/html; charset=…`), or
    None — so a non-utf-8 page decodes with the bytes it was actually sent as."""
    for part in content_type.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"').strip("'")
    return None


_MIN_ARTICLE_CHARS = 500  # below this, trust the simple full-page pass over trafilatura


def _extract_html(html: str, *, base: str) -> tuple[str, str, list[str]]:
    """(title, body markdown, hrefs) for an HTML page. The title and navigation links
    always come from the dependency-free `extract_page`. The body prefers trafilatura
    (a real readability engine — it isolates the article and drops the chrome our
    heuristic keeps) WHEN it returns a substantial article; on a short page, or one
    trafilatura declines (not article-shaped, or not installed), we keep `extract_page`'s
    full-page markdown. So a content-heavy page reads as clean as a third-party reader's
    output, while a simple page keeps the behaviour it always had."""
    title, fallback_text, hrefs = extract_page(html, base=base)
    article = _trafilatura_markdown(html, base=base)
    text = article if len(article) >= _MIN_ARTICLE_CHARS else fallback_text
    return title, text, hrefs


def _trafilatura_markdown(html: str, *, base: str) -> str:
    """trafilatura's main-content extraction as markdown, or "" when it declines or is
    unavailable. Pure function over the HTML string — no network — so it runs the same
    under a test transport. Any failure degrades to the `extract_page` fallback."""
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        extracted = trafilatura.extract(
            html,
            url=base or None,
            output_format="markdown",
            include_links=True,
            include_tables=True,
        )
    except Exception as exc:  # trafilatura/lxml parse failure: fall back, never crash
        log.warning("web.trafilatura_failed", error=repr(exc))
        return ""
    return (extracted or "").strip()


def _extract_pdf_text(body: bytes) -> str:
    """The text layer of a fetched PDF via PyMuPDF (the same engine the ingest pipeline
    uses), pages joined in order. A scanned PDF with no text layer yields "" — the
    handler then reports the page had no readable text, as for any empty page."""
    import pymupdf

    parts: list[str] = []
    try:
        with pymupdf.open(stream=body, filetype="pdf") as doc:
            for page in doc:
                page_text = cast(str, page.get_text("text")).strip()
                if page_text:
                    parts.append(page_text)
    except Exception as exc:  # a truncated/corrupt PDF body: treat as unreadable
        log.warning("web.pdf_failed", error=repr(exc))
        return ""
    return "\n\n".join(parts).strip()
