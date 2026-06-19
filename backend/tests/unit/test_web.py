"""The jerv chatbot's on-box web client + tools (docs/ASSISTANT.md "Agent
selection"). HTTP is faked via MockTransport — no live network, like the
connector and LLM adapters."""

import httpx
import pytest

from jbrain.agent.loop import ToolContext
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

    result = await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/start")
    assert result.url == "https://x.example/final" and "Heading" in result.text


async def test_fetch_refuses_a_redirect_loop() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://x.example/again"})

    with pytest.raises(WebFetchError):
        await WebFetcher(transport=httpx.MockTransport(handle)).fetch("https://x.example/again")


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
