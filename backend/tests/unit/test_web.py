"""The jerv chatbot's on-box web client + tools (docs/reference/ASSISTANT.md "Agent
selection"). HTTP is faked via MockTransport — no live network, like the
connector and LLM adapters."""

import json
from collections.abc import AsyncIterator, Mapping
from email.utils import formatdate

import httpx
import pytest

from jbrain.agent.loop import ToolCallBudget, ToolContext, ToolOutput
from jbrain.agent.webtools import build_web_handlers
from jbrain.db.session import SessionContext
from jbrain.web.feeds import FeedClient
from jbrain.web.fetch import SearchFormError, WebFetcher, WebFetchError
from jbrain.web.search import SearxngClient, WebSearchError

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

_SEARX_OK = {
    "results": [
        {"title": "Result one", "url": "https://a.example/1", "content": "first snippet"},
        {"title": "Result two", "url": "https://b.example/2", "content": "second snippet"},
        {"title": "no url", "url": "", "content": "dropped"},
    ]
}


def _searx(handler) -> SearxngClient:  # type: ignore[no-untyped-def]
    return SearxngClient("http://searxng:8080", transport=httpx.MockTransport(handler))


# --- SearxngClient ---------------------------------------------------------


async def test_search_parses_and_drops_urlless_rows() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_OK)

    hits = (await _searx(handle).search("python", limit=5)).hits
    assert [h.url for h in hits] == ["https://a.example/1", "https://b.example/2"]
    assert hits[0].title == "Result one" and hits[0].snippet == "first snippet"
    # The query rode as ?q=, JSON format requested, against the pinned base URL.
    assert calls[0].url.params["q"] == "python"
    assert calls[0].url.params["format"] == "json"
    assert str(calls[0].url).startswith("http://searxng:8080/search")


async def test_search_honors_limit() -> None:
    hits = (await _searx(lambda r: httpx.Response(200, json=_SEARX_OK)).search("q", limit=1)).hits
    assert len(hits) == 1


async def test_search_unconfigured_raises() -> None:
    with pytest.raises(WebSearchError):
        await SearxngClient("").search("q")


async def test_search_http_error_raises_web_search_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    with pytest.raises(WebSearchError):
        await _searx(boom).search("q")


async def test_search_forbidden_raises_web_search_error() -> None:
    # A 403 is the tell-tale of a reachable instance with the JSON format disabled;
    # it must surface as a recoverable WebSearchError, not crash the turn.
    with pytest.raises(WebSearchError):
        await _searx(lambda r: httpx.Response(403)).search("q")


_SEARX_NEWS_OK = {
    "results": [
        {
            "title": "Fed holds rates",
            "url": "https://apnews.com/article/fed",
            "content": "The Federal Reserve held rates steady.",
            "publishedDate": "2026-08-12T09:14:00",
        },
        {
            "title": "Markets react",
            "url": "https://npr.org/2026/08/12/markets",
            "content": "Stocks moved on the decision.",
            # No publishedDate — the tool must tolerate a dateless row.
        },
    ]
}


async def test_search_news_requests_the_news_category_and_time_range() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_NEWS_OK)

    hits = await _searx(handle).search_news("federal reserve", time_range="day", limit=5)
    assert [h.url for h in hits] == [
        "https://apnews.com/article/fed",
        "https://npr.org/2026/08/12/markets",
    ]
    # The publish date is carried through; a dateless row surfaces as "".
    assert hits[0].published == "2026-08-12T09:14:00"
    assert hits[1].published == ""
    # The request asked SearXNG for the NEWS category, the recency window, and JSON.
    assert calls[0].url.params["categories"] == "news"
    assert calls[0].url.params["time_range"] == "day"
    assert calls[0].url.params["format"] == "json"


async def test_search_news_ignores_an_unknown_time_range() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_NEWS_OK)

    await _searx(handle).search_news("q", time_range="fortnight")
    # A bad window is dropped (no time_range param) rather than passed through to SearXNG.
    assert "time_range" not in calls[0].url.params


async def test_search_news_is_cached_separately_per_window() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_NEWS_OK)

    client = _searx(handle)
    await client.search_news("q", time_range="day")
    await client.search_news("q", time_range="day")  # repeat → served from cache
    assert len(calls) == 1
    await client.search_news("q", time_range="week")  # different window → a fresh request
    assert len(calls) == 2


async def test_search_news_unconfigured_raises() -> None:
    with pytest.raises(WebSearchError):
        await SearxngClient("").search_news("q")


_SEARX_INFOBOX_OK = {
    "results": [
        {
            "title": "Ada Lovelace",
            "url": "https://en.wikipedia.org/wiki/Ada_Lovelace",
            "content": "mathematician",
        }
    ],
    "infoboxes": [
        {
            "infobox": "Ada Lovelace",
            "content": "English mathematician, the first programmer.",
            "attributes": [
                {"label": "Born", "value": "10 December 1815"},
                {"label": "Died", "value": "27 November 1852"},
            ],
            "urls": [{"title": "Wikipedia", "url": "https://en.wikipedia.org/wiki/Ada_Lovelace"}],
        }
    ],
    "answers": [{"answer": "Ada Lovelace was born on 10 December 1815."}, "42 km = 26.1 miles"],
}


async def test_search_returns_infobox_and_answers() -> None:
    result = await _searx(lambda r: httpx.Response(200, json=_SEARX_INFOBOX_OK)).search(
        "ada lovelace"
    )
    assert result.infobox is not None
    assert result.infobox.title == "Ada Lovelace" and "first programmer" in result.infobox.content
    assert result.infobox.attributes[0] == ("Born", "10 December 1815")
    assert result.infobox.url == "https://en.wikipedia.org/wiki/Ada_Lovelace"
    # Instant answers come through, both the {answer} and the bare-string shapes.
    assert result.answers == ("Ada Lovelace was born on 10 December 1815.", "42 km = 26.1 miles")
    assert result.hits[0].url == "https://en.wikipedia.org/wiki/Ada_Lovelace"


async def test_search_result_is_cached_even_with_no_hits_when_an_extra_is_present() -> None:
    # A response with only an infobox (no result rows) still carries an answer, so it IS cached
    # (a repeat serves it without re-hitting the upstreams).
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"results": [], "infoboxes": _SEARX_INFOBOX_OK["infoboxes"]}
        )

    client = _searx(handle)
    first = await client.search("ada")
    assert first.hits == [] and first.infobox is not None
    await client.search("ada")
    assert len(calls) == 1  # served from cache the second time


async def test_search_passes_a_time_range_window() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_OK)

    await _searx(handle).search("gpu prices", time_range="week")
    assert calls[0].url.params["time_range"] == "week"
    # A bad window is dropped, not sent.
    await _searx(handle).search("gpu prices", time_range="decade")
    assert "time_range" not in calls[1].url.params


def _info_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """structlog does not route through caplog; capture the event + its fields directly."""
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr("jbrain.web.search.log.info", lambda ev, **kw: seen.append((ev, kw)))
    return seen


async def test_search_logs_the_engines_that_failed_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SearXNG names the failed engines per query; we log them so engine health is queryable
    instead of reconstructed from the container log by hand. No query text on that line."""
    body: dict[str, object] = dict(_SEARX_OK)
    body["unresponsive_engines"] = [
        ["duckduckgo", "CAPTCHA required"],
        ["brave", "Too many requests"],
        "mojeek",  # a bare name (a looser upstream shape) is still reported
    ]
    seen = _info_events(monkeypatch)
    await _searx(lambda r: httpx.Response(200, json=body)).search("gpu prices", time_range="day")
    fields = next(kw for ev, kw in seen if ev == "web.search_engines_down")
    assert fields["engines"] == [
        "duckduckgo: CAPTCHA required",
        "brave: Too many requests",
        "mojeek",
    ]
    # The window rides along: a window itself removes engines, so a reader can tell a blocked
    # engine from a filtered-out one. The query text does NOT — engine health is independent
    # of what was asked, and this line is for operating the box.
    assert fields["time_range"] == "day" and fields["categories"] == "general"
    assert "gpu prices" not in repr(fields), "engine health must not carry the query text"


async def test_search_logs_nothing_when_every_engine_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No line when nothing failed — the signal has to stay rare enough to be worth reading."""
    seen = _info_events(monkeypatch)
    await _searx(lambda r: httpx.Response(200, json=_SEARX_OK)).search("gpu prices")
    assert [ev for ev, _ in seen if ev == "web.search_engines_down"] == []


async def test_unresponsive_engines_tolerates_a_junk_shape() -> None:
    """Diagnostics must never sink a search that otherwise worked, whatever upstream sends."""
    for junk in ("not-a-list", [None, 42, [], {}], {"duckduckgo": "captcha"}):
        body = _SEARX_OK | {"unresponsive_engines": junk}
        client = _searx(lambda r, b=body: httpx.Response(200, json=b))
        result = await client.search("q")
        assert result.hits, f"a junk unresponsive_engines ({junk!r}) must not break the search"


async def test_search_retries_without_a_window_that_blanked_it() -> None:
    """The 2026-09-01 blanked-search bug: nine `since=day` searches for cinema showtimes each
    returned nothing while the same query with no window returned ten hits. A window filters on
    a page's PUBLISH date and drops every engine that can't filter by date, so it can blank a
    search on its own — retry once without it rather than let the agent reword forever."""
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "time_range" in request.url.params:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json=_SEARX_OK)

    result = await _searx(handle).search("titusville fl movie showtimes", time_range="day")
    assert [c.url.params.get("time_range") for c in calls] == ["day", None]
    assert result.hits and result.window_dropped is True


async def test_search_keeps_a_window_that_found_something() -> None:
    """A window that answered is never widened — recency was asked for and honoured."""
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_OK)

    result = await _searx(handle).search("gpu prices", time_range="week")
    assert len(calls) == 1 and result.window_dropped is False


async def test_web_search_tool_says_when_it_dropped_the_window() -> None:
    """The model chose the window, so it must be told the results it is reading are NOT
    filtered — and told why, or it will set `since` again on the next search."""

    def handle(request: httpx.Request) -> httpx.Response:
        if "time_range" in request.url.params:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json=_SEARX_OK)

    handlers = build_web_handlers(_searx(handle), WebFetcher())
    out = await handlers["web_search"]({"query": "titusville fl showtimes", "since": "day"}, CTX)
    text = str(out)
    assert "`since=day` window returned nothing, so it was dropped" in text
    assert "when a PAGE was published" in text
    assert "https://a.example/1" in text  # the widened hits are still delivered


async def test_web_search_tool_reports_the_windows_it_already_tried() -> None:
    """A genuinely empty search must not read as "try different words": the window has already
    been retried away, so wording is the only remaining variable — say so."""
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json={"results": []})), WebFetcher()
    )
    out = await handlers["web_search"]({"query": "zzzz", "since": "day"}, CTX)
    assert "searched the last day, then again with no time limit" in str(out)


async def test_search_empty_at_every_window_stays_empty() -> None:
    """Widening is a retry, not a guarantee: a query nothing answers still reports nothing,
    and reports it as un-windowed so the caller doesn't blame the filter twice."""
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"results": []})

    result = await _searx(handle).search("zzzz", time_range="day")
    assert len(calls) == 2
    assert result.is_empty and result.window_dropped is False


_SEARX_SCIENCE_OK = {
    "results": [
        {
            "title": "Attention Is All You Need",
            "url": "https://arxiv.org/abs/1706.03762",
            "content": "We propose the Transformer...",
            "publishedDate": "2017-06-12T00:00:00",
            "authors": ["Vaswani", "Shazeer", "Parmar"],
        },
        {"title": "No authors", "url": "https://arxiv.org/abs/0000", "content": "x"},
    ]
}


async def test_search_science_requests_the_science_category_and_keeps_authors() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_SCIENCE_OK)

    hits = await _searx(handle).search_science("transformer architecture", limit=5)
    assert calls[0].url.params["categories"] == "science"
    assert hits[0].url == "https://arxiv.org/abs/1706.03762"
    assert (
        hits[0].authors == "Vaswani, Shazeer, Parmar" and hits[0].published == "2017-06-12T00:00:00"
    )
    assert hits[1].authors == ""  # a row with no authors surfaces as ""


async def test_search_science_unconfigured_raises() -> None:
    with pytest.raises(WebSearchError):
        await SearxngClient("").search_science("q")


async def test_repeat_search_is_served_from_cache_without_a_second_request() -> None:
    # A repeat of the SAME (query, limit) inside the TTL window returns the cached hits
    # and never touches SearXNG again — the whole point: stop re-hitting the upstream
    # engines that rate-limit us.
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_OK)

    client = _searx(handle)
    first = await client.search("python", limit=5)
    second = await client.search("python", limit=5)
    assert first == second
    assert len(calls) == 1  # the second search hit the cache, not the network
    # A different limit is a distinct key — it must go to the network.
    await client.search("python", limit=2)
    assert len(calls) == 2


async def test_cache_entry_expires_after_the_ttl() -> None:
    calls: list[httpx.Request] = []
    clock = [1000.0]  # a hand-cranked monotonic clock

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_OK)

    client = SearxngClient(
        "http://searxng:8080",
        transport=httpx.MockTransport(handle),
        cache_ttl_s=900.0,
        clock=lambda: clock[0],
    )
    await client.search("python")
    clock[0] += 899.0  # still inside the window
    await client.search("python")
    assert len(calls) == 1
    clock[0] += 2.0  # now past 900s — the entry has expired
    await client.search("python")
    assert len(calls) == 2


async def test_empty_result_is_not_cached_so_a_throttle_retries() -> None:
    # An empty result is often a transient upstream throttle, not a real "no results";
    # caching it would blank searches for the whole TTL. It must retry next time.
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"results": []})

    client = _searx(handle)
    assert (await client.search("nothing")).hits == []
    assert (await client.search("nothing")).hits == []
    assert len(calls) == 2  # not cached — each call reached the network


async def test_cache_can_be_disabled_with_a_zero_ttl() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_OK)

    client = SearxngClient(
        "http://searxng:8080", transport=httpx.MockTransport(handle), cache_ttl_s=0
    )
    await client.search("python")
    await client.search("python")
    assert len(calls) == 2  # caching off — both reached the network


# --- WebFetcher ------------------------------------------------------------

_HTML = b"""<html><head><title>Hi There</title><style>x{}</style></head>
<body><script>bad()</script><h1>Heading</h1><p>First para.</p><p>Second para.</p></body></html>"""


async def test_fetch_extracts_readable_text_and_title() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/p")
    assert result.title == "Hi There"
    assert "Heading" in result.text and "First para." in result.text
    # Scripts and styles are dropped, never surfaced to the model.
    assert "bad()" not in result.text and "x{}" not in result.text


_HTML_LINKS = b"""<html><head><title>Repo</title></head><body>
<a href="/jeffmhopkins/JBrain2/tree/main/backend">backend</a>
<a href="docs">docs</a>
<a href="https://other.example/page">elsewhere</a>
<a href="https://x.example/repo#readme">self with fragment</a>
<a href="mailto:nope@x.example">mail</a>
<a href="/jeffmhopkins/JBrain2/tree/main/backend">dup backend</a>
</body></html>"""


_HTML_MD = b"""<html><head><title>Doc</title></head><body>
<nav><a href="/skip">menu</a></nav>
<h1>Title</h1>
<p>Intro with a <a href="/page">link</a> and <strong>bold</strong>.</p>
<ul><li>one</li><li>two</li></ul>
<pre><code>def f():
    return 1</code></pre>
<footer>footer junk</footer>
</body></html>"""


async def test_fetch_renders_markdown_structure() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML_MD, headers={"content-type": "text/html"})

    md = (
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/doc")
    ).text
    assert "# Title" in md  # heading
    assert "[link](https://x.example/page)" in md  # inline link, resolved to absolute
    assert "**bold**" in md  # emphasis
    assert "- one" in md and "- two" in md  # list items
    # Fenced code block with indentation preserved (not whitespace-collapsed).
    assert "```" in md and "def f():\n    return 1" in md


