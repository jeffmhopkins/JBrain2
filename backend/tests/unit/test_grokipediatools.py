"""jerv's Grokipedia tool handlers (docs/plans/GROKIPEDIA_TOOL_PLAN.md). The client's
HTTP is faked via MockTransport — no live network."""

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
# inline wiki-link so grokipedia_related / grokipedia_section(detailed) surface a linked article.
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


def _handlers(handler):  # type: ignore[no-untyped-def]
    client = GrokipediaClient(transport=httpx.MockTransport(handler))
    return build_grokipedia_handlers(client)


def test_every_grokipedia_sidecar_is_valid_and_has_a_handler() -> None:
    # Each grokipedia_*.tool loads as a valid `web`-class sidecar whose name matches its
    # handler, so the strict registry pairing (load_registry) can never ship one unwired.
    handler_names = set(build_grokipedia_handlers(GrokipediaClient()))
    sidecar_names = set()
    for path in sorted(TOOLS_DIR.glob("grokipedia_*.tool")):
        spec = load_tool(path).spec
        assert spec.name == path.stem and spec.permission == "web"
        sidecar_names.add(spec.name)
    assert sidecar_names == handler_names
    assert handler_names == {
        "grokipedia_search",
        "grokipedia_outline",
        "grokipedia_section",
        "grokipedia_citations",
        "grokipedia_related",
    }


def _ok_handler():  # type: ignore[no-untyped-def]
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/typeahead":
            return httpx.Response(200, json=_TYPEAHEAD)
        return httpx.Response(200, json=_PREVIEW)

    return handle


# --- grokipedia_search ------------------------------------------------------------


async def test_grokipedia_search_lists_slugs_and_surfaces_page_sources() -> None:
    out = await _handlers(_ok_handler())["grokipedia_search"]({"query": "elon"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "[slug: Elon_Musk]" in out and "[slug: SpaceX]" in out
    assert "900 views" in out
    # Each hit is a citation chip pointing at its Grokipedia page.
    assert [s.url for s in out.web_sources] == [
        "https://grokipedia.com/page/Elon_Musk",
        "https://grokipedia.com/page/SpaceX",
    ]


async def test_grokipedia_search_needs_a_query() -> None:
    assert "non-empty query" in await _handlers(_ok_handler())["grokipedia_search"](
        {"query": " "}, CTX
    )


# --- grokipedia_outline -----------------------------------------------------------


async def test_grokipedia_outline_returns_numbered_toc_without_body() -> None:
    out = await _handlers(_ok_handler())["grokipedia_outline"]({"slug": "Elon_Musk"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "1 Early Life" in out and "2 Career" in out
    assert "last updated 2026-04-22" in out
    assert "Born in 1971" not in out  # the body text is NOT in the outline
    assert out.web_sources[0].url == "https://grokipedia.com/page/Elon_Musk"


# --- grokipedia_section -----------------------------------------------------------


async def test_grokipedia_section_reads_one_section_by_number() -> None:
    out = await _handlers(_ok_handler())["grokipedia_section"](
        {"slug": "Elon_Musk", "section": "1"}, CTX
    )
    assert "Born in 1971" in out
    assert "Built companies" not in out  # only the requested section


async def test_grokipedia_section_detailed_appends_citations_and_links() -> None:
    out = await _handlers(_ok_handler())["grokipedia_section"](
        {"slug": "Elon_Musk", "section": "Early Life", "response_format": "detailed"}, CTX
    )
    assert isinstance(out, ToolOutput)
    # The section cites [1] and [2] → those sources are appended and surfaced as chips.
    assert "britannica.com" in out and "apnews.com" in out
    assert "arstechnica.com" not in out  # [3] belongs to Career, not this section
    assert "SpaceX" in out  # the linked article this section points at
    urls = {s.url for s in out.web_sources}
    assert "https://www.britannica.com/biography/Elon-Musk" in urls


async def test_grokipedia_section_unknown_section_lists_available() -> None:
    out = await _handlers(_ok_handler())["grokipedia_section"](
        {"slug": "Elon_Musk", "section": "Nonexistent"}, CTX
    )
    assert "No section" in out and "Early Life" in out  # steers back to a real section


# --- grokipedia_citations (the primary-source hand-off) ---------------------------


async def test_grokipedia_citations_returns_followable_primary_sources() -> None:
    out = await _handlers(_ok_handler())["grokipedia_citations"]({"slug": "Elon_Musk"}, CTX)
    assert isinstance(out, ToolOutput)
    # Every citation is a chip whose URL points at the primary source, not Grokipedia.
    assert [s.url for s in out.web_sources] == [
        "https://www.britannica.com/biography/Elon-Musk",
        "https://apnews.com/article/xyz",
        "https://arstechnica.com/space",
    ]
    assert "britannica.com" in out


async def test_grokipedia_citations_scopes_to_a_section() -> None:
    out = await _handlers(_ok_handler())["grokipedia_citations"](
        {"slug": "Elon_Musk", "section": "Career"}, CTX
    )
    assert isinstance(out, ToolOutput)
    # The Career section cites only [3].
    assert [s.url for s in out.web_sources] == ["https://arstechnica.com/space"]


# --- grokipedia_related -----------------------------------------------------------


async def test_grokipedia_related_lists_linked_articles() -> None:
    out = await _handlers(_ok_handler())["grokipedia_related"]({"slug": "Elon_Musk"}, CTX)
    assert isinstance(out, ToolOutput)
    assert "[slug: SpaceX]" in out
    assert out.web_sources[0].url == "https://grokipedia.com/page/SpaceX"


# --- caching + errors + tendrils --------------------------------------------


async def test_drilldown_on_one_slug_costs_a_single_fetch() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_PREVIEW)

    handlers = _handlers(handle)
    await handlers["grokipedia_outline"]({"slug": "Elon_Musk"}, CTX)
    await handlers["grokipedia_section"]({"slug": "Elon_Musk", "section": "1"}, CTX)
    await handlers["grokipedia_citations"]({"slug": "Elon_Musk"}, CTX)
    assert calls == ["/api/page-preview"]  # cached parse served the section + citations


async def test_handlers_surface_errors_as_recoverable_text() -> None:
    handlers = _handlers(lambda r: httpx.Response(503))
    out = await handlers["grokipedia_outline"]({"slug": "X"}, CTX)
    assert not isinstance(out, ToolOutput)  # a plain error string, not a citable result
    assert "Grokipedia" in out


async def test_grokipedia_search_emits_a_tendril() -> None:
    fired: list[tuple[str, str | None]] = []
    client = GrokipediaClient(transport=httpx.MockTransport(_ok_handler()))
    handlers = build_grokipedia_handlers(
        client, emit=lambda kind, text=None: fired.append((kind, text))
    )
    await handlers["grokipedia_search"]({"query": "elon"}, CTX)
    assert fired == [("web_search", "elon")]
