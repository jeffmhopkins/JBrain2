"""A dependency-free HTML → markdown pass, shared by the jerv web fetch and the
archivist's email triage.

Drops non-content/boilerplate subtrees (scripts, styles, nav/header/footer/aside),
renders headings, lists, emphasis, inline links, and fenced code as markdown, and
collapses incidental whitespace outside code. It is a pragmatic streaming heuristic
over `html.parser`, not a full DOM/readability engine — but it turns a wall of HTML
(an email's body, a fetched page) into the compact, readable text an LLM reasons over
far better than raw markup, with no third-party dependency.

`extract_page` additionally returns the title and the page's outbound links (for the
web fetcher's navigation); `html_to_markdown` is the body-only shortcut email triage
uses.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

# Tags whose entire subtree is dropped: non-content (script/style/svg/…) plus the
# page-boilerplate landmarks (nav/header/footer/aside) a readability pass would strip.
_DROP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "nav", "header", "footer", "aside"}
)
# Tags that just force a line break in the prose stream (their own markup carries no
# markdown). Headings, list items, emphasis, links, and code are handled explicitly.
_BLOCK_TAGS = frozenset({"p", "div", "tr", "section", "article"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
# Inline emphasis → markdown markers (the same token opens and closes).
_EMPHASIS = {"strong": "**", "b": "**", "em": "*", "i": "*"}


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
    """Drop non-content/boilerplate subtrees, render headings/lists/emphasis/links/code
    as markdown, and gather the page's links (resolved to absolute http(s) URLs against
    `base`). Not a full DOM/readability engine — a pragmatic streaming heuristic."""

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


def extract_page(html: str, *, base: str) -> tuple[str, str, list[str]]:
    """(title, markdown, hrefs) for a full HTML page — the web fetcher's view, where
    `base` resolves relative links to absolute http(s) URLs for navigation."""
    parser = _Extractor(base)
    parser.feed(html)
    return parser.title.strip(), parser.markdown(), parser.hrefs


def html_to_markdown(html: str, *, base: str = "") -> str:
    """Just the markdown body of an HTML fragment (no title/links) — what email triage
    feeds the classifier. `base` is usually irrelevant for email (links are absolute);
    a relative href with no base simply keeps its text."""
    parser = _Extractor(base)
    parser.feed(html)
    return parser.markdown()