async def test_fetch_drops_page_boilerplate() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML_MD, headers={"content-type": "text/html"})

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/doc")
    # nav/footer subtrees are dropped — neither their text nor their links survive.
    assert "menu" not in result.text and "footer junk" not in result.text
    assert all("/skip" not in link for link in result.links)


async def test_fetch_surfaces_links_resolved_to_absolute_urls() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML_LINKS, headers={"content-type": "text/html"})

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/repo")
    # Relative hrefs resolve against the page URL; an external link is kept verbatim;
    # mailto: is dropped, the page's own URL (a bare fragment) is dropped, and the
    # duplicate collapses — order preserved.
    assert result.links == (
        "https://x.example/jeffmhopkins/JBrain2/tree/main/backend",
        "https://x.example/docs",
        "https://other.example/page",
    )


async def test_web_fetch_tool_lists_links_for_navigation() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML_LINKS, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = await handlers["web_fetch"]({"url": "https://x.example/repo"}, CTX)
    assert "Links on this page" in out
    assert "https://x.example/docs" in out


# --- fetch_bytes (the image byte path, redirect-safe) ----------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def test_fetch_bytes_returns_content_type_and_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    ct, body = await WebFetcher(transport=httpx.MockTransport(handle)).fetch_bytes(
        "https://cdn.example/a.png"
    )
    assert ct == "image/png" and body == _PNG


async def test_fetch_bytes_follows_a_redirect_through_the_per_hop_guard() -> None:
    # Auto-redirect is OFF; the manual loop re-guards each hop and follows a 30x by hand.
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redir":
            return httpx.Response(302, headers={"location": "https://cdn.example/final.png"})
        return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    ct, body = await WebFetcher(transport=httpx.MockTransport(handle)).fetch_bytes(
        "https://cdn.example/redir"
    )
    assert ct == "image/png" and body == _PNG


async def test_fetch_bytes_refuses_a_redirect_to_a_non_public_scheme() -> None:
    # A 30x whose Location is a file:/ target is refused by the per-hop guard — a crafted
    # redirect can't turn the fetch into a local-file read (the SSRF discipline, invariant #9).
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch_bytes(
            "https://cdn.example/redir"
        )


async def test_fetch_bytes_caps_the_download_size() -> None:
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10_000

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "image/png"})

    _, body = await WebFetcher(transport=httpx.MockTransport(handle)).fetch_bytes(
        "https://cdn.example/big.png", max_bytes=1000
    )
    assert len(body) == 1000  # truncated to the cap, never buffered whole


# --- POST (a JSON/search API endpoint the GET path can't reach) ---------------

_SEARCH_API_JSON = {"count": 1, "results": [{"caseName": "SMITH v. STATE", "court": "Fla."}]}


async def test_fetch_post_sends_body_and_returns_pretty_json() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json=_SEARCH_API_JSON, headers={"content-type": "application/json"}
        )

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch(
        "https://api.example/search",
        method="POST",
        body='{"q":"smith"}',
        content_type="application/json",
    )
    assert seen[0].method == "POST"
    assert seen[0].content == b'{"q":"smith"}'
    assert seen[0].headers["content-type"] == "application/json"
    # A JSON reply comes back pretty-printed (indented), so the model reads it as structure.
    assert '"caseName": "SMITH v. STATE"' in result.text
    assert result.url == "https://api.example/search"


async def test_fetch_post_form_encoded_content_type_rides() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"content-type": "application/json"})

    await WebFetcher(transport=httpx.MockTransport(handle)).fetch(
        "https://api.example/search",
        method="POST",
        body="q=smith",
        content_type="application/x-www-form-urlencoded",
    )
    assert seen[0].headers["content-type"] == "application/x-www-form-urlencoded"


async def test_fetch_post_caps_an_oversized_body() -> None:
    from jbrain.web.fetch import _MAX_POST_BODY

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, headers={"content-type": "application/json"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch(
            "https://api.example/search", method="POST", body="x" * (_MAX_POST_BODY + 1)
        )


async def test_fetch_post_windows_a_non_json_text_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"plain result rows", headers={"content-type": "text/plain"}
        )

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch(
        "https://api.example/q", method="POST", body="q=1"
    )
    assert "plain result rows" in result.text


async def test_post_blocks_a_non_public_address() -> None:
    # The POST path shares the GET path's SSRF guard: a model-supplied private/loopback/
    # link-local host is refused before any request leaves the box (invariant #9), with a
    # real DNS resolve (no injected transport).
    with pytest.raises(WebFetchError):
        await WebFetcher().fetch("http://169.254.169.254/latest/api", method="POST", body="{}")


async def test_post_refuses_a_redirect_to_a_non_public_scheme() -> None:
    # A 30x whose Location is a file:/ target is refused by the per-hop guard on the POST
    # path too — a crafted redirect can't turn a POST into a local read.
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch(
            "https://api.example/redir", method="POST", body="{}"
        )


async def test_get_still_works_unchanged_alongside_post_support() -> None:
    # The default (no method) path is a plain GET, unaffected by the POST addition.
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/p")
    assert seen[0].method == "GET"
    assert result.title == "Hi There" and "First para." in result.text


# --- fetch_html (the portal-resolver raw-HTML/POST primitive) ---------------


async def test_fetch_html_returns_raw_html_not_extracted() -> None:
    # A resolver parses result rows itself, so fetch_html returns the RAW HTML (table markup
    # intact), not trafilatura-extracted prose.
    raw = "<html><body><table><tr><td>PROPERTY PRO SERVICES</td></tr></table></body></html>"

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=raw, headers={"content-type": "text/html"})

    final_url, body = await WebFetcher(transport=httpx.MockTransport(handle)).fetch_html(
        "https://p.example/results?q=x"
    )
    assert "<table>" in body and "<td>PROPERTY PRO SERVICES</td>" in body
    assert final_url == "https://p.example/results?q=x"


async def test_fetch_html_post_sends_the_body_and_method() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})

    await WebFetcher(transport=httpx.MockTransport(handle)).fetch_html(
        "https://p.example/search",
        method="POST",
        body="name=smith",
        content_type="application/x-www-form-urlencoded",
    )
    assert seen[0].method == "POST"
    assert seen[0].headers["content-type"] == "application/x-www-form-urlencoded"


async def test_fetch_html_refuses_a_private_host() -> None:
    # Real DNS resolve (no injected transport): a link-local/metadata host is refused before any
    # request leaves the box — the same SSRF guard as fetch().
    with pytest.raises(WebFetchError):
        await WebFetcher().fetch_html("http://169.254.169.254/latest/meta-data")


async def test_fetch_html_refuses_a_redirect_hop_to_a_non_public_target() -> None:
    # A 30x whose Location is a non-public target is refused by the PER-HOP guard on the
    # fetch_html path too — a resolver can't be redirected into the box's own services (R4).
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch_html(
            "https://p.example/redir"
        )


async def test_fetch_html_http_error_raises() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, headers={"content-type": "text/html"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch_html("https://p.example/x")


async def test_submit_form_primes_session_then_posts_with_cookie_and_token() -> None:
    # The stateful portal flow: GET primes a session (Set-Cookie) + carries a CSRF token the
    # caller reads out; the POST must ride the SAME cookie jar and the token the build_body read.
    seen: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                text='<form action="/"><input name="csrf_token" value="TOK123"/></form>',
                headers={"content-type": "text/html", "set-cookie": "SESS=abc; Path=/"},
            )
        seen["cookie"] = request.headers.get("cookie", "")
        seen["body"] = request.content.decode()
        return httpx.Response(
            200, text="<table>results here</table>", headers={"content-type": "text/html"}
        )

    def build(page_html: str) -> str:
        import re as _re

        tok = _re.search(r'name="csrf_token" value="([^"]*)"', page_html).group(1)  # type: ignore[union-attr]
        return f"csrf_token={tok}&IndividualLNameFilter=Smith"

    final_url, html = await WebFetcher(transport=httpx.MockTransport(handle)).submit_form(
        "https://p.example/", "https://p.example/", build
    )
    assert "results here" in html
    assert "SESS=abc" in seen["cookie"]  # the GET's session cookie rode the POST
    assert "csrf_token=TOK123" in seen["body"]  # build_body read the primed page's token


async def test_submit_form_refuses_a_private_host() -> None:
    # Both hops run the SSRF guard; a private page_url is refused before any egress (real resolve).
    with pytest.raises(WebFetchError):
        await WebFetcher().submit_form(
            "http://169.254.169.254/", "http://169.254.169.254/", lambda _p: "q=x"
        )


async def test_web_fetch_tool_post_returns_the_json() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_SEARCH_API_JSON, headers={"content-type": "application/json"}
        )

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://api.example/search", "method": "POST", "body": '{"q":"smith"}'},
            _fresh_ctx(),
        )
    )
    assert "SMITH v. STATE" in out


async def test_web_fetch_tool_post_rejects_an_unsupported_content_type() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={}, headers={"content-type": "application/json"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = str(
        await handlers["web_fetch"](
            {
                "url": "https://api.example/s",
                "method": "POST",
                "body": "{}",
                "content_type": "multipart/form-data",
            },
            _fresh_ctx(),
        )
    )
    assert "content_type" in out
    assert calls["n"] == 0  # rejected before any request left the box


async def test_web_fetch_tool_rejects_an_unknown_method() -> None:
    handlers = build_web_handlers(SearxngClient(""), WebFetcher())
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/p", "method": "DELETE"}, _fresh_ctx()
        )
    )
    assert "GET or POST" in out


# --- web sources (favicon citation chips) ----------------------------------


async def test_web_search_surfaces_web_sources_in_hit_order() -> None:
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_OK)), WebFetcher()
    )
    out = await handlers["web_search"]({"query": "python"}, CTX)
    # The structured twin rides alongside the model text, in the order the model
    # reads the hits — so a [^1]/[^2] marker resolves to a real reached URL.
    assert isinstance(out, ToolOutput)
    assert [(s.url, s.title) for s in out.web_sources] == [
        ("https://a.example/1", "Result one"),
        ("https://b.example/2", "Result two"),
    ]


async def test_web_fetch_surfaces_the_fetched_page_as_a_web_source() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = await handlers["web_fetch"]({"url": "https://x.example/p"}, CTX)
    assert isinstance(out, ToolOutput)
    assert len(out.web_sources) == 1
    assert out.web_sources[0].url == "https://x.example/p"
    assert out.web_sources[0].title == "Hi There"


async def test_web_search_error_carries_no_web_sources() -> None:
    # A recoverable error is plain text, never a citable source.
    handlers = build_web_handlers(_searx(lambda r: httpx.Response(502)), WebFetcher())
    out = await handlers["web_search"]({"query": "q"}, CTX)
    assert not isinstance(out, ToolOutput) or not out.web_sources


# --- news_search handler (dated leads from the news category) -----------------


async def test_news_search_requests_news_category_and_surfaces_dated_leads() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_NEWS_OK)

    handlers = build_web_handlers(_searx(handle), WebFetcher())
    out = await handlers["news_search"]({"query": "federal reserve", "since": "day"}, CTX)
    assert isinstance(out, ToolOutput)
    # The request went to the news category with the recency window.
    assert (
        calls[0].url.params["categories"] == "news" and calls[0].url.params["time_range"] == "day"
    )
    # The publish date and outlet are surfaced in the text; each hit is a citable source.
    assert "2026-08-12T09:14:00" in str(out) and "apnews.com" in str(out)
    assert [s.url for s in out.web_sources] == [
        "https://apnews.com/article/fed",
        "https://npr.org/2026/08/12/markets",
    ]


async def test_news_search_rejects_an_empty_query() -> None:
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_NEWS_OK)), WebFetcher()
    )
    out = await handlers["news_search"]({"query": "  "}, CTX)
    assert "needs a non-empty query" in str(out)


async def test_news_search_shares_the_scout_search_budget() -> None:
    # news_search draws on the SAME ToolCallBudget as web_search, so a scout can't dodge its
    # ceiling by switching tools: one news_search plus the cap's worth of web_search exhausts it.
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_NEWS_OK)), WebFetcher()
    )
    ctx = _budget_ctx(1)
    first = await handlers["news_search"]({"query": "fed"}, ctx)
    assert isinstance(first, ToolOutput)  # the one allowed call succeeds
    # The budget is now spent — a following web_search is refused by the shared counter.
    second = await handlers["web_search"]({"query": "fed"}, ctx)
    assert "SEARCH BUDGET SPENT" in str(second)


# --- news_feed handler (curated per-category RSS/Atom) ------------------------

_FEED_NOW = 1_786_000_000  # a fixed wall clock for deterministic recency windows
_FEED_LONG = "This is a full feed article body sentence with substance. " * 20


def _feed_rss(items: str) -> bytes:
    return (
        "<?xml version='1.0'?>"
        "<rss version='2.0' xmlns:content='http://purl.org/rss/1.0/modules/content/'>"
        f"<channel><title>Space Wire</title>{items}</channel></rss>"
    ).encode()


def _feed_full(url: str) -> str:
    return (
        f"<item><title>Full launch story</title><link>{url}</link>"
        f"<pubDate>{formatdate(_FEED_NOW - 600, usegmt=True)}</pubDate>"
        f"<content:encoded><![CDATA[<p>{_FEED_LONG}</p>]]></content:encoded></item>"
    )


def _feed_summary(url: str) -> str:
    return (
        f"<item><title>Lead only story</title><link>{url}</link>"
        f"<pubDate>{formatdate(_FEED_NOW - 600, usegmt=True)}</pubDate>"
        "<description>A short summary lead, no full body.</description></item>"
    )


def _feed_client(routes: Mapping[str, bytes | int], feeds: dict[str, list[str]]) -> FeedClient:
    def handle(request: httpx.Request) -> httpx.Response:
        val = routes.get(str(request.url))
        if val is None:
            return httpx.Response(404)
        if isinstance(val, int):
            return httpx.Response(val)
        return httpx.Response(200, content=val, headers={"content-type": "application/rss+xml"})

    fetcher = WebFetcher(transport=httpx.MockTransport(handle))
    return FeedClient(fetcher, feeds, clock=lambda: float(_FEED_NOW))


async def test_news_feed_surfaces_full_text_as_read_and_leads_as_unread() -> None:
    feed = _feed_full("https://space.example/full") + _feed_summary("https://space.example/lead")
    feeds = _feed_client(
        {"https://feed.example/rss": _feed_rss(feed)}, {"space": ["https://feed.example/rss"]}
    )
    handlers = build_web_handlers(SearxngClient(""), WebFetcher(), feeds=feeds)
    out = await handlers["news_feed"]({"category": "space"}, CTX)
    assert isinstance(out, ToolOutput)
    body = str(out)
    assert "✓ full text" in body and "lead only" in body
    assert "full feed article body" in body  # the full item's body is inline
    # The full-text item is a read source; the lead-only item is an unopened one.
    read = {s.url: s.read for s in out.web_sources}
    assert read == {"https://space.example/full": True, "https://space.example/lead": False}


async def test_news_feed_reports_not_configured_when_disabled() -> None:
    handlers = build_web_handlers(SearxngClient(""), WebFetcher())  # no feeds wired
    assert "not configured" in str(await handlers["news_feed"]({"category": "space"}, CTX))


async def test_news_feed_lists_known_categories_for_an_unknown_one() -> None:
    feeds = _feed_client(
        {"https://feed.example/rss": _feed_rss(_feed_summary("https://space.example/a"))},
        {"space": ["https://feed.example/rss"]},
    )
    handlers = build_web_handlers(SearxngClient(""), WebFetcher(), feeds=feeds)
    out = str(await handlers["news_feed"]({"category": "sports"}, CTX))
    assert "No feed items" in out and "space" in out


