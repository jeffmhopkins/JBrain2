"""The agent's list tools: read and maintain the owner's lists.

Lists are user-managed structured records (docs/reference/ARCHITECTURE.md "Lists"), not
citable truth — so unlike `propose_correction`, these write **directly** under
the session's RLS scope (the firewall is Postgres, the memory-scratchpad
category, invariant #7). Every handler runs on `ToolContext.session`, so a
narrowed session only ever touches in-scope lists. Ids ride in the tool's
model-facing text so the model can chain (create → add → check), but the prose
the model shows the owner shouldn't paste them — the app renders the checklist.
"""

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.lists.service import ListInfo, ListsRepo, UnknownDomain


def format_lists(lists: list[ListInfo]) -> str:
    """The model-facing index — title, domain, open/total counts, and id."""
    if not lists:
        return "No lists yet."
    return "\n".join(
        f"- {lst.title} [{lst.domain}] ({lst.open_count}/{lst.total_count} open) id={lst.id}"
        for lst in lists
    )


def format_list(lst: ListInfo) -> str:
    """One list with its items — a checkbox per line and the item id to act on."""
    head = f"{lst.title} [{lst.domain}]"
    if not lst.items:
        return f"{head}\n(empty)"
    lines = [f"{'[x]' if i.checked else '[ ]'} {i.body} id={i.id}" for i in lst.items]
    return head + "\n" + "\n".join(lines)


def list_card(lst: ListInfo) -> ViewPayload:
    """The structured twin of format_list: a `list_card` the PWA renders as a
    tappable checklist (data-only slots, never model-authored markup)."""
    return ViewPayload(
        view="list_card",
        surface="inline",
        data={
            "list_id": lst.id,
            "title": lst.title,
            "domain": lst.domain,
            "items": [{"id": i.id, "body": i.body, "checked": i.checked} for i in lst.items],
        },
    )


def build_list_handlers(lists: ListsRepo) -> dict[str, ToolHandler]:
    async def read_lists_tool(arguments: dict, ctx: ToolContext) -> str:
        include_archived = bool(arguments.get("include_archived", False))
        rows = await lists.list_lists(ctx.session, include_archived=include_archived)
        return format_lists(rows)

    async def read_list_tool(arguments: dict, ctx: ToolContext) -> str:
        list_id = str(arguments.get("list_id", "")).strip()
        if not list_id:
            return "read_list needs a list_id."
        info = await lists.get_list(ctx.session, list_id)
        if info is None:
            return "No list with that id is in scope."
        # The text is the model's; the card is the owner's tappable checklist.
        return ToolOutput(format_list(info), view=list_card(info))

    async def create_list_tool(arguments: dict, ctx: ToolContext) -> str:
        title = str(arguments.get("title", "")).strip()
        if not title:
            return "create_list needs a title."
        domain = str(arguments.get("domain", "")).strip() or (
            ctx.scopes[0] if ctx.scopes else "general"
        )
        # You can only make a list in a domain this session can read.
        if ctx.scopes and domain not in ctx.scopes:
            return f"can't make a list in '{domain}' — this session isn't scoped to it."
        try:
            info = await lists.create_list(ctx.session, domain=domain, title=title)
        except UnknownDomain:
            return f"'{domain}' isn't a real domain — use general, health, finance, or location."
        return f"Created list '{info.title}' [{info.domain}] id={info.id}."

    async def add_list_item_tool(arguments: dict, ctx: ToolContext) -> str:
        list_id = str(arguments.get("list_id", "")).strip()
        body = str(arguments.get("body", "")).strip()
        if not list_id or not body:
            return "add_list_item needs a list_id and a body."
        item = await lists.add_item(ctx.session, list_id, body)
        if item is None:
            return "No list with that id is in scope."
        return f"Added '{item.body}' id={item.id}."

    async def check_list_item_tool(arguments: dict, ctx: ToolContext) -> str:
        item_id = str(arguments.get("item_id", "")).strip()
        if not item_id:
            return "check_list_item needs an item_id."
        checked = bool(arguments.get("checked", True))
        item = await lists.set_item_checked(ctx.session, item_id, checked=checked)
        if item is None:
            return "No item with that id is in scope."
        return f"{'Checked off' if item.checked else 'Reopened'} '{item.body}'."

    async def remove_list_item_tool(arguments: dict, ctx: ToolContext) -> str:
        item_id = str(arguments.get("item_id", "")).strip()
        if not item_id:
            return "remove_list_item needs an item_id."
        removed = await lists.remove_item(ctx.session, item_id)
        return "Removed the item." if removed else "No item with that id is in scope."

    return {
        "read_lists": read_lists_tool,
        "read_list": read_list_tool,
        "create_list": create_list_tool,
        "add_list_item": add_list_item_tool,
        "check_list_item": check_list_item_tool,
        "remove_list_item": remove_list_item_tool,
    }
