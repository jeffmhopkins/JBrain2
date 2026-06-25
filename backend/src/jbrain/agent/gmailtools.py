"""The archivist persona's Gmail tools (docs/EMAIL_ARCHIVIST_PLAN.md).

Like jerv's web tools (`jbrain.agent.webtools`), these are the `web` permission class
and run DIRECTLY — the owner-authorized widening of invariant #9 from "public reads
with no owner data" to a single owner-configured Gmail account. Each handler is thin
over the `GmailApi` client; the archivist is allowlisted to exactly these tools and
reads no knowledge base, so no owner note/entity data rides along. Reads return Gmail
content as DATA (the model treats it as such, never as instructions); the three writes
(create_label / label / archive) act only on the owner's own mailbox and never delete.
"""

from collections import Counter
from collections.abc import Awaitable, Callable
from email.utils import parseaddr

from jbrain.agent.loop import ToolContext, ToolHandler
from jbrain.gmail import GmailApi, GmailError

_SEARCH_DEFAULT = 25
_SEARCH_MAX = 100
_BREAKDOWN_DEFAULT = 200
_BREAKDOWN_MAX = 500
_BREAKDOWN_TOP = 20

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
        try:
            client = await get_client()
            msg = await client.get(message_id)
        except GmailError as exc:
            return str(exc)
        header = f"From: {msg.sender}\nTo: {msg.to}\nDate: {msg.date}\nSubject: {msg.subject}\n\n"
        return header + (msg.body or msg.snippet or "(no readable body)")

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
        "gmail_list_labels": gmail_list_labels,
        "gmail_create_label": gmail_create_label,
        "gmail_label": gmail_label,
        "gmail_archive": gmail_archive,
        "gmail_count": gmail_count,
        "gmail_sender_breakdown": gmail_sender_breakdown,
        "gmail_bulk_label": gmail_bulk_label,
    }
