"""The `portal_search` tool: returns a WebSource per result detail URL (so it folds into the
deep-research citation registry), steers an unknown/unconfigured selection instead of silently
widening, and fires the emit tendril. Portal HTTP is faked (DYNAMIC_PORTAL_FETCH_PLAN.md)."""

from __future__ import annotations

import httpx

from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.agent.portaltools import build_portal_handlers
from jbrain.db.session import SessionContext
from jbrain.web.fetch import WebFetcher
from jbrain.web.portals import FlDfsResolver, FlSunbizResolver

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=("general",))

_RESULTS_HTML = (
    "<html><body><table><tbody>"
    "<tr><td><a href='/Inquiry/CorporationSearch/SearchResultDetail?aggregateId=domp-p04-abc'>"
    "PROPERTY PRO SERVICES, INC</a></td><td>P04000120548</td><td>Active</td></tr>"
    "</tbody></table></body></html>"
)


def _handlers(handler, *, url: str = "https://search.sunbiz.org"):  # noqa: ANN001
    fetcher = WebFetcher(transport=httpx.MockTransport(handler))
    fired: list[tuple[str, str | None]] = []

    def _emit(kind: str, text: str | None = None) -> None:
        fired.append((kind, text))

    handlers = build_portal_handlers(fetcher, (FlSunbizResolver(url),), emit=_emit)
    return handlers["portal_search"], fired


_DFS_PAGE = '<html><body><form action="/"><input name="csrf_token" value="T"/></form></body></html>'
_DFS_RESULTS = (
    "<html><body><table><tbody>"
    '<tr data-licensee-id="1876293" data-license-num="W690060" '
    'data-licensee-name="COLLIGE, FRANK WILLIAM">'
    '<td><a href="/Licensee/1876293">COLLIGE, FRANK WILLIAM</a></td></tr>'
    "</tbody></table></body></html>"
)


async def test_portal_search_dispatches_license_to_dfs_and_returns_websource() -> None:
    # Two configured resolvers; kind=license must route to the DFS adapter (its session GET→POST
    # flow) and surface the licensee detail URL as a citable WebSource.
    def handle(request: httpx.Request) -> httpx.Response:
        body = _DFS_PAGE if request.method == "GET" else _DFS_RESULTS
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    fetcher = WebFetcher(transport=httpx.MockTransport(handle))
    resolvers = (
        FlSunbizResolver("https://search.sunbiz.org"),
        FlDfsResolver("https://dfs.example"),
    )
    tool = build_portal_handlers(fetcher, resolvers)["portal_search"]
    out = await tool({"name": "Collige", "jurisdiction": "FL", "kind": "license"}, CTX)
    assert isinstance(out, ToolOutput)
    assert len(out.web_sources) == 1
    assert out.web_sources[0].url == "https://dfs.example/Licensee/1876293"
    assert out.web_sources[0].title == "COLLIGE, FRANK WILLIAM"


async def test_portal_search_returns_websources_for_each_detail_url() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_RESULTS_HTML, headers={"content-type": "text/html"})

    tool, fired = _handlers(handle)
    out = await tool({"name": "Property Pros", "jurisdiction": "FL", "kind": "business"}, CTX)
    assert isinstance(out, ToolOutput)
    assert len(out.web_sources) == 1
    ws = out.web_sources[0]
    assert ws.title == "PROPERTY PRO SERVICES, INC"
    assert "SearchResultDetail" in ws.url and ws.read is False
    assert fired == [("web_search", "Property Pros")]  # the emit tendril fired once


async def test_portal_search_steers_an_unknown_selection() -> None:
    tool, _ = _handlers(lambda r: httpx.Response(200))
    out = await tool({"name": "Acme", "jurisdiction": "TX", "kind": "business"}, CTX)
    assert isinstance(out, str)
    assert "no portal matches" in out.lower() and "fl/business" in out.lower()


async def test_portal_search_needs_a_selector() -> None:
    tool, _ = _handlers(lambda r: httpx.Response(200))
    out = await tool({"name": "Acme"}, CTX)
    assert isinstance(out, str) and "jurisdiction" in out.lower()


async def test_portal_search_reports_unconfigured() -> None:
    tool, _ = _handlers(lambda r: httpx.Response(200), url="")  # empty sunbiz_url → not configured
    out = await tool({"name": "Acme", "jurisdiction": "FL", "kind": "business"}, CTX)
    assert isinstance(out, str) and "not configured" in out.lower()


async def test_portal_search_empty_result_is_not_absence() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body><p>nothing here</p></body></html>",
            headers={"content-type": "text/html"},
        )

    tool, _ = _handlers(handle)
    out = await tool({"name": "Zzz", "jurisdiction": "FL", "kind": "business"}, CTX)
    assert isinstance(out, ToolOutput)  # ToolOutput is a str subclass — the text IS the object
    assert "no match" in out.lower() and "not proof no record" in out.lower()
