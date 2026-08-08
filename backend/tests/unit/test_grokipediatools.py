"""jerv's Grokipedia umbrella tool (docs/plans/GROKIPEDIA_TOOL_PLAN.md). ONE
`grokipedia(action=…)` tool dispatches the five operations; the client's HTTP is faked via
MockTransport — no live network."""

import httpx

from jbrain.agent.grokipediatools import build_grokipedia_handlers
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.readtools import TOOLS_DIR
from jbrain.agent.toolfile import load_tool
from jbrain.db.session import SessionContext
from jbrain.web.grokipedia import GrokipediaClient

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=())

_TYPEAHEAD = {
    "results": [
        {"slug": "Elon_Musk", "title": "Elon Musk", "snippet": "Entrepreneur.", "viewCount": 900},
        {"slug": "SpaceX", "title": "SpaceX", "snippet": "Rocket company.", "viewCount": 400},
    ]
}
# Content with inline [N] citation markers so section-scoped citations resolve, and an
# inline wiki-link so action=related / section(detailed) surface a linked article.
_CONTENT = (
    "Intro paragraph.\n\n"
    "## Early Life\n\nBorn in 1971 [1]. Educated [2]. Founded [SpaceX](/page/SpaceX).\n\n"
    "## Career\n\nBuilt companies [3].\n\n"
)
_PREVIEW = {
    "page": {
        "title": "Elon Musk",
        "content": _CONTENT,
        "citations": [
            {"id": 1, "url": "https://www.britannica.com/biography/Elon-Musk", "title": ""},
            {"id": 2, "url": "https://apnews.com/article/xyz", "title": "AP story"},
            {"id": 3, "url": "https://arstechnica.com/space", "title": ""},
        ],
        "metadata": {"lastModified": 1776869679, "categories": ["Entrepreneurs"]},
    }
}


def _grok(handler):  # type: ignore[no-untyped-def]
    """The single umbrella handler bound to a faked client."""
    client = GrokipediaClient(transport=httpx.MockTransport(handler))
    return build_grokipedia_handlers(client)["grokipedia"]


def test_grokipedia_sidecar_is_valid_and_has_the_umbrella_handler() -> None:
    # The single grokipedia.tool loads as a valid `web`-class sidecar whose name matches its
    # one handler, so the strict registry pairing (load_registry) can never ship it unwired.
    handler_names = set(build_grokipedia_handlers(GrokipediaClient()))
    assert handler_names == {"grokipedia"}
    sidecars = sorted(p.stem for p in TOOLS_DIR.glob("grokipedia*.tool"))
    assert sidecars == ["grokipedia"]  # the flat grokipedia_* sidecars are gone
    spec = load_tool(TOOLS_DIR / "grokipedia.tool").spec
    assert spec.name == "grokipedia" and spec.permission == "web"
    assert spec.params["properties"]["action"]["enum"] == [
        "search",
        "outline",
        "section",
        "citations",
        "related",
    ]


def _ok_handler():  # type: ignore[no-untyped-def]
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/typeahead":
            return httpx.Response(200, json=_TYPEAHEAD)
        return httpx.Response(200, json=_PREVIEW)

    return handle


async def test_grokipedia_needs_a_known_action() -> None:
    grok = _grok(_ok_handler())
    assert "action=" in await grok({"action": "nope"}, CTX)  # unknown action → steer
    assert "action=" in await grok({}, CTX)  # missing action → steer


# --- action=search ----------------------------------------------------------------