async def test_news_feed_shares_the_scout_search_budget() -> None:
    # news_feed is a discovery call, so it draws on the SAME ToolCallBudget as web_search/
    # news_search: a scout can't dodge the ceiling (or fan out unbounded feed fetches) by
    # switching to feeds — one news_feed plus the cap's worth of search exhausts it.
    feeds = _feed_client(
        {"https://feed.example/rss": _feed_rss(_feed_summary("https://space.example/a"))},
        {"space": ["https://feed.example/rss"]},
    )
    handlers = build_web_handlers(SearxngClient(""), WebFetcher(), feeds=feeds)
    ctx = _budget_ctx(1)
    first = await handlers["news_feed"]({"category": "space"}, ctx)
    assert isinstance(first, ToolOutput)  # the one allowed call succeeds
    second = await handlers["web_search"]({"query": "x"}, ctx)
    assert "SEARCH BUDGET SPENT" in str(second)  # the shared counter is now spent


# --- web_search extras (infobox + instant answers) and the recency window ------


async def test_web_search_leads_with_the_infobox_and_answers() -> None:
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_INFOBOX_OK)), WebFetcher()
    )
    out = await handlers["web_search"]({"query": "ada lovelace"}, CTX)
    text = str(out)
    # The direct-answer block leads, above the "Web results" leads header.
    assert "Direct answer" in text and "first programmer" in text
    assert "Born: 10 December 1815" in text
    assert text.index("Direct answer") < text.index("Web results")


async def test_web_search_returns_extras_even_with_no_hits() -> None:
    body = {"results": [], "answers": ["4 + 4 = 8"]}
    handlers = build_web_handlers(_searx(lambda r: httpx.Response(200, json=body)), WebFetcher())
    out = await handlers["web_search"]({"query": "4+4"}, CTX)
    assert isinstance(out, ToolOutput) and "4 + 4 = 8" in str(out)


async def test_web_search_forwards_the_since_window() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_OK)

    handlers = build_web_handlers(_searx(handle), WebFetcher())
    await handlers["web_search"]({"query": "gpu prices", "since": "week"}, CTX)
    assert calls[0].url.params["time_range"] == "week"


# --- science_search handler (scholarly leads) ---------------------------------


async def test_science_search_requests_science_and_surfaces_authors() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_SCIENCE_OK)

    handlers = build_web_handlers(_searx(handle), WebFetcher())
    out = await handlers["science_search"]({"query": "transformer"}, CTX)
    assert isinstance(out, ToolOutput)
    assert calls[0].url.params["categories"] == "science"
    assert "Vaswani, Shazeer, Parmar" in str(out) and "arxiv.org/abs/1706.03762" in str(out)
    assert out.web_sources[0].url == "https://arxiv.org/abs/1706.03762"


async def test_science_search_shares_the_scout_budget() -> None:
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_SCIENCE_OK)), WebFetcher()
    )
    ctx = _budget_ctx(1)
    assert isinstance(await handlers["science_search"]({"query": "x"}, ctx), ToolOutput)
    assert "SEARCH BUDGET SPENT" in str(await handlers["web_search"]({"query": "y"}, ctx))


# --- scout search budget (the engine-enforced ceiling the prompt alone can't hold) ---


def _budget_ctx(limit: int) -> ToolContext:
    """A ToolContext carrying its own fresh, mutable search ToolCallBudget — one per test so the
    `used` counter never leaks between them."""
    return ToolContext(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        search_budget=ToolCallBudget(limit=limit),
    )


def _fetch_budget_ctx(limit: int) -> ToolContext:
    """A ToolContext with a fresh fetch ToolCallBudget (search left uncapped)."""
    return ToolContext(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        fetch_budget=ToolCallBudget(limit=limit),
    )


async def test_web_search_annotates_remaining_budget() -> None:
    # Every search result tells the scout how many calls it has left — the "update the middle
    # with searches left" contract — and points it at the uncapped web_fetch.
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_OK)), WebFetcher()
    )
    out = await handlers["web_search"]({"query": "python news"}, _budget_ctx(8))
    assert isinstance(out, ToolOutput)  # a served search still carries its citation sources
    assert "SEARCH BUDGET: 7 web_search call(s) left of 8" in str(out)
    assert "web_fetch is unlimited" in str(out)


async def test_web_search_last_call_note_then_refusal() -> None:
    # A limit of 2: the 2nd search is flagged as the last, and the 3rd is REFUSED by the engine
    # — no network call is made for it — regardless of what the model tries.
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_SEARX_OK)

    handlers = build_web_handlers(_searx(handle), WebFetcher())
    ctx = _budget_ctx(2)

    first = str(await handlers["web_search"]({"query": "one"}, ctx))
    assert "SEARCH BUDGET: 1 web_search call(s) left of 2" in first

    second = str(await handlers["web_search"]({"query": "two"}, ctx))
    assert "SEARCH BUDGET: 0 web_search call(s) left of 2" in second
    assert "that was your LAST one" in second

    # The 7th-search-style overflow: refused before any network call, and told to fetch + return.
    refused = str(await handlers["web_search"]({"query": "three"}, ctx))
    assert "SEARCH BUDGET SPENT" in refused
    assert "RECOMMENDED SOURCES" in refused
    assert len(calls) == 2  # the refused search never reached the network


async def test_web_search_uncapped_by_default_has_no_budget_note() -> None:
    # No budget on the context (every persona but the scout): the result is byte-for-byte what it
    # always was — no budget annotation leaks into an ordinary agent's search.
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_OK)), WebFetcher()
    )
    out = str(await handlers["web_search"]({"query": "python"}, CTX))
    assert "SEARCH BUDGET" not in out


async def test_web_fetch_annotates_remaining_budget() -> None:
    # A served fetch tells the scout how many pages it has left AND stays citable (web_sources
    # preserved through the annotation).
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = await handlers["web_fetch"]({"url": "https://x.example/p"}, _fetch_budget_ctx(10))
    assert isinstance(out, ToolOutput)
    assert out.web_sources and out.web_sources[0].url == "https://x.example/p"
    assert "FETCH BUDGET: 9 web_fetch call(s) left of 10" in str(out)


async def test_web_fetch_last_page_note_then_refusal() -> None:
    # A limit of 2: the 2nd fetch is flagged as the last, the 3rd is REFUSED with no network
    # read — the fix for the scout that looped to 23 fetches.
    urls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    ctx = _fetch_budget_ctx(2)

    first = str(await handlers["web_fetch"]({"url": "https://x.example/1"}, ctx))
    assert "FETCH BUDGET: 1 web_fetch call(s) left of 2" in first

    second = str(await handlers["web_fetch"]({"url": "https://x.example/2"}, ctx))
    assert "FETCH BUDGET: 0 web_fetch call(s) left of 2" in second
    assert "that was your LAST page" in second

    refused = str(await handlers["web_fetch"]({"url": "https://x.example/3"}, ctx))
    assert "FETCH BUDGET SPENT" in refused
    assert "RECOMMENDED SOURCES" in refused
    assert not any("x.example/3" in u for u in urls)  # the refused fetch never hit the network


async def test_web_fetch_uncapped_by_default_has_no_budget_note() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = str(await handlers["web_fetch"]({"url": "https://x.example/p"}, CTX))
    assert "FETCH BUDGET" not in out


async def test_fetch_rejects_non_http_scheme() -> None:
    with pytest.raises(WebFetchError):
        await WebFetcher().fetch("ftp://x.example/file")


async def test_fetch_rejects_non_text_body() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/img.png")


async def test_fetch_http_error_raises() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "text/html"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/missing")


async def test_fetch_follows_a_redirect() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(301, headers={"location": "https://x.example/final"})
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch(
        "https://x.example/start"
    )
    assert result.url == "https://x.example/final" and "Heading" in result.text


async def test_fetch_refuses_a_redirect_loop() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://x.example/again"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/again")


# --- browser headers (stop bot-wall 403s that push the model to a reader) -----


async def test_fetch_presents_as_a_browser() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/p")
    ua = seen[0].headers.get("user-agent", "")
    # A real browser UA, not "JBrain2 jerv/1.0" — the bare custom UA is what gets 403'd.
    assert "Mozilla/5.0" in ua and "jerv" not in ua
    assert seen[0].headers.get("accept-language", "").startswith("en")


# --- PDF text layer (a linked PDF is content, not a dead end) -----------------


def _make_pdf(text: str) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


async def test_fetch_extracts_pdf_text_layer() -> None:
    pdf = _make_pdf("Quarterly report: revenue rose twelve percent.")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch(
        "https://x.example/r.pdf"
    )
    assert "revenue rose twelve percent" in result.text


async def test_fetch_still_refuses_non_pdf_binary() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNG\r\n", headers={"content-type": "image/png"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/i.png")


# --- trafilatura main-content extraction (clean article, drop chrome) ---------

_ARTICLE = (
    b"<html><head><title>Feature</title></head><body>"
    b"<nav><a href='/home'>Home</a> <a href='/about'>About</a> SITEWIDE-NAV-JUNK</nav>"
    b"<article><h1>The Long Read</h1>"
    + b"<p>The committee weighed the proposal at length and the debate ran for hours. </p>" * 12
    + b"</article><footer>COPYRIGHT-FOOTER-JUNK 2026</footer></body></html>"
)


async def test_fetch_prefers_trafilatura_for_a_real_article() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ARTICLE, headers={"content-type": "text/html"})

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/a")
    # The article body survives; the nav/footer chrome trafilatura strips does not.
    assert "the debate ran for hours" in result.text
    assert "SITEWIDE-NAV-JUNK" not in result.text and "COPYRIGHT-FOOTER-JUNK" not in result.text


# --- reader fallback (sanctioned replacement for the model's r.jina.ai trick) -


_READER_MD = b"Rendered by the reader: the content the static HTML never carried."


def _reader_handler(direct: httpx.Response):  # type: ignore[no-untyped-def]
    """A transport that answers the reader host with markdown and every other host
    with `direct` — so one MockTransport serves both legs of the fetch."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "reader":
            return httpx.Response(
                200, content=_READER_MD, headers={"content-type": "text/markdown"}
            )
        return direct

    return handle


async def test_reader_fallback_recovers_a_blocked_page() -> None:
    blocked = httpx.Response(403, headers={"content-type": "text/html"})
    fetcher = WebFetcher(
        transport=httpx.MockTransport(_reader_handler(blocked)),
        reader_url="http://reader:3000",
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "Rendered by the reader" in result.text
    assert result.url == "https://x.example/walled"  # the public URL, not the reader's


async def test_reader_fallback_recovers_an_empty_js_shell() -> None:
    shell = httpx.Response(
        200, content=b"<html><body></body></html>", headers={"content-type": "text/html"}
    )
    fetcher = WebFetcher(
        transport=httpx.MockTransport(_reader_handler(shell)),
        reader_url="http://reader:3000",
    )
    result = await fetcher.fetch("https://x.example/spa")
    assert "Rendered by the reader" in result.text


async def test_no_reader_configured_surfaces_the_block() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"content-type": "text/html"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/walled")


async def test_a_404_does_not_fall_back_to_the_reader() -> None:
    # A definitive 404 means the page does not exist — the reader would only render the
    # origin's soft "no such page" stub (enough text to read as success, hiding the miss).
    # So the fetch re-raises the 404 instead of returning the reader's stub, even though a
    # reader is configured and would answer (it's what recovers a 403 bot-wall).
    gone = httpx.Response(404, headers={"content-type": "text/html"})
    fetcher = WebFetcher(
        transport=httpx.MockTransport(_reader_handler(gone)),
        reader_url="http://reader:3000",
    )
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://en.wikipedia.org/wiki/Nonexistent_(2026)")
    assert excinfo.value.status == 404
    assert "404" in str(excinfo.value)  # the model sees the real status, not a glitch


async def test_a_403_still_falls_back_to_the_reader() -> None:
    # The reader guard is scoped to 404/410 only: a bot-wall 403 still gets the reader.
    blocked = httpx.Response(403, headers={"content-type": "text/html"})
    fetcher = WebFetcher(
        transport=httpx.MockTransport(_reader_handler(blocked)),
        reader_url="http://reader:3000",
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "Rendered by the reader" in result.text


# A bot-challenge interstitial as the reader renders it (Cloudflare "Update Browser
# Required") — a 200 with junk text, the shape that silently became a cited source before
# challenge detection existed.
_CHALLENGE_MD = b"# Update Browser Required\n\nfloridapolitics.com requires a modern browser."
# A site that serves the Cloudflare challenge directly with a 200 (title + JS/cookie wall).
_CHALLENGE_HTML = (
    b"<html><head><title>Just a moment...</title></head><body>"
    b"<h1>Checking your browser before accessing the site.</h1>"
    b"<p>Please enable JavaScript and cookies to continue.</p></body></html>"
)


async def test_reader_returning_a_bot_challenge_is_a_blocked_fetch() -> None:
    # The reader "succeeds" (200) but renders the origin's bot-challenge interstitial, not the
    # article. It must count as a reader miss so the block surfaces honestly and the junk never
    # becomes a cited source — the "Update Browser Required" laundering bug this fix targets.
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "reader":
            return httpx.Response(
                200, content=_CHALLENGE_MD, headers={"content-type": "text/markdown"}
            )
        return httpx.Response(403, headers={"content-type": "text/html"})

    fetcher = WebFetcher(transport=httpx.MockTransport(handle), reader_url="http://reader:3000")
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 403  # the real block surfaces, not the reader's junk
    assert "update browser" not in str(excinfo.value).lower()


async def test_direct_200_challenge_page_is_blocked() -> None:
    # A site that answers 200 with the Cloudflare challenge (no reader configured): the page is
    # detected by content and raised, so the junk is never returned as real text.
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_CHALLENGE_HTML, headers={"content-type": "text/html"})

    with pytest.raises(WebFetchError) as excinfo:
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/cf")
    assert "bot-challenge" in str(excinfo.value)


async def test_direct_200_challenge_falls_back_to_the_reader() -> None:
    # A direct 200 challenge leaves status None when raised, so `fetch` still tries the reader —
    # which here renders the real article past the wall (the 403 recovery path, reused).
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "reader":
            return httpx.Response(
                200, content=_READER_MD, headers={"content-type": "text/markdown"}
            )
        return httpx.Response(200, content=_CHALLENGE_HTML, headers={"content-type": "text/html"})

    fetcher = WebFetcher(transport=httpx.MockTransport(handle), reader_url="http://reader:3000")
    result = await fetcher.fetch("https://x.example/cf")
    assert "Rendered by the reader" in result.text


def test_is_challenge_page_precision() -> None:
    from jbrain.web.fetch import _is_challenge_page

    # Canonical title / body markers → blocked at any length.
    assert _is_challenge_page("Just a moment...", "")
    assert _is_challenge_page("", "Please enable JavaScript and cookies to continue.")
    assert _is_challenge_page("", "# Update Browser Required\nUse a modern browser.")
    # A real ~360-word article that merely MENTIONS Cloudflare must NOT be flagged (precision).
    article = "Cloudflare reported a record quarter. " * 60
    assert not _is_challenge_page("Cloudflare posts record earnings", article)
    # An ordinary short page is not a challenge either.
    assert not _is_challenge_page("Hi There", "Heading. First para. Second para.")


def test_google_unusual_traffic_page_is_a_challenge() -> None:
    # Google's reCAPTCHA "sorry" page — what the defunct webcache fallback returns. Its ~130-word
    # body clears the weak-marker length gate, so before this fix it slipped through and got cited
    # as a source in deep-research news runs. Its canonical strings must flag it at any length.
    from jbrain.web.fetch import _is_challenge_page

    google_sorry = (
        "About this page\n\n"
        "Our systems have detected unusual traffic from your computer network. This page "
        "checks to see if it's really you sending the requests, and not a robot. "
        "This page appears when Google automatically detects requests coming from your "
        "computer network which appear to be in violation of the Terms of Service. "
        "The block will expire shortly after those requests stop. In the meantime, solving "
        "the above CAPTCHA will let you continue to use our services. This traffic may have "
        "been sent by malicious software, a browser plug-in, or a script that sends automated "
        "requests. If you share your network connection, ask your administrator for help."
    )
    assert _is_challenge_page("", google_sorry)
    # A genuine article that happens to discuss network traffic is NOT flagged (precision).
    assert not _is_challenge_page(
        "Carriers report unusual traffic surge",
        "Mobile carriers said data traffic from home networks rose sharply this quarter. " * 12,
    )


async def test_reader_returning_the_google_captcha_is_a_blocked_fetch() -> None:
    # The reader renders the Google "unusual traffic" CAPTCHA (its webcache fallback) as markdown.
    # It must count as a reader miss so the block surfaces honestly instead of being cited — the
    # exact page that appeared as a "source" in the news deep-research screenshots.
    google_md = (
        b"# About this page\n\nOur systems have detected unusual traffic from your computer "
        b"network. This page checks to see if it's really you sending the requests, and not a "
        b"robot. Solving the above CAPTCHA will let you continue to use our services."
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "reader":
            return httpx.Response(200, content=google_md, headers={"content-type": "text/markdown"})
        return httpx.Response(401, headers={"content-type": "text/html"})

    fetcher = WebFetcher(transport=httpx.MockTransport(handle), reader_url="http://reader:3000")
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 401  # the real block, not Google's CAPTCHA text
    assert "unusual traffic" not in str(excinfo.value).lower()


# A dynamic gov portal served as its empty search form (the Sunbiz ByName / DFS pb_*.asp shape):
# a short page whose main content is a query field + submit, with no results.
_SEARCH_FORM_HTML = (
    b"<html><head><title>Search for Corporations by Name</title></head><body>"
    b"<h1>Search the Corporation Records</h1>"
    b"<form action='/Inquiry/CorporationSearch/SearchResults' method='get'>"
    b"<label>Entity Name:</label><input type='text' name='SearchTerm'/>"
    b"<input type='submit' value='Search Now'/></form>"
    b"<p>Other search options: by officer, by registered agent.</p></body></html>"
)


def test_is_search_form_page_precision() -> None:
    from jbrain.web.fetch import _is_search_form_page

    form_html = "<form><input type='text' name='q'/><input type='submit' value='Search'/></form>"
    # A bare search form on a short page → flagged (the query was never run).
    assert _is_search_form_page(
        "Search for Corporations by Name", "Entity Name: search by name.", form_html
    )  # noqa: E501
    # No raw HTML (the reader's markdown path) → never flagged; detection rides the direct path.
    assert not _is_search_form_page("Search", "Entity Name:", "")
    # A real results page — even an EMPTY one — says so: the query ran, it is not a bare form.
    empty = "No records found for your search."
    assert not _is_search_form_page("Search Results", empty, form_html)
    # A result-bearing table in the raw HTML → results present even if the extractor dropped them.
    results_html = form_html + (
        "<table><tr><td><a href='/d/1'>ACME LLC</a></td></tr>"
        "<tr><td><a href='/d/2'>ACME INC</a></td></tr></table>"
    )
    assert not _is_search_form_page("Search Results", "", results_html)
    # A content-rich article that merely has a nav search box → never flagged (precision).
    article = "The county commission met to discuss the annual budget in detail. " * 30
    assert not _is_search_form_page("County budget talks", article, form_html)
    # A page with no form at all → not flagged.
    assert not _is_search_form_page("About us", "We are a company.", "<div>hello</div>")


async def test_direct_search_form_is_surfaced_not_recovered() -> None:
    # A dynamic portal served as its empty search form. Even with a reader configured that WOULD
    # return content, the form error surfaces DIRECTLY — no reader/solver recovery (a renderer
    # can't submit the form either), and the message says the query was not executed.
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "reader":
            md = {"content-type": "text/markdown"}
            return httpx.Response(200, content=_READER_MD, headers=md)
        return httpx.Response(200, content=_SEARCH_FORM_HTML, headers={"content-type": "text/html"})

    fetcher = WebFetcher(transport=httpx.MockTransport(handle), reader_url="http://reader:3000")
    with pytest.raises(SearchFormError) as excinfo:
        await fetcher.fetch("https://search.example/Inquiry/CorporationSearch/ByName")
    msg = str(excinfo.value).lower()
    assert "search form" in msg and "not evidence" in msg
    assert "rendered by the reader" not in msg  # the reader was never used — no recovery


# The solved page the challenge solver returns (a FlareSolverr-shape `/v1` response wraps the
# HTML in solution.response) — real content the stealth browser recovered past the wall.
_SOLVED_HTML = (
    "<html><head><title>CFO Race</title></head><body>"
    "<h1>Blaise Ingoglia</h1><p>Real article content the stealth browser recovered.</p>"
    "</body></html>"
)


def _tiered_handler(direct: httpx.Response, *, reader: bytes | None, solver_response):  # type: ignore[no-untyped-def]
    """A transport serving all three fetch legs off one MockTransport: the solver host
    (`byparr`) with a FlareSolverr-shape JSON body (None ⇒ 500), the reader host with markdown
    (None ⇒ not answered), and every other host with `direct`."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "byparr":
            if solver_response is None:
                return httpx.Response(500)
            solution = {"url": str(request.url), **solver_response}
            return httpx.Response(200, json={"solution": solution})
        if request.url.host == "reader" and reader is not None:
            return httpx.Response(200, content=reader, headers={"content-type": "text/markdown"})
        return direct

    return handle


