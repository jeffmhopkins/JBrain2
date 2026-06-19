"""The jerv chatbot's internet tools: `web_search` and `web_fetch`
(docs/ASSISTANT.md "Agent selection").

Unlike the egress connectors (which stage an owner-approved Proposal before any
off-box call), these run DIRECTLY — the deliberate, bounded exception to
invariant #9. The bound is the sandbox: only the jerv agent allowlists them, and
jerv holds no knowledge-base tools and reads no owner domain data, so no personal
context can ride along into a query or a fetched URL. The handlers are thin over
the on-box SearXNG client and the URL fetcher; they surface no NoteSources (a web
result is not an owner note).
"""

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.web.fetch import WebFetcher, WebFetchError
from jbrain.web.search import SearxngClient, WebSearchError

_MAX_LIMIT = 10


def build_web_handlers(search: SearxngClient, fetcher: WebFetcher) -> dict[str, ToolHandler]:
    async def web_search_tool(arguments: dict, ctx: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "web_search needs a non-empty query."
        limit = max(1, min(int(arguments.get("limit", 6) or 6), _MAX_LIMIT))
        try:
            hits = await search.search(query, limit)
        except WebSearchError as exc:
            return str(exc)
        if not hits:
            return f"No web results for '{query}'."
        lines = [f"- {h.title}\n  {h.url}\n  {h.snippet}" for h in hits]
        return "Web results:\n" + "\n".join(lines)

    async def web_fetch_tool(arguments: dict, ctx: ToolContext) -> str:
        url = str(arguments.get("url", "")).strip()
        if not url:
            return "web_fetch needs a url."
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
        return body

    return {"web_search": web_search_tool, "web_fetch": web_fetch_tool}
