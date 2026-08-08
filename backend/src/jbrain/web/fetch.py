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
import json
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
# The challenge solver drives a stealth browser that WAITS OUT a JS/managed challenge, so it
# is far slower than a plain fetch: `maxTimeout` is what the solver spends clearing the wall,
# and the HTTP timeout sits above it so we don't abort a solve that's about to succeed.
_SOLVER_MAX_TIMEOUT_MS = 60_000
_SOLVER_TIMEOUT = 70.0
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
# Cap the POST request body (a search/JSON API query). A model-built body is small; this
# bounds it so an accidental huge payload can't be shipped off-box in one call. The
# response is bounded by `_read_capped` (`_MAX_BYTES`) like the GET path.
_MAX_POST_BODY = 64 * 1024
# The content types a POST body may declare — a JSON payload or an HTML form encoding, the
# two shapes a search/JSON API endpoint expects. Nothing else (no multipart, no arbitrary
# type) so the POST path stays a narrow API-call primitive, not a general uploader.
POST_CONTENT_TYPES = frozenset({"application/json", "application/x-www-form-urlencoded"})
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


class SearchFormError(WebFetchError):
    """The fetched page is an interactive SEARCH FORM, not results — the query was never
    executed, so it is NOT evidence the record is absent. A distinct subclass so `fetch()`
    surfaces it WITHOUT the reader/solver recovery a bot-wall gets: a renderer cannot submit the
    form either, so escalation would be pure latency waste (`_is_search_form_page`)."""


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


# --- Bot-challenge / anti-bot interstitial detection -------------------------
# A Cloudflare/DataDome/Imperva/etc. wall often answers 200 with a CHALLENGE page
# ("Just a moment…", "Update Browser Required", "enable JavaScript and cookies") instead
# of the article — served directly, or (the common case here) laundered through the
# reader, which renders the challenge in a real browser and hands its markdown back as a
# clean 200. Without this the pipeline ingested that junk as content and CITED it — the
# "Update Browser Required" floridapolitics.com miss the reader turned into a fake source.
# Detection makes a blocked page an honest failure: the direct path raises (status left
# None, so the reader is still tried exactly as for a 403), the reader path returns None
# (so `fetch` surfaces the original block), and neither ever becomes a WebSource.
#
# Precision over recall — a false positive DROPS A REAL ARTICLE, so the bar is "never flag
# legitimate prose". Tier 1 markers (title/body strings that don't occur in real content)
# fire at any length; every weaker marker is gated on a SHORT page (a challenge page is
# tens of words, an article is hundreds) and/or a co-occurring combo, so an article that
# merely mentions Cloudflare is not flagged. Signatures from FlareSolverr, microlinkhq's
# is-antibot, and current Cloudflare/DataDome/Imperva/Akamai/PerimeterX challenge markup.
_CHALLENGE_TITLE_MARKERS = (
    "just a moment",
    "attention required! | cloudflare",
    "access to this page has been denied",
    "update browser required",
)
_CHALLENGE_BODY_MARKERS = (  # canonical phrases — never in real prose, so length-independent
    "enable javascript and cookies to continue",
    "needs to review the security of your connection before proceeding",
    "checking your browser before accessing",
    "request unsuccessful. incapsula incident id",
    "access to this page has been denied because we believe you are using automation tools",
    "please enable js and disable any ad blocker",
    "press & hold to confirm you are a human",
    "verify you are human by completing the action below",
)
_CHALLENGE_SHORT_WORDS = 200  # a challenge page is tiny; a real article clears this easily
_CHALLENGE_TINY_WORDS = 60
_CHALLENGE_SHORT_MARKERS = (  # specific, but gated to a short page for false-positive safety
    "update browser required",
    "sorry, you have been blocked",
    "please turn javascript on and reload the page",
    "captcha-delivery.com",
    "checking if the site connection is secure",
)
_CHALLENGE_WEAK_MARKERS = (  # risky single words — counted only on a near-empty page
    "cloudflare",
    "captcha",
    "ddos-guard",
    "incapsula",
    "datadome",
)


