"""Matching a BDS version to Mojang's changelog article, and quoting it verbatim.

The titles are the real shapes from the feed (2026-10-10): a plain release, a hotfix,
and a combined hotfix listing several minors, with Java and preview articles in the
same section."""

from __future__ import annotations

import httpx
import pytest

from jbrain.minecraft import changelog

ARTICLES = [
    {"title": "Minecraft Java Edition - 26.3", "html_url": "j", "created_at": "t", "body": ""},
    {
        "title": "Minecraft: Bedrock Edition 26.52 Hotfix Changelog",
        "html_url": "https://feedback.minecraft.net/hc/en-us/articles/1",
        "created_at": "2026-09-25T22:05:12Z",
        "body": (
            "<h2>Fixes</h2><ul><li>Improved stability for Nintendo Switch</li>"
            "<li>Players in <b>Spectator</b> Mode are no longer hit &amp; hurt</li>"
            "<li><ul><li>Nested leaf</li></ul></li><li>Three</li><li>Four</li>"
            "<li>Five</li></ul>"
        ),
    },
    {"title": "Minecraft: Bedrock Edition 26.41/42/43 Hotfix Changelog", "html_url": "h"},
    {"title": "Minecraft Beta & Preview - 26.60.20", "html_url": "p"},
]


@pytest.mark.parametrize(
    ("title", "version", "expected"),
    [
        ("Minecraft: Bedrock Edition 26.52 Hotfix Changelog", "1.26.52.3", True),
        ("Minecraft: Bedrock Edition 26.50 Changelog - Wilderness Bound", "1.26.50.1", True),
        ("Minecraft: Bedrock Edition 26.41/42/43 Hotfix Changelog", "1.26.42.2", True),
        ("Minecraft: Bedrock Edition 26.52 Hotfix Changelog", "1.26.5.1", False),
        ("Minecraft Java Edition - 26.3", "1.26.3.1", False),
        ("Minecraft Beta & Preview - 26.60.20", "1.26.60.20", False),
    ],
)
def test_titles_match_the_marketing_number(title: str, version: str, expected: bool) -> None:
    assert changelog.title_matches(title, version) is expected


def test_the_first_bullets_are_quoted_verbatim_as_plain_text() -> None:
    notes = changelog.pick(ARTICLES, "1.26.52.3")
    assert notes is not None
    assert notes.title == "Minecraft: Bedrock Edition 26.52 Hotfix Changelog"
    assert notes.lines == [
        "Improved stability for Nintendo Switch",
        "Players in Spectator Mode are no longer hit & hurt",
        "Nested leaf",
        "Three",
    ]


def test_an_unpublished_version_has_no_notes() -> None:
    assert changelog.pick(ARTICLES, "1.26.60.4") is None


async def test_a_failed_fetch_answers_not_published(monkeypatch: pytest.MonkeyPatch) -> None:
    def down(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(changelog, "_transport", httpx.MockTransport(down))
    assert await changelog.Changelog().notes_for("1.26.52.3") is None


async def test_the_listing_is_fetched_once_per_hour(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def ok(r: httpx.Request) -> httpx.Response:
        calls.append(r)
        return httpx.Response(200, json={"articles": ARTICLES})

    monkeypatch.setattr(changelog, "_transport", httpx.MockTransport(ok))
    c = changelog.Changelog()
    assert (await c.notes_for("1.26.52.3")) is not None
    await c.notes_for("1.26.52.3")
    assert len(calls) == 1
