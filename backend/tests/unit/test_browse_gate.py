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
    return build_browse_handlers(BrowseAgent(router, mcp, loop="b1"))["browse"], browser


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
    for start in ("https://epictheatres.com/titusville", "https://WWW.EpicTheatres.com/"):
        ctx = _turn()  # one browse per site per turn: each start gets a turn of its own
        fetched = await fetch({"url": f"https://{host}/"}, ctx)
        assert ctx.browser_needed == {"epictheatres.com": reason}
        assert "Call `browse`" in fetched  # the hint and the gate agree
        out = await browse({"goal": "g", "start_url": start}, ctx)
        assert isinstance(out, ToolOutput) and out.startswith("[BROWSE RESULT")
    assert len(fake.converse_calls) == 2 and browser.calls


async def test_a_second_browse_of_the_same_site_in_a_turn_is_refused() -> None:
    """Seen live: a timeout, then a second full browse of the same site. Once a run on a
    site came back this turn — whatever its outcome — another is refused before anything
    runs; another site, or the next turn, is not affected."""
    fetch = _fetch_handler({"www.epictheatres.com": _GATE, "www.amctheatres.com": _GATE})
    fake = _give_up()
    browse, browser = _browse(fake)
    ctx = _turn()
    await fetch({"url": "https://www.epictheatres.com/"}, ctx)
    await fetch({"url": "https://www.amctheatres.com/"}, ctx)
    first = await browse({"goal": "g", "start_url": "https://www.epictheatres.com/"}, ctx)
    assert isinstance(first, ToolOutput) and first.startswith("[BROWSE RESULT")
    assert ctx.browsed == {"epictheatres.com": "gave_up"}
    calls, sessions = len(fake.converse_calls), browser.methods.count("initialize")
    again = await browse({"goal": "other", "start_url": "https://epictheatres.com/titusville"}, ctx)
    assert isinstance(again, ToolOutput) and again.result_brief == "refused · already browsed"
    assert "epictheatres.com was already browsed this turn (it came back gave up)" in again
    assert len(fake.converse_calls) == calls and browser.methods.count("initialize") == sessions
    other = await browse({"goal": "g", "start_url": "https://www.amctheatres.com/"}, ctx)
    assert isinstance(other, ToolOutput) and other.startswith("[BROWSE RESULT")
    nxt = _turn()
    await fetch({"url": "https://www.epictheatres.com/"}, nxt)
    out = await browse({"goal": "g", "start_url": "https://www.epictheatres.com/"}, nxt)
    assert isinstance(out, ToolOutput) and out.startswith("[BROWSE RESULT")


def test_a_browse_is_recorded_under_the_sites_it_started_and_ended_on() -> None:
    browsed: dict[str, str] = {}
    browse_gate.record_browse(
        browsed, "timeout", "https://www.epictheatres.com/", "", "http://10.0.0.1/"
    )
    browse_gate.record_browse(browsed, "answered", "https://a.example/", "https://www.b.example/x")
    assert browsed == {
        "epictheatres.com": "timeout",
        "a.example": "answered",
        "b.example": "answered",
    }
    assert browse_gate.repeat_refusal(None, browsed) is None
    assert browse_gate.repeat_refusal("https://c.example/", browsed) is None
    assert browse_gate.repeat_refusal("https://b.example/", browsed) is not None


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
        # ...but never down to a dotless name.
        ("https://www.example/", "www.example"),
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


def test_a_redirect_opens_only_the_site_it_ended_on() -> None:
    """The page that needed a browser is the one the fetch landed on; the site that merely
    redirected there is not opened."""
    seen: dict[str, str] = {}
    result = FetchResult(url="https://www.epictheatres.com/home", title="", text="", gated=True)
    browse_gate.record_fetch(seen, result, "https://epic.example/", offset=0, find="")
    assert seen == {"epictheatres.com": "gated"}
    # A result that names no final URL keys on the requested one.
    seen.clear()
    unnamed = FetchResult(url="", title="", text="", gated=True)
    browse_gate.record_fetch(seen, unnamed, "https://epic.example/", offset=0, find="")
    assert seen == {"epic.example": "gated"}
    # An address with no site to key on records nothing.
    seen.clear()
    browse_gate.record_fetch(seen, unnamed, "http://10.0.0.1/", offset=0, find="")
    browse_gate.record_blocked(seen, "http://10.0.0.1/")
    assert seen == {}


class _Skips:
    """A stand-in 24h skip list holding one host."""

    def __init__(self, host: str) -> None:
        self.host = host

    async def active_hosts(self) -> frozenset[str]:
        return frozenset({self.host})

    async def record(self, host: str, reason: str, url: str) -> None:
        return None


def _status_fetch(status: int, domain_skips: object = None) -> ToolHandler:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"<html><body>no</body></html>")

    fetcher = WebFetcher(transport=httpx.MockTransport(handle))
    return build_web_handlers(
        SearxngClient(""),
        fetcher,
        domain_skips=domain_skips,  # type: ignore[arg-type]
    )["web_fetch"]


@pytest.mark.parametrize("status", [403, 429, 402])
async def test_a_hard_block_opens_browse_for_that_site(status: int) -> None:
    """A bot wall, a challenge or a paywall is exactly where a real browser can help."""
    fetch = _status_fetch(status)
    fake = _give_up()
    browse, _ = _browse(fake)
    ctx = _turn()
    await fetch({"url": "https://www.epictheatres.com/"}, ctx)
    assert ctx.browser_needed == {"epictheatres.com": browse_gate.BLOCKED}
    out = await browse({"goal": "g", "start_url": "https://epictheatres.com/"}, ctx)
    assert isinstance(out, ToolOutput) and out.startswith("[BROWSE RESULT")


@pytest.mark.parametrize("status", [404, 500])
async def test_a_missing_page_or_a_glitch_does_not(status: int) -> None:
    ctx = _turn()
    await _status_fetch(status)({"url": "https://www.epictheatres.com/x"}, ctx)
    assert ctx.browser_needed == {}


async def test_a_skip_listed_site_opens_browse_without_a_fetch() -> None:
    fetch = _status_fetch(200, _Skips("www.epictheatres.com"))
    ctx = _turn()
    out = await fetch({"url": "https://www.epictheatres.com/"}, ctx)
    assert "skipped for the next day" in out
    assert ctx.browser_needed == {"epictheatres.com": browse_gate.BLOCKED}


def test_an_address_with_no_site_never_passes() -> None:
    assert browse_gate.refusal("http://10.0.0.1/", {"10.0.0.1": "gated"}) is not None
    assert browse_gate.refusal("https://epictheatres.com/", {"epictheatres.com": "thin"}) is None
