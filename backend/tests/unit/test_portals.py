"""Portal resolvers: the FL Sunbiz adapter parses a real-shaped results table into rows with
static detail URLs, degrades honestly on an outage, and the registry dispatch selects by
(jurisdiction, kind) and by key. Portal HTTP is faked with an injected transport — never a live
government site (DYNAMIC_PORTAL_FETCH_PLAN.md)."""

from __future__ import annotations

import httpx

from jbrain.web.fetch import WebFetcher
from jbrain.web.portals import (
    FlDfsResolver,
    FlSunbizResolver,
    advertised_capabilities,
    select_resolvers,
)

# The DFS search PAGE (GET) — a form carrying the session's csrf_token hidden field.
_DFS_PAGE = (
    '<html><body><form action="/" id="frmMainSearch" method="post">'
    '<input type="hidden" name="csrf_token" value="TOKEN-XYZ"/>'
    '<input name="IndividualLNameFilter" value=""/>'
    "</form></body></html>"
)
# The DFS results (POST) — a table whose rows carry the licensee id/number/name as data-attrs.
_DFS_RESULTS = (
    "<html><body><table><tbody>"
    '<tr data-licensee-id="1876293" data-license-num="W690060" '
    'data-licensee-name="COLLIGE, FRANK WILLIAM">'
    '<td><a href="/Licensee/1876293">COLLIGE, FRANK WILLIAM</a></td></tr>'
    '<tr data-licensee-id="42" data-license-num="P145401" data-licensee-name="ROHN, JEFF">'
    '<td><a href="/Licensee/42">ROHN, JEFF</a></td></tr>'
    "</tbody></table></body></html>"
)


def _dfs_transport(results: str = _DFS_RESULTS):  # noqa: ANN202
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=_DFS_PAGE, headers={"content-type": "text/html"})
        # The POST must carry the page's csrf_token (proves the session/token flow wired through)
        assert "csrf_token=TOKEN-XYZ" in request.content.decode()
        return httpx.Response(200, text=results, headers={"content-type": "text/html"})

    return httpx.MockTransport(handle)


# A Sunbiz SearchResults page as the server renders it: a table whose result rows each link to a
# static SearchResultDetail page, then the document number and status cells (the real structure).
_RESULTS_HTML = """
<html><head><title>Search Results</title></head><body>
<table id="search-results"><tbody>
<tr><th>Name</th><th>Doc #</th><th>Status</th></tr>
<tr><td>
<a href="/CorporationSearch/SearchResultDetail?id=domp-p04">PROPERTY PRO SERVICES, INC</a>
</td><td>P04000120548</td><td>Active</td></tr>
<tr><td>
<a href="/CorporationSearch/SearchResultDetail?id=flal-l18">PROPERTY PROS ELITE REALTY LLC</a>
</td><td>L18000188973</td><td>INACT</td></tr>
</tbody></table>
</body></html>
"""

# The empty ByName search FORM — no result rows (what a bare fetch of the search page returns).
_FORM_HTML = """
<html><head><title>Search by Name</title></head><body>
<form action="/Inquiry/CorporationSearch/SearchResults"><input type="text" name="SearchTerm"/>
<input type="submit" value="Search"/></form></body></html>
"""


def _fetcher(handler) -> WebFetcher:  # noqa: ANN001
    return WebFetcher(transport=httpx.MockTransport(handler))


async def test_sunbiz_parses_results_into_rows_with_static_detail_urls() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "SearchResults" in str(request.url)  # the resolver hits results, not the ByName form
        return httpx.Response(200, text=_RESULTS_HTML, headers={"content-type": "text/html"})

    result = await FlSunbizResolver("https://search.sunbiz.org").search(
        _fetcher(handle), "Property Pros", limit=10
    )
    assert result.ok
    assert [r.title for r in result.rows] == [
        "PROPERTY PRO SERVICES, INC",
        "PROPERTY PROS ELITE REALTY LLC",
    ]
    first = result.rows[0]
    assert first.detail_url.startswith("https://search.sunbiz.org/")
    assert first.detail_url.endswith("/CorporationSearch/SearchResultDetail?id=domp-p04")
    assert "P04000120548" in first.subtitle and "Active" in first.subtitle
    assert ("document_number", "P04000120548") in first.identifiers


