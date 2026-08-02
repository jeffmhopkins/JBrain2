"""The Grokipedia client + parser for the jerv agent
(docs/plans/GROKIPEDIA_TOOL_PLAN.md). HTTP is faked via MockTransport — no live
network, like the SearXNG and web-fetch clients."""

import httpx
import pytest

from jbrain.web.grokipedia import (
    DEFAULT_BASE_URL,
    GrokipediaClient,
    GrokipediaError,
    article_from_html,
    article_from_preview,
    parse_typeahead,
)

# --- fixtures ---------------------------------------------------------------

_TYPEAHEAD_OK = {
    "results": [
        {"slug": "Elon_Musk", "title": "Elon Musk", "snippet": "Entrepreneur.", "viewCount": 900},
        {"slug": "SpaceX", "title": "SpaceX", "snippet": "Rocket company.", "viewCount": 400},
        {"slug": "", "title": "no slug", "snippet": "dropped"},  # slugless → dropped
    ]
}

# A page-preview body: markdown content (headings, an inline wiki-link), a structured
# citation array, and metadata. `lastModified` 1776869679 is 2026-04-22 UTC.
_CONTENT = (
    "Intro paragraph about the subject.\n\n"
    "## Early Life\n\nBorn in 1971. See [SpaceX](/page/SpaceX).\n\n"
    "### Education\n\nStudied physics.\n\n"
    "## Career\n\nFounded companies. Also [Tesla, Inc.](/page/Tesla,_Inc.).\n\n"
    "## References\n\nSee sources.\n"
)
_PREVIEW_OK = {
    "page": {
        "title": "Elon Musk",
        "content": _CONTENT,
        "citations": [
            {"id": 1, "url": "https://www.britannica.com/biography/Elon-Musk", "title": ""},
            {"id": 2, "url": "https://apnews.com/article/xyz", "title": "AP story"},
            {"id": 3, "url": "", "title": "no url dropped"},
        ],
        "metadata": {
            "lastModified": 1776869679,
            "categories": ["Entrepreneurs", "Engineers"],
            "language": "en",
        },
        "stats": {"totalViews": 1000},
    }
}

_PARA = "A slug is the human-readable part of a URL that identifies a page clearly. "
_SSR_HTML = (
    "<html><head><title>Slug (publishing) - Grokipedia</title></head><body><article>"
    "<h1>Slug (publishing)</h1>"
    "<p>" + _PARA * 8 + ' See <a href="/page/URL">URL</a>.</p>'
    "<h2>Etymology</h2><p>" + _PARA * 8 + "</p>"
    "<h2>Usage</h2><p>" + _PARA * 8 + "</p>"
    '<div id="references"><ol>'
    '<li id="ref-1"><a href="https://example.com/a" target="_blank">src a</a></li>'
    '<li id="ref-2"><a href="https://news.example.org/b">src b</a></li>'
    "</ol></div></article></body></html>"
).encode()


def _client(handler) -> GrokipediaClient:  # type: ignore[no-untyped-def]
    return GrokipediaClient(transport=httpx.MockTransport(handler))


# --- parse_typeahead --------------------------------------------------------


def test_parse_typeahead_keeps_slugged_rows_and_drops_the_rest() -> None:
    hits = parse_typeahead(_TYPEAHEAD_OK, limit=10)
    assert [h.slug for h in hits] == ["Elon_Musk", "SpaceX"]
    assert hits[0].title == "Elon Musk" and hits[0].view_count == 900
    assert hits[0].snippet == "Entrepreneur."


def test_parse_typeahead_honors_limit_and_bad_bodies() -> None:
    assert len(parse_typeahead(_TYPEAHEAD_OK, limit=1)) == 1
    assert parse_typeahead({"results": "nope"}, limit=5) == []
    assert parse_typeahead("garbage", limit=5) == []


def test_parse_typeahead_falls_back_to_slug_for_a_missing_title() -> None:
    hits = parse_typeahead({"results": [{"slug": "New_York_City"}]}, limit=5)
    assert hits[0].title == "New York City" and hits[0].view_count is None


