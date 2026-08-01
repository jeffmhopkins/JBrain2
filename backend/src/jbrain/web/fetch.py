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
is far less likely to 403 it; and when a direct fetch IS blocked (403/429) or comes back
empty (a JS-rendered shell), an owner-configured reader endpoint (`reader_url`, an on-box
reader shipped with the stock stack by default) is used as a sanctioned fallback — the
target URL is the only thing that travels off-box, and it does so through a pinned
endpoint the owner controls rather than an unmonitored `r.jina.ai/<url>` the model built.
Empty `reader_url` disables the fallback. A definitive 404/410 is NOT retried through the
reader — the page does not exist, and the reader would only render the origin's soft
"no such page" stub (enough text to read as a successful fetch and hide the miss); the
real status rides back on `WebFetchError.status` so the model sees the 404 and corrects
the URL. A long page is returned in windows: `fetch(url, offset=…)` returns
`full_text[offset : offset+_MAX_CHARS]` alongside `FetchResult.total_chars`, so the tool
can page the model through the tail (the most recent rows of a long list) instead of
silently dropping it; `FetchResult.truncated` flags only the rarer case where the raw
download itself hit the byte cap, so even the full text is short of the real page.

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
import re
import socket
from dataclasses import dataclass
from typing import cast
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
import structlog

from jbrain.htmltext import extract_page

log = structlog.get_logger()