def _is_challenge_page(title: str, text: str) -> bool:
    """Whether (title, extracted text) is a bot-challenge interstitial rather than real
    content — see the block comment above for the seams that act on a True verdict and why
    this is tuned hard for precision. Tier 1 fires on canonical title/body strings at any
    length; Tier 2 on a short page with a specific marker or a combo; Tier 3 on a near-empty
    page dominated by a single weak marker."""
    t = title.strip().lower()
    b = text.lower()
    if any(m in t for m in _CHALLENGE_TITLE_MARKERS):
        return True
    if any(m in b for m in _CHALLENGE_BODY_MARKERS):
        return True
    words = len(text.split())
    if words < _CHALLENGE_SHORT_WORDS:
        if any(m in b for m in _CHALLENGE_SHORT_MARKERS):
            return True
        if (
            ("access denied" in b and "don't have permission to access" in b and "reference #" in b)
            or ("cloudflare" in b and "ray id" in b and ("blocked" in b or "unable to access" in b))
            or ("you have been blocked" in b and "why did this happen" in b)
            or ("verifying you are human" in b and "few seconds" in b)
            or ("verify you are a human" in b and ("perimeterx" in b or "px-captcha" in b))
        ):
            return True
    return words < _CHALLENGE_TINY_WORDS and any(m in b for m in _CHALLENGE_WEAK_MARKERS)


# --- "Search form, not results" detection (honest degradation for un-queryable portals) ------
# A dynamic government portal (FL Sunbiz `CorporationSearch/ByName`, FL DFS DICE `pb_*.asp`)
# answers a bare GET with its empty SEARCH FORM, not results — `web_fetch` can't run the form's
# client-side / POST query. Ingested as content, that empty form read as "no record found" — the
# exact laundering that let a candidate profile assert "no entity" when the search never ran.
# On a True verdict `_fetch_direct` raises `SearchFormError`, which `fetch()` surfaces WITHOUT
# the reader/solver recovery a bot-wall gets: a renderer cannot submit the form either, so
# escalation would be pure latency waste (a search form IS the served content, not a wall to
# render past).
#
# Precision over recall — a false positive DROPS A REAL PAGE — so this fires ONLY when the main
# content genuinely IS a search form: a SHORT extracted body (the form labels + boilerplate, not
# an article) that carries a query input + a submit control in the raw HTML, AND shows no sign
# the query actually ran (no result-bearing table in the raw HTML, no "results"/"no results"
# text). A nav search box on a real article, or a genuine (even empty) results page, is not
# flagged. The detector needs the RAW HTML; on the reader path (markdown only) it is a no-op, so
# form detection relies on the direct path (where the un-queried portal form is served).
_FORM_SHORT_WORDS = 200  # a bare form is form labels + boilerplate; a real article clears this
_RESULTS_PRESENT_MARKERS = (  # the query DID run (even to empty) — a real results page, not a form
    "no results",
    "no records found",
    "no record found",
    "no matching records",
    "0 results",
    "did not match",
    "your search returned",
    "results found",
    "showing results",
    "search results for",
)
_TR_RE = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)


def _has_search_form(h: str) -> bool:
    """Whether the raw HTML's main interactive element is a search form — a `<form>` carrying a
    free-text query field (or a select/textarea) AND a submit control. Deliberately structural
    (not phrase-based) so it generalizes across portals."""
    if "<form" not in h:
        return False
    has_query_field = (
        'type="text"' in h
        or "type='text'" in h
        or 'type="search"' in h
        or "type='search'" in h
        or "<select" in h
        or "<textarea" in h
    )
    has_submit = (
        'type="submit"' in h
        or "type='submit'" in h
        or "<button" in h
        or 'value="search"' in h
        or "value='search'" in h
    )
    return has_query_field and has_submit


def _has_result_rows(h: str) -> bool:
    """A result-bearing table in the RAW HTML — ≥2 rows each carrying a data cell with a link (a
    results list linking to detail pages, the shape a portal's SearchResults page has). A bare
    form, or a layout table with no linked data rows, does not trip it — so a real (even empty)
    results page whose rows the extractor dropped is never mistaken for an unexecuted form."""
    linked = sum(1 for r in _TR_RE.findall(h) if "<td" in r.lower() and "<a " in r.lower())
    return linked >= 2


def _is_search_form_page(title: str, text: str, html: str) -> bool:
    """Whether (title, extracted text, raw HTML) is an un-queried search FORM rather than
    content or results — see the block comment above for the seams and the precision bar. False
    whenever the raw HTML is unavailable (the reader's markdown path), so detection rides the
    direct path where the portal serves the form."""
    if not html:
        return False
    b = text.lower()
    if any(m in b for m in _RESULTS_PRESENT_MARKERS):
        return False  # the query ran (even to empty) — a real results page, not a bare form
    if len(text.split()) >= _FORM_SHORT_WORDS:
        return False  # a content-rich page with an incidental search box — never flag prose
    if _has_result_rows(html):
        return False  # results present in the raw HTML even if the extractor dropped them
    return _has_search_form(html)