async def test_solver_recovers_when_the_reader_returns_a_challenge() -> None:
    # Direct 403 → reader renders the challenge interstitial (a miss) → the solver's stealth
    # browser clears the wall and returns the real article. The escalation the whole feature is for.
    blocked = httpx.Response(403, headers={"content-type": "text/html"})
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tiered_handler(
                blocked,
                reader=_CHALLENGE_MD,
                solver_response={"url": "https://x.example/walled", "response": _SOLVED_HTML},
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "Real article content" in result.text
    assert result.url == "https://x.example/walled"  # the public URL, not the solver's


async def test_solver_that_is_still_challenged_surfaces_the_block() -> None:
    # The solver ran but the stealth browser ALSO got a challenge (interactive Turnstile it
    # can't clear): that is a miss too, so the original 403 surfaces — never the challenge text.
    challenge = {"response": "<html><head><title>Just a moment...</title></head></html>"}
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tiered_handler(
                httpx.Response(403, headers={"content-type": "text/html"}),
                reader=_CHALLENGE_MD,
                solver_response=challenge,
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
    )
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 403


async def test_solver_failure_surfaces_the_block() -> None:
    # The solver itself errors (500): it returns None like any tier miss, so the original block
    # surfaces rather than crashing the fetch.
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tiered_handler(
                httpx.Response(403, headers={"content-type": "text/html"}),
                reader=_CHALLENGE_MD,
                solver_response=None,
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
    )
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 403


# The reader "succeeds" (200) but renders only the blocked origin's bare title — a thin shell,
# not the article (a real deep-research news miss: Reuters 401 → reader 429 → "reuters.com\n===").
_THIN_READER_MD = b"reuters.com\n==============="


async def test_a_thin_reader_shell_still_escalates_to_the_solver() -> None:
    # Direct 401 (Reuters's bot wall) → the reader renders a near-empty shell that is NOT a
    # challenge page, so it used to be accepted and the solver never ran. Now a sub-threshold
    # result no longer short-circuits escalation: the stealth browser is tried and recovers the
    # real article. This is the byparr-never-invoked bug the fix targets.
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tiered_handler(
                httpx.Response(401, headers={"content-type": "text/html"}),
                reader=_THIN_READER_MD,
                solver_response={"url": "https://x.example/walled", "response": _SOLVED_HTML},
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "Real article content" in result.text  # the solver's page, not the reader's shell


async def test_a_thin_reader_shell_is_returned_when_no_tier_beats_it() -> None:
    # When the solver also fails to recover more, the thin reader result is still returned rather
    # than lost — escalation only PREFERS a richer tier, it never discards what it already has.
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tiered_handler(
                httpx.Response(401, headers={"content-type": "text/html"}),
                reader=_THIN_READER_MD,
                solver_response=None,  # solver 500s → a miss
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "reuters.com" in result.text


# --- JS-app shells (a 200 whose content is painted by scripts, not served as HTML) ------


# An SPA shell that LEAKS a little chrome — the shape that used to defeat escalation entirely,
# because the trigger was a strictly EMPTY extraction and this is a few words short of empty.
_JS_SHELL_HTML = (
    b'<html><head><title>Qwen</title></head><body><div id="root"></div>'
    b"<p>Loading. Accept cookies to continue.</p>"
    b'<script src="/app.js"></script></body></html>'
)
# The same failure with no framework marker at all (the real `qwen.ai/blog`: ~92 KB of HTML and
# 53 script tags painting a page our extractor reads as blank). Caught by shape, not by name, so
# a framework we have never heard of still escalates.
_SCRIPT_HEAVY_SHELL_HTML = (
    b"<html><head><title>App</title></head><body>"
    + b"<script>var payload = '"
    + b"x" * 25_000
    + b"';</script>" * 1
    + b"<script>init();</script>" * 9
    + b"</body></html>"
)
# A reader render long enough to WIN outright (past `_MIN_RECOVERED_CHARS`) rather than merely
# be held as the best thin candidate — the difference between recovering the app and confirming
# we still haven't seen it.
_RENDERED_MD = b"Rendered by the reader: the content the static HTML never carried. " * 5
# A page that is genuinely short — not a shell. Plain HTML, no scripts: escalating it would burn
# the heavy solver and the PAID Tavily tier on every fetch, which is why detection needs evidence
# rather than a text-length rule.
_SHORT_REAL_HTML = (
    b"<html><head><title>Status</title></head><body><p>The service is operating normally."
    b" No incidents are open.</p></body></html>"
)


async def test_a_leaky_js_shell_escalates_to_the_reader() -> None:
    # The regression this feature targets: a 200 SPA shell carrying a few words of chrome is not
    # empty, so it used to be returned as a successful fetch and read as "this page has nothing
    # on it". It now escalates like the empty case and the renderer supplies the real content.
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tiered_handler(
                httpx.Response(200, content=_JS_SHELL_HTML, headers={"content-type": "text/html"}),
                reader=_RENDERED_MD,
                solver_response=None,
            )
        ),
        reader_url="http://reader:3000",
    )
    result = await fetcher.fetch("https://qwen.example/blog")
    assert "the content the static HTML never carried" in result.text
    assert not result.js_shell  # the page was recovered, so it is no longer a shell


async def test_a_script_heavy_shell_with_no_framework_marker_escalates() -> None:
    # Detection by SHAPE (a lot of HTML and a lot of script for almost no text), so a framework
    # whose markers we don't carry is still recovered.
    shell = httpx.Response(
        200, content=_SCRIPT_HEAVY_SHELL_HTML, headers={"content-type": "text/html"}
    )
    fetcher = WebFetcher(
        transport=httpx.MockTransport(_reader_handler(shell)),
        reader_url="http://reader:3000",
    )
    result = await fetcher.fetch("https://spa.example/page")
    assert "Rendered by the reader" in result.text


async def test_a_leaky_js_shell_escalates_all_the_way_to_the_solver() -> None:
    # The reader renders the shell no better than we did (a thin miss), so the stealth browser
    # gets its turn — the same ladder a bot-wall walks, now reachable from a 200.
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tiered_handler(
                httpx.Response(200, content=_JS_SHELL_HTML, headers={"content-type": "text/html"}),
                reader=_THIN_READER_MD,
                solver_response={"url": "https://qwen.example/blog", "response": _SOLVED_HTML},
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
    )
    result = await fetcher.fetch("https://qwen.example/blog")
    assert "Real article content" in result.text


async def test_an_unrecovered_js_shell_is_flagged_not_dropped() -> None:
    # No tier renders it: the shell is still returned (nothing is ever discarded) but carries
    # `js_shell`, which is what lets the tool say "this needs a browser we already tried"
    # instead of "no readable text" — the ambiguity the model was left to guess at.
    shell = httpx.Response(200, content=_JS_SHELL_HTML, headers={"content-type": "text/html"})
    result = await WebFetcher(transport=httpx.MockTransport(lambda _: shell)).fetch(
        "https://qwen.example/blog"
    )
    assert result.js_shell
    assert "Accept cookies" in result.text  # the shell we do have is not thrown away


async def test_a_thin_recovery_of_a_js_shell_stays_flagged() -> None:
    # The reader "succeeds" but renders only a bare title: that hasn't painted the app either,
    # so the flag survives the recovery rather than passing a few words off as the article.
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tiered_handler(
                httpx.Response(200, content=_JS_SHELL_HTML, headers={"content-type": "text/html"}),
                reader=_THIN_READER_MD,
                solver_response=None,  # solver 500s → a miss
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
    )
    result = await fetcher.fetch("https://qwen.example/blog")
    assert result.js_shell


async def test_a_short_real_page_is_not_treated_as_a_js_shell() -> None:
    # The precision bar: a genuinely short page is served as-is, with no recovery tier touched.
    # A false positive here would spend a stealth-browser solve and a paid extraction per fetch.
    hits: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host or "")
        if request.url.host == "reader":
            return httpx.Response(200, content=b"reader ran", headers={"content-type": "text/x"})
        return httpx.Response(200, content=_SHORT_REAL_HTML, headers={"content-type": "text/html"})

    fetcher = WebFetcher(
        transport=httpx.MockTransport(handle),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
    )
    result = await fetcher.fetch("https://status.example/")
    assert "operating normally" in result.text
    assert not result.js_shell
    assert "reader" not in hits and "byparr" not in hits


async def test_a_real_article_on_a_framework_page_is_never_a_js_shell() -> None:
    # Every signal is gated on a thin extraction, so a hydrated framework page — markers, scripts
    # and all — that DID serve its article is never flagged and never re-fetched.
    article = (
        b'<html><head><title>Real</title></head><body><div id="root">'
        b"<p>" + b"Real reporting that the server rendered into the HTML. " * 20 + b"</p>"
        b'</div><script src="/app.js"></script></body></html>'
    )
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, content=article, headers={"content-type": "text/html"})
        ),
        reader_url="http://reader:3000",
    )
    result = await fetcher.fetch("https://news.example/story")
    assert "Real reporting" in result.text
    assert not result.js_shell


async def test_a_js_shell_at_an_offset_does_not_re_escalate() -> None:
    # Escalation is a FIRST-page decision: a thin window deep into a page (or a `find` that
    # simply didn't match) is not a shell, so paging never silently re-runs the whole ladder.
    hits: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host or "")
        return httpx.Response(200, content=_JS_SHELL_HTML, headers={"content-type": "text/html"})

    fetcher = WebFetcher(transport=httpx.MockTransport(handle), reader_url="http://reader:3000")
    await fetcher.fetch("https://qwen.example/blog", offset=5_000)
    assert "reader" not in hits


async def test_the_web_fetch_tool_explains_an_unrendered_js_app() -> None:
    # What the model actually reads. "That page had no readable text" is indistinguishable from
    # "the topic doesn't exist", and it hides that the whole ladder already ran — so the model
    # either gives up or burns calls re-fetching the same URL. The message says both.
    shell = httpx.Response(
        200,
        content=b'<html><head><title>Qwen</title></head><body><div id="root"></div></body></html>',
        headers={"content-type": "text/html"},
    )
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(lambda _: shell))
    )
    out = str(await handlers["web_fetch"]({"url": "https://qwen.example/blog"}, _fresh_ctx()))
    assert "JavaScript app" in out
    assert "will not help" in out  # don't re-fetch this URL
    assert "NOT evidence" in out  # don't read it as "the page/topic is empty"


