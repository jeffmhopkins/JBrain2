"""The archivist persona's Gmail tools (docs/archive/EMAIL_ARCHIVIST_PLAN.md).

Like jerv's web tools (`jbrain.agent.webtools`), these are the `web` permission class
and run DIRECTLY — the owner-authorized widening of invariant #9 from "public reads
with no owner data" to a single owner-configured Gmail account. Each handler is thin
over the `GmailApi` client; the archivist is allowlisted to exactly these tools and
reads no knowledge base, so no owner note/entity data rides along. Reads return Gmail
content as DATA (the model treats it as such, never as instructions); the three writes
(create_label / label / archive) act only on the owner's own mailbox and never delete.

Reading is deliberately cheap. A body is rendered to clean text before the model sees it
(`jbrain.gmail.body`), `gmail_read` windows it and can keyword-jump or regex-extract from
it instead of dumping it whole, and `gmail_extract` runs ONE regex across many messages in
a single call — so "what did each of this week's deliveries cost" costs one bounded reply
rather than a full HTML body per message.
"""

import asyncio
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from email.utils import parseaddr

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.gmail import GmailApi, GmailError, GmailMessage
from jbrain.gmail.body import render_body

# The windowing and regex-extraction engines jerv reads a web page with. They are pure text
# functions that happen to live beside the fetcher; reusing them means an email pages,
# keyword-jumps and extracts with exactly the semantics (and the offsets) a page does,
# rather than growing a second, subtly different implementation here.
from jbrain.web.fetch import extract_matches, window_text

_SEARCH_DEFAULT = 25
_SEARCH_MAX = 100
_BREAKDOWN_DEFAULT = 200
_BREAKDOWN_MAX = 500
_BREAKDOWN_TOP = 20
# One window of an email body. Far smaller than a web page's 30k: a rendered email is
# usually a few thousand characters, so this only ever binds on a genuine monster (a long
# statement, a deep quoted thread) — exactly the case where dumping the whole thing into
# the context is the wrong default. The tail stays reachable with `offset`.
_READ_WINDOW = 12_000
# Per-message caps for the extract paths. The point of extracting is to spend a fraction of
# what reading costs, so a runaway pattern is capped and the TRUE total still reported.
_READ_EXTRACT_MATCHES = 40
_SCAN_EXTRACT_MATCHES = 6
_SCAN_CONTEXT = 50
# gmail_extract's message budget: enough to answer "every delivery this week" in one call,
# bounded so a wide query can't fetch hundreds of bodies.
_SCAN_DEFAULT = 10
_SCAN_MAX = 25
# Bodies are fetched concurrently in small chunks, like the sender breakdown's metadata
# reads — one slow id-at-a-time loop over 25 messages is a visibly stalled turn.
_SCAN_FETCH_CHUNK = 5


def _bad_regex(pattern: str) -> str:
    """The error text for a `find` that will not compile, or "" when it is fine. Checked
    before any Gmail call, so a typo'd pattern costs nothing and says what to fix."""
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"That regex doesn't compile: {exc}. Fix the pattern and call again."
    return ""


def _headers(msg: GmailMessage) -> str:
    return f"From: {msg.sender}\nTo: {msg.to}\nDate: {msg.date}\nSubject: {msg.subject}"


def _present_matches(text: str, pattern: str, *, max_matches: int, context: int) -> str:
    """Every regex match in one body as a compact numbered list — the match, its capture
    groups, and a little surrounding context — instead of the body itself."""
    matches, total = extract_matches(text, pattern, max_matches=max_matches, context=context)
    if not total:
        return f"[no match for regex '{pattern}' in this message ({len(text)} chars)]"
    lines = []
    for i, m in enumerate(matches, 1):
        line = f"{i}. {m.match}"
        if m.groups:
            line += "  [groups: " + " | ".join(m.groups) + "]"
        lines.append(line + f"\n   …{m.context}…  (offset {m.offset})")
    body = "\n".join(lines)
    if total > len(matches):
        body += f"\n[+{total - len(matches)} more match(es) not shown — narrow the pattern.]"
    return body


# Resolves the live Gmail client per call (credentials come from the settings panel,
# so they can change without a restart). Raises GmailError when Gmail isn't connected
# yet — each handler catches it and surfaces the "connect in Settings" message.
GmailClientGetter = Callable[[], Awaitable[GmailApi]]


