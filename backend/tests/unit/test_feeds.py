"""The curated-feed source (jbrain.web.feeds): the pure offline parser and the
FeedClient's fetch/window/dedupe over a MockTransport WebFetcher — no network, like
every other web-client test (docs/plans/NEWS_FEED_PLAN.md)."""

from __future__ import annotations

import calendar
from email.utils import formatdate

import httpx

from jbrain.web.feeds import FeedClient, parse_feed
from jbrain.web.fetch import WebFetcher

# A body long enough to clear _MIN_BODY_CHARS (400) after extraction, so a content:encoded
# item is recognised as full-text rather than a thin teaser.
_LONG_BODY = "This is a full article body sentence with real substance. " * 20


def _rss(items_xml: str, *, title: str = "Feed") -> bytes:
    return (
        "<?xml version='1.0'?>"
        "<rss version='2.0' xmlns:content='http://purl.org/rss/1.0/modules/content/'>"
        f"<channel><title>{title}</title>{items_xml}</channel></rss>"
    ).encode()


def _full_item(url: str, epoch: int) -> str:
    return (
        "<item><title>Full story</title>"
        f"<link>{url}</link>"
        f"<pubDate>{formatdate(epoch, usegmt=True)}</pubDate>"
        "<description>Short teaser.</description>"
        f"<content:encoded><![CDATA[<p>{_LONG_BODY}</p>]]></content:encoded></item>"
    )


def _summary_item(url: str, epoch: int) -> str:
    return (
        "<item><title>Lead story</title>"
        f"<link>{url}</link>"
        f"<pubDate>{formatdate(epoch, usegmt=True)}</pubDate>"
        "<description>Just a short summary, no full body.</description></item>"
    )


# --- parse_feed (pure, offline) ----------------------------------------------


def test_parse_feed_splits_full_body_from_summary_and_keeps_dates() -> None:
    epoch = calendar.timegm((2026, 8, 12, 9, 0, 0, 0, 0, 0))
    raw = _rss(
        _full_item("https://s.example/a", epoch) + _summary_item("https://s.example/b", epoch)
    )
    by_url = {it.url: it for it in parse_feed(raw)}
    a = by_url["https://s.example/a"]
    assert a.has_full_body and "full article body" in a.body
    assert a.epoch == float(epoch) and a.published  # human date string kept for display
    assert a.source == "Feed"
    b = by_url["https://s.example/b"]
    assert not b.has_full_body and b.body == "" and "short summary" in b.summary


def test_parse_feed_handles_atom_and_undated_entries() -> None:
    atom = (
        b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'><title>Atom</title>"
        b"<entry><title>Atom entry</title><link href='https://a.example/x'/>"
        b"<summary>a lead</summary></entry></feed>"
    )
    items = parse_feed(atom)
    assert [it.url for it in items] == ["https://a.example/x"]
    assert items[0].epoch is None  # undated → no epoch, but the item is still kept


def test_parse_feed_skips_entries_with_no_link() -> None:
    assert parse_feed(_rss("<item><title>no link to open</title></item>")) == []


# --- FeedClient (fetch + window + dedupe over a fake transport) ---------------

_NOW = calendar.timegm((2026, 8, 12, 12, 0, 0, 0, 0, 0))


def _fetcher(routes: dict[str, bytes | int]) -> WebFetcher:
    """A WebFetcher whose transport serves canned feed bytes (or a status code to fail) per URL.
    An injected transport skips the SSRF DNS check, so the https feed URLs need no real host."""

    def handle(request: httpx.Request) -> httpx.Response:
        val = routes.get(str(request.url))
        if val is None:
            return httpx.Response(404)
        if isinstance(val, int):
            return httpx.Response(val)
        return httpx.Response(200, content=val, headers={"content-type": "application/rss+xml"})

    return WebFetcher(transport=httpx.MockTransport(handle))


async def test_fetch_category_windows_sorts_and_dedupes() -> None:
    recent = _NOW - 3600  # 1h ago — inside the day window
    old = _NOW - 3 * 86_400  # 3 days ago — outside day, inside week
    feed = _rss(
        _full_item("https://s.example/recent", recent)
        + _summary_item("https://s.example/old", old)
        + _summary_item("https://s.example/recent", recent)  # duplicate URL → collapses
    )
    client = FeedClient(
        _fetcher({"https://feed.example/rss": feed}),
        {"space": ["https://feed.example/rss"]},
        clock=lambda: float(_NOW),
    )
    day = await client.fetch_category("space", since="day")
    assert [it.url for it in day] == ["https://s.example/recent"]  # old dropped, dup collapsed
    assert day[0].has_full_body  # the full-body copy won the dedup (first seen)
    week = await client.fetch_category("space", since="week")
    # Newest first: the recent item leads, the 3-day-old one follows.
    assert [it.url for it in week] == ["https://s.example/recent", "https://s.example/old"]