async def test_sunbiz_form_only_page_yields_ok_empty_not_an_error() -> None:
    # A form-only page (no result rows) is a REAL empty result (ok=True, rows=()), not an outage —
    # so the tool says "no match here", never "no record exists".
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_FORM_HTML, headers={"content-type": "text/html"})

    result = await FlSunbizResolver("https://search.sunbiz.org").search(
        _fetcher(handle), "Nonexistent Co", limit=10
    )
    assert result.ok and result.rows == ()


async def test_sunbiz_http_error_is_ok_false() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"content-type": "text/html"})

    result = await FlSunbizResolver("https://search.sunbiz.org").search(
        _fetcher(handle), "Property Pros", limit=10
    )
    assert result.ok is False and result.rows == ()


async def test_sunbiz_respects_limit() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_RESULTS_HTML, headers={"content-type": "text/html"})

    result = await FlSunbizResolver("https://search.sunbiz.org").search(
        _fetcher(handle), "Property Pros", limit=1
    )
    assert len(result.rows) == 1


async def test_unconfigured_resolver_reports_not_configured() -> None:
    resolver = FlSunbizResolver("")
    assert resolver.configured is False
    result = await resolver.search(_fetcher(lambda r: httpx.Response(200)), "X", limit=10)
    assert result.ok is False and result.rows == ()


async def test_dfs_parses_licensee_rows_via_the_session_post_flow() -> None:
    result = await FlDfsResolver("https://licenseesearch.fldfs.com").search(
        WebFetcher(transport=_dfs_transport()), "Collige", limit=10
    )
    assert result.ok
    assert [r.title for r in result.rows] == ["COLLIGE, FRANK WILLIAM", "ROHN, JEFF"]
    first = result.rows[0]
    assert first.detail_url == "https://licenseesearch.fldfs.com/Licensee/1876293"
    assert first.subtitle == "License W690060"
    assert ("license_number", "W690060") in first.identifiers


async def test_dfs_empty_results_is_ok_empty() -> None:
    empty = "<html><body><table><tbody></tbody></table></body></html>"
    result = await FlDfsResolver("https://licenseesearch.fldfs.com").search(
        WebFetcher(transport=_dfs_transport(empty)), "Zzzabc", limit=10
    )
    assert result.ok and result.rows == ()


async def test_dfs_short_last_name_is_steered_not_an_outage() -> None:
    # The portal enforces a 3-char minimum last name; a shorter one is a real constraint (ok=True).
    result = await FlDfsResolver("https://licenseesearch.fldfs.com").search(
        WebFetcher(transport=_dfs_transport()), "Ng", limit=10
    )
    assert result.ok and result.rows == () and "at least 3" in result.note


async def test_dfs_outage_is_ok_false() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"content-type": "text/html"})

    result = await FlDfsResolver("https://licenseesearch.fldfs.com").search(
        WebFetcher(transport=httpx.MockTransport(handle)), "Collige", limit=10
    )
    assert result.ok is False and result.rows == ()


def test_registry_dispatch_by_jurisdiction_kind_and_key() -> None:
    sunbiz = FlSunbizResolver("https://search.sunbiz.org")
    dfs = FlDfsResolver("https://licenseesearch.fldfs.com")
    resolvers = (sunbiz, dfs)
    assert select_resolvers(resolvers, jurisdiction="FL", kind="business") == (sunbiz,)
    assert select_resolvers(resolvers, jurisdiction="FL", kind="license") == (dfs,)
    assert select_resolvers(resolvers, key="fl_license") == (dfs,)
    assert select_resolvers(resolvers, jurisdiction="TX", kind="business") == ()
    assert advertised_capabilities(resolvers) == "FL/business, FL/license"
    assert advertised_capabilities((FlSunbizResolver(""),)) == "(none configured)"
