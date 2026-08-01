"""The jerv chatbot's on-box web client + tools (docs/reference/ASSISTANT.md "Agent
selection"). HTTP is faked via MockTransport — no live network, like the
connector and LLM adapters."""

import httpx
import pytest

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.webtools import build_web_handlers
from jbrain.db.session import SessionContext
from jbrain.web.fetch import WebFetcher, WebFetchError
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

    hits = await _searx(handle).search("python", limit=5)
    assert [h.url for h in hits] == ["https://a.example/1", "https://b.example/2"]
    assert hits[0].title == "Result one" and hits[0].snippet == "first snippet"
    # The query rode as ?q=, JSON format requested, against the pinned base URL.
    assert calls[0].url.params["q"] == "python"
    assert calls[0].url.params["format"] == "json"
    assert str(calls[0].url).startswith("http://searxng:8080/search")


async def test_search_honors_limit() -> None:
    hits = await _searx(lambda r: httpx.Response(200, json=_SEARX_OK)).search("q", limit=1)
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
    assert await client.search("nothing") == []
    assert await client.search("nothing") == []
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
    assert "Web results:" in out
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
