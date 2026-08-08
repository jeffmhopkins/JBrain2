"""jerv's Grokipedia tool (docs/archive/GROKIPEDIA_TOOL_PLAN.md).

ONE umbrella tool — `grokipedia(action=…)` — over five operations that let jerv search
Grokipedia, traverse an article by its table of contents, read a single section without
loading the whole page, and pull the article's citations as followable primary-source links:

    grokipedia(action=search) → outline → section → citations   (+ related)

Collapsed from five flat tools into one action-dispatched tool (TOOL_CATALOG_PLAN): the
model selects the action and the local gpt-oss-120b fills it as reliably as the flat set
(validated by the tool-selection probe), while the surface stays one line.

Like `web_search`/`web_fetch` it runs DIRECTLY (the `web` permission class, the bounded
exception to invariant #9): jerv holds no knowledge-base tools and no owner data, and the
reach is a pinned public site. Citations surface as `WebSource` chips so the owner (and the
agent, via `web_fetch`) can follow each to its primary source.
"""

from __future__ import annotations

import re

import structlog

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.web.grokipedia import (
    DEFAULT_BASE_URL,
    GrokArticle,
    GrokCitation,
    GrokipediaClient,
    GrokipediaError,
)

log = structlog.get_logger()

_DEFAULT_SEARCH_LIMIT = 6
_DEFAULT_RELATED_LIMIT = 20
_DEFAULT_CITATION_LIMIT = 25
_MAX_CITATION_LIMIT = 60
_MAX_SECTION_CHARS = 8_000  # one section window; a longer section points at its subsections
# Inline citation markers in a section's markdown ("[128]") — used to scope citations to a
# section. Absent markers just fall back to the whole-article citation list.
_SECTION_REF_RE = re.compile(r"\[(\d+)\]")


def _page_url(slug: str) -> str:
    return f"{DEFAULT_BASE_URL}/page/{slug}"


def _coerce_int(raw: object, default: int) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _page_source(article: GrokArticle) -> WebSource:
    return WebSource(url=_page_url(article.slug), title=article.title)


def _article_header(article: GrokArticle) -> str:
    parts = [f"# {article.title}", _page_url(article.slug)]
    if article.last_modified is not None:
        parts.append(f"last updated {article.last_modified:%Y-%m-%d}")
    if article.categories:
        parts.append("categories: " + ", ".join(article.categories))
    return "\n".join(parts) + "\n\n"


def _citation_lines(cites: tuple[GrokCitation, ...]) -> str:
    return "\n".join(f"[{c.ref}] {c.title or c.publisher or c.url}\n  {c.url}" for c in cites)


def _section_citations(article: GrokArticle, section_text: str) -> tuple[GrokCitation, ...]:
    refs = {int(m.group(1)) for m in _SECTION_REF_RE.finditer(section_text)}
    if not refs:
        return ()
    return tuple(c for c in article.citations if c.ref in refs)


