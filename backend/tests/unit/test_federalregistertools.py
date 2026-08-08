"""jerv's `federal_register` tool + Federal Register client (docs/reference/ASSISTANT.md
"Agent selection"). HTTP is faked via MockTransport — no live network. The canned data
includes the <span class="match"> highlight HTML the API returns, so the strip is exercised."""


import httpx

from jbrain.agent.federalregistertools import build_federal_register_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.web.federal_register import FederalRegisterClient

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

_MATCH = {
    "count": 1,
    "results": [
        {
            "title": "Debarment Order for a Drug Manufacturer",
            "agencies": [{"name": "Food and Drug Administration"}],
            "html_url": "https://www.federalregister.gov/documents/2026/01/02/abc/debarment-order",
            "publication_date": "2026-01-02",
            "excerpts": 'An order <span class="match">debarring</span> the firm.',
        }
    ],
}
_EMPTY = {"count": 0, "results": []}


def _client(body: dict) -> FederalRegisterClient:
    """A Federal Register client whose transport answers with `body` and records `.seen`."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    client = FederalRegisterClient(
        "https://www.federalregister.gov", transport=httpx.MockTransport(handle)
    )
    client.seen = seen  # type: ignore[attr-defined]
    return client


# --- FederalRegisterClient -------------------------------------------------


async def test_search_parses_a_notice_and_strips_highlight_html() -> None:
    client = _client(_MATCH)
    notices, ok = await client.search("debarment")
    assert ok
    assert len(notices) == 1
    n = notices[0]
    assert n.title == "Debarment Order for a Drug Manufacturer"
    assert n.agencies == ("Food and Drug Administration",)
    assert n.publication_date == "2026-01-02"
    assert n.url.endswith("/debarment-order")
    # The <span class="match"> highlight tags are stripped from the excerpt.
    assert "debarring the firm" in n.excerpt and "<span" not in n.excerpt
    # A descriptive User-Agent (the API requests one) and the newest-first order ride the call.
    params = client.seen[0].url.params  # type: ignore[attr-defined]
    assert params.get("conditions[term]") == "debarment" and params.get("order") == "newest"
    assert "JBrain2 research" in client.seen[0].headers["user-agent"]  # type: ignore[attr-defined]


async def test_search_unconfigured_returns_empty_ok_false() -> None:
    notices, ok = await FederalRegisterClient("").search("anything")
    assert notices == [] and ok is False


async def test_search_empty_is_ok_true_and_empty() -> None:
    notices, ok = await _client(_EMPTY).search("nothing here")
    assert ok is True and notices == []


async def test_search_errors_return_empty_not_raise() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = FederalRegisterClient(
        "https://www.federalregister.gov", transport=httpx.MockTransport(boom)
    )
    notices, ok = await client.search("anything")
    assert notices == [] and ok is False  # graceful: never raises into the tool


async def test_repeat_search_is_served_from_cache() -> None:
    client = _client(_MATCH)
    await client.search("debarment")
    await client.search("debarment")
    assert len(client.seen) == 1  # type: ignore[attr-defined]  # the repeat served from cache


# --- federal_register tool --------------------------------------------------


async def test_tool_surfaces_the_notice_agency_and_excerpt() -> None:
    handlers = build_federal_register_handlers(_client(_MATCH))
    out = await handlers["federal_register"]({"query": "debarment"}, CTX)
    assert isinstance(out, ToolOutput)
    text = str(out)
    assert "Debarment Order for a Drug Manufacturer" in text
    assert "Food and Drug Administration" in text
    assert "2026-01-02" in text
    assert "debarring the firm" in text
    assert "Federal Register" in text
    assert "verify" in text.lower() or "lead" in text.lower()
    assert out.web_sources and out.web_sources[0].url.endswith("/debarment-order")


async def test_tool_no_results_is_a_clean_line() -> None:
    handlers = build_federal_register_handlers(_client(_EMPTY))
    out = str(await handlers["federal_register"]({"query": "nobody at all"}, CTX))
    assert "No Federal Register documents" in out
    assert "nobody at all" in out


async def test_tool_needs_a_query() -> None:
    handlers = build_federal_register_handlers(_client(_EMPTY))
    assert "needs a query" in await handlers["federal_register"]({"query": "  "}, CTX)


async def test_tool_reports_an_outage_distinctly_from_empty() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = FederalRegisterClient(
        "https://www.federalregister.gov", transport=httpx.MockTransport(boom)
    )
    out = str(
        await build_federal_register_handlers(client)["federal_register"]({"query": "x"}, CTX)
    )
    assert "unavailable" in out.lower()


async def test_tool_unconfigured_reports_not_configured() -> None:
    handlers = build_federal_register_handlers(FederalRegisterClient(""))
    out = str(await handlers["federal_register"]({"query": "x"}, CTX))
    assert "not configured" in out.lower()


async def test_tool_emits_a_tendril_event() -> None:
    fired: list[tuple[str, str | None]] = []
    handlers = build_federal_register_handlers(
        _client(_EMPTY), emit=lambda kind, text=None: fired.append((kind, text))
    )
    await handlers["federal_register"]({"query": "debarment"}, CTX)
    assert fired == [("web_search", "debarment")]


# federal_register is no longer a top-level tool — it is a `source` of the public_records
# umbrella (docs/plans/TOOL_CATALOG_PLAN.md); its sidecar + jerv-only/web-class registration are
# covered by test_public_records.py. This file keeps the Federal Register source's own behaviour.
