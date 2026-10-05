"""The data a page already carries, read without a browser (BROWSER_FAST_LOOP_PLAN L1).

Many pages that render as a JavaScript shell — or hide the fact behind a click — ship the
answer in the HTML anyway: schema.org JSON-LD (an event's times, a store's hours), microdata,
or the framework's hydration state (`__NEXT_DATA__`, Nuxt's payload, `__APOLLO_STATE__`,
Gatsby's page-data). Reading it is generic — no site knowledge, only the standard and the
framework conventions — and turns a browse run into a plain fetch.

The output is flattened `path: value` lines, deduplicated and capped. It is page data like
the page's text: the tool fences it as quoted data, and invisible and control characters are
stripped here so nothing hides in it.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# What one page's embedded data may add to a fetch result (~1.5k tokens). Enough for a
# store's hours or a day of showtimes; a hydration blob is mostly config and is cut here.
MAX_STRUCTURED_CHARS = 6_000
_MAX_VALUE_CHARS = 300
_MAX_DEPTH = 12
# The most JSON one script may hold before it is skipped: a multi-megabyte state blob is a
# cache dump, not a page's facts, and parsing it is the cost.
_MAX_SCRIPT_CHARS = 2_000_000

_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.IGNORECASE | re.DOTALL)
_TYPE_ATTR = re.compile(r"""\btype\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
_ID_ATTR = re.compile(r"""\bid\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
# `window.__APOLLO_STATE__ = {...};` and the like: a JSON object assigned in a script.
_ASSIGNED_STATE = re.compile(
    r"(?:window\.)?(__APOLLO_STATE__|__NUXT__|__INITIAL_STATE__|__PRELOADED_STATE__)\s*=\s*",
)
_STATE_IDS = frozenset({"__NEXT_DATA__", "__NUXT_DATA__"})
# Keys that are plumbing on every framework, never a page's facts.
_SKIP_KEYS = frozenset(
    {
        "@context",
        "buildId",
        "assetPrefix",
        "runtimeConfig",
        "isFallback",
        "gssp",
        "gsp",
        "scriptLoader",
        "locales",
        "defaultLocale",
        "appGip",
        "__N_SSP",
        "__N_SSG",
        "_sentryTraceData",
        "_sentryBaggage",
        "config",
        "i18n",
        "translations",
        "messages",
    }
)
# A value that is an opaque token (a hash, a base64 blob, an id) says nothing to a reader.
_OPAQUE = re.compile(r"^[A-Za-z0-9+/=_-]{24,}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def is_gatsby(html: str) -> bool:
    """Whether `html` is a Gatsby page, whose data lives in a sibling page-data.json."""
    return 'id="___gatsby"' in html or "id='___gatsby'" in html


def gatsby_page_data_url(url: str) -> str | None:
    """The page-data.json URL Gatsby serves for `url`'s path (same origin), or None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    path = parts.path.strip("/") or "index"
    return urlunsplit((parts.scheme, parts.netloc, f"/page-data/{path}/page-data.json", "", ""))


def gatsby_lines(body: str) -> list[str]:
    """The flattened `result.data` of a Gatsby page-data.json body."""
    try:
        doc = json.loads(body[:_MAX_SCRIPT_CHARS])
    except ValueError:
        return []
    data = doc.get("result", {}).get("data") if isinstance(doc, dict) else None
    return list(_flatten(data, "gatsby")) if data is not None else []


def embedded_data(html: str, *, extra: list[str] | None = None) -> str:
    """The page's embedded structured data as capped `path: value` lines, or ""."""
    if not html:
        return ""
    lines: list[str] = []
    for attrs, body in _SCRIPT.findall(html):
        if len(body) > _MAX_SCRIPT_CHARS:
            continue
        kind = (_TYPE_ATTR.search(attrs) or _EMPTY).group(1).lower()
        script_id = (_ID_ATTR.search(attrs) or _EMPTY).group(1)
        if kind == "application/ld+json":
            lines += _json_lines(body, "ld")
        elif script_id in _STATE_IDS:
            lines += _json_lines(body, script_id.strip("_").lower(), next_data=True)
        else:
            lines += _assigned_lines(body)
    lines += _microdata_lines(html)
    lines += extra or []
    return _cap(lines)


class _NoMatch:
    def group(self, _n: int) -> str:
        return ""


_EMPTY = _NoMatch()


def _json_lines(body: str, root: str, *, next_data: bool = False) -> list[str]:
    try:
        doc = json.loads(body.strip().removeprefix("<!--").removesuffix("-->"))
    except ValueError:
        return []
    if next_data and isinstance(doc, dict) and isinstance(doc.get("props"), dict):
        # Next.js: the page's own data is `props.pageProps`; the rest is router plumbing.
        doc = doc["props"].get("pageProps", doc["props"])
    return list(_flatten(doc, root))


def _assigned_lines(body: str) -> list[str]:
    out: list[str] = []
    for match in _ASSIGNED_STATE.finditer(body):
        rest = body[match.end() :]
        if not rest.startswith(("{", "[")):
            continue  # Nuxt 2's `(function(a,b){…})` is code, not data
        try:
            doc, _ = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            continue
        out += _flatten(doc, match.group(1).strip("_").lower())
    return out


def _flatten(node: Any, path: str, depth: int = 0) -> Iterator[str]:
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, dict):
        kind = node.get("@type")
        if isinstance(kind, str) and kind:
            # A schema.org node is named by its type ("Event.startDate"), not its nesting.
            path = kind
        for key, value in node.items():
            if key in _SKIP_KEYS or key == "@type" or key.startswith("__"):
                continue
            yield from _flatten(value, f"{_tail(path)}.{key}", depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _flatten(item, path, depth + 1)
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, int | float):
        yield f"{path}: {node}"
    elif isinstance(node, str):
        value = _clean(node)
        if value and not _OPAQUE.match(value):
            yield f"{path}: {value}"


