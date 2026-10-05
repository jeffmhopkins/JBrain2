"""The `browse` tool's fetch-first gate (agent/browse_gate.py, owner decision 2026-10-05).

Driven end to end through the REAL web_fetch and browse handlers sharing one ToolContext, as
a turn does: the gate opens only on what web_fetch itself recorded — never on the model's
word — and a refused call never reaches the browser. Held at 100%: it is the line between
"one fetch" and "a minute of the box's model and a Chromium context"."""

from __future__ import annotations

import httpx
import pytest

from jbrain.agent import browse_gate
from jbrain.agent.browse import BrowseAgent
from jbrain.agent.browsetools import build_browse_handlers
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.webtools import build_web_handlers
from jbrain.db.session import SessionContext
from jbrain.llm import FakeLlmClient, LlmRouter, LlmTurn, LlmUsage, ToolCall
from jbrain.web.fetch import THIN_PAGE_CHARS, FetchResult, WebFetcher
from jbrain.web.mcp_client import McpHttpClient
from jbrain.web.search import SearxngClient
from tests.unit.browse_fakes import FakeBrowser

_GATE = (
    b"<html><head><title>Home - EPIC Theatres</title></head><body>"
    b"<p>Your theater:</p><p>Please select a location</p></body></html>"
)
_SHELL = b'<html><head><title>App</title></head><body><div id="root"></div></body></html>'
_THIN = b"<html><head><title>Hi</title></head><body><p>Welcome.</p></body></html>"
_ARTICLE = (
    b"<html><head><title>News</title></head><body><article>"
    + b"<p>A long article with real content in it, more than enough to read.</p>" * 40
    + b"</article></body></html>"
)


def _fetch_handler(pages: dict[str, bytes]) -> ToolHandler:
    def handle(request: httpx.Request) -> httpx.Response:
        body = pages.get(request.url.host, _ARTICLE)
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    fetcher = WebFetcher(transport=httpx.MockTransport(handle))
    return build_web_handlers(SearxngClient(""), fetcher)["web_fetch"]


def _browse(fake: FakeLlmClient) -> tuple[ToolHandler, FakeBrowser]:
    router = LlmRouter({"xai": fake}, {"browse.step": ("xai", "grok-4.3")})
    browser = FakeBrowser()
    mcp = McpHttpClient("http://browser:8931/mcp", transport=browser.transport())
    return build_browse_handlers(BrowseAgent(router, mcp))["browse"], browser


def _turn() -> ToolContext:
    """One turn's context: the loop builds a fresh one per turn."""
    return ToolContext(
        session=SessionContext(principal_kind="owner"),
        scopes=(),
        agent_tools=frozenset({"web_fetch", "browse"}),
    )


def _give_up() -> FakeLlmClient:
    turn = LlmTurn("", [ToolCall("c1", "give_up", {"reason": "x"})], "tool_use", LlmUsage(1, 1))
    return FakeLlmClient(turns=[turn])


async def _assert_refused(out: object, fake: FakeLlmClient, browser: FakeBrowser) -> None:
    assert isinstance(out, ToolOutput) and out.result_brief == "refused · fetch first"
    assert "web_fetch" in out
    # Refused before anything ran: no model call, no browser session.
    assert fake.converse_calls == [] and browser.methods == []


# --- Through the handlers --------------------------------------------------------------


async def test_browse_without_a_prior_fetch_is_refused() -> None:
    fake = _give_up()
    browse, browser = _browse(fake)
    out = await browse({"goal": "g", "start_url": "https://www.epictheatres.com/"}, _turn())
    await _assert_refused(out, fake, browser)
    assert "web_fetch https://www.epictheatres.com/ first" in out


async def test_browse_without_a_start_url_is_refused() -> None:
    fake = _give_up()
    browse, browser = _browse(fake)
    ctx = _turn()
    ctx.browser_needed["epictheatres.com"] = "gated"
    out = await browse({"goal": "g"}, ctx)
    await _assert_refused(out, fake, browser)
    assert "needs a start_url" in out


async def test_browse_after_a_fetch_that_read_fine_is_refused() -> None:
    fetch = _fetch_handler({})
    fake = _give_up()
    browse, browser = _browse(fake)
    ctx = _turn()
    await fetch({"url": "https://news.bbc.co.uk/a"}, ctx)
    assert ctx.browser_needed == {}
    out = await browse({"goal": "g", "start_url": "https://news.bbc.co.uk/a"}, ctx)
    await _assert_refused(out, fake, browser)


async def test_a_gated_fetch_of_another_site_does_not_open_this_one() -> None:
    fetch = _fetch_handler({"www.epictheatres.com": _GATE})
    fake = _give_up()
    browse, browser = _browse(fake)
    ctx = _turn()
    await fetch({"url": "https://www.epictheatres.com/"}, ctx)
    # Another site; and a sibling under the same public suffix is another site too.
    for other in ("https://www.amctheatres.com/", "https://epictheatres.co.uk/"):
        await _assert_refused(await browse({"goal": "g", "start_url": other}, ctx), fake, browser)