_TIMEOUT = 20.0
# Cap the raw HTML download; a page beyond this is truncated (streamed, never buffered
# whole). Sized so a large reference page (a ~3 MB Wikipedia list article — HTML runs
# ~5-6 bytes per char of extracted text) downloads WHOLE, so the byte cap never starves
# extraction of the page tail; the extracted-text cap below is the intended binding one.
_MAX_BYTES = 5_000_000
# A larger cap for a binary image fetch (fetch_bytes) — a product photo is often a
# few MB, well over the text page cap, but still bounded so an endless/huge body can't
# be buffered whole. The decoded-pixel cap (agent.chat_images) is the memory bound;
# this bounds the encoded transfer.
_MAX_IMAGE_BYTES = 10_000_000
# Cap the extracted text handed to the model — one page window. ~30k chars ≈ ~8k tokens,
# a small enough slice that several fetches, history, and the model's own reasoning coexist
# in the agent window AND the local model's prompt-processing stays fast (a big window tanks
# throughput on the box as context grows). A long page is paged over via `offset`, and `find`
# jumps the window straight to a keyword so the model targets the right section instead of
# reading (or blindly guessing an offset into) the whole thing.
_MAX_CHARS = 30_000
_MAX_LINKS = 40  # cap the links surfaced for navigation; a link-heavy page is trimmed
_MAX_REDIRECTS = 4
# When `find` lands the window on a keyword, start a little before the match so the model
# sees the lead-in (a table header, the sentence introducing the row), not a mid-line cut.
_FIND_LEAD = 200
# Cap the match-offset map surfaced for `find` — enough to navigate a clustered term without
# flooding the reply when a word appears hundreds of times; the full count is reported too.
_MAX_MATCH_OFFSETS = 20
# Cap the section outline (markdown headings + offsets) surfaced for a large page — enough to
# navigate a long article's structure without a heading line per row of a giant table.
_MAX_OUTLINE = 50
# A markdown heading line: 1–6 '#', a space, the title (trailing '#'s tolerated). The extracted
# text is markdown (trafilatura / the fallback), so section headings are exactly these lines.
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(\S.*?)[ \t#]*$", re.MULTILINE)

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
    non-2xx response, or a non-text body. Surfaced as a recoverable tool error.

    `status` carries the upstream HTTP status when the failure was an HTTP error
    (else None), so a caller can tell a definitive 404/410 (the page is gone —
    don't launder it through the reader) apart from a bot-wall 403/429."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


# A definitive "this resource does not exist" — no reader fallback (the reader would
# only render the origin's soft "no such page" stub, which has enough text to read as
# a successful fetch and hide the miss). A bot-wall (403/429) or transient error still
# gets the reader, which can render past it.
_GONE_STATUSES = frozenset({404, 410})


def _fetch_error_message(status: int | None) -> str:
    """The recoverable-error text handed to the model. A 404/410 says the page is
    gone and nudges the model to verify/search rather than re-fetch a guessed URL."""
    if status in _GONE_STATUSES:
        return (
            f"that URL could not be fetched (HTTP {status}) — the page does not exist. "
            "Verify the URL or web_search for the right one; do not guess a URL variant."
        )
    if status is not None:
        return f"that URL could not be fetched right now (HTTP {status})"
    return "that URL could not be fetched right now"


@dataclass(frozen=True)
class FetchResult:
    url: str
    title: str
    text: str
    # The page's outbound links, resolved to absolute http(s) URLs, deduped and
    # capped — what lets the agent navigate (fetch one to follow it) instead of being
    # stuck on a single page. Empty for a non-HTML body.
    links: tuple[str, ...] = ()
    # True when even the FULL extracted text is itself incomplete because the raw body hit
    # `_MAX_BYTES` — the page was too large to download whole, so pagination cannot reach
    # past it. (Clipping to `_MAX_CHARS` is NOT this: that tail is recoverable via `offset`.)
    truncated: bool = False
    # Pagination over the extracted text. `text` is the window `full[offset : offset+_MAX_CHARS]`;
    # `offset` is where this window starts and `total_chars` is the length of the FULL extracted
    # text. The tool compares `offset + len(text)` against `total_chars` to tell the model whether
    # more remains and, if so, the next `offset` to pass — so a long list's tail is fetchable, not
    # just flagged as dropped.
    offset: int = 0
    total_chars: int = 0
    # Keyword locate (`find`): when the caller passes a term, the window is positioned at the first
    # occurrence at/after the requested offset so the model reads the right SECTION of a big page
    # instead of guessing an offset. `match_offsets` is the (capped) list of match positions from
    # there on and `match_count` the true total — the tool surfaces them so the model can jump to a
    # specific later hit with `offset=`. `find_term` echoes the query for the reply text.
    find_term: str = ""
    match_offsets: tuple[int, ...] = ()
    match_count: int = 0
    # Section outline of the FULL page: (offset, heading level, title) per markdown heading,
    # capped at `_MAX_OUTLINE`, with `outline_count` the true total. The tool surfaces it on a
    # big page so the model can jump straight to a section by `offset` instead of paging blindly.
    outline: tuple[tuple[int, int, str], ...] = ()
    outline_count: int = 0


def _find_offsets(
    text: str, needle: str, *, at_least: int, find_regex: bool = False
) -> tuple[tuple[int, ...], int]:
    """Character offsets of `needle` in `text` at/after `at_least`, as (capped_list,
    total_count). Case-insensitive. `find_regex` treats `needle` as a regular expression
    (the caller validates it compiles first); otherwise it is a literal substring. Both scan
    non-overlapping; the capped list bounds the match-map tokens while `total_count` still
    reports the true number of hits."""
    offsets: list[int] = []
    total = 0
    if find_regex:
        try:
            pattern = re.compile(needle, re.IGNORECASE)
        except re.error:
            return (), 0  # a bad pattern is caught+reported by the tool; defensive here
        for m in pattern.finditer(text, at_least):
            if m.start() == m.end():
                continue  # skip zero-width matches (e.g. `a*`) so they don't flood the map
            total += 1
            if len(offsets) < _MAX_MATCH_OFFSETS:
                offsets.append(m.start())
        return tuple(offsets), total
    hay = text.lower()
    pin = needle.lower()
    step = max(1, len(pin))
    i = hay.find(pin, at_least)
    while i != -1:
        total += 1
        if len(offsets) < _MAX_MATCH_OFFSETS:
            offsets.append(i)
        i = hay.find(pin, i + step)
    return tuple(offsets), total


def _outline(text: str) -> tuple[tuple[tuple[int, int, str], ...], int]:
    """The page's section outline as ((offset, level, title), …), capped, plus the true total.
    Each markdown heading in the FULL text becomes one entry whose offset the model can pass as
    `offset` to jump to that section — a table of contents for a page too big for one window."""
    entries: list[tuple[int, int, str]] = []
    total = 0
    for m in _HEADING_RE.finditer(text):
        total += 1
        if len(entries) < _MAX_OUTLINE:
            entries.append((m.start(), len(m.group(1)), m.group(2).strip()))
    return tuple(entries), total


def _window_and_find(
    text: str,
    *,
    url: str,
    title: str,
    links: tuple[str, ...],
    offset: int,
    find: str,
    find_regex: bool = False,
    body_truncated: bool,
) -> FetchResult:
    """Build the FetchResult: window `text` at `offset` (pagination) and, when `find` is
    given, re-anchor the window on the first keyword match at/after `offset` (so the model
    lands on the right SECTION, not a guessed offset) and attach the match map + section
    outline. `find_regex` matches `find` as a regular expression. Shared by the direct and
    reader paths so both page and locate identically."""
    match_offsets: tuple[int, ...] = ()
    match_count = 0
    start = offset
    if find:
        match_offsets, match_count = _find_offsets(
            text, find, at_least=offset, find_regex=find_regex
        )
        if match_offsets:
            start = max(0, match_offsets[0] - _FIND_LEAD)
    outline, outline_count = _outline(text)
    return FetchResult(
        url=url,
        title=title,
        text=text[start : start + _MAX_CHARS],
        links=links,
        truncated=body_truncated,
        offset=start,
        total_chars=len(text),
        find_term=find,
        match_offsets=match_offsets,
        match_count=match_count,
        outline=outline,
        outline_count=outline_count,
    )


def window_text(
    text: str, *, url: str, title: str, offset: int = 0, find: str = "", find_regex: bool = False
) -> FetchResult:
    """Wrap ready-made text (not fetched HTML — e.g. a composed YouTube view) in a FetchResult
    with the SAME offset/find windowing a fetched page gets, so the tool pages and keyword-jumps
    it identically. `truncated` is False: the caller already holds the whole text."""
    return _window_and_find(
        text,
        url=url,
        title=title,
        links=(),
        offset=max(0, offset),
        find=find,
        find_regex=find_regex,
        body_truncated=False,
    )


_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com", "youtu.be"}
)


def is_youtube_url(url: str) -> bool:
    """Whether `url` is a YouTube watch/short/share link — the pages web_fetch reads as a
    lightweight title+channel+description+captions view instead of scraping the JS shell."""
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.") in _YOUTUBE_HOSTS


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

    async def fetch(
        self, url: str, *, offset: int = 0, find: str = "", find_regex: bool = False
    ) -> FetchResult:
        """Fetch `url` and return one window of its extracted text. `offset` pages through a
        long page (window = `full_text[offset : offset+_MAX_CHARS]`). `find`, if given, jumps
        the window to the first occurrence of that keyword at/after `offset` and attaches the
        match map — so the model targets the right SECTION of a big page instead of reading
        (or blindly guessing an offset into) the whole thing."""
        offset = max(0, offset)
        try:
            result = await self._fetch_direct(url, offset=offset, find=find, find_regex=find_regex)
        except WebFetchError as exc:
            # A bot-wall (403/429) or unreachable host: the reader, if configured,
            # renders the page from a real browser and gets past what blocked us. But a
            # definitive 404/410 means the page does not exist — the reader would only
            # return the origin's soft "no such page" stub, which has enough text to read
            # as a successful fetch and hide the miss. Re-raise so the model SEES the 404
            # and corrects the URL instead of quietly reading an empty stub.
            if exc.status not in _GONE_STATUSES and self._reader_url:
                reader = await self._fetch_via_reader(
                    url, offset=offset, find=find, find_regex=find_regex
                )
                if reader is not None:
                    return reader
            raise
        # Only the FIRST page's emptiness (with no keyword miss) signals a JS-rendered shell
        # worth the reader; an empty window at offset>0, or a `find` that just didn't match,
        # is not a shell.
        empty_shell = offset == 0 and not find and not result.text.strip()
        if empty_shell and self._reader_url:
            # A JS-rendered shell our static extractor can't see — the reader runs the
            # page's scripts and returns the content that wasn't in the served HTML.
            reader = await self._fetch_via_reader(
                url, offset=offset, find=find, find_regex=find_regex
            )
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
                body, _ = await _read_capped(resp, max_bytes=max_bytes)
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            log.warning("web.fetch_bytes_failed", error=repr(exc), status=status)
            raise WebFetchError(_fetch_error_message(status), status=status) from exc
        return content_type, body

    async def _fetch_direct(
        self, url: str, *, offset: int = 0, find: str = "", find_regex: bool = False
    ) -> FetchResult:
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
                body, body_truncated = await _read_capped(resp)
        except httpx.HTTPError as exc:
            # Carry the real HTTP status so the model can tell a definitive 404 (wrong /
            # non-existent URL — search for the right one) from a transient glitch, rather
            # than reading the same "couldn't fetch right now" for both.
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            log.warning("web.fetch_failed", error=repr(exc), status=status)
            raise WebFetchError(_fetch_error_message(status), status=status) from exc
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
        # Window the full text at `offset` (pagination), or jump it to a `find` keyword;
        # `total_chars` lets the tool tell the model whether more remains. `truncated` means
        # the FULL text is itself short of the real page because the download hit the byte
        # cap — not recoverable by paging.
        return _window_and_find(
            text,
            url=final_url,
            title=title,
            links=links,
            offset=offset,
            find=find,
            find_regex=find_regex,
            body_truncated=body_truncated,
        )

    async def _fetch_via_reader(
        self, url: str, *, offset: int = 0, find: str = "", find_regex: bool = False
    ) -> FetchResult | None:
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
                raw, body_truncated = await _read_capped(resp)
                text = raw.decode(
                    _charset(resp.headers.get("content-type", "")) or "utf-8", errors="replace"
                )
        except (httpx.HTTPError, WebFetchError) as exc:
            log.warning("web.reader_failed", error=repr(exc))
            return None
        clean = text.strip()
        return _window_and_find(
            clean,
            url=url,
            title="",
            links=(),
            offset=offset,
            find=find,
            find_regex=find_regex,
            body_truncated=body_truncated,
        )

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


async def _read_capped(resp: httpx.Response, *, max_bytes: int = _MAX_BYTES) -> tuple[bytes, bool]:
    """Read a streamed response body up to `max_bytes` and stop — so an oversized
    or endless response is truncated, never buffered whole into memory (DoS guard).
    Returns (body, truncated) where `truncated` is True when the cap was hit and the
    tail was dropped, so the caller can tell the model it saw only the head."""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            truncated = True
            break
    await resp.aclose()
    return b"".join(chunks)[:max_bytes], truncated


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
