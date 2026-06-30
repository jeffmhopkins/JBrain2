"""Fetch a site's favicon ON-BOX, so the chat can show a tappable source logo
without the client ever touching the third-party host (docs/ASSISTANT.md "Agent
selection"; invariant #9 — agent output triggers no render-time external load).

The PWA renders a web citation as `<img src="/api/agent/favicon?host=…">`, a
same-origin request to our own API; this module is what that route calls to do
the actual outbound fetch server-side. It is the same posture as `web_fetch`: a
GET of a public host derived from content the model surfaced, behind the shared
SSRF guard (`guard_public_host`) so a poisoned host can't become a read primitive
into the box's own services or the cloud metadata endpoint.

Resolution mirrors a browser's: parse the homepage for declared `<link rel="…icon…">`
hrefs, then fall back to the well-known `/favicon.ico`. Only a recognised RASTER
image (png/jpeg/gif/webp/ico) is accepted — an SVG favicon is refused, since SVG
can carry script and we serve these bytes back to the PWA. Everything is
size-capped; any failure yields None and the PWA falls back to a plain initial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
import structlog

from jbrain.web.fetch import BROWSER_HEADERS, WebFetchError, guard_public_host

log = structlog.get_logger()

_TIMEOUT = 8.0
_MAX_HTML_BYTES = 1_000_000  # homepage read cap, just to find <link rel=icon>
_MAX_ICON_BYTES = 200_000  # a favicon beyond this is refused (not a favicon)
_MAX_CANDIDATES = 4  # declared icons to try before the /favicon.ico fallback

# `<link rel="… icon …" href="…">` in either attribute order. We only keep rels
# whose token set includes "icon" (icon / shortcut icon / apple-touch-icon).
_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_REL_RE = re.compile(r"""\brel\s*=\s*["']?([^"'>]+)""", re.IGNORECASE)
_HREF_RE = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


@dataclass(frozen=True)
class FaviconResult:
    """A fetched favicon: the validated media type and the raw bytes, ready to
    serve back to the PWA with that content type."""

    content_type: str
    data: bytes


def _sniff_icon_type(header: bytes) -> str | None:
    """The raster image type implied by leading magic bytes, or None. SVG and any
    unrecognised body return None and are refused (we never serve script-bearing
    or unknown bytes back to the client)."""
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[:4] == b"\x00\x00\x01\x00":  # ICO
        return "image/x-icon"
    return None


def normalize_host(raw: str) -> str | None:
    """The bare lowercase hostname from a host or full URL, or None if there isn't
    one. `example.com`, `https://example.com/x`, and `EXAMPLE.com:443` all reduce
    to `example.com` — so the favicon cache keys one icon per site."""
    raw = (raw or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "//" in raw else f"//{raw}", scheme="https")
    return parsed.hostname.lower() if parsed.hostname else None


def _icon_candidates(html: str, *, origin: str) -> list[str]:
    """Absolute icon URLs declared on the homepage (`<link rel=…icon… href>`), in
    document order, then the well-known `/favicon.ico` — deduped, capped."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in _LINK_RE.findall(html):
        rel = _REL_RE.search(tag)
        href = _HREF_RE.search(tag)
        if not rel or not href:
            continue
        if "icon" not in rel.group(1).lower().split():
            continue
        url = urljoin(origin, href.group(1).strip())
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= _MAX_CANDIDATES:
            break
    fallback = urljoin(origin, "/favicon.ico")
    if fallback not in seen:
        out.append(fallback)
    return out


class FaviconFetcher:
    """Fetch and validate one site's favicon. `transport` is injectable so tests
    run without network; when supplied, the SSRF DNS check is skipped (there is no
    real network to reach), matching `WebFetcher`."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    async def fetch(self, host: str) -> FaviconResult | None:
        normalized = normalize_host(host)
        if not normalized:
            return None
        origin = f"https://{normalized}"
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport, follow_redirects=False
            ) as client:
                html = await self._homepage(client, origin)
                candidates = _icon_candidates(html, origin=origin)
                for url in candidates:
                    icon = await self._try_icon(client, url)
                    if icon is not None:
                        return icon
        except (httpx.HTTPError, WebFetchError) as exc:
            log.info("favicon.fetch_failed", host=normalized, error=repr(exc))
        return None

    async def _homepage(self, client: httpx.AsyncClient, origin: str) -> str:
        """The homepage HTML (capped), or "" when it can't be read — a site with no
        readable homepage still gets the `/favicon.ico` fallback from an empty parse."""
        try:
            guard_public_host(origin, skip_dns=self._transport is not None)
            resp = await client.get(origin, headers=BROWSER_HEADERS)
            if resp.is_error or "html" not in resp.headers.get("content-type", "").lower():
                return ""
            return resp.text[:_MAX_HTML_BYTES]
        except (httpx.HTTPError, WebFetchError):
            return ""

    async def _try_icon(self, client: httpx.AsyncClient, url: str) -> FaviconResult | None:
        """GET one candidate icon URL and return it only if it is a recognised
        raster image within the size cap; otherwise None (try the next candidate).
        Re-guards the host: a candidate may live on a different (CDN) host."""
        try:
            guard_public_host(url, skip_dns=self._transport is not None)
            resp = await client.get(url, headers=BROWSER_HEADERS)
        except (httpx.HTTPError, WebFetchError):
            return None
        if resp.is_error:
            return None
        data = resp.content[:_MAX_ICON_BYTES]
        if len(resp.content) > _MAX_ICON_BYTES or not data:
            return None
        content_type = _sniff_icon_type(data[:16])
        if content_type is None:
            return None
        return FaviconResult(content_type=content_type, data=data)
