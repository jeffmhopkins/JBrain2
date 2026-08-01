"""The jerv chatbot's internet tools: `web_search` and `web_fetch`
(docs/reference/ASSISTANT.md "Agent selection").

Unlike the egress connectors (which stage an owner-approved Proposal before any
off-box call), these run DIRECTLY — the deliberate, bounded exception to
invariant #9. The bound is the sandbox: only the jerv agent allowlists them, and
jerv holds no knowledge-base tools and reads no owner domain data, so no personal
context can ride along into a query or a fetched URL. The handlers are thin over
the on-box SearXNG client and the URL fetcher; they surface no NoteSources (a web
result is not an owner note).
"""

from urllib.parse import urlsplit, urlunsplit

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.web.fetch import WebFetcher, WebFetchError
from jbrain.web.search import SearxngClient, WebSearchError

_MAX_LIMIT = 10


def _fetch_key(url: str) -> str:
    """A per-turn dedup key so a re-fetch of the SAME page — bar a fragment, a trailing
    slash, or host casing — is recognized as the URL that already failed this turn. A
    genuinely different path or query keeps a distinct key (a `_(2026)` variant is not the
    base article), so only an exact repeat is short-circuited, never a real alternative."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), parts.query, ""))
    except ValueError:
        return url.strip().lower()


def build_web_handlers(
    search: SearxngClient,
    fetcher: WebFetcher,
    emit: BrainEmit | None = None,
) -> dict[str, ToolHandler]:
    """`emit(kind, text)`, if given, fires a best-effort wall-display tendril event the
    moment jerv reaches out to the web (see jbrain.agent.brainevents). The query / URL
    text rides the tendril only when the turn opted into text streaming; otherwise the
    marker is content-free."""

    async def web_search_tool(arguments: dict, ctx: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "web_search needs a non-empty query."
        limit = max(1, min(int(arguments.get("limit", 6) or 6), _MAX_LIMIT))
        if emit:
            emit("web_search", query)
        try:
            hits = await search.search(query, limit)
        except WebSearchError as exc:
            return str(exc)
        if not hits:
            return f"No web results for '{query}'."
        lines = [f"- {h.title}\n  {h.url}\n  {h.snippet}" for h in hits]
        # The structured twin of the text: one citation source per hit, in the same
        # order the model reads them, so a `[^n]` marker resolves to a real URL the
        # search reached (and a favicon chip), never to a string the model invents.
        web_sources = tuple(WebSource(url=h.url, title=h.title) for h in hits)
        return ToolOutput("Web results:\n" + "\n".join(lines), web_sources=web_sources)

    async def web_fetch_tool(arguments: dict, ctx: ToolContext) -> str:
        url = str(arguments.get("url", "")).strip()
        if not url:
            return "web_fetch needs a url."
        # Break the re-fetch loop: a URL that already failed this turn (a 404 the model
        # keeps reconstructing, a bot-wall) will keep failing, so refuse it without a
        # network call and point at web_search instead of burning the budget on it.
        key = _fetch_key(url)
        prior = ctx.failed_fetches.get(key)
        if prior is not None:
            status = f"HTTP {prior}" if prior else "an error"
            return (
                f"You already fetched {url} this turn and it returned {status}. It will keep"
                " failing — do not fetch it again. If it was a 404 the page does not exist at"
                " that URL; web_search for the correct page (or a different source) instead."
            )
        if emit:
            emit("web_fetch", url)
        try:
            result = await fetcher.fetch(url)
        except WebFetchError as exc:
            # Remember the miss so an identical re-fetch this turn short-circuits above.
            ctx.failed_fetches[key] = exc.status or 0
            return str(exc)
        if not result.text:
            return f"That page ({url}) had no readable text."
        header = f"# {result.title}\n{result.url}\n\n" if result.title else f"{result.url}\n\n"
        body = header + result.text
        if result.links:
            # The page's outbound links, so the model can navigate by fetching one
            # rather than stopping at this page (web_fetch any of them to follow it).
            links = "\n".join(f"- {u}" for u in result.links)
            body += f"\n\nLinks on this page (web_fetch any of these to follow it):\n{links}"
        if result.truncated:
            # The page was clipped to fit the size caps — the model is seeing only its
            # head. Say so explicitly so it does not answer as though it read the whole
            # page (a long list's tail, the most recent rows, is what gets dropped).
            body += (
                "\n\n[This page was truncated — you are seeing only the beginning. If what"
                " you need isn't above (e.g. the most recent / last rows of a long list or"
                " table), it may be further down: fetch a more specific URL or section, or"
                " web_search for the exact item, rather than answering from this excerpt.]"
            )
        # The fetched page is itself a citable source — title from the page, url the
        # FINAL url after redirects (what the favicon + link should point at).
        source = WebSource(url=result.url, title=result.title or result.url)
        return ToolOutput(body, web_sources=(source,))

    return {"web_search": web_search_tool, "web_fetch": web_fetch_tool}