@pytest.mark.parametrize(
    ("host", "body", "reason"),
    [
        ("www.epictheatres.com", _GATE, "gated"),
        ("app.epictheatres.com", _SHELL, "js_shell"),
        ("epictheatres.com", _THIN, "thin"),
    ],
)
async def test_browse_runs_after_a_fetch_said_the_site_needs_a_browser(
    host: str, body: bytes, reason: str
) -> None:
    """www. vs bare and other subdomains are one site: the fetch of one opens the others."""
    fetch = _fetch_handler({host: body})
    fake = _give_up()
    browse, browser = _browse(fake)
    ctx = _turn()
    fetched = await fetch({"url": f"https://{host}/"}, ctx)
    assert ctx.browser_needed == {"epictheatres.com": reason}
    assert "Call `browse`" in fetched  # the hint and the gate agree
    for start in ("https://epictheatres.com/titusville", "https://WWW.EpicTheatres.com/"):
        out = await browse({"goal": "g", "start_url": start}, ctx)
        assert isinstance(out, ToolOutput) and out.startswith("[BROWSE RESULT")
    assert len(fake.converse_calls) == 2 and browser.calls


async def test_a_gated_fetch_in_an_earlier_turn_does_not_count() -> None:
    fetch = _fetch_handler({"www.epictheatres.com": _GATE})
    fake = _give_up()
    browse, browser = _browse(fake)
    earlier = _turn()
    await fetch({"url": "https://www.epictheatres.com/"}, earlier)
    assert earlier.browser_needed
    now = _turn()
    assert now.browser_needed == {}  # each turn's memo is its own
    out = await browse({"goal": "g", "start_url": "https://www.epictheatres.com/"}, now)
    await _assert_refused(out, fake, browser)


async def test_a_paged_or_searched_window_is_not_thin() -> None:
    """A `find` or an offset window is short by design: it says nothing about the page."""
    fetch = _fetch_handler({"epictheatres.com": _THIN})
    ctx = _turn()
    await fetch({"url": "https://epictheatres.com/", "find": "Welcome"}, ctx)
    await fetch({"url": "https://epictheatres.com/", "offset": 3}, ctx)
    assert ctx.browser_needed == {}


# --- The pieces ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("https://www.epictheatres.com/x", "epictheatres.com"),
        ("HTTPS://WWW.EpicTheatres.COM.", "epictheatres.com"),
        ("https://shop.bbc.co.uk/a", "bbc.co.uk"),
        # A suffix the list does not know: the host itself, less www.
        ("https://www.cinema.example/", "cinema.example"),
        ("http://10.0.0.1/", None),
        ("http://127.1/", None),
        ("http://[::1]/", None),
        ("https://localhost/", None),
        ("http://[bad", None),
        ("", None),
    ],
)
def test_the_site_is_the_registrable_domain(url: str, domain: str | None) -> None:
    assert browse_gate.registrable_domain(url) == domain


def test_what_counts_as_needing_a_browser() -> None:
    rich = FetchResult(url="u", title="t", text="x", total_chars=THIN_PAGE_CHARS)
    assert browse_gate.needs_browser(rich, offset=0, find="") is None
    thin = FetchResult(url="u", title="t", text="x", total_chars=THIN_PAGE_CHARS - 1)
    assert browse_gate.needs_browser(thin, offset=0, find="") == browse_gate.THIN
    assert browse_gate.needs_browser(thin, offset=0, find="x") is None
    assert browse_gate.needs_browser(thin, offset=5, find="") is None
    gated = FetchResult(url="u", title="t", text="x", total_chars=900, gated=True)
    assert browse_gate.needs_browser(gated, offset=5, find="x") == browse_gate.GATED
    shell = FetchResult(url="u", title="t", text="", total_chars=900, js_shell=True)
    assert browse_gate.needs_browser(shell, offset=0, find="") == browse_gate.JS_SHELL


def test_a_redirect_opens_both_the_asked_and_the_final_site() -> None:
    seen: dict[str, str] = {}
    result = FetchResult(url="https://www.epictheatres.com/home", title="", text="", gated=True)
    browse_gate.record_fetch(seen, result, "https://epic.example/", offset=0, find="")
    assert seen == {"epic.example": "gated", "epictheatres.com": "gated"}
    # An address with no site to key on records nothing.
    seen.clear()
    blank = FetchResult(url="", title="", text="", gated=True)
    browse_gate.record_fetch(seen, blank, "http://10.0.0.1/", offset=0, find="")
    assert seen == {}


def test_an_address_with_no_site_never_passes() -> None:
    assert browse_gate.refusal("http://10.0.0.1/", {"10.0.0.1": "gated"}) is not None
    assert browse_gate.refusal("https://epictheatres.com/", {"epictheatres.com": "thin"}) is None
