"""Fetch a URL and extract its readable text (docs/ASSISTANT.md "Agent
selection").

This is the one genuinely outbound leg of the jerv sandbox: it GETs a
model-supplied URL and returns the page's main text as plain prose the agent can
read. It is bounded deliberately — only the jerv agent (no knowledge-base access,
no owner data in context) can reach it, the response is size-capped, and the
extractor is a dependency-free HTML-to-text pass (scripts/styles dropped,
whitespace collapsed) rather than a full browser. Non-HTML text bodies pass
through; binary content is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
import structlog

log = structlog.get_logger()

_TIMEOUT = 20.0
_MAX_BYTES = 2_000_000  # cap the download; a page beyond this is truncated
_MAX_CHARS = 20_000  # cap the extracted text handed to the model
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
    without network."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    async def fetch(self, url: str) -> FetchResult:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise WebFetchError("only http(s) URLs can be fetched")
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport, follow_redirects=True
            ) as client:
                resp = await client.get(url, headers={"User-Agent": "JBrain2 jerv/1.0"})
                resp.raise_for_status()
                content_type = resp.headers.get("content-type", "")
                if not _is_textual(content_type):
                    kind = content_type or "unknown"
                    raise WebFetchError(f"that URL is not a text page ({kind})")
                body = resp.content[:_MAX_BYTES]
                html = body.decode(resp.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            log.warning("web.fetch_failed", error=repr(exc))
            raise WebFetchError("that URL could not be fetched right now") from exc
        title, text = _extract(html) if "html" in content_type.lower() else ("", html.strip())
        return FetchResult(url=url, title=title, text=text[:_MAX_CHARS])


def _is_textual(content_type: str) -> bool:
    ct = content_type.lower()
    return (
        not ct
        or ct.startswith("text/")
        or "html" in ct
        or "json" in ct
        or "xml" in ct
    )
