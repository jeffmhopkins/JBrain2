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

from jbrain.agent.brainevents import BrainEmit
from jbrain.agent.contracts import WebSource
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.web.fetch import WebFetcher, WebFetchError
from jbrain.web.search import SearxngClient, WebSearchError

_MAX_LIMIT = 10


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
        if emit:
            emit("web_fetch", url)
        try:
            result = await fetcher.fetch(url)
        except WebFetchError as exc:
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
        # The fetched page is itself a citable source — title from the page, url the
        # FINAL url after redirects (what the favicon + link should point at).
        source = WebSource(url=result.url, title=result.title or result.url)
        return ToolOutput(body, web_sources=(source,))

    return {"web_search": web_search_tool, "web_fetch": web_fetch_tool}