def _tail(path: str) -> str:
    """The last two segments of a path: enough to say what a value is, short enough not to
    spend the cap on nesting."""
    return ".".join(path.split(".")[-2:])


def _clean(value: str) -> str:
    value = "".join(
        c
        for c in unicodedata.normalize("NFKC", value)
        if unicodedata.category(c) != "Cf" and not _CONTROL.match(c)
    )
    value = " ".join(re.sub(r"<[^>]{0,200}>", " ", value).split())
    if len(value) > _MAX_VALUE_CHARS:
        value = value[: _MAX_VALUE_CHARS - 1].rstrip() + "…"
    return value


class _Microdata(HTMLParser):
    """`itemprop` values: the `content` (or `datetime`) attribute, else the element's text."""

    _VOID = frozenset({"meta", "link", "img", "br", "hr", "input", "source"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._open: list[tuple[str, str | None, list[str]]] = []
        self._types: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if "itemtype" in a:
            self._types.append(a["itemtype"].rstrip("/").rsplit("/", 1)[-1])
        prop = a.get("itemprop")
        given = a.get("content") or a.get("datetime")
        if prop and given:
            self._emit(prop, given)
            prop = None
        if tag in self._VOID:
            return
        self._open.append((tag, prop if prop and "itemscope" not in a else None, []))

    def handle_endtag(self, tag: str) -> None:
        while self._open:
            open_tag, prop, text = self._open.pop()
            if prop:
                self._emit(prop, " ".join(text))
            if self._open and text:
                self._open[-1][2].extend(text)
            if open_tag == tag:
                return

    def handle_data(self, data: str) -> None:
        if self._open and data.strip():
            self._open[-1][2].append(data.strip())

    def _emit(self, prop: str, value: str) -> None:
        value = _clean(value)
        if value and not _OPAQUE.match(value):
            where = self._types[-1] if self._types else "item"
            self.lines.append(f"microdata.{where}.{prop}: {value}")


def _microdata_lines(html: str) -> list[str]:
    if "itemprop" not in html:
        return []
    parser = _Microdata()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed page just yields what parsed
        pass
    return parser.lines


def _cap(lines: list[str]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    size = 0
    for line in lines:
        if line in seen:
            continue
        if size + len(line) + 1 > MAX_STRUCTURED_CHARS:
            break
        seen.add(line)
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)
