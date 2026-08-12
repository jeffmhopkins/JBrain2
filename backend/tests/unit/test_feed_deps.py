"""Dependency smoke test for the news_feed tool (docs/plans/NEWS_FEED_PLAN.md;
CLAUDE.md rule #8 single-source-of-truth). Fails fast if `feedparser` (RSS/Atom
parsing) is missing from a synced environment — so a broken `uv sync` / dev-setup
step reddens here instead of deep in the tool wiring."""

from __future__ import annotations


def test_feedparser_importable_and_parses() -> None:
    import feedparser

    # The surface jbrain.web.feeds.parse_feed relies on: parse() over bytes, entries with
    # a link, and the *_parsed date normalization.
    parsed = feedparser.parse(
        b"<?xml version='1.0'?><rss version='2.0'><channel><title>T</title>"
        b"<item><title>Hello</title><link>https://example.com/a</link>"
        b"<pubDate>Tue, 12 Aug 2026 10:00:00 GMT</pubDate></item></channel></rss>"
    )
    assert parsed.entries
    assert parsed.entries[0].link == "https://example.com/a"
    assert parsed.entries[0].published_parsed is not None