async def test_the_web_fetch_tool_flags_a_shell_that_leaked_a_few_words() -> None:
    # The more dangerous half: a shell with a little text LOOKS like a successful short read, so
    # without the note the model quotes page chrome as if it were the article.
    shell = httpx.Response(200, content=_JS_SHELL_HTML, headers={"content-type": "text/html"})
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(lambda _: shell))
    )
    out = str(await handlers["web_fetch"]({"url": "https://qwen.example/blog"}, _fresh_ctx()))
    assert "UNREAD page" in out
    assert "Accept cookies" in out  # the shell text is still shown, just labelled


# --- solver-first domains (a known hard-wall origin skips the doomed direct+reader legs) ---


_ORIGIN_HTML = (
    b"<html><head><title>Origin</title></head><body><p>Direct origin content.</p></body></html>"
)


def _tracking_handler(*, reader: bytes | None, solver_response, hits: list[str], direct=None):  # type: ignore[no-untyped-def]
    """Like `_tiered_handler` but records the host of every leg, so a test can assert which
    tiers ran. The origin host answers with `direct` (default: a 200 article)."""

    def handle(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        if request.url.host == "byparr":
            if solver_response is None:
                return httpx.Response(500)
            return httpx.Response(
                200, json={"solution": {"url": str(request.url), **solver_response}}
            )
        if request.url.host == "reader":
            if reader is None:
                return httpx.Response(500)
            return httpx.Response(200, content=reader, headers={"content-type": "text/markdown"})
        if direct is not None:
            return direct
        return httpx.Response(200, content=_ORIGIN_HTML, headers={"content-type": "text/html"})

    return handle


async def test_solver_first_domain_skips_the_direct_and_reader_legs() -> None:
    # A listed domain (reuters.com) goes straight to the solver — no direct fetch, no reader —
    # so it never pays the 401 + 429 that always fail on it. The solver's page is returned.
    hits: list[str] = []
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tracking_handler(
                reader=_THIN_READER_MD,
                solver_response={"response": _SOLVED_HTML},
                hits=hits,
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
        solver_first_domains=("reuters.com",),
    )
    result = await fetcher.fetch("https://www.reuters.com/world/us/some-story-2026-08-12/")
    assert "Real article content" in result.text  # the solver served it
    assert hits == ["byparr"]  # neither the origin nor the reader was touched


async def test_solver_first_falls_back_to_the_normal_path_on_a_solver_miss() -> None:
    # byparr is down (500): the solver-first shortcut misses, so the fetch falls through to the
    # ordinary path. Here the origin also 401s and the reader recovers it — and crucially the
    # solver is NOT retried on the fall-through (skip_solver), so byparr is hit exactly once.
    hits: list[str] = []
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tracking_handler(
                reader=_READER_MD,  # the reader recovers the page
                solver_response=None,  # byparr 500s
                hits=hits,
                direct=httpx.Response(401, headers={"content-type": "text/html"}),
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
        solver_first_domains=("reuters.com",),
    )
    result = await fetcher.fetch("https://www.reuters.com/world/us/some-story/")
    assert "Rendered by the reader" in result.text  # the reader safety-net recovered it
    assert hits.count("byparr") == 1  # tried once up front, NOT again on the fall-through
    assert "www.reuters.com" in hits and "reader" in hits  # both fall-through legs ran


async def test_a_non_listed_domain_takes_the_normal_direct_path() -> None:
    # An unlisted domain is unaffected: it fetches directly first and never front-loads the solver.
    hits: list[str] = []
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _tracking_handler(
                reader=_READER_MD, solver_response={"response": _SOLVED_HTML}, hits=hits
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
        solver_first_domains=("reuters.com",),
    )
    result = await fetcher.fetch("https://apnews.com/article/some-story")
    assert "Direct origin content" in result.text  # served by the direct leg
    assert hits == ["apnews.com"]  # solver never front-loaded


def test_prefers_solver_matches_domain_and_subdomains_only() -> None:
    fetcher = WebFetcher(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        solver_url="http://byparr:8191",
        solver_first_domains=("reuters.com", "wsj.com"),
    )
    assert fetcher._prefers_solver("https://reuters.com/x")
    assert fetcher._prefers_solver("https://www.reuters.com/x")
    assert fetcher._prefers_solver("https://feeds.wsj.com/y")
    assert not fetcher._prefers_solver("https://apnews.com/x")
    assert not fetcher._prefers_solver("https://notreuters.com/x")  # suffix guard, not substring
    assert not fetcher._prefers_solver("https://reuters.com.evil.test/x")


def test_solver_first_is_off_when_the_solver_is_unconfigured() -> None:
    # No solver endpoint → nothing to prefer, so a listed domain still takes the normal path.
    fetcher = WebFetcher(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        solver_first_domains=("reuters.com",),
    )
    assert not fetcher._prefers_solver("https://www.reuters.com/x")


# --- truncation + pagination (a long page's tail is fetchable, not just flagged) ----


def _plain(n_chars: int):  # type: ignore[no-untyped-def]
    """A MockTransport serving `n_chars` of plain text (distinct start/end markers so a
    window can be told apart from the whole)."""
    body = ("S" + "x" * (n_chars - 2) + "E").encode()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/plain"})

    return handle


async def test_fetch_windows_a_long_page_and_reports_the_total() -> None:
    from jbrain.web.fetch import _MAX_CHARS

    total = _MAX_CHARS + 12_345
    fetcher = WebFetcher(transport=httpx.MockTransport(_plain(total)))
    first = await fetcher.fetch("https://x.example/big")
    assert first.offset == 0
    assert len(first.text) == _MAX_CHARS  # first window is exactly one cap
    assert first.total_chars == total
    assert first.text.startswith("S")  # the head
    assert first.truncated is False  # sub-cap download: the full text IS available, via paging

    tail = await fetcher.fetch("https://x.example/big", offset=_MAX_CHARS)
    assert tail.offset == _MAX_CHARS
    assert len(tail.text) == 12_345
    assert tail.text.endswith("E")  # the tail we couldn't see in the first window


async def test_fetch_short_page_is_a_single_whole_window() -> None:
    result = await WebFetcher(transport=httpx.MockTransport(_plain(200))).fetch(
        "https://x.example/p"
    )
    assert result.offset == 0
    assert result.total_chars == 200
    assert result.truncated is False


async def test_web_fetch_tool_gives_an_actionable_next_offset() -> None:
    from jbrain.web.fetch import _MAX_CHARS

    total = _MAX_CHARS + 12_345
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(_plain(total)))
    )
    out = str(await handlers["web_fetch"]({"url": "https://x.example/big"}, _fresh_ctx()))
    assert "truncated" in out.lower()
    # The notice names the EXACT next call so the model can page, not just "it's cut off".
    assert f"offset={_MAX_CHARS}" in out


async def test_web_fetch_tool_second_page_reads_the_tail_without_a_notice() -> None:
    from jbrain.web.fetch import _MAX_CHARS

    total = _MAX_CHARS + 12_345
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(_plain(total)))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/big", "offset": _MAX_CHARS}, _fresh_ctx()
        )
    )
    assert out.rstrip().endswith("E")  # the real end of the page
    assert "offset=" not in out  # nothing left to page to
    assert f"continued from offset {_MAX_CHARS}" in out


async def test_web_fetch_tool_offset_past_the_end_says_so() -> None:
    from jbrain.web.fetch import _MAX_CHARS

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(_plain(1000)))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/p", "offset": _MAX_CHARS}, _fresh_ctx()
        )
    )
    assert "no more content" in out.lower()
    assert "1000 characters" in out


async def test_web_fetch_tool_omits_the_notice_for_a_whole_page() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = await handlers["web_fetch"]({"url": "https://x.example/p"}, _fresh_ctx())
    assert "truncated" not in str(out).lower()


# --- find: jump the window to a keyword on a big page --------------------------


def _plain_body(content: str):  # type: ignore[no-untyped-def]
    data = content.encode()

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data, headers={"content-type": "text/plain"})

    return handle


async def test_fetch_find_positions_the_window_on_the_keyword() -> None:
    from jbrain.web.fetch import _FIND_LEAD

    needle = "MARKER2026"
    content = ("H" * 40_000) + needle + ("Z" * 5_000)  # keyword buried deep in the page
    result = await WebFetcher(transport=httpx.MockTransport(_plain_body(content))).fetch(
        "https://x.example/big", find=needle
    )
    assert result.match_offsets == (40_000,)
    assert result.match_count == 1
    assert result.offset == 40_000 - _FIND_LEAD  # a little lead-in before the match
    assert needle in result.text  # the window actually contains the section we searched for


async def test_fetch_find_counts_all_matches_but_caps_the_offset_list() -> None:
    from jbrain.web.fetch import _MAX_MATCH_OFFSETS

    block = "q" * 100 + "ROW"  # "ROW" every 103 chars
    content = block * 30  # 30 occurrences
    result = await WebFetcher(transport=httpx.MockTransport(_plain_body(content))).fetch(
        "https://x.example/rows",
        find="row",  # case-insensitive
    )
    assert result.match_count == 30
    assert len(result.match_offsets) == _MAX_MATCH_OFFSETS  # capped, but the true count is kept
    assert result.match_offsets[0] == 100


async def test_fetch_find_after_offset_skips_earlier_matches() -> None:
    content = ("A" * 1_000) + "NEEDLE" + ("B" * 10_000) + "NEEDLE" + ("C" * 1_000)
    fetcher = WebFetcher(transport=httpx.MockTransport(_plain_body(content)))
    # With an offset past the first hit, find lands on the SECOND occurrence.
    result = await fetcher.fetch("https://x.example/p", offset=2_000, find="NEEDLE")
    assert result.match_offsets[0] == 1_000 + 6 + 10_000  # the second NEEDLE
    assert result.match_count == 1  # only matches at/after the offset are counted


async def test_web_fetch_tool_find_jumps_and_lists_other_matches() -> None:
    block = "q" * 100 + "2026"
    content = block * 5  # five "2026" hits
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(_plain_body(content)))
    )
    out = str(
        await handlers["web_fetch"]({"url": "https://x.example/list", "find": "2026"}, _fresh_ctx())
    )
    assert "found 5 match" in out.lower()
    assert "other matches for '2026' at offsets:" in out.lower()


async def test_web_fetch_tool_find_no_match_says_so() -> None:
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(_plain_body("no digits here")))
    )
    out = str(
        await handlers["web_fetch"]({"url": "https://x.example/p", "find": "2026"}, _fresh_ctx())
    )
    assert "no match for '2026'" in out.lower()
    assert "14 chars" in out  # reports the page length so the model can decide what to do


# --- repeated-failed-fetch backstop (don't burn the budget on a dead URL) ------


def _fresh_ctx() -> ToolContext:
    # A per-turn context (fresh failed-fetch memo), so the guard's state doesn't leak
    # across tests the way the shared module-level CTX would.
    return ToolContext(session=SessionContext(principal_kind="owner"), scopes=())


async def test_web_fetch_refuses_to_refetch_a_url_that_already_404d_this_turn() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    ctx = _fresh_ctx()
    url = "https://en.wikipedia.org/wiki/List_of_Falcon_9_and_Falcon_Heavy_launches_(2026)"
    first = await handlers["web_fetch"]({"url": url}, ctx)
    assert "404" in str(first)
    # The identical re-fetch is short-circuited BEFORE any network call — the backstop
    # that stops the model burning its whole budget re-requesting one dead URL.
    second = await handlers["web_fetch"]({"url": url}, ctx)
    assert calls["n"] == 1  # no second request was made
    assert "already fetched" in str(second).lower()
    assert "web_search" in str(second)


async def test_web_fetch_refetch_guard_ignores_a_fragment_and_trailing_slash() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    ctx = _fresh_ctx()
    await handlers["web_fetch"]({"url": "https://x.example/gone/"}, ctx)
    # Same page, differing only by a fragment and the trailing slash — still recognized.
    again = await handlers["web_fetch"]({"url": "https://x.example/gone#section"}, ctx)
    assert calls["n"] == 1
    assert "already fetched" in str(again).lower()


async def test_web_fetch_guard_does_not_block_a_different_url() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("gone"):
            return httpx.Response(404, headers={"content-type": "text/html"})
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    ctx = _fresh_ctx()
    await handlers["web_fetch"]({"url": "https://x.example/gone"}, ctx)
    # A genuinely different path (the real page) is NOT blocked by the failed one.
    ok = await handlers["web_fetch"]({"url": "https://x.example/real"}, ctx)
    assert "Hi There" in str(ok)


async def test_web_fetch_guard_is_scoped_to_one_turn() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    url = "https://x.example/gone"
    await handlers["web_fetch"]({"url": url}, _fresh_ctx())
    # A different turn (fresh context) starts with a clean memo — the failure from another
    # turn must not suppress this one's first attempt.
    retry = await handlers["web_fetch"]({"url": url}, _fresh_ctx())
    assert "already fetched" not in str(retry).lower()
    assert "404" in str(retry)


async def test_reader_still_refuses_a_non_public_target() -> None:
    # The reader path guards the TARGET host the same way: a model-supplied private URL
    # can't be laundered off-box through the reader. (Real DNS — no transport.)
    fetcher = WebFetcher(reader_url="http://reader:3000")
    with pytest.raises(WebFetchError):
        await fetcher.fetch("http://169.254.169.254/latest/meta-data")


# --- SSRF guard (the real-network host check, no transport) ----------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",  # loopback
        "http://localhost/x",  # loopback by name
        "http://10.0.0.1/x",  # private
        "http://192.168.1.1/x",  # private
        "http://169.254.169.254/latest/meta-data",  # link-local / cloud metadata
        "http://[::1]/x",  # IPv6 loopback
    ],
)
async def test_fetch_blocks_non_public_addresses(url: str) -> None:
    """The model-supplied URL can't be pointed at the box's own internal services
    (db, embed, searxng) or the cloud metadata endpoint — the SSRF guard."""
    with pytest.raises(WebFetchError):
        await WebFetcher().fetch(url)


async def test_fetch_rejects_non_http_scheme_before_resolving() -> None:
    with pytest.raises(WebFetchError):
        await WebFetcher().fetch("file:///etc/passwd")


def test_is_public_classifies_addresses() -> None:
    import ipaddress

    from jbrain.web.fetch import _is_public

    assert _is_public(ipaddress.ip_address("8.8.8.8"))
    assert not _is_public(ipaddress.ip_address("127.0.0.1"))
    assert not _is_public(ipaddress.ip_address("10.0.0.1"))
    assert not _is_public(ipaddress.ip_address("169.254.169.254"))
    # An IPv4-mapped IPv6 private address must not slip through its v6 form.
    assert not _is_public(ipaddress.ip_address("::ffff:10.0.0.1"))


# --- web tool handlers -----------------------------------------------------


async def test_web_search_tool_formats_results() -> None:
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_OK)), WebFetcher()
    )
    out = await handlers["web_search"]({"query": "python"}, CTX)
    # The header warns that results are unverified leads that must be fetched before use.
    assert "Web results" in out and "UNVERIFIED LEADS" in out and "web_fetch" in out
    assert "https://a.example/1" in out and "Result one" in out


async def test_web_search_tool_needs_a_query() -> None:
    handlers = build_web_handlers(SearxngClient(""), WebFetcher())
    assert "non-empty query" in await handlers["web_search"]({"query": "  "}, CTX)


async def test_web_search_tool_surfaces_errors_as_recoverable_text() -> None:
    handlers = build_web_handlers(SearxngClient(""), WebFetcher())
    # Unconfigured search returns a message, not an exception (the loop keeps going).
    assert "not configured" in await handlers["web_search"]({"query": "x"}, CTX)


