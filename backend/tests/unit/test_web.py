"""The jerv chatbot's on-box web client + tools (docs/ASSISTANT.md "Agent
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
    fired: list[str] = []
    handlers = build_web_handlers(
        _searx(lambda r: httpx.Response(200, json=_SEARX_OK)), WebFetcher(), emit=fired.append
    )
    await handlers["web_search"]({"query": "python"}, CTX)
    assert fired == ["web_search"]


async def test_web_fetch_emits_a_tendril_event() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    fired: list[str] = []
    handlers = build_web_handlers(
        SearxngClient(""), WebFetcher(transport=httpx.MockTransport(handle)), emit=fired.append
    )
    await handlers["web_fetch"]({"url": "https://x.example/p"}, CTX)
    assert fired == ["web_fetch"]


async def test_invalid_web_calls_do_not_emit() -> None:
    # An empty query / missing url never reaches out, so it fires no tendril.
    fired: list[str] = []
    handlers = build_web_handlers(SearxngClient(""), WebFetcher(), emit=fired.append)
    await handlers["web_search"]({"query": "  "}, CTX)
    await handlers["web_fetch"]({}, CTX)
    assert fired == []