async def test_search_lists_slugs_and_surfaces_page_sources() -> None:
    out = await _grok(_ok_handler())({"action": "search", "query": "elon"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "[slug: Elon_Musk]" in out and "[slug: SpaceX]" in out
    assert "900 views" in out
    assert [s.url for s in out.web_sources] == [
        "https://grokipedia.com/page/Elon_Musk",
        "https://grokipedia.com/page/SpaceX",
    ]


async def test_search_needs_a_query() -> None:
    assert "non-empty query" in await _grok(_ok_handler())({"action": "search", "query": " "}, CTX)


# --- action=outline ---------------------------------------------------------------


async def test_outline_returns_numbered_toc_without_body() -> None:
    out = await _grok(_ok_handler())({"action": "outline", "slug": "Elon_Musk"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "1 Early Life" in out and "2 Career" in out
    assert "last updated 2026-04-22" in out
    assert "Born in 1971" not in out  # the body text is NOT in the outline
    assert out.web_sources[0].url == "https://grokipedia.com/page/Elon_Musk"


# --- action=section ---------------------------------------------------------------


async def test_section_reads_one_section_by_number() -> None:
    out = await _grok(_ok_handler())(
        {"action": "section", "slug": "Elon_Musk", "section": "1"}, CTX
    )
    assert "Born in 1971" in out
    assert "Built companies" not in out  # only the requested section


async def test_section_detailed_appends_citations_and_links() -> None:
    out = await _grok(_ok_handler())(
        {
            "action": "section",
            "slug": "Elon_Musk",
            "section": "Early Life",
            "response_format": "detailed",
        },
        CTX,
    )
    assert isinstance(out, ToolOutput)
    assert "britannica.com" in out and "apnews.com" in out
    assert "arstechnica.com" not in out  # [3] belongs to Career, not this section
    assert "SpaceX" in out  # the linked article this section points at
    urls = {s.url for s in out.web_sources}
    assert "https://www.britannica.com/biography/Elon-Musk" in urls


async def test_section_unknown_section_lists_available() -> None:
    out = await _grok(_ok_handler())(
        {"action": "section", "slug": "Elon_Musk", "section": "Nonexistent"}, CTX
    )
    assert "No section" in out and "Early Life" in out  # steers back to a real section


# --- action=citations (the primary-source hand-off) -------------------------------


async def test_citations_returns_followable_primary_sources() -> None:
    out = await _grok(_ok_handler())({"action": "citations", "slug": "Elon_Musk"}, CTX)
    assert isinstance(out, ToolOutput)
    assert [s.url for s in out.web_sources] == [
        "https://www.britannica.com/biography/Elon-Musk",
        "https://apnews.com/article/xyz",
        "https://arstechnica.com/space",
    ]
    assert "britannica.com" in out


async def test_citations_scopes_to_a_section() -> None:
    out = await _grok(_ok_handler())(
        {"action": "citations", "slug": "Elon_Musk", "section": "Career"}, CTX
    )
    assert isinstance(out, ToolOutput)
    assert [s.url for s in out.web_sources] == ["https://arstechnica.com/space"]


# --- action=related ---------------------------------------------------------------


async def test_related_lists_linked_articles() -> None:
    out = await _grok(_ok_handler())({"action": "related", "slug": "Elon_Musk"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "[slug: SpaceX]" in out
    assert out.web_sources[0].url == "https://grokipedia.com/page/SpaceX"


# --- caching + errors + tendrils --------------------------------------------


async def test_drilldown_on_one_slug_costs_a_single_fetch() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_PREVIEW)

    grok = _grok(handle)
    await grok({"action": "outline", "slug": "Elon_Musk"}, CTX)
    await grok({"action": "section", "slug": "Elon_Musk", "section": "1"}, CTX)
    await grok({"action": "citations", "slug": "Elon_Musk"}, CTX)
    assert calls == ["/api/page-preview"]  # cached parse served the section + citations


async def test_errors_surface_as_recoverable_text() -> None:
    grok = _grok(lambda r: httpx.Response(503))
    out = await grok({"action": "outline", "slug": "X"}, CTX)
    assert not isinstance(out, ToolOutput)  # a plain error string, not a citable result
    assert "Grokipedia" in out


async def test_search_emits_a_tendril() -> None:
    fired: list[tuple[str, str | None]] = []
    client = GrokipediaClient(transport=httpx.MockTransport(_ok_handler()))
    grok = build_grokipedia_handlers(
        client, emit=lambda kind, text=None: fired.append((kind, text))
    )["grokipedia"]
    await grok({"action": "search", "query": "elon"}, CTX)
    assert fired == [("web_search", "elon")]