def build_grokipedia_handlers(
    client: GrokipediaClient, emit: BrainEmit | None = None
) -> dict[str, ToolHandler]:
    """`emit(kind, text)`, if given, fires a wall-display tendril when jerv reaches
    Grokipedia — reusing the recognized `web_search`/`web_fetch` markers (these are
    web reaches). The parsed article is cached per slug in the client, so a drill-down
    (outline → section → citations → related on one slug) costs a single fetch."""

    async def grokipedia_search_tool(arguments: dict, ctx: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "grokipedia(action=search) needs a non-empty query."
        limit = _coerce_int(arguments.get("limit"), _DEFAULT_SEARCH_LIMIT)
        if emit:
            emit("web_search", query)
        try:
            hits = await client.search(query, limit)
        except GrokipediaError as exc:
            return str(exc)
        if not hits:
            return f"No Grokipedia articles for '{query}'."
        lines = []
        for h in hits:
            views = f" · {h.view_count:,} views" if h.view_count else ""
            lines.append(f"- {h.title}  [slug: {h.slug}]{views}\n  {h.snippet}")
        body = (
            "Grokipedia articles (pass a slug to grokipedia action=outline / section / "
            "citations):\n" + "\n".join(lines)
        )
        sources = tuple(WebSource(url=_page_url(h.slug), title=h.title) for h in hits)
        return ToolOutput(body, web_sources=sources)

    async def grokipedia_outline_tool(arguments: dict, ctx: ToolContext) -> str:
        slug = str(arguments.get("slug", "")).strip()
        if not slug:
            return (
                "grokipedia(action=outline) needs a slug of a Grokipedia article"
                " (from action=search)."
            )
        if emit:
            emit("web_fetch", _page_url(slug))
        try:
            article = await client.fetch_article(slug)
        except GrokipediaError as exc:
            return str(exc)
        header = _article_header(article)
        if not article.outline:
            lead = article.content.strip()[:1500]
            return ToolOutput(
                f"{header}This article has no section headings.\n\n{lead}",
                web_sources=(_page_source(article),),
            )
        lines = [f"{'  ' * (s.level - 1)}{s.number} {s.title}" for s in article.outline]
        body = (
            f"{header}Outline — {len(article.outline)} sections. Read one with "
            "grokipedia(action=section, slug, section=<number or title>):\n\n" + "\n".join(lines)
        )
        return ToolOutput(body, web_sources=(_page_source(article),))

    async def grokipedia_section_tool(arguments: dict, ctx: ToolContext) -> str:
        slug = str(arguments.get("slug", "")).strip()
        section_ref = str(arguments.get("section", "")).strip()
        if not slug or not section_ref:
            return (
                "grokipedia(action=section) needs a slug and a section (a number or title "
                "from action=outline)."
            )
        detailed = str(arguments.get("response_format", "")).strip().lower() == "detailed"
        if emit:
            emit("web_fetch", _page_url(slug))
        try:
            article = await client.fetch_article(slug)
        except GrokipediaError as exc:
            return str(exc)
        found = article.section(section_ref)
        if found is None:
            available = ", ".join(f"{s.number} {s.title}" for s in article.outline[:30]) or "none"
            return (
                f"No section {section_ref!r} in '{article.title}'. Available sections: "
                f"{available}. Call grokipedia(action=outline) for the full list."
            )
        section, text = found
        suffix = ""
        if len(text) > _MAX_SECTION_CHARS:
            text = text[:_MAX_SECTION_CHARS]
            suffix = (
                "\n\n[Section truncated — read its sub-sections (action=outline) or its "
                "sources (action=citations) for the rest.]"
            )
        head = f"{_page_url(article.slug)} · section {section.number} {section.title}"
        body = f"{head}\n\n{text}{suffix}"
        web_sources = [_page_source(article)]
        if detailed:
            cites = _section_citations(article, text)
            if cites:
                body += "\n\nSources cited in this section:\n" + _citation_lines(cites)
                web_sources += [
                    WebSource(url=c.url, title=c.title or c.publisher or c.url) for c in cites
                ]
            links = [r for r in article.related if r.slug in text]
            if links:
                body += "\n\nLinked articles:\n" + "\n".join(
                    f"- {r.title}  [slug: {r.slug}]" for r in links
                )
        return ToolOutput(body, web_sources=tuple(web_sources))

    async def grokipedia_citations_tool(arguments: dict, ctx: ToolContext) -> str:
        slug = str(arguments.get("slug", "")).strip()
        if not slug:
            return "grokipedia(action=citations) needs the slug of a Grokipedia article."
        section_ref = str(arguments.get("section", "")).strip()
        want = _coerce_int(arguments.get("limit"), _DEFAULT_CITATION_LIMIT)
        limit = max(1, min(want, _MAX_CITATION_LIMIT))
        if emit:
            emit("web_fetch", _page_url(slug))
        try:
            article = await client.fetch_article(slug)
        except GrokipediaError as exc:
            return str(exc)
        cites = article.citations
        scope = article.title
        if section_ref:
            found = article.section(section_ref)
            if found is None:
                return (
                    f"No section {section_ref!r} in '{article.title}'. Call"
                    " grokipedia(action=outline) for the list."
                )
            section, text = found
            scoped = _section_citations(article, text)
            if scoped:
                cites, scope = scoped, f"{article.title} · {section.number} {section.title}"
            else:
                # No inline markers to isolate the section — fall back to the whole article.
                scope = f"{article.title} (couldn't isolate section {section.number}; all sources)"
        if not cites:
            return f"No citations found for {scope}."
        shown = cites[:limit]
        body = (
            f"Citations for {scope} ({len(shown)} of {len(cites)}). web_fetch a url to read the "
            "primary source:\n" + _citation_lines(shown)
        )
        sources = tuple(WebSource(url=c.url, title=c.title or c.publisher or c.url) for c in shown)
        return ToolOutput(body, web_sources=sources)

    async def grokipedia_related_tool(arguments: dict, ctx: ToolContext) -> str:
        slug = str(arguments.get("slug", "")).strip()
        if not slug:
            return "grokipedia(action=related) needs the slug of a Grokipedia article."
        limit = max(1, _coerce_int(arguments.get("limit"), _DEFAULT_RELATED_LIMIT))
        if emit:
            emit("web_fetch", _page_url(slug))
        try:
            article = await client.fetch_article(slug)
        except GrokipediaError as exc:
            return str(exc)
        if not article.related:
            return f"No linked articles found on '{article.title}'."
        shown = article.related[:limit]
        body = (
            f"Articles linked from '{article.title}' (pass a slug to grokipedia action=outline / "
            "section):\n" + "\n".join(f"- {r.title}  [slug: {r.slug}]" for r in shown)
        )
        return ToolOutput(
            body, web_sources=tuple(WebSource(url=_page_url(r.slug), title=r.title) for r in shown)
        )

    _actions: dict[str, ToolHandler] = {
        "search": grokipedia_search_tool,
        "outline": grokipedia_outline_tool,
        "section": grokipedia_section_tool,
        "citations": grokipedia_citations_tool,
        "related": grokipedia_related_tool,
    }

    async def grokipedia_tool(arguments: dict, ctx: ToolContext) -> str | ToolOutput:
        action = str(arguments.get("action", "")).strip().lower()
        fn = _actions.get(action)
        if fn is None:
            return (
                "grokipedia needs action= one of search, outline, section, citations, related"
                f" (got {action or 'nothing'!r})."
            )
        return await fn(arguments, ctx)

    return {"grokipedia": grokipedia_tool}
