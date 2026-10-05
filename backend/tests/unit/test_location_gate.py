"""web_fetch's location/store-gate verdict and its hand-off to `browse` (BROWSER_AGENT_PLAN B1).

The case this exists for: Epic Theatres answers every visitor without a chosen theater with
a ~221-character "Please select a location" template. It cleared the 200-character recovery
bar, so web_fetch reported a successful read of a page with nothing on it."""

from __future__ import annotations

import httpx

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.webtools import build_web_handlers
from jbrain.db.session import SessionContext
from jbrain.web.fetch import LOCATION_GATE_NOTE, WebFetcher, looks_like_location_gate
from jbrain.web.search import SearxngClient

_EPIC = (
    b"<html><head><title>Home \xe2\x80\x94 EPIC Theatres</title></head><body>"
    b"<nav>Showtimes Movies Gift Cards Loyalty Events More Merch Store</nav>"
    b"<p>Your theater:</p><p>Please select a location</p>"
    b"<footer>Skip to main content</footer></body></html>"
)
_ARTICLE = (
    b"<html><head><title>News</title></head><body><header>Find your store</header><article>"
    + b"<p>A long article about something else entirely, with real content in it.</p>" * 40
    + b"</article></body></html>"
)


def _fetcher(body: bytes) -> WebFetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    return WebFetcher(transport=httpx.MockTransport(handle))


def test_the_gate_is_told_apart_by_its_wording_not_its_length() -> None:
    assert looks_like_location_gate("Home", "Your theater: Please select a location")
    assert looks_like_location_gate("Choose your store", "")
    assert not looks_like_location_gate("Short notice", "We are closed on Monday.")
    # A real page that merely mentions a picker in its chrome is long enough to be content.
    assert not looks_like_location_gate("News", "Find your store. " + "real text " * 300)


async def test_the_epic_template_comes_back_flagged() -> None:
    result = await _fetcher(_EPIC).fetch("https://www.epictheatres.com/")
    assert result.gated
    # Before, this was simply a successful direct read of a page with nothing on it.
    assert result.tier == "direct" and "select a location" in result.text


async def test_a_long_page_is_not_flagged_and_neither_is_a_later_window() -> None:
    assert not (await _fetcher(_ARTICLE).fetch("https://news.example/a")).gated
    epic = _fetcher(_EPIC)
    assert not (await epic.fetch("https://www.epictheatres.com/", find="location")).gated
    assert not (await epic.fetch("https://www.epictheatres.com/", offset=10)).gated


def _ctx(tools: frozenset[str]) -> ToolContext:
    return ToolContext(session=SessionContext(principal_kind="owner"), scopes=(), agent_tools=tools)


async def test_web_fetch_says_it_is_a_gate_and_suggests_browse_to_a_caller_holding_it() -> None:
    fetch = build_web_handlers(SearxngClient(""), _fetcher(_EPIC))["web_fetch"]
    url = "https://www.epictheatres.com/"

    with_browse = await fetch({"url": url}, _ctx(frozenset({"web_fetch", "browse"})))
    assert LOCATION_GATE_NOTE in with_browse
    assert "Call `browse`" in with_browse and f"start_url={url}" in with_browse
    assert isinstance(with_browse, ToolOutput) and with_browse.web_sources  # still citable

    # A research child reading the same page has no browser: it gets the diagnosis only.
    without = await fetch({"url": url}, _ctx(frozenset({"web_fetch"})))
    assert LOCATION_GATE_NOTE in without and "`browse`" not in without


async def test_an_ordinary_page_gets_no_hint() -> None:
    fetch = build_web_handlers(SearxngClient(""), _fetcher(_ARTICLE))["web_fetch"]
    out = await fetch({"url": "https://news.example/a"}, _ctx(frozenset({"browse"})))
    assert "`browse`" not in out and LOCATION_GATE_NOTE not in out


async def test_an_unrendered_js_app_also_suggests_browse() -> None:
    shell = b'<html><head><title>App</title></head><body><div id="root"></div></body></html>'
    fetch = build_web_handlers(SearxngClient(""), _fetcher(shell))["web_fetch"]
    out = await fetch({"url": "https://spa.example/"}, _ctx(frozenset({"browse"})))
    assert "JavaScript app" in out and "Call `browse`" in out
