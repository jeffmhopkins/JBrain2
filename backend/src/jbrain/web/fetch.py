"""Fetch a URL and extract its readable text (docs/ASSISTANT.md "Agent
selection").

This is the one genuinely outbound leg of the jerv sandbox: it GETs a
model-supplied URL and returns the page's main content as clean **markdown**
(headings, lists, emphasis, inline links, fenced code), PLUS the links the page
points at (resolved to absolute URLs) so the agent can navigate — follow a link, a
"next page", a file in a repository — by fetching one of them, not just read a
single page. It is bounded deliberately — only the jerv agent (no knowledge-base
access, no owner data in context) can reach it, the response and the link list are
size-capped, and the extractor is a dependency-free HTML→markdown pass (scripts,
styles, and page boilerplate — nav/header/footer/aside — dropped, whitespace
collapsed outside code) rather than a full browser. Non-HTML text bodies pass
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
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 20.0
_MAX_BYTES = 2_000_000  # cap the download; a page beyond this is truncated
_MAX_CHARS = 20_000  # cap the extracted text handed to the model
_MAX_LINKS = 40  # cap the links surfaced for navigation; a link-heavy page is trimmed
_MAX_REDIRECTS = 4
# Tags whose entire subtree is dropped: non-content (script/style/svg/…) plus the
# page-boilerplate landmarks (nav/header/footer/aside) a readability pass would
# strip. A cheap heuristic, not full main-content scoring, but it removes most menus
# and chrome so the markdown is closer to the page's actual content.
_DROP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside"}
)
# Tags that just force a line break in the prose stream (their own markup carries no
# markdown). Headings, list items, emphasis, links, and code are handled explicitly.
_BLOCK_TAGS = frozenset({"p", "div", "tr", "section", "article"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# Inline emphasis → markdown markers (the same token opens and closes).
_EMPHASIS = {"strong": "**", "b": "**", "em": "*", "i": "*"}


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


def _normalize_prose(raw: str) -> str:
    """Collapse a prose run's incidental HTML whitespace into tidy lines, keeping at
    most one blank line between paragraphs (markdown ignores the rest)."""
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def _format_code(raw: str) -> str:
    """A `<pre>` block as a fenced markdown code block, indentation preserved (only
    trailing whitespace and surrounding blank lines trimmed) — the one place we do
    NOT collapse whitespace, since it carries the code's meaning."""
    code = "\n".join(line.rstrip() for line in raw.strip("\n").splitlines())
    return f"```\n{code}\n```" if code.strip() else ""


class _Extractor(HTMLParser):
    """A dependency-free HTML→markdown pass: drop non-content/boilerplate subtrees,
    render headings/lists/emphasis/links/code as markdown, and gather the page's
    links (resolved to absolute http(s) URLs against `base`) so the agent can follow
    them. Not a full DOM/readability engine — a pragmatic streaming heuristic."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base = base
        self._out: list[str] = []  # finished blocks (prose + fenced code), in order
        self._buf: list[str] = []  # the prose run being built, pre-normalization
        self._code: list[str] = []  # raw text inside the current <pre>
        self._skip_depth = 0  # >0 inside a dropped/boilerplate subtree
        self._pre_depth = 0  # >0 inside a <pre> (verbatim, no markup)
        self._in_title = False
        self.title = ""
        self.hrefs: list[str] = []
        self._link_href: str | None = None  # set while inside an <a>; its text buffers
        self._link_buf: list[str] = []

    def _flush_prose(self) -> None:
        if text := _normalize_prose("".join(self._buf)):
            self._out.append(text)
        self._buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
            return
        if tag == "pre":
            self._flush_prose()
            self._pre_depth += 1
            return
        if self._pre_depth or self._link_href is not None:
            # Inside <pre> (verbatim) or an <a> (text-only): ignore nested markup.
            return
        if tag == "a":
            href = next((v for n, v in attrs if n == "href" and v), None)
            if href:
                self._link_href = href
                self._link_buf = []
            return
        if tag in _HEADINGS:
            self._buf.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self._buf.append("\n- ")
        elif tag == "code":
            self._buf.append("`")
        elif tag in _EMPHASIS:
            self._buf.append(_EMPHASIS[tag])
        elif tag == "br" or tag in _BLOCK_TAGS:
            self._buf.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
            return
        if tag == "pre":
            if self._pre_depth:
                self._pre_depth -= 1
                if self._pre_depth == 0:
                    if block := _format_code("".join(self._code)):
                        self._out.append(block)
                    self._code = []
            return
        if self._pre_depth:
            return
        if tag == "a" and self._link_href is not None:
            self._close_link()
            return
        if self._link_href is not None:
            return
        if tag == "code" or tag in _EMPHASIS:
            self._buf.append(_EMPHASIS.get(tag, "`"))
        elif tag in _HEADINGS or tag in _BLOCK_TAGS:
            self._buf.append("\n")

    def _close_link(self) -> None:
        """Emit the just-closed anchor as a markdown link, resolving its href to an
        absolute http(s) URL (and recording it for the navigable link list). A
        non-http(s) target (mailto:, javascript:) keeps only its text."""
        text = " ".join("".join(self._link_buf).split())
        absolute = urldefrag(urljoin(self._base, (self._link_href or "").strip()))[0]
        self._link_href = None
        self._link_buf = []
        if urlparse(absolute).scheme in ("http", "https"):
            self.hrefs.append(absolute)
            self._buf.append(f"[{text}]({absolute})" if text else absolute)
        elif text:
            self._buf.append(text)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        if self._pre_depth:
            self._code.append(data)
            return
        if self._link_href is not None:
            self._link_buf.append(data)
            return
        self._buf.append(data)

    def markdown(self) -> str:
        self._flush_prose()
        body = "\n\n".join(block for block in self._out if block)
        while "\n\n\n" in body:
            body = body.replace("\n\n\n", "\n\n")
        return body.strip()


def _extract(html: str, *, base: str) -> tuple[str, str, list[str]]:
    parser = _Extractor(base)
    parser.feed(html)
    return parser.title.strip(), parser.markdown(), parser.hrefs


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
        if "html" in content_type.lower():
            title, text, hrefs = _extract(html, base=final_url)
            links = _collect_links(hrefs, base=final_url)
        else:
            title, text, links = "", html.strip(), ()
        return FetchResult(url=final_url, title=title, text=text[:_MAX_CHARS], links=links)

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