# --- article_from_preview (the primary JSON path) ---------------------------


def test_article_from_preview_builds_outline_with_hierarchical_numbers() -> None:
    article = article_from_preview(_PREVIEW_OK, "Elon_Musk")
    assert article is not None
    assert article.title == "Elon Musk"
    numbered = [(s.number, s.title, s.anchor) for s in article.outline]
    assert numbered == [
        ("1", "Early Life", "early-life"),
        ("1.1", "Education", "education"),
        ("2", "Career", "career"),
        ("3", "References", "references"),
    ]


def test_article_from_preview_parses_metadata() -> None:
    article = article_from_preview(_PREVIEW_OK, "Elon_Musk")
    assert article is not None
    assert article.categories == ("Entrepreneurs", "Engineers")
    assert article.last_modified is not None and article.last_modified.year == 2026


def test_article_from_preview_citations_derive_publisher_from_domain() -> None:
    article = article_from_preview(_PREVIEW_OK, "Elon_Musk")
    assert article is not None
    # The url-less citation is dropped; publisher comes from the domain even when the
    # source's own title is blank.
    assert [(c.ref, c.publisher, c.title) for c in article.citations] == [
        (1, "britannica.com", ""),
        (2, "apnews.com", "AP story"),
    ]


def test_article_from_preview_surfaces_related_wiki_links() -> None:
    article = article_from_preview(_PREVIEW_OK, "Elon_Musk")
    assert article is not None
    assert [(r.slug, r.title) for r in article.related] == [
        ("SpaceX", "SpaceX"),
        ("Tesla,_Inc.", "Tesla, Inc."),
    ]


def test_article_from_preview_empty_content_is_none() -> None:
    assert article_from_preview({"page": {"content": "  "}}, "X") is None
    assert article_from_preview("garbage", "X") is None


def test_article_from_preview_accepts_top_level_page_fields() -> None:
    # Some responses put the page fields at the top level rather than under "page".
    article = article_from_preview({"content": "## H\n\nbody", "metadata": {}}, "X")
    assert article is not None and article.outline[0].title == "H"


# --- GrokArticle.section (drill-down) ---------------------------------------


def test_section_lookup_by_number_anchor_and_title() -> None:
    article = article_from_preview(_PREVIEW_OK, "Elon_Musk")
    assert article is not None
    by_number = article.section("1.1")
    by_anchor = article.section("education")
    by_title = article.section("Education")
    assert by_number == by_anchor == by_title
    assert by_number is not None
    section, body = by_number
    assert section.title == "Education" and body.startswith("### Education")
    assert "Studied physics." in body


def test_section_spans_to_the_next_same_or_higher_heading() -> None:
    article = article_from_preview(_PREVIEW_OK, "Elon_Musk")
    assert article is not None
    result = article.section("Early Life")
    assert result is not None
    _section, body = result
    # A parent section includes its subsection but stops at the next sibling (## Career).
    assert "Studied physics." in body  # the Education subsection is inside Early Life
    assert "Founded companies." not in body  # Career is a sibling, not included


def test_section_unknown_ref_is_none() -> None:
    article = article_from_preview(_PREVIEW_OK, "Elon_Musk")
    assert article is not None
    assert article.section("nonexistent") is None
    assert article.section("  ") is None


# --- article_from_html (the SSR fallback path) ------------------------------


def test_article_from_html_strips_the_site_title_suffix() -> None:
    article = article_from_html(_SSR_HTML.decode(), "Slug_(publishing)")
    assert article.title == "Slug (publishing)"


def test_article_from_html_parses_reference_list_citations() -> None:
    article = article_from_html(_SSR_HTML.decode(), "Slug_(publishing)")
    assert [(c.ref, c.url, c.publisher) for c in article.citations] == [
        (1, "https://example.com/a", "example.com"),
        (2, "https://news.example.org/b", "news.example.org"),
    ]