async def test_web_fetch_tool_returns_page_text() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = await handlers["web_fetch"]({"url": "https://x.example/p"}, CTX)
    assert "Hi There" in out and "First para." in out


async def test_web_fetch_tool_needs_a_url() -> None:
    handlers = build_web_handlers(SearxngClient(""), WebFetcher())
    assert "needs a url" in await handlers["web_fetch"]({}, CTX)


# --- wall-display tendril events -------------------------------------------


async def test_web_search_emits_a_tendril_event() -> None:
    fired: list[tuple[str, str | None]] = []
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_OK)),
        WebFetcher(),
        emit=lambda kind, text=None: fired.append((kind, text)),
    )
    await handlers["web_search"]({"query": "python"}, CTX)
    # The query rides the emit so the display can stream it (the emitter gates the text
    # on the turn's opt-in; here we assert the tool forwards it).
    assert fired == [("web_search", "python")]


async def test_web_fetch_emits_a_tendril_event() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    fired: list[tuple[str, str | None]] = []
    handlers = build_web_handlers(
        SearxngClient(""),
        WebFetcher(transport=httpx.MockTransport(handle)),
        emit=lambda kind, text=None: fired.append((kind, text)),
    )
    await handlers["web_fetch"]({"url": "https://x.example/p"}, CTX)
    assert fired == [("web_fetch", "https://x.example/p")]


async def test_invalid_web_calls_do_not_emit() -> None:
    # An empty query / missing url never reaches out, so it fires no tendril.
    fired: list[tuple[str, str | None]] = []
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(), emit=lambda kind, text=None: fired.append((kind, text))
    )
    await handlers["web_search"]({"query": "  "}, CTX)
    await handlers["web_fetch"]({}, CTX)
    assert fired == []


# --- YouTube view (lightweight title/channel/description/captions via web_fetch) ----


async def _fake_youtube(url: str):  # type: ignore[no-untyped-def]
    md = (
        "**Channel:** Space Channel\n\n"
        "## Description\n\nA recap of the launch.\n\n"
        "## Transcript (captions)\n\nten nine eight liftoff"
    )
    return ("Rocket Launch Recap", md)


async def test_web_fetch_renders_a_youtube_url_as_the_video_view() -> None:
    handlers = build_web_handlers(SearxngClient(""), WebFetcher(), youtube=_fake_youtube)
    out = str(await handlers["web_fetch"]({"url": "https://youtu.be/abc"}, _fresh_ctx()))
    assert "Rocket Launch Recap" in out
    assert "Space Channel" in out
    assert "## Transcript (captions)" in out
    assert "ten nine eight liftoff" in out


async def test_web_fetch_youtube_supports_find() -> None:
    handlers = build_web_handlers(SearxngClient(""), WebFetcher(), youtube=_fake_youtube)
    out = str(
        await handlers["web_fetch"](
            {"url": "https://youtu.be/abc", "find": "liftoff"}, _fresh_ctx()
        )
    )
    assert "found 1 match(es) for 'liftoff'" in out


async def test_web_fetch_youtube_falls_back_to_html_when_unresolvable() -> None:
    async def yt_none(url: str):  # type: ignore[no-untyped-def]
        return None  # unresolvable video → fall through to a normal HTML fetch

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle)), youtube=yt_none
    )
    out = str(await handlers["web_fetch"]({"url": "https://youtu.be/gone"}, _fresh_ctx()))
    assert "Hi There" in out  # the HTML fetch ran instead


async def test_web_fetch_youtube_not_wired_uses_normal_fetch() -> None:
    # No youtube resolver injected (e.g. yt-dlp absent): a youtube URL just gets a plain fetch.
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = str(await handlers["web_fetch"]({"url": "https://youtu.be/abc"}, _fresh_ctx()))
    assert "Hi There" in out


# --- outline: a section map for a big page ------------------------------------

_OUTLINE_PAGE = (
    "## Intro\n"
    + ("a" * 20_000)
    + "\n## History\n"
    + ("b" * 20_000)
    + "\n## Y2026\n"
    + ("c" * 5_000)
).encode()


def _outline_handlers():  # type: ignore[no-untyped-def]
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_OUTLINE_PAGE, headers={"content-type": "text/plain"})

    return build_web_handlers(SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle)))


async def test_web_fetch_appends_a_section_outline_on_a_big_page() -> None:
    out = str(
        await _outline_handlers()["web_fetch"]({"url": "https://x.example/big"}, _fresh_ctx())
    )
    assert "Sections on this page" in out
    assert "History → offset" in out
    assert "Y2026 → offset" in out  # the far section is reachable by the offset given


async def test_web_fetch_outline_true_returns_only_the_outline() -> None:
    out = str(
        await _outline_handlers()["web_fetch"](
            {"url": "https://x.example/big", "outline": True}, _fresh_ctx()
        )
    )
    assert "Outline — 3 sections" in out
    assert "History → offset" in out
    assert "a" * 100 not in out  # the body text is NOT included in the outline-only view


async def test_web_fetch_outline_true_on_a_flat_page_says_no_sections() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"just prose, no headings", headers={"content-type": "text/plain"}
        )

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/flat", "outline": True}, _fresh_ctx()
        )
    )
    assert "no section headings" in out.lower()


# --- find with regex ----------------------------------------------------------


async def test_fetch_find_regex_matches_a_pattern() -> None:
    from jbrain.web.fetch import _FIND_LEAD

    content = ("H" * 5_000) + "seen 2026-07-04 launch and 2026-07-30 launch" + ("Z" * 500)
    first_match = 5_000 + len("seen ")  # where "2026-07-04" begins
    result = await WebFetcher(transport=httpx.MockTransport(_plain_body(content))).fetch(
        "https://x.example/dates", find=r"2026-\d\d-\d\d", find_regex=True
    )
    assert result.match_count == 2  # both ISO dates matched
    assert result.offset == first_match - _FIND_LEAD  # window jumped to the match (with lead-in)
    assert "2026-07-04" in result.text  # positioned at the first match


async def test_fetch_find_literal_does_not_treat_dot_as_wildcard() -> None:
    # Default (literal) mode: "a.c" matches the literal string, not "abc".
    fetcher = WebFetcher(transport=httpx.MockTransport(_plain_body("abc and a.c here")))
    assert (await fetcher.fetch("https://x.example/p", find="a.c")).match_count == 1
    # As a regex, "a.c" matches both "abc" and "a.c".
    assert (
        await fetcher.fetch("https://x.example/p", find="a.c", find_regex=True)
    ).match_count == 2


async def test_web_fetch_tool_regex_reports_and_labels_matches() -> None:
    content = ("q" * 200) + "2025-01-01" + ("q" * 200) + "2026-12-31"
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(_plain_body(content)))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/d", "find": r"202[56]-\d\d-\d\d", "regex": True},
            _fresh_ctx(),
        )
    )
    assert "regex '202[56]-\\d\\d-\\d\\d'" in out
    assert "found 2 match(es)" in out


async def test_web_fetch_tool_rejects_an_invalid_regex_without_fetching() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"whatever", headers={"content-type": "text/plain"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/p", "find": "a(b", "regex": True}, _fresh_ctx()
        )
    )
    assert "invalid regex" in out.lower()
    assert calls["n"] == 0  # bailed before any fetch, so no dead-URL memo either


# --- extract mode (regex → matches, not a window) -----------------------------


def test_extract_matches_returns_capture_groups_and_true_total() -> None:
    from jbrain.web.fetch import extract_matches

    text = "Total: 12 items, Total: 7 items, Total: 340 items"
    matches, total = extract_matches(text, r"Total:\s*(\d+)")
    assert total == 3
    assert [m.match for m in matches] == ["Total: 12", "Total: 7", "Total: 340"]
    assert [m.groups for m in matches] == [("12",), ("7",), ("340",)]
    assert matches[0].offset == 0  # offset into the full text, for a follow-up read
    assert "Total: 12 items" in matches[0].context  # context surrounds the hit


def test_extract_matches_skips_zero_width_so_a_star_pattern_cannot_flood() -> None:
    from jbrain.web.fetch import extract_matches

    # `a*` matches empty strings everywhere; those must not become matches.
    matches, total = extract_matches("banana", "a*")
    assert total == 3  # the three runs of "a", not a match at every position
    assert all(m.match for m in matches)


def test_extract_matches_caps_the_list_but_keeps_the_real_count() -> None:
    from jbrain.web.fetch import _MAX_EXTRACT_MATCHES, extract_matches

    text = "x9 " * (_MAX_EXTRACT_MATCHES + 25)
    matches, total = extract_matches(text, r"x\d")
    assert total == _MAX_EXTRACT_MATCHES + 25  # every occurrence counted
    assert len(matches) == _MAX_EXTRACT_MATCHES  # but the returned list is capped


async def test_web_fetch_tool_extract_lists_matches_from_the_whole_page() -> None:
    # Two ISO dates far apart on a long page — extract pulls both, not just the first window.
    content = "2025-01-01" + ("q" * 40_000) + "2026-12-31"
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(_plain_body(content)))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/d", "find": r"20\d\d-\d\d-\d\d", "extract": True},
            _fresh_ctx(),
        )
    )
    assert "extracted 2 match(es)" in out.lower()
    assert "2025-01-01" in out and "2026-12-31" in out


async def test_web_fetch_tool_extract_reports_capture_groups() -> None:
    content = "Price: $12.50 here and Price: $9.99 there"
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(_plain_body(content)))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/p", "find": r"Price: \$(\d+\.\d\d)", "extract": True},
            _fresh_ctx(),
        )
    )
    assert "groups: 12.50" in out and "groups: 9.99" in out


async def test_web_fetch_tool_extract_no_match_says_so() -> None:
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(_plain_body("no digits here")))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/p", "find": r"\d{4}", "extract": True}, _fresh_ctx()
        )
    )
    assert "no match for regex" in out.lower()


async def test_web_fetch_tool_extract_without_find_is_rejected_before_fetching() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"x", headers={"content-type": "text/plain"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = str(
        await handlers["web_fetch"]({"url": "https://x.example/p", "extract": True}, _fresh_ctx())
    )
    assert "needs a find pattern" in out.lower()
    assert calls["n"] == 0  # rejected before any network call


async def test_web_fetch_tool_extract_validates_the_regex_even_without_regex_flag() -> None:
    # Extract always treats find as a regex, so a bad pattern is caught even if regex is unset.
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=b"x", headers={"content-type": "text/plain"})

    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle))
    )
    out = str(
        await handlers["web_fetch"](
            {"url": "https://x.example/p", "find": "a(b", "extract": True}, _fresh_ctx()
        )
    )
    assert "invalid regex" in out.lower()
    assert calls["n"] == 0


# --- cross-turn tool-result artifacts (CROSS_TURN_TOOL_RESULTS_PLAN.md) ---------

import hashlib  # noqa: E402
from dataclasses import replace  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import cast  # noqa: E402

from jbrain.agent.tool_artifacts import ToolArtifact, ToolArtifactRepo  # noqa: E402