# The distinct, model-visible observation for a True verdict — NOT the form text and NOT a bare
# "no readable text": the query was never executed, so this is not evidence of absence.
_SEARCH_FORM_MESSAGE = (
    "that URL is an interactive search FORM, not results — the query was not executed, so this "
    "is NOT evidence the record does not exist. Find the portal's result/detail URL (the page "
    "its search leads to), a cached copy, or an official index/record page, or web_search for "
    "the specific record; do not read this as 'no record found'."
)


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
    # The WHOLE extracted text (not just the `text` window) — what a caller persists so a
    # long page/transcript can be re-read and paged across turns without a network re-fetch
    # (docs/plans/CROSS_TURN_TOOL_RESULTS_PLAN.md). Empty on a result built without it.
    full_text: str = ""


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
        full_text=text,
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
        solver_url: str = "",
    ):
        self._transport = transport
        self._reader_url = reader_url.rstrip("/")
        # A pinned bot-challenge solver (Byparr / FlareSolverr-compatible) the fetch escalates
        # to when the reader itself returns a challenge interstitial. Empty = tier disabled.
        self._solver_url = solver_url.rstrip("/")

    async def fetch(
        self,
        url: str,
        *,
        offset: int = 0,
        find: str = "",
        find_regex: bool = False,
        method: str = "GET",
        body: str = "",
        content_type: str = "application/json",
    ) -> FetchResult:
        """Fetch `url` and return one window of its extracted text. `offset` pages through a
        long page (window = `full_text[offset : offset+_MAX_CHARS]`). `find`, if given, jumps
        the window to the first occurrence of that keyword at/after `offset` and attaches the
        match map — so the model targets the right SECTION of a big page instead of reading
        (or blindly guessing an offset into) the whole thing.

        `method="POST"` sends `body` (with `content_type`) to a JSON/search API endpoint
        instead of a plain GET — the same SSRF host/redirect guard, no reader/solver
        escalation (those render a GET URL), no youtube path. A JSON response comes back
        pretty-printed; anything else is extracted like a text page."""
        offset = max(0, offset)
        if method.upper() == "POST":
            return await self._fetch_post(
                url,
                body=body,
                content_type=content_type,
                offset=offset,
                find=find,
                find_regex=find_regex,
            )
        try:
            result = await self._fetch_direct(url, offset=offset, find=find, find_regex=find_regex)
        except SearchFormError:
            # An un-queried search form: the reader/solver can't submit it either, so recovery
            # would only burn latency. Surface the "query not executed" observation directly.
            raise
        except WebFetchError as exc:
            # A bot-wall (403/429/challenge) or unreachable host: escalate to the recovery
            # tiers (reader, then solver), which render the page from a real browser and get
            # past what blocked us. But a definitive 404/410 means the page does not exist —
            # a renderer would only return the origin's soft "no such page" stub, which has
            # enough text to read as a successful fetch and hide the miss. Re-raise so the
            # model SEES the 404 and corrects the URL instead of quietly reading an empty stub.
            if exc.status not in _GONE_STATUSES:
                recovered = await self._recover(
                    url, offset=offset, find=find, find_regex=find_regex, require_text=False
                )
                if recovered is not None:
                    return recovered
            raise
        # Only the FIRST page's emptiness (with no keyword miss) signals a JS-rendered shell
        # worth recovering; an empty window at offset>0, or a `find` that just didn't match,
        # is not a shell.
        empty_shell = offset == 0 and not find and not result.text.strip()
        if empty_shell:
            # A JS-rendered shell our static extractor can't see — a renderer runs the page's
            # scripts and returns the content that wasn't in the served HTML.
            recovered = await self._recover(
                url, offset=offset, find=find, find_regex=find_regex, require_text=True
            )
            if recovered is not None:
                return recovered
        return result

    async def _recover(
        self,
        url: str,
        *,
        offset: int,
        find: str,
        find_regex: bool,
        require_text: bool,
    ) -> FetchResult | None:
        """Try the recovery tiers in order — the reader, then the heavier challenge solver —
        returning the first that yields a usable (non-challenge) page, else None. Each tier
        returns None when it is unconfigured, fails, or hands back a challenge interstitial,
        so escalation is simply "try the next one". `require_text` additionally rejects an
        empty result (the JS-shell case, where a tier that renders nothing hasn't recovered)."""
        for tier in (self._fetch_via_reader, self._fetch_via_solver):
            recovered = await tier(url, offset=offset, find=find, find_regex=find_regex)
            if recovered is not None and (not require_text or recovered.text.strip()):
                return recovered
        return None

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
        raw_html = ""
        if "pdf" in content_type.lower():
            # A linked PDF (common in research) is real text, not a dead end: pull its
            # text layer with PyMuPDF rather than refusing the way a binary body is.
            title, text, links = "", _extract_pdf_text(body), ()
        elif "html" in content_type.lower():
            raw_html = body.decode(_charset(content_type) or "utf-8", errors="replace")
            title, text, hrefs = _extract_html(raw_html, base=final_url)
            links = _collect_links(hrefs, base=final_url)
        else:
            text = body.decode(_charset(content_type) or "utf-8", errors="replace").strip()
            title, links = "", ()
        # A bot-wall that answered 200 with a challenge page (not the article): raise rather
        # than return the junk, so it never becomes a cited source. Status stays None, so
        # `fetch` still tries the reader — the same recovery a 403 bot-wall gets.
        if _is_challenge_page(title, text):
            log.warning("web.challenge_blocked", url=final_url, via="direct", title=title[:80])
            raise WebFetchError("that URL returned a bot-challenge page, not its content")
        # An un-queried search form (a dynamic gov portal served as its empty form): raise a
        # DISTINCT error so `fetch()` surfaces "the query was not executed" WITHOUT trying the
        # reader/solver — a renderer can't submit the form, so that recovery is pure waste.
        if _is_search_form_page(title, text, raw_html):
            log.info("web.search_form_detected", url=final_url, via="direct", title=title[:80])
            raise SearchFormError(_SEARCH_FORM_MESSAGE)
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

    async def _fetch_post(
        self,
        url: str,
        *,
        body: str,
        content_type: str,
        offset: int = 0,
        find: str = "",
        find_regex: bool = False,
    ) -> FetchResult:
        """POST `body` to `url` and window the response — for hitting a JSON/search API
        endpoint the GET-only path can't reach. Shares the GET path's SSRF discipline
        EXACTLY (`_send_following_safe_redirects`: httpx auto-redirect off, every hop's host
        re-checked), so a POST can't be pointed at the box's own services or 30x its way
        there. The request body is capped (`_MAX_POST_BODY`) and the response bounded
        (`_read_capped`) like a GET. No reader/solver escalation (those render a GET URL) and
        no youtube path — this is a bare API call. A JSON response is returned pretty-printed;
        any other text body is extracted like an HTML/text page."""
        payload = body.encode("utf-8")
        if len(payload) > _MAX_POST_BODY:
            raise WebFetchError("that POST body is too large to send")
        headers = {
            **BROWSER_HEADERS,
            "Content-Type": content_type,
            # Ask for JSON first — these endpoints are APIs — but accept a text/HTML body too.
            "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        }
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT, transport=self._transport, follow_redirects=False
            ) as client:
                resp = await self._send_following_safe_redirects(
                    client, "POST", url, headers=headers, content=payload
                )
                resp_content_type = resp.headers.get("content-type", "")
                final_url = str(resp.url)
                if not _is_textual(resp_content_type) and "pdf" not in resp_content_type.lower():
                    await resp.aclose()
                    kind = resp_content_type or "unknown"
                    raise WebFetchError(f"that endpoint did not return a text response ({kind})")
                raw, body_truncated = await _read_capped(resp)
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            log.warning("web.fetch_post_failed", error=repr(exc), status=status)
            raise WebFetchError(_fetch_error_message(status), status=status) from exc
        decoded = raw.decode(_charset(resp_content_type) or "utf-8", errors="replace")
        if "json" in resp_content_type.lower():
            title, text = "", _pretty_json(decoded)
        elif "html" in resp_content_type.lower():
            title, text, _ = _extract_html(decoded, base=final_url)
        else:
            title, text = "", decoded.strip()
        return _window_and_find(
            text,
            url=final_url,
            title=title,
            links=(),
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
        target URL travels in the path. Returns None when unconfigured or on any reader
        failure, so the caller falls back to the next tier / its original error."""
        if not self._reader_url:
            return None
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
        # The reader can "succeed" (200) yet return the ORIGIN's bot-challenge interstitial
        # (Cloudflare "Update Browser Required", "Just a moment…") rendered as markdown, not
        # the article. Treat that as a reader miss — return None so `fetch` surfaces the
        # original block instead of laundering the challenge text into a cited source.
        if _is_challenge_page("", clean):
            log.warning("web.challenge_blocked", url=url, via="reader")
            return None
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

    async def _fetch_via_solver(
        self, url: str, *, offset: int = 0, find: str = "", find_regex: bool = False
    ) -> FetchResult | None:
        """Re-fetch `url` through the pinned challenge-solver sidecar — Byparr or any
        FlareSolverr-compatible `POST /v1` API. It drives a stealth browser that clears the
        JS / managed bot-challenge the plain reader can't, and returns the SOLVED page HTML,
        which we extract exactly like a direct fetch (so tables, links, and pagination all
        work). Like the reader the endpoint is owner-pinned and trusted, so it is NOT
        SSRF-guarded — only the public TARGET url is (it must be public), and only that url
        travels in the request body. Returns None when unconfigured, on any solver failure,
        or when the solved page is ITSELF a challenge (an interactive Turnstile/CAPTCHA the
        stealth browser couldn't clear) — so the caller surfaces an honest block rather than
        laundering the challenge text into a cited source."""
        if not self._solver_url:
            return None
        guard_public_host(url, skip_dns=self._transport is not None)  # the TARGET must be public
        payload = {"cmd": "request.get", "url": url, "maxTimeout": _SOLVER_MAX_TIMEOUT_MS}
        try:
            async with httpx.AsyncClient(timeout=_SOLVER_TIMEOUT, transport=self._transport) as c:
                resp = await c.post(f"{self._solver_url}/v1", json=payload)
                resp.raise_for_status()
                body = resp.json()
            solution = body.get("solution") if isinstance(body, dict) else None
            if not isinstance(solution, dict):
                return None
            html = str(solution.get("response") or "")
            final_url = str(solution.get("url") or url)
        except (httpx.HTTPError, ValueError, WebFetchError) as exc:
            log.warning("web.solver_failed", error=repr(exc))
            return None
        if not html.strip():
            return None
        title, text, hrefs = _extract_html(html, base=final_url)
        if _is_challenge_page(title, text):
            log.warning("web.challenge_blocked", url=url, via="solver", title=title[:80])
            return None
        # A solved page that is itself just a search form is not recovered content — return None
        # so a form never becomes a cited source (mirrors the challenge seam).
        if _is_search_form_page(title, text, html):
            log.info("web.search_form_detected", url=final_url, via="solver")
            return None
        log.info("web.solver_used", url=final_url)
        return _window_and_find(
            text,
            url=final_url,
            title=title,
            links=_collect_links(hrefs, base=final_url),
            offset=offset,
            find=find,
            find_regex=find_regex,
            body_truncated=False,
        )

    async def _get_following_safe_redirects(
        self, client: httpx.AsyncClient, url: str
    ) -> httpx.Response:
        """GET `url` through the per-hop SSRF guard — the plain-fetch and image-byte path."""
        return await self._send_following_safe_redirects(
            client, "GET", url, headers=BROWSER_HEADERS
        )

    async def _send_following_safe_redirects(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None = None,
    ) -> httpx.Response:
        """Send `method` to `url`, validating the host of every hop against the SSRF blocklist
        and following up to `_MAX_REDIRECTS` redirects by hand (httpx auto-redirect is off, so
        a 30x to a private address can't slip past the per-hop check). Shared by the GET path
        and the POST path so both guard identically; a redirected POST re-sends its body to
        the re-validated host (a search API rarely redirects, but the guard holds if it does)."""
        for _hop in range(_MAX_REDIRECTS + 1):
            self._guard_host(url)
            resp = await client.send(
                client.build_request(method, url, headers=headers, content=content),
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


def _pretty_json(raw: str) -> str:
    """A JSON API body re-serialized indented so the model reads it as structured text
    (windowed by the same offset/find machinery as a page). A body that doesn't parse as
    JSON — despite the content-type — degrades to the raw text rather than erroring."""
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return raw.strip()


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