def test_article_from_html_recovers_content_and_sections() -> None:
    article = article_from_html(_SSR_HTML.decode(), "Slug_(publishing)")
    assert article.content.strip()
    titles = [s.title for s in article.outline]
    assert "Etymology" in titles and "Usage" in titles


# --- GrokipediaClient.search -----------------------------------------------


async def test_search_sends_query_and_limit_and_parses() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_TYPEAHEAD_OK)

    hits = await _client(handle).search("elon", limit=5)
    assert [h.slug for h in hits] == ["Elon_Musk", "SpaceX"]
    assert calls[0].url.params["query"] == "elon"
    assert calls[0].url.params["limit"] == "5"
    assert str(calls[0].url).startswith(f"{DEFAULT_BASE_URL}/api/typeahead")


async def test_search_empty_query_makes_no_request() -> None:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_TYPEAHEAD_OK)

    assert await _client(handle).search("   ") == []
    assert calls == []


async def test_search_clamps_the_limit() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["limit"])
        return httpx.Response(200, json={"results": []})

    await _client(handle).search("q", limit=999)
    assert seen == ["10"]  # capped at _MAX_LIMIT


async def test_search_http_error_raises_grokipedia_error() -> None:
    with pytest.raises(GrokipediaError):
        await _client(lambda r: httpx.Response(503)).search("q")


async def test_non_json_body_raises_grokipedia_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(GrokipediaError):
        await _client(handle).search("q")


# --- GrokipediaClient.fetch_article (API-first, SSR fallback) ---------------


async def test_fetch_article_uses_the_preview_api() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=_PREVIEW_OK)

    article = await _client(handle).fetch_article("Elon_Musk")
    assert article.title == "Elon Musk" and len(article.outline) == 4
    assert calls == ["/api/page-preview"]  # the API answered; no SSR fetch needed


async def test_fetch_article_falls_back_to_ssr_when_the_api_fails() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/page-preview":
            return httpx.Response(500)  # transient API failure → fall back to the SSR page
        return httpx.Response(200, content=_SSR_HTML, headers={"content-type": "text/html"})

    article = await _client(handle).fetch_article("Slug_(publishing)")
    assert article.title == "Slug (publishing)"
    assert len(article.citations) == 2  # parsed from the SSR reference list
    # The API was tried first, then the SSR page (the slug is percent-encoded on the wire;
    # httpx's .url.path decodes it back for inspection).
    assert calls == ["/api/page-preview", "/page/Slug_(publishing)"]


async def test_fetch_article_404_does_not_fall_back() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(404)

    with pytest.raises(GrokipediaError) as excinfo:
        await _client(handle).fetch_article("No_Such_Page")
    assert excinfo.value.status == 404
    assert calls == ["/api/page-preview"]  # a definitive 404 is not laundered through the SSR page


async def test_fetch_article_falls_back_when_preview_is_unparseable() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/page-preview":
            return httpx.Response(200, json={"page": {"content": ""}})  # empty → unparseable
        return httpx.Response(200, content=_SSR_HTML, headers={"content-type": "text/html"})

    article = await _client(handle).fetch_article("Slug_(publishing)")
    assert article.title == "Slug (publishing)"


async def test_fetch_article_requires_a_slug() -> None:
    with pytest.raises(GrokipediaError):
        await _client(lambda r: httpx.Response(200, json={})).fetch_article("  ")


# --- Cloudflare cookie jar persists across calls ----------------------------


async def test_cookie_jar_persists_across_requests() -> None:
    seen_cookies: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie", ""))
        # Cloudflare sets its bot cookie on the first response; it must ride the next request.
        return httpx.Response(200, json={"results": []}, headers={"set-cookie": "__cf_bm=token"})

    client = _client(handle)
    await client.search("first")
    await client.search("second")
    assert seen_cookies[0] == ""  # no cookie on the first call
    assert "__cf_bm=token" in seen_cookies[1]  # the jar carried it to the second
