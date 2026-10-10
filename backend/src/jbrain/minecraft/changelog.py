"""Mojang's release notes for a Bedrock server version, for the Ops card's "what changed".

docs/plans/MINECRAFT_BEDROCK_PLAN.md §M1. Mojang posts every Bedrock release in the
"Release Changelogs" section of feedback.minecraft.net, a Zendesk help centre with a
public listing API. Article titles use the marketing number:
- BDS `1.26.52.3` is "Minecraft: Bedrock Edition **26.52** Hotfix Changelog";
- a combined hotfix reads "26.41/42/43".

**The lines are quoted, never summarised.** The card shows the article's first few
bullet points verbatim, under the article's own title, and links to the full article. A
model condensing release notes is a way to state a fix that isn't there; a quote can't
be wrong about what Mojang wrote. When no article matches yet (Mojang usually posts
within a day), the answer is None and the card links Mojang's general update page.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

SECTION_URL = (
    "https://feedback.minecraft.net/api/v2/help_center/en-us/sections/360001186971/"
    "articles.json?sort_by=created_at&sort_order=desc&per_page=30"
)
FALLBACK_URL = "https://aka.ms/MinecraftUpdate"
MAX_LINES = 4
# Mojang's listing changes a few times a month; the card asks on every open.
CACHE_S = 3600.0
_TITLE = re.compile(r"Bedrock Edition\s+(\d+)\.(\d+(?:/\d+)*)", re.I)
_LI = re.compile(r"<li[^>]*>(.*?)</li>", re.I | re.S)
_LI_OPEN = re.compile(r"<li[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
# Tests swap in an httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class Notes:
    version: str
    title: str
    url: str
    published_at: str
    lines: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "lines": list(self.lines),
        }


def title_matches(title: str, version: str) -> bool:
    """Does a changelog title cover this BDS version (`1.MAJOR.MINOR.build`)?

    Java and preview articles share the section, so the title must say Bedrock and
    must not say Preview."""
    if "preview" in title.lower() or "beta" in title.lower():
        return False
    m = _TITLE.search(title)
    parts = version.split(".")
    if not m or len(parts) < 3:
        return False
    return m.group(1) == parts[1] and parts[2] in m.group(2).split("/")


def first_lines(body_html: str, n: int = MAX_LINES) -> list[str]:
    """The article's first `n` bullet points, as plain text, verbatim."""
    out: list[str] = []
    for item in _LI.findall(body_html):
        # The lazy match stops at the first </li>, so a bullet holding a nested list
        # arrives as "<ul><li>leaf"; the leaf is the text after the last opening tag.
        item = _LI_OPEN.split(item)[-1]
        text = " ".join(html.unescape(_TAG.sub(" ", item)).split())
        if text:
            out.append(text)
        if len(out) == n:
            break
    return out


def pick(articles: list[dict[str, Any]], version: str) -> Notes | None:
    for a in articles:
        title = str(a.get("title", ""))
        if title_matches(title, version):
            return Notes(
                version=version,
                title=title,
                url=str(a.get("html_url", "")),
                published_at=str(a.get("created_at", "")),
                lines=first_lines(str(a.get("body", ""))),
            )
    return None


class Changelog:
    """The listing, fetched at most hourly. A fetch failure keeps the last good
    listing; with none, the answer is "not published", which is honest either way."""

    def __init__(self) -> None:
        self._articles: list[dict[str, Any]] = []
        self._fetched_at = 0.0

    async def notes_for(self, version: str | None) -> Notes | None:
        if not version:
            return None
        if time.time() - self._fetched_at > CACHE_S:
            await self._refresh()
        return pick(self._articles, version)

    async def _refresh(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=_transport) as client:
                resp = await client.get(SECTION_URL)
                resp.raise_for_status()
                self._articles = list(resp.json().get("articles", []))
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("minecraft.changelog_fetch_failed", error=repr(exc))
        self._fetched_at = time.time()