class _FakeBlobs:
    """An in-memory content-addressed store — a structural BlobStore stand-in."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self.blobs[digest] = data
        return digest

    async def put_stream(self, chunks: AsyncIterator[bytes]) -> str:
        return await self.put(b"".join([chunk async for chunk in chunks]))

    async def get(self, sha256: str) -> bytes:
        try:
            return self.blobs[sha256]
        except KeyError:
            raise FileNotFoundError(sha256) from None

    def path_for(self, sha256: str) -> Path:
        return Path(sha256)

    async def exists(self, sha256: str) -> bool:
        return sha256 in self.blobs

    def usage(self) -> tuple[int, int]:
        return (len(self.blobs), sum(len(b) for b in self.blobs.values()))


class _FakeArtifacts:
    """An in-memory ToolArtifactRepo: upsert-by-url, get, cursor-advance, list — the
    firewall is Postgres RLS in prod, so the fake just tracks rows."""

    def __init__(self) -> None:
        self.by_id: dict[str, ToolArtifact] = {}
        self._by_url: dict[tuple[str, str], str] = {}

    async def session_context(self, session, session_id):  # type: ignore[no-untyped-def]
        return (session, "general")

    async def remember(  # type: ignore[no-untyped-def]
        self, ctx, session_id, *, kind, source_url, title, sha256, total_chars, domain_code
    ):
        key = (session_id, source_url)
        if key in self._by_url:
            aid = self._by_url[key]
            art = replace(
                self.by_id[aid],
                kind=kind,
                title=title,
                sha256=sha256,
                total_chars=total_chars,
                last_offset=0,
            )
        else:
            aid = f"art-{len(self.by_id) + 1}"
            art = ToolArtifact(
                id=aid,
                kind=kind,
                source_url=source_url,
                title=title,
                sha256=sha256,
                total_chars=total_chars,
                last_offset=0,
                domain_code=domain_code,
            )
            self._by_url[key] = aid
        self.by_id[aid] = art
        return art

    async def get(self, ctx, artifact_id):  # type: ignore[no-untyped-def]
        return self.by_id.get(artifact_id)

    async def set_offset(self, ctx, artifact_id, last_offset):  # type: ignore[no-untyped-def]
        art = self.by_id.get(artifact_id)
        if art is not None:
            self.by_id[artifact_id] = replace(art, last_offset=max(0, last_offset))

    async def list_for_session(self, ctx, session_id, *, limit):  # type: ignore[no-untyped-def]
        return list(self.by_id.values())[:limit]


def _chat_ctx() -> ToolContext:
    # A jerv chat turn: an agent_session_id is what lets a tool persist a cross-turn artifact.
    return ToolContext(
        session=SessionContext(principal_kind="owner"), scopes=(), agent_session_id="sess-1"
    )


async def test_fetch_result_carries_the_whole_text_for_persistence() -> None:
    from jbrain.web.fetch import _MAX_CHARS

    total = _MAX_CHARS + 1_000
    result = await WebFetcher(transport=httpx.MockTransport(_plain(total))).fetch(
        "https://x.example/big"
    )
    assert len(result.text) == _MAX_CHARS  # the window
    assert len(result.full_text) == total  # the WHOLE text, for cross-turn persistence
    assert result.full_text.startswith("S") and result.full_text.endswith("E")


async def test_web_fetch_persists_a_cross_turn_artifact() -> None:
    from jbrain.web.fetch import _MAX_CHARS

    total = _MAX_CHARS + 5_000
    arts, blobs = _FakeArtifacts(), _FakeBlobs()
    handlers = build_web_handlers(
        SearxngClient(""),
        WebFetcher(transport=httpx.MockTransport(_plain(total))),
        artifacts=cast(ToolArtifactRepo, arts),
        blobs=blobs,
    )
    await handlers["web_fetch"]({"url": "https://x.example/big"}, _chat_ctx())
    assert len(arts.by_id) == 1
    art = next(iter(arts.by_id.values()))
    assert art.kind == "web_fetch"
    assert art.total_chars == total  # the whole page cached, not just the first window
    cached = (await blobs.get(art.sha256)).decode()
    assert cached.startswith("S") and cached.endswith("E")
    assert "read_artifact" in handlers  # the re-read tool is offered when a store is wired


async def test_web_fetch_does_not_persist_a_tiny_page() -> None:
    arts, blobs = _FakeArtifacts(), _FakeBlobs()
    handlers = build_web_handlers(
        SearxngClient(""),
        WebFetcher(transport=httpx.MockTransport(_plain(200))),
        artifacts=cast(ToolArtifactRepo, arts),
        blobs=blobs,
    )
    await handlers["web_fetch"]({"url": "https://x.example/small"}, _chat_ctx())
    assert arts.by_id == {}  # below the min-size threshold — cheaper to re-fetch than remember


async def test_web_fetch_without_a_store_is_a_plain_fetch() -> None:
    handlers = build_web_handlers(SearxngClient(""), WebFetcher())
    assert "read_artifact" not in handlers  # no store wired → the re-read tool isn't registered


async def test_read_artifact_pages_from_cache_and_continues_across_calls() -> None:
    from jbrain.web.fetch import _MAX_CHARS

    total = _MAX_CHARS + 5_000
    arts, blobs = _FakeArtifacts(), _FakeBlobs()
    handlers = build_web_handlers(
        SearxngClient(""),
        WebFetcher(transport=httpx.MockTransport(_plain(total))),
        artifacts=cast(ToolArtifactRepo, arts),
        blobs=blobs,
    )
    await handlers["web_fetch"]({"url": "https://x.example/big"}, _chat_ctx())
    aid = next(iter(arts.by_id))

    first = str(await handlers["read_artifact"]({"id": aid}, _chat_ctx()))
    assert "cached text" in first.lower()  # DATA fence
    assert "more remain" in first.lower()  # an actionable continue note
    assert arts.by_id[aid].last_offset == _MAX_CHARS  # cursor advanced by one window

    # A bare second call CONTINUES from the cursor to the tail — no network, no re-fetch.
    second = str(await handlers["read_artifact"]({"id": aid}, _chat_ctx()))
    assert second.rstrip().endswith("E")  # the real end of the page
    assert "continued from offset" in second.lower()
    assert "more remain" not in second.lower()  # nothing left to page


async def test_read_artifact_unknown_id_is_a_clean_message() -> None:
    arts, blobs = _FakeArtifacts(), _FakeBlobs()
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(), artifacts=cast(ToolArtifactRepo, arts), blobs=blobs
    )
    out = str(await handlers["read_artifact"]({"id": "nope"}, _chat_ctx()))
    assert "no page or transcript" in out.lower()


# --- domain skip list (24h paywall/bot-wall skip) --------------------------
# docs/archive/DOMAIN_HEALTH_PLAN.md: a persistent hard block records the DOMAIN for 24h;
# later searches drop it (reporting the count) and later fetches short-circuit. These run
# as unit tests with a fake repo (no DB); the RLS/upsert are proven in the integration test.

from typing import Any  # noqa: E402

from jbrain.web.domain_health import DomainSkipRepo  # noqa: E402


class _FakeDomainSkips:
    """A stand-in DomainSkipRepo: a fixed active set + a log of recorded blocks, so the
    webtools seams test without a database."""

    def __init__(self, active: frozenset[str] = frozenset()) -> None:
        self._active = frozenset(active)
        self.recorded: list[tuple[str, str, str]] = []

    async def active_hosts(self) -> frozenset[str]:
        return self._active

    async def record(self, host: str, reason: str, url: str) -> None:
        self.recorded.append((host, reason, url))


# A short subscriber wall served 200 in place of the article (the metered-site shape).
_PAYWALL_HTML = (
    b"<html><head><title>Members Only</title></head><body>"
    b"<h1>Members Only</h1>"
    b"<p>Subscribe to continue reading this article.</p></body></html>"
)


def test_is_paywall_page_precision() -> None:
    from jbrain.web.fetch import _is_paywall_page

    # A short wall carrying a canonical subscribe phrase → blocked.
    assert _is_paywall_page("Members Only", "Subscribe to continue reading.")
    assert _is_paywall_page("", "This content is for subscribers.")
    # A long article that merely CONTAINS the phrase must NOT be flagged (length gate).
    long_article = ("word " * 260) + "subscribe to continue reading"
    assert not _is_paywall_page("A real article", long_article)
    # An ordinary short page is not a paywall.
    assert not _is_paywall_page("Hi There", "Heading. First para. Second para.")


async def test_paywalled_fetch_raises_non_transient() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PAYWALL_HTML, headers={"content-type": "text/html"})

    with pytest.raises(WebFetchError) as excinfo:
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://wall.example/a")
    assert "paywall" in str(excinfo.value).lower()
    assert excinfo.value.transient is False  # a persistent block, so it IS recordable


async def test_a_timeout_is_flagged_transient() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(WebFetchError) as excinfo:
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://slow.example/a")
    assert excinfo.value.transient is True  # a glitch, not the site's fault
    assert excinfo.value.status is None


async def test_a_5xx_is_flagged_transient() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"content-type": "text/html"})

    with pytest.raises(WebFetchError) as excinfo:
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://down.example/a")
    assert excinfo.value.transient is True
    assert excinfo.value.status == 503


async def test_a_404_is_not_transient_and_not_recordable() -> None:
    from jbrain.agent.webtools import _block_reason

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "text/html"})

    with pytest.raises(WebFetchError) as excinfo:
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/gone")
    assert excinfo.value.status == 404
    assert excinfo.value.transient is False  # a real HTTP status, but the page is GONE
    assert _block_reason(excinfo.value) is None  # 404/410 never blocklists the domain


def test_block_reason_classifies_persistent_blocks_only() -> None:
    from jbrain.agent.webtools import _block_reason
    from jbrain.web.fetch import SearchFormError

    assert _block_reason(WebFetchError("paywalled/subscriber-only page")) == "paywalled"
    assert _block_reason(WebFetchError("nope", status=402)) == "paywalled"
    assert _block_reason(WebFetchError("bot-challenge page")) == "bot_blocked"
    assert _block_reason(WebFetchError("nope", status=403)) == "bot_blocked"
    assert _block_reason(WebFetchError("nope", status=429)) == "bot_blocked"
    # Not recordable: a 404/410, a transient glitch, a search form, or an unclassifiable error.
    assert _block_reason(WebFetchError("gone", status=404)) is None
    assert _block_reason(WebFetchError("glitch", status=500, transient=True)) is None
    assert _block_reason(SearchFormError("a search form, not results")) is None
    assert _block_reason(WebFetchError("some other error")) is None


async def test_web_search_drops_a_blocked_host_and_reports_the_count() -> None:
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_OK)),
        WebFetcher(),
        domain_skips=cast(DomainSkipRepo, _FakeDomainSkips(frozenset({"a.example"}))),
    )
    out = await handlers["web_search"]({"query": "python"}, CTX)
    assert isinstance(out, ToolOutput)
    # The blocked host is gone; the clean one stays; the model is told one was hidden.
    assert "https://a.example/1" not in str(out)
    assert "https://b.example/2" in str(out)
    assert "1 result(s) hidden" in str(out)
    assert [s.url for s in out.web_sources] == ["https://b.example/2"]


async def test_web_search_with_no_blocked_hosts_is_unchanged() -> None:
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_OK)),
        WebFetcher(),
        domain_skips=cast(DomainSkipRepo, _FakeDomainSkips()),
    )
    out = await handlers["web_search"]({"query": "python"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "hidden" not in str(out)
    assert [s.url for s in out.web_sources] == ["https://a.example/1", "https://b.example/2"]


async def test_web_fetch_short_circuits_a_blocked_host_without_a_network_call() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    handlers = build_web_handlers(
        SearxngClient(""),
        WebFetcher(transport=httpx.MockTransport(handle)),
        domain_skips=cast(DomainSkipRepo, _FakeDomainSkips(frozenset({"blocked.example"}))),
    )
    out = str(await handlers["web_fetch"]({"url": "https://blocked.example/p"}, _fresh_ctx()))
    assert calls["n"] == 0  # never hit the network
    assert "skipped" in out.lower()
    assert "web_search" in out


async def test_web_fetch_records_a_paywall_block() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PAYWALL_HTML, headers={"content-type": "text/html"})

    repo = _FakeDomainSkips()
    handlers = build_web_handlers(
        SearxngClient(""),
        WebFetcher(transport=httpx.MockTransport(handle)),
        domain_skips=cast(DomainSkipRepo, repo),
    )
    await handlers["web_fetch"]({"url": "https://wall.example/a"}, _fresh_ctx())
    assert repo.recorded == [("wall.example", "paywalled", "https://wall.example/a")]


async def test_web_fetch_does_not_record_a_404() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "text/html"})

    repo = _FakeDomainSkips()
    handlers = build_web_handlers(
        SearxngClient(""),
        WebFetcher(transport=httpx.MockTransport(handle)),
        domain_skips=cast(DomainSkipRepo, repo),
    )
    await handlers["web_fetch"]({"url": "https://gone.example/x"}, _fresh_ctx())
    assert repo.recorded == []  # a definitive 404 is the page, not the site — never recorded


async def test_domain_skip_repo_fails_open_without_a_db() -> None:
    # A maker that blows up the moment it is used: active_hosts fails open to empty, and
    # record swallows the error — neither ever raises into a fetch/search.
    repo = DomainSkipRepo(cast(Any, object()))
    assert await repo.active_hosts() == frozenset()
    await repo.record("x.example", "paywalled", "https://x.example/a")  # no exception


async def test_domain_skip_repo_ignores_an_invalid_reason() -> None:
    # An out-of-CHECK reason returns before touching the DB — a broken maker is never reached.
    repo = DomainSkipRepo(cast(Any, object()))
    await repo.record("x.example", "not_a_reason", "https://x.example/a")  # no exception


# --- Tavily Extract tier (a hosted fourth recovery leg after direct→reader→solver) ----------
# docs/plans/TAVILY_FETCH_TIER_PLAN.md. Faked via MockTransport (host api.tavily.com), like the
# reader/solver legs. The tier reads its (enabled, key) live from an injected async provider.

# Padded past _MIN_RECOVERED_CHARS (200) so a Tavily recovery is a clean win, not just the
# "richest thin result" fallback — the test asserts the content, unambiguously.
_TAVILY_CONTENT = (
    "Real article content Tavily's hosted extractor recovered past the managed wall. "
    "It rendered the page on Tavily's cloud and returned clean readable text, the same "
    "shape the reader tier hands back, so pagination and find all work over it exactly "
    "as they do for any other recovered page. This body is deliberately long enough to "
    "clear the thin-shell threshold so the tier wins outright."
)


def _tavily_provider(enabled: bool = True, key: str = "tvly-key"):  # type: ignore[no-untyped-def]
    """An async (enabled, api_key) provider, the live-settings seam WebFetcher reads per fetch."""

    async def provider() -> tuple[bool, str]:
        return enabled, key

    return provider


def _tavily_body(url: str, content: str = _TAVILY_CONTENT) -> dict:
    """A Tavily Extract `/extract` response shape: results[0].{url, raw_content}."""
    return {"results": [{"url": url, "raw_content": content}]}


def _four_tier_handler(  # type: ignore[no-untyped-def]
    *, direct: httpx.Response, reader, solver_response, tavily_response, hits=None
):
    """One MockTransport serving all four legs by host: byparr (solver), reader, api.tavily.com
    (Tavily; None ⇒ 500, else a JSON body), and every other host with `direct`. `hits`, if given,
    records each leg's host so a test can assert which tiers ran (and which were skipped)."""

    def handle(request: httpx.Request) -> httpx.Response:
        if hits is not None:
            hits.append(request.url.host)
        host = request.url.host
        if host == "byparr":
            if solver_response is None:
                return httpx.Response(500)
            return httpx.Response(
                200, json={"solution": {"url": str(request.url), **solver_response}}
            )
        if host == "reader":
            if reader is None:
                return httpx.Response(500)
            return httpx.Response(200, content=reader, headers={"content-type": "text/markdown"})
        if host == "api.tavily.com":
            if tavily_response is None:
                return httpx.Response(500)
            return httpx.Response(200, json=tavily_response)
        return direct

    return handle


def _tavily_fetcher(  # type: ignore[no-untyped-def]
    handler,
    *,
    enabled: bool = True,
    key: str = "tvly-key",
    tavily_first_hosts=None,
    record_solver_failed=None,
) -> WebFetcher:
    return WebFetcher(
        transport=httpx.MockTransport(handler),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
        tavily_url="https://api.tavily.com",
        tavily_extract_depth="advanced",
        tavily_settings=_tavily_provider(enabled, key),
        tavily_first_hosts=tavily_first_hosts,
        record_solver_failed=record_solver_failed,
    )