def build_gmail_handlers(get_client: GmailClientGetter) -> dict[str, ToolHandler]:
    """One handler per gmail_* tool, each resolving the live client on every call."""

    async def gmail_search(arguments: dict, ctx: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "gmail_search needs a non-empty query."
        raw_limit = arguments.get("limit", _SEARCH_DEFAULT) or _SEARCH_DEFAULT
        limit = max(1, min(int(raw_limit), _SEARCH_MAX))
        try:
            client = await get_client()
            ids = await client.search(query, max_results=limit)
            if not ids:
                return f"No Gmail messages match '{query}'."
            rows = []
            for mid in ids:
                msg = await client.get(mid, metadata_only=True)
                rows.append(
                    f"- [{msg.id}] {msg.date} — from {msg.sender}\n  {msg.subject}\n  {msg.snippet}"
                )
        except GmailError as exc:
            return str(exc)
        return f"{len(rows)} message(s) for '{query}':\n" + "\n".join(rows)

    async def gmail_read(arguments: dict, ctx: ToolContext) -> str:
        message_id = str(arguments.get("message_id", "")).strip()
        if not message_id:
            return "gmail_read needs a message_id."
        find = str(arguments.get("find", "") or "").strip()
        extract = bool(arguments.get("extract"))
        use_regex = bool(arguments.get("regex")) or extract
        try:
            offset = max(0, int(arguments.get("offset", 0) or 0))
        except (TypeError, ValueError):
            offset = 0
        if extract and not find:
            return "gmail_read with extract=true needs `find` — the regex to pull out."
        if find and use_regex and (bad := _bad_regex(find)):
            return bad
        try:
            client = await get_client()
            msg = await client.get(message_id)
        except GmailError as exc:
            return str(exc)
        # The body as clean text — HTML rendered to markdown, tracking URLs collapsed. Reading
        # a modern email raw costs thousands of tokens of layout markup the model then has to
        # see past; this is the same pass the triage sweep classifies on.
        text = render_body(msg)
        header = _headers(msg) + "\n\n"
        if not text:
            return header + "(no readable body)"
        if extract:
            return (
                header
                + f"[Extracting regex '{find}' from this message ({len(text)} chars).]\n"
                + _present_matches(
                    text, find, max_matches=_READ_EXTRACT_MATCHES, context=_SCAN_CONTEXT
                )
            )
        result = window_text(
            text,
            url="",
            title=msg.subject,
            offset=offset,
            find=find,
            find_regex=use_regex,
            window=_READ_WINDOW,
        )
        if find and not result.match_count:
            kind = "regex" if use_regex else "'find'"
            return (
                header + f"[No match for {kind} '{find}' in this message ({len(text)} chars). Call"
                " gmail_read again without `find` to read it, or try another term.]"
            )
        notes = []
        if find and result.match_count:
            notes.append(
                f"[found {result.match_count} match(es) for '{find}'; window at offset"
                f" {result.offset}"
                + (
                    " — others at " + ", ".join(str(o) for o in result.match_offsets[1:6])
                    if len(result.match_offsets) > 1
                    else ""
                )
                + "]"
            )
        end = result.offset + len(result.text)
        if end < result.total_chars:
            notes.append(
                f"[Showing chars {result.offset}–{end} of {result.total_chars}. Call gmail_read"
                f' again with message_id="{message_id}" and offset={end} for the rest — or'
                ' pass find="<term>" to jump straight to what you need.]'
            )
        elif not result.text:
            notes.append(f"[Nothing at offset {offset}: this message is {len(text)} chars.]")
        body = result.text
        return header + (body + ("\n\n" + "\n".join(notes) if notes else "")).strip()

    async def gmail_extract(arguments: dict, ctx: ToolContext) -> str:
        """Run one regex across the bodies of the messages a query matches — the answer to
        "what did each of these cost / when is each appointment" in ONE call, instead of a
        gmail_read (and a full body in the context) per message."""
        query = str(arguments.get("query", "")).strip()
        find = str(arguments.get("find", "")).strip()
        if not query:
            return "gmail_extract needs a non-empty query."
        if not find:
            return "gmail_extract needs `find` — the regex to pull out of each message."
        if bad := _bad_regex(find):
            return bad
        try:
            limit = max(
                1, min(int(arguments.get("limit", _SCAN_DEFAULT) or _SCAN_DEFAULT), _SCAN_MAX)
            )
        except (TypeError, ValueError):
            limit = _SCAN_DEFAULT
        try:
            client = await get_client()
            ids = await client.search(query, max_results=limit)
            if not ids:
                return f"No Gmail messages match '{query}'."
            msgs: list[GmailMessage] = []
            for start in range(0, len(ids), _SCAN_FETCH_CHUNK):
                chunk = ids[start : start + _SCAN_FETCH_CHUNK]
                msgs.extend(await asyncio.gather(*(client.get(mid) for mid in chunk)))
        except GmailError as exc:
            return str(exc)
        blocks: list[str] = []
        empty: list[str] = []
        hits = 0
        for msg in msgs:
            text = render_body(msg)
            matches, total = extract_matches(
                text, find, max_matches=_SCAN_EXTRACT_MATCHES, context=_SCAN_CONTEXT
            )
            if not total:
                empty.append(msg.id)
                continue
            hits += 1
            lines = [
                f"  - {m.match}"
                + ("  [groups: " + " | ".join(m.groups) + "]" if m.groups else "")
                + f"  …{m.context}…"
                for m in matches
            ]
            more = (
                f"\n  (+{total - len(matches)} more in this message)"
                if total > len(matches)
                else ""
            )
            blocks.append(
                f"- [{msg.id}] {msg.date} — from {msg.sender}\n  {msg.subject}\n"
                + "\n".join(lines)
                + more
            )
        head = (
            f"Regex '{find}' across the {len(msgs)} message(s) matching '{query}' —"
            f" {hits} with a match:"
        )
        tail = ""
        if empty:
            tail = f"\n\n[No match in {len(empty)}: " + ", ".join(empty[:10])
            tail += ", …]" if len(empty) > 10 else "]"
        if len(ids) >= limit:
            tail += (
                f"\n[Scanned the {limit} most recent matches — run gmail_count on the query to"
                " see whether more exist, and narrow it or raise `limit` to cover them.]"
            )
        if not blocks:
            return (
                f"No match for regex '{find}' in any of the {len(msgs)} message(s) matching"
                f" '{query}'. Check the pattern (it is case-insensitive over the whole body),"
                " or gmail_read one of them to see the actual wording." + tail
            )
        return head + "\n" + "\n".join(blocks) + tail

    async def gmail_list_labels(arguments: dict, ctx: ToolContext) -> str:
        try:
            client = await get_client()
            labels = await client.list_labels()
        except GmailError as exc:
            return str(exc)
        if not labels:
            return "No labels exist yet."
        names = sorted(label.name for label in labels)
        return "Labels:\n" + "\n".join(f"- {name}" for name in names)

    async def gmail_create_label(arguments: dict, ctx: ToolContext) -> str:
        name = str(arguments.get("name", "")).strip()
        if not name:
            return "gmail_create_label needs a name."
        try:
            client = await get_client()
            label = await client.create_label(name)
        except GmailError as exc:
            return str(exc)
        return f"Label '{label.name}' is ready to use."

    async def gmail_label(arguments: dict, ctx: ToolContext) -> str:
        message_id = str(arguments.get("message_id", "")).strip()
        if not message_id:
            return "gmail_label needs a message_id."
        add = [str(x).strip() for x in (arguments.get("add") or []) if str(x).strip()]
        remove = [str(x).strip() for x in (arguments.get("remove") or []) if str(x).strip()]
        if not add and not remove:
            return "gmail_label needs at least one label to add or remove."
        try:
            client = await get_client()
            by_name = {label.name: label.id for label in await client.list_labels()}
            missing = [n for n in add if n not in by_name]
            if missing:
                return (
                    "These labels don't exist yet: "
                    + ", ".join(missing)
                    + ". Create them with gmail_create_label first — I won't invent labels."
                )
            removed = [n for n in remove if n in by_name]
            await client.modify(
                message_id,
                add_label_ids=[by_name[n] for n in add],
                remove_label_ids=[by_name[n] for n in removed],
            )
        except GmailError as exc:
            return str(exc)
        done = []
        if add:
            done.append("applied " + ", ".join(add))
        if removed:
            done.append("removed " + ", ".join(removed))
        return f"Message {message_id}: " + "; ".join(done) + "."

    async def gmail_archive(arguments: dict, ctx: ToolContext) -> str:
        message_id = str(arguments.get("message_id", "")).strip()
        if not message_id:
            return "gmail_archive needs a message_id."
        try:
            client = await get_client()
            await client.modify(message_id, remove_label_ids=["INBOX"])
        except GmailError as exc:
            return str(exc)
        return f"Message {message_id} archived — out of the inbox, still in All Mail."

    async def gmail_count(arguments: dict, ctx: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "gmail_count needs a non-empty query."
        try:
            client = await get_client()
            total, capped = await client.count(query)
        except GmailError as exc:
            return str(exc)
        if capped:
            return f"At least {total:,} messages match '{query}' (stopped counting at the cap)."
        return f"{total:,} message(s) match '{query}'."

    async def gmail_sender_breakdown(arguments: dict, ctx: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return (
                "gmail_sender_breakdown needs a non-empty query"
                " (use in:anywhere to cover the whole mailbox)."
            )
        by = str(arguments.get("by", "domain")).strip().lower()
        if by not in ("domain", "address"):
            by = "domain"
        try:
            sample = max(1, min(int(arguments.get("sample", _BREAKDOWN_DEFAULT)), _BREAKDOWN_MAX))
        except (TypeError, ValueError):
            sample = _BREAKDOWN_DEFAULT
        try:
            client = await get_client()
            froms, capped = await client.sender_sample(query, sample=sample)
        except GmailError as exc:
            return str(exc)
        if not froms:
            return f"No messages match '{query}' to break down."
        counts: Counter[str] = Counter()
        for frm in froms:
            addr = parseaddr(frm)[1].lower()
            if "@" not in addr:
                key = addr or "(unknown sender)"
            else:
                key = addr.rsplit("@", 1)[-1] if by == "domain" else addr
            counts[key] += 1
        rows = [f"- {key} — {n}" for key, n in counts.most_common(_BREAKDOWN_TOP)]
        head = f"Top {by}s across {len(froms)} sampled message(s) for '{query}':"
        note = ""
        if capped:
            note = (
                f"\nNOTE: this is the {len(froms)} most recent of more matches — the busiest"
                " among recent mail, not a full-history tally. Confirm an exact per-sender"
                " total with gmail_count before a bulk move."
            )
        return f"{head}\n" + "\n".join(rows) + note

    async def gmail_bulk_label(arguments: dict, ctx: ToolContext) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return "gmail_bulk_label needs a non-empty query."
        add = [str(x).strip() for x in (arguments.get("add") or []) if str(x).strip()]
        remove = [str(x).strip() for x in (arguments.get("remove") or []) if str(x).strip()]
        if not add and not remove:
            return "gmail_bulk_label needs at least one label to add or remove."
        try:
            client = await get_client()
            by_name = {label.name: label.id for label in await client.list_labels()}
            missing = [n for n in add if n not in by_name]
            if missing:
                return (
                    "These labels don't exist yet: "
                    + ", ".join(missing)
                    + ". Create them with gmail_create_label first — I won't invent labels."
                )
            ids, capped = await client.search_all(query)
            if not ids:
                return f"No messages match '{query}' — nothing changed."
            removed = [n for n in remove if n in by_name]
            await client.batch_modify(
                ids,
                add_label_ids=[by_name[n] for n in add],
                remove_label_ids=[by_name[n] for n in removed],
            )
        except GmailError as exc:
            return str(exc)
        done = []
        if add:
            done.append("applied " + ", ".join(add))
        if removed:
            done.append("removed " + ", ".join(removed))
        result = f"Bulk-updated {len(ids):,} message(s) for '{query}': " + "; ".join(done) + "."
        if capped:
            result += (
                f" NOTE: more than {len(ids):,} matched — only the first {len(ids):,} were"
                " changed. Narrow the query and run again for the rest."
            )
        return result

    return {
        "gmail_search": gmail_search,
        "gmail_read": gmail_read,
        "gmail_extract": gmail_extract,
        "gmail_list_labels": gmail_list_labels,
        "gmail_create_label": gmail_create_label,
        "gmail_label": gmail_label,
        "gmail_archive": gmail_archive,
        "gmail_count": gmail_count,
        "gmail_sender_breakdown": gmail_sender_breakdown,
        "gmail_bulk_label": gmail_bulk_label,
    }