async def test_fetch_category_resolves_a_substring_category_and_refuses_unknown() -> None:
    feed = _rss(_summary_item("https://s.example/a", _NOW - 100))
    client = FeedClient(
        _fetcher({"https://feed.example/rss": feed}),
        {"space": ["https://feed.example/rss"]},
        clock=lambda: float(_NOW),
    )
    # An angle title like "space industry outlook" resolves to the `space` category by substring.
    assert await client.fetch_category("space industry outlook", since="week")
    # An unknown category returns nothing (the handler then lists the known ones).
    assert await client.fetch_category("sports", since="week") == []
    assert client.categories() == ("space",) and client.enabled


async def test_fetch_category_skips_a_failing_feed_best_effort() -> None:
    good = _rss(_summary_item("https://s.example/ok", _NOW - 100))
    client = FeedClient(
        _fetcher({"https://good.example/rss": good, "https://bad.example/rss": 403}),
        {"world": ["https://bad.example/rss", "https://good.example/rss"]},
        clock=lambda: float(_NOW),
    )
    out = await client.fetch_category("world", since="week")
    assert [it.url for it in out] == ["https://s.example/ok"]  # the 403 feed is dropped, not fatal


async def test_dedupe_prefers_the_full_text_copy_regardless_of_feed_order() -> None:
    url = "https://s.example/story"
    routes = {
        "https://sum.example/rss": _rss(_summary_item(url, _NOW - 100)),  # summary-only, first
        "https://full.example/rss": _rss(_full_item(url, _NOW - 100)),  # carries the body, second
    }
    client = FeedClient(
        _fetcher(routes),
        {"space": ["https://sum.example/rss", "https://full.example/rss"]},
        clock=lambda: float(_NOW),
    )
    out = await client.fetch_category("space", since="week")
    assert [it.url for it in out] == [url]
    assert out[0].has_full_body  # feed order didn't downgrade the already-opened article


async def test_resolve_uses_whole_word_overlap_not_substring() -> None:
    routes = {
        "https://space.example/rss": _rss(_summary_item("https://x.example/space", _NOW - 100)),
        "https://ai.example/rss": _rss(_summary_item("https://x.example/ai", _NOW - 100)),
    }
    client = FeedClient(
        _fetcher(routes),
        {"space": ["https://space.example/rss"], "ai_tech": ["https://ai.example/rss"]},
        clock=lambda: float(_NOW),
    )
    # A whole-word token resolves: "ai" → ai_tech, "space industry outlook" → space.
    assert (await client.fetch_category("ai", since="week"))[0].url == "https://x.example/ai"
    assert (await client.fetch_category("space industry outlook", since="week"))[
        0
    ].url == "https://x.example/space"
    # "fintech" shares no whole word with any key — it must NOT mis-route to ai_tech by substring.
    assert await client.fetch_category("fintech", since="week") == []


async def test_cache_is_keyed_per_category_and_since_not_limit() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        feed = _rss(
            _summary_item("https://x.example/a", _NOW - 100)
            + _summary_item("https://x.example/b", _NOW - 200)
        )
        return httpx.Response(200, content=feed, headers={"content-type": "application/rss+xml"})

    client = FeedClient(
        WebFetcher(transport=httpx.MockTransport(handle)),
        {"space": ["https://feed.example/rss"]},
        clock=lambda: float(_NOW),
    )
    first = await client.fetch_category("space", since="week", limit=1)
    assert len(first) == 1 and len(calls) == 1
    # A different limit is a post-slice of the cached list — no second outbound fetch (the scout
    # can't amplify one category into a fan of re-fetches by varying limit).
    second = await client.fetch_category("space", since="week", limit=2)
    assert len(second) == 2 and len(calls) == 1


def test_empty_feed_map_is_disabled() -> None:
    client = FeedClient(WebFetcher(), {"space": [], "": ["https://x"]})
    assert not client.enabled and client.categories() == ()