class _RecordedMisses:
    """A fake `record_solver_failed` callback that captures the URLs it was asked to learn."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def __call__(self, url: str) -> None:
        self.urls.append(url)


def _first_hosts(*hosts: str):  # type: ignore[no-untyped-def]
    """A fake `tavily_first_hosts` lookup returning a fixed learned set."""

    async def provider() -> frozenset[str]:
        return frozenset(hosts)

    return provider


# A byparr solved page that is ITSELF a challenge — the stealth browser ran but couldn't clear the
# wall (a genuine MISS, the signal the domain is worth routing Tavily-first).
_SOLVER_CHALLENGE = {"response": "<html><head><title>Just a moment...</title></head></html>"}


async def test_records_solver_failed_when_byparr_misses_and_tavily_recovers() -> None:
    # Direct 403 → reader challenge (miss) → byparr ran but is still challenged (a GENUINE miss) →
    # Tavily recovers. The domain is learned so the next fetch routes it Tavily-first.
    recorder = _RecordedMisses()
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=_SOLVER_CHALLENGE,
            tavily_response=_tavily_body("https://x.example/walled"),
        ),
        record_solver_failed=recorder,
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "Tavily's hosted extractor recovered" in result.text
    assert recorder.urls == ["https://x.example/walled"]  # learned for next time


async def test_no_record_on_a_byparr_outage_even_if_tavily_recovers() -> None:
    # byparr 500s (an OUTAGE, not the domain's fault): even though Tavily saves the fetch, the
    # domain is NOT learned — a transient byparr blip must never route a site Tavily-first.
    recorder = _RecordedMisses()
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=None,  # byparr 500 → OUTAGE
            tavily_response=_tavily_body("https://x.example/walled"),
        ),
        record_solver_failed=recorder,
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "Tavily's hosted extractor recovered" in result.text
    assert recorder.urls == []  # an outage is never learned


async def test_no_record_when_tavily_also_misses() -> None:
    # byparr genuinely misses AND Tavily also can't read it: nothing to route Tavily-first, so the
    # domain is not learned — it surfaces as an honest block (the existing skip path handles it).
    recorder = _RecordedMisses()
    challenge = "Please enable JavaScript and cookies to continue."
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=_SOLVER_CHALLENGE,
            tavily_response=_tavily_body("https://x.example/walled", content=challenge),
        ),
        record_solver_failed=recorder,
    )
    with pytest.raises(WebFetchError):
        await fetcher.fetch("https://x.example/walled")
    assert recorder.urls == []


async def test_tavily_first_routes_a_learned_host_straight_to_tavily() -> None:
    # A learned host skips the doomed direct→reader→byparr legs and goes straight to Tavily —
    # the payoff of the recording above. Neither the origin, reader, nor byparr is touched.
    hits: list[str] = []
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=_SOLVER_CHALLENGE,
            tavily_response=_tavily_body("https://x.example/walled"),
            hits=hits,
        ),
        tavily_first_hosts=_first_hosts("x.example"),
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "Tavily's hosted extractor recovered" in result.text
    assert hits == ["api.tavily.com"]  # went straight to Tavily


async def test_tavily_first_miss_falls_through_to_the_normal_path() -> None:
    # A learned host, but Tavily misses this time: it falls through to the ordinary path (here the
    # origin serves a normal 200), so a stale learned entry degrades gracefully, never hard-fails.
    hits: list[str] = []
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(200, content=_ORIGIN_HTML, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=_SOLVER_CHALLENGE,
            tavily_response=None,  # Tavily 500s on the tavily-first attempt
            hits=hits,
        ),
        tavily_first_hosts=_first_hosts("x.example"),
    )
    result = await fetcher.fetch("https://x.example/p")
    assert "Direct origin content" in result.text
    assert hits[0] == "api.tavily.com" and "x.example" in hits  # tried Tavily first, then fell back


async def test_tavily_first_is_a_noop_when_the_tier_is_disabled() -> None:
    # A learned host but the toggle is OFF: routing is inert (Tavily is never called), and the
    # normal path runs unchanged — so disabling the tier fully removes it, learned list and all.
    hits: list[str] = []
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(200, content=_ORIGIN_HTML, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=_SOLVER_CHALLENGE,
            tavily_response=_tavily_body("https://x.example/walled"),
            hits=hits,
        ),
        enabled=False,
        tavily_first_hosts=_first_hosts("x.example"),
    )
    result = await fetcher.fetch("https://x.example/p")
    assert "Direct origin content" in result.text
    assert "api.tavily.com" not in hits  # disabled ⇒ never called, even for a learned host


async def test_a_thin_tavily_result_is_returned_but_not_learned() -> None:
    # byparr genuinely misses and Tavily extracts only a THIN sub-threshold shell (a banner, not
    # the article): it is returned as the last-resort fallback, but the domain is NOT learned —
    # a thin shell isn't a strong enough signal to permanently reroute past the on-box legs.
    recorder = _RecordedMisses()
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,  # reader miss
            solver_response=_SOLVER_CHALLENGE,  # byparr genuine miss
            tavily_response=_tavily_body(
                "https://x.example/walled", content="A short cookie banner."
            ),
        ),
        record_solver_failed=recorder,
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "short cookie banner" in result.text  # returned as the richest thin fallback
    assert recorder.urls == []  # but NOT recorded — Tavily didn't clear the bar


async def test_a_thin_tavily_first_result_escalates_instead_of_short_circuiting() -> None:
    # A learned host, but this time Tavily's tavily-first attempt returns only a THIN shell: it must
    # NOT short-circuit the on-box legs — escalation runs and the richer origin content wins, while
    # the thin Tavily shell is carried as a fallback (never lost, never re-fetched).
    hits: list[str] = []
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(200, content=_ORIGIN_HTML, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=_SOLVER_CHALLENGE,
            tavily_response=_tavily_body("https://x.example/p", content="A thin banner."),
            hits=hits,
        ),
        tavily_first_hosts=_first_hosts("x.example"),
    )
    result = await fetcher.fetch("https://x.example/p")
    assert "Direct origin content" in result.text  # escalated past the thin Tavily-first shell
    assert hits.count("api.tavily.com") == 1  # tried once up front, never re-fetched


async def test_solver_first_genuine_miss_then_tavily_win_is_learned() -> None:
    # A solver-FIRST domain (byparr runs up front) that genuinely misses, then Tavily recovers on
    # the fall-through: the genuine-miss signal propagates via solver_already_missed so the domain
    # is still learned. Exercises the solver-first → record path (no tavily-first here).
    recorder = _RecordedMisses()
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _four_tier_handler(
                direct=httpx.Response(403, headers={"content-type": "text/html"}),
                reader=_CHALLENGE_MD,  # reader miss on the fall-through
                solver_response=_SOLVER_CHALLENGE,  # byparr runs first, genuinely misses
                tavily_response=_tavily_body("https://x.example/walled"),  # Tavily wins
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
        solver_first_domains=("x.example",),
        tavily_url="https://api.tavily.com",
        tavily_settings=_tavily_provider(True, "tvly-key"),
        record_solver_failed=recorder,
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "Tavily's hosted extractor recovered" in result.text
    assert recorder.urls == ["https://x.example/walled"]  # solver-first miss + Tavily win → learned


async def test_tavily_recovers_when_reader_and_solver_both_miss() -> None:
    # Direct 403 → reader renders the challenge (miss) → byparr 500s (miss) → Tavily's hosted
    # extractor clears the wall and returns the article. The escalation this tier is for.
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=None,
            tavily_response=_tavily_body("https://x.example/walled"),
        )
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "Tavily's hosted extractor recovered" in result.text
    assert result.url == "https://x.example/walled"  # the public/final url, not Tavily's endpoint


# A solver page rich enough (>_MIN_RECOVERED_CHARS) to WIN outright, so escalation stops at the
# solver and the Tavily tier after it is never reached — proving Tavily runs strictly last.
_RICH_SOLVED_HTML = (
    "<html><head><title>CFO Race</title></head><body><h1>Blaise Ingoglia</h1><p>"
    + "The stealth browser recovered the full article body past the managed wall. " * 5
    + "</p></body></html>"
)


async def test_tavily_runs_last_after_reader_and_solver() -> None:
    # When byparr recovers richly, Tavily is never reached — the free on-box tiers run first.
    hits: list[str] = []
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_THIN_READER_MD,  # thin: held, not a win, so escalation continues to the solver
            solver_response={"response": _RICH_SOLVED_HTML},  # solver wins before Tavily is tried
            tavily_response=_tavily_body("https://x.example/walled"),
            hits=hits,
        )
    )
    result = await fetcher.fetch("https://x.example/walled")
    assert "recovered the full article body" in result.text  # the solver's page, not Tavily's
    assert "api.tavily.com" not in hits  # the paid tier was never reached


async def test_tavily_disabled_is_skipped() -> None:
    # The toggle is OFF: the tier is a no-op (never hits Tavily), so the original block surfaces.
    hits: list[str] = []
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=None,
            tavily_response=_tavily_body("https://x.example/walled"),
            hits=hits,
        ),
        enabled=False,
    )
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 403
    assert "api.tavily.com" not in hits  # disabled ⇒ the endpoint is never called


async def test_tavily_without_a_key_is_skipped() -> None:
    # Enabled but keyless (no stored key, no env fallback): inert, never calls Tavily.
    hits: list[str] = []
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=None,
            tavily_response=_tavily_body("https://x.example/walled"),
            hits=hits,
        ),
        key="",
    )
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 403
    assert "api.tavily.com" not in hits


async def test_tavily_error_surfaces_the_block() -> None:
    # Tavily itself errors (500): it returns None like any tier miss, so the block surfaces.
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=None,
            tavily_response=None,  # Tavily 500s
        )
    )
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 403


async def test_tavily_that_returns_a_challenge_is_a_miss() -> None:
    # Tavily can extract the origin's challenge wall as clean text (the wall, not the article):
    # the challenge guard treats that as a miss so it never becomes a cited source.
    challenge = "Please enable JavaScript and cookies to continue to the site you requested."
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=None,
            tavily_response=_tavily_body("https://x.example/walled", content=challenge),
        )
    )
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 403


async def test_tavily_that_returns_a_paywall_is_a_miss() -> None:
    # A short subscribe-wall Tavily handed back is a block too, not readable content.
    wall = "Subscribe to continue reading. Already a subscriber? Sign in."
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(403, headers={"content-type": "text/html"}),
            reader=_CHALLENGE_MD,
            solver_response=None,
            tavily_response=_tavily_body("https://x.example/walled", content=wall),
        )
    )
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 403


async def test_tavily_tier_is_off_when_unwired() -> None:
    # No tavily_url / no provider ⇒ the tier is a no-op and the three-tier behaviour is unchanged:
    # tavily_wired is False and _fetch_via_tavily returns None even with a walled page available.
    fetcher = WebFetcher(
        transport=httpx.MockTransport(
            _four_tier_handler(
                direct=httpx.Response(403, headers={"content-type": "text/html"}),
                reader=_CHALLENGE_MD,
                solver_response=None,
                tavily_response=_tavily_body("https://x.example/walled"),
            )
        ),
        reader_url="http://reader:3000",
        solver_url="http://byparr:8191",
    )  # tavily_url unset
    assert fetcher.tavily_wired is False
    with pytest.raises(WebFetchError) as excinfo:
        await fetcher.fetch("https://x.example/walled")
    assert excinfo.value.status == 403


async def test_tavily_isolation_method_forces_only_the_tavily_tier() -> None:
    # The debug/`Test key` entry point: `tavily()` skips direct/reader/solver and hits only Tavily.
    hits: list[str] = []
    fetcher = _tavily_fetcher(
        _four_tier_handler(
            direct=httpx.Response(200, content=_ORIGIN_HTML, headers={"content-type": "text/html"}),
            reader=_READER_MD,
            solver_response={"response": _SOLVED_HTML},
            tavily_response=_tavily_body("https://x.example/walled"),
            hits=hits,
        )
    )
    result = await fetcher.tavily("https://x.example/walled")
    assert result is not None and "Tavily's hosted extractor recovered" in result.text
    assert hits == ["api.tavily.com"]  # neither the origin, the reader, nor byparr was touched


async def test_tavily_target_is_ssrf_guarded() -> None:
    # A private/link-local target is refused by the shared SSRF guard BEFORE any Tavily call —
    # so the tier can't be pointed at the box's own services (no transport ⇒ the real guard runs).
    fetcher = WebFetcher(
        tavily_url="https://api.tavily.com", tavily_settings=_tavily_provider(True, "tvly-key")
    )
    with pytest.raises(WebFetchError):
        await fetcher.tavily("http://169.254.169.254/latest/meta-data")


async def test_tavily_authenticates_with_a_bearer_header_not_a_body_key() -> None:
    # Tavily's API rejects a body `api_key` with 401 — the key rides the Authorization: Bearer
    # header, and `urls` is a list. This test locks that request shape in (it caused the live 401).
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=_tavily_body("https://x.example/walled"))
        return httpx.Response(403, headers={"content-type": "text/html"})

    fetcher = WebFetcher(
        transport=httpx.MockTransport(handle),
        tavily_url="https://api.tavily.com",
        tavily_settings=_tavily_provider(True, "tvly-key"),
    )
    result = await fetcher.tavily("https://x.example/walled")
    assert result is not None and "Tavily's hosted extractor recovered" in result.text
    assert captured["auth"] == "Bearer tvly-key"  # header auth, never a body api_key
    body = cast(dict, captured["body"])
    assert "api_key" not in body  # the key must NOT be in the body (the 401 cause)
    assert body["urls"] == ["https://x.example/walled"]  # a list, per the Tavily docs


def _tavily_only_handler(status: int, *, content: str = _TAVILY_CONTENT):  # type: ignore[no-untyped-def]
    """A transport that answers ONLY api.tavily.com — with `status`, and (on 200) a result whose
    raw_content is `content`. For exercising `tavily_probe`'s per-failure-mode messages."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            if status == 200:
                return httpx.Response(
                    200, json=_tavily_body("https://x.example/p", content=content)
                )
            return httpx.Response(status, json={"detail": {"error": "nope"}})
        return httpx.Response(200)

    return handle


def _probe_fetcher(handler, *, enabled: bool = True, key: str = "tvly-key") -> WebFetcher:  # type: ignore[no-untyped-def]
    return WebFetcher(
        transport=httpx.MockTransport(handler),
        tavily_url="https://api.tavily.com",
        tavily_settings=_tavily_provider(enabled, key),
    )


async def test_tavily_probe_reports_success() -> None:
    ok, detail = await _probe_fetcher(_tavily_only_handler(200)).tavily_probe("https://x.example/p")
    assert ok is True and "key works" in detail


async def test_tavily_probe_reports_a_key_rejection_distinctly() -> None:
    # The whole point: a 401/403 reads as a KEY REJECTION, not a vague "no page came back".
    for status in (401, 403):
        ok, detail = await _probe_fetcher(_tavily_only_handler(status)).tavily_probe(
            "https://x.example/p"
        )
        assert ok is False
        assert f"rejected the key (HTTP {status})" in detail and "tvly-" in detail


async def test_tavily_probe_reports_a_rate_limit() -> None:
    ok, detail = await _probe_fetcher(_tavily_only_handler(429)).tavily_probe("https://x.example/p")
    assert ok is False and "429" in detail and "rate-limit" in detail


async def test_tavily_probe_reports_disabled_and_keyless_distinctly() -> None:
    off, off_detail = await _probe_fetcher(_tavily_only_handler(200), enabled=False).tavily_probe(
        "https://x.example/p"
    )
    assert off is False and "off" in off_detail

    nokey, nokey_detail = await _probe_fetcher(_tavily_only_handler(200), key="").tavily_probe(
        "https://x.example/p"
    )
    assert nokey is False and "No API key" in nokey_detail


async def test_tavily_probe_reports_an_empty_result() -> None:
    ok, detail = await _probe_fetcher(_tavily_only_handler(200, content="   ")).tavily_probe(
        "https://x.example/p"
    )
    assert ok is False and "no content" in detail


async def test_tavily_probe_reports_unwired() -> None:
    ok, detail = await WebFetcher().tavily_probe("https://x.example/p")  # no tavily_url/provider
    assert ok is False and "isn't set up" in detail


def _counting_tavily_handler(counter: dict, *, content: str = _TAVILY_CONTENT):  # type: ignore[no-untyped-def]
    """A Tavily transport that counts every /extract call, so a test can prove the cache collapses
    repeat fetches of the same URL to ONE paid call."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.tavily.com":
            counter["n"] += 1
            return httpx.Response(200, json=_tavily_body("https://x.example/p", content=content))
        return httpx.Response(403, headers={"content-type": "text/html"})

    return handle


async def test_tavily_caches_a_successful_extraction_within_the_ttl() -> None:
    # A research fan re-opens the same walled URL across rounds; the cache makes that ONE paid call.
    calls = {"n": 0}
    fetcher = WebFetcher(
        transport=httpx.MockTransport(_counting_tavily_handler(calls)),
        tavily_url="https://api.tavily.com",
        tavily_settings=_tavily_provider(True, "tvly-key"),
    )
    r1 = await fetcher.tavily("https://x.example/p")
    r2 = await fetcher.tavily("https://x.example/p")
    assert r1 is not None and r2 is not None
    assert "Tavily's hosted extractor recovered" in r2.text  # the cached text, re-windowed
    assert calls["n"] == 1  # second fetch served from cache — one paid call, not two


async def test_tavily_cache_can_be_disabled() -> None:
    calls = {"n": 0}
    fetcher = WebFetcher(
        transport=httpx.MockTransport(_counting_tavily_handler(calls)),
        tavily_url="https://api.tavily.com",
        tavily_settings=_tavily_provider(True, "tvly-key"),
        tavily_cache_ttl_s=0,  # disabled
    )
    await fetcher.tavily("https://x.example/p")
    await fetcher.tavily("https://x.example/p")
    assert calls["n"] == 2  # no cache → both fetches call Tavily


async def test_tavily_does_not_cache_a_blocked_result() -> None:
    # A challenge/paywall Tavily result is a miss, and must NOT be cached — the block may be
    # transient, so a later fetch retries rather than serving a cached "None" for the whole window.
    calls = {"n": 0}
    challenge = "Just a moment... enable javascript and cookies to continue."
    fetcher = WebFetcher(
        transport=httpx.MockTransport(_counting_tavily_handler(calls, content=challenge)),
        tavily_url="https://api.tavily.com",
        tavily_settings=_tavily_provider(True, "tvly-key"),
    )
    assert await fetcher.tavily("https://x.example/p") is None
    assert await fetcher.tavily("https://x.example/p") is None
    assert calls["n"] == 2  # a block is never cached — the second fetch retries


async def test_tavily_cache_expires_after_the_ttl() -> None:
    # Deterministic expiry via an injected clock (the SearXNG-cache pattern): a hit within the TTL,
    # a re-fetch past it.
    calls = {"n": 0}
    now = {"t": 0.0}
    fetcher = WebFetcher(
        transport=httpx.MockTransport(_counting_tavily_handler(calls)),
        tavily_url="https://api.tavily.com",
        tavily_settings=_tavily_provider(True, "tvly-key"),
        tavily_cache_ttl_s=100.0,
        clock=lambda: now["t"],
    )
    await fetcher.tavily("https://x.example/p")  # call 1 → cached
    now["t"] = 50.0
    await fetcher.tavily("https://x.example/p")  # within TTL → cache hit
    assert calls["n"] == 1
    now["t"] = 150.0
    await fetcher.tavily("https://x.example/p")  # past TTL → re-fetched
    assert calls["n"] == 2
