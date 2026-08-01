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


def _coerce_offset(raw: object) -> int:
    """The `offset` argument as a non-negative int — the model may send an int, a numeric
    string, or nothing. A bad value degrades to 0 (start of the page), never an error."""
    try:
        return max(0, int(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


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
        offset = _coerce_offset(arguments.get("offset"))
        find = str(arguments.get("find", "")).strip()
        # Break the re-fetch loop: a URL that already failed this turn (a 404 the model
        # keeps reconstructing, a bot-wall) will keep failing, so refuse it without a
        # network call and point at web_search instead of burning the budget on it. Keyed
        # by URL only (offset-independent) — a dead page is dead at any offset.
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
            result = await fetcher.fetch(url, offset=offset, find=find)
        except WebFetchError as exc:
            # Remember the miss so an identical re-fetch this turn short-circuits above.
            ctx.failed_fetches[key] = exc.status or 0
            return str(exc)
        # A `find` that matched nothing: don't dump an irrelevant window — tell the model the
        # term isn't on the page (with its length) so it retries a different term or reads plainly.
        if find and not result.match_offsets:
            return (
                f"No match for '{find}' in {result.url} ({result.total_chars} chars). Try a"
                " different term (check spelling/phrasing), or web_fetch with offset=0 to read"
                " from the top."
            )
        if not result.text:
            # An empty window at offset>0 means the model paged past the end — a normal
            # stop, not a dead page — so say so with the real length instead of "no text".
            if offset > 0 and result.total_chars:
                return (
                    f"No more content at offset {offset}: {result.url} has"
                    f" {result.total_chars} characters, which you've already read past."
                )
            return f"That page ({url}) had no readable text."
        header = f"# {result.title}\n{result.url}\n\n" if result.title else f"{result.url}\n\n"
        if find and result.match_offsets:
            # Jumped straight to the keyword — say where, so the model knows this window is the
            # matched SECTION (positioned at the first hit), not the top of the page.
            header += (
                f"[found {result.match_count} match(es) for '{find}'; window positioned at the"
                f" first, near offset {result.match_offsets[0]}]\n\n"
            )
        elif offset:
            header += f"[continued from offset {offset} of {result.total_chars} chars]\n\n"
        body = header + result.text
        # Links only on the first page — they don't change across windows, and repeating
        # the whole list on every continuation is noise.
        if result.links and offset == 0:
            links = "\n".join(f"- {u}" for u in result.links)
            body += f"\n\nLinks on this page (web_fetch any of these to follow it):\n{links}"
        next_offset = result.offset + len(result.text)
        if next_offset < result.total_chars:
            # More text remains below this window — tell the model the exact next call so a
            # long list's tail (the most recent rows) is fetchable, not just flagged as dropped.
            remaining = result.total_chars - next_offset
            body += (
                f"\n\n[Truncated: showing chars {result.offset}–{next_offset} of"
                f" {result.total_chars}; {remaining} more remain below. To read the rest, call"
                f" web_fetch again with the SAME url and offset={next_offset} (repeat, advancing"
                " the offset, until you reach the end). Don't answer from this window alone if"
                " what you need — e.g. the last rows of a long list — may be further down.]"
            )
        elif result.truncated:
            # We reached the end of what we HAVE, but the raw download hit the byte cap, so the
            # real page is even longer than we could fetch — paging can't recover that tail.
            body += (
                "\n\n[This page was too large to download in full (over the size cap), so the"
                " end of it is not available here. If you need content past this point, look for"
                " a more specific URL/section or web_search for the exact item.]"
            )
        # When a term appears more than once, surface the other match offsets so the model can
        # jump to a specific later hit instead of paging there — the whole point of `find`.
        if find and result.match_count > 1:
            shown = ", ".join(str(o) for o in result.match_offsets)
            more = (
                f" (+{result.match_count - len(result.match_offsets)} more)"
                if result.match_count > len(result.match_offsets)
                else ""
            )
            body += (
                f"\n\n[Other matches for '{find}' at offsets: {shown}{more}. To read around a"
                " specific one, web_fetch the SAME url with offset set to that number.]"
            )
        # The fetched page is itself a citable source — title from the page, url the
        # FINAL url after redirects (what the favicon + link should point at).
        source = WebSource(url=result.url, title=result.title or result.url)
        return ToolOutput(body, web_sources=(source,))

    return {"web_search": web_search_tool, "web_fetch": web_fetch_tool}
