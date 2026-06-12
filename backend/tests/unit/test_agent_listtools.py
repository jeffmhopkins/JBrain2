"""The agent list tools: formatting, RLS-scope passthrough, the direct-write
mutations, and the scope/empty guards. The repo is faked (the real RLS firewall
is proven in tests/integration/test_lists_rls.py)."""

from datetime import UTC, datetime

from jbrain.agent.listtools import build_list_handlers, format_list, format_lists
from jbrain.agent.loop import ToolContext, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.lists.service import ListInfo, ListItemInfo, UnknownDomain

CTX = ToolContext(session=SessionContext(principal_kind="owner"), scopes=("general",))
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def item(item_id: str, body: str, *, checked: bool = False, pos: int = 0) -> ListItemInfo:
    return ListItemInfo(
        id=item_id, body=body, checked=checked, position=pos, source_note_id=None, created_at=NOW
    )


def lst(
    list_id: str = "L1",
    title: str = "Groceries",
    domain: str = "general",
    items: list[ListItemInfo] | None = None,
) -> ListInfo:
    return ListInfo(
        id=list_id,
        domain=domain,
        title=title,
        archived=False,
        created_at=NOW,
        updated_at=NOW,
        items=items or [],
    )


class FakeLists:
    """Records calls and returns canned rows — enough to assert the handlers
    forward the scope and shape their text."""

    def __init__(
        self,
        lists: list[ListInfo] | None = None,
        one: ListInfo | None = None,
        item_result: ListItemInfo | None = None,
        bad_domain: bool = False,
    ):
        self.lists = lists or []
        self.one = one
        self.item_result = item_result
        self.bad_domain = bad_domain
        self.calls: list[tuple] = []

    async def list_lists(self, ctx, *, include_archived=False):  # noqa: ANN001
        self.calls.append(("list_lists", ctx, include_archived))
        return self.lists

    async def get_list(self, ctx, list_id):  # noqa: ANN001
        self.calls.append(("get_list", ctx, list_id))
        return self.one if self.one is not None and list_id == self.one.id else None

    async def create_list(self, ctx, *, domain, title):  # noqa: ANN001
        self.calls.append(("create_list", ctx, domain, title))
        if self.bad_domain:
            raise UnknownDomain(domain)
        return lst(list_id="new", title=title, domain=domain)

    async def add_item(self, ctx, list_id, body, *, source_note_id=None):  # noqa: ANN001
        self.calls.append(("add_item", ctx, list_id, body))
        return self.item_result

    async def set_item_checked(self, ctx, item_id, *, checked):  # noqa: ANN001
        self.calls.append(("set_item_checked", ctx, item_id, checked))
        return self.item_result

    async def remove_item(self, ctx, item_id):  # noqa: ANN001
        self.calls.append(("remove_item", ctx, item_id))
        return self.item_result is not None


def handlers(fake: FakeLists):
    return build_list_handlers(fake)  # type: ignore[arg-type]


# --- formatting ----------------------------------------------------------


def test_format_lists_shows_counts_and_ids() -> None:
    out = format_lists([lst(items=[item("a", "eggs"), item("b", "milk", checked=True)])])
    assert "Groceries [general] (1/2 open) id=L1" in out


def test_format_lists_empty() -> None:
    assert format_lists([]) == "No lists yet."


def test_format_list_shows_checkboxes_and_item_ids() -> None:
    out = format_list(lst(items=[item("a", "eggs"), item("b", "milk", checked=True)]))
    assert "[ ] eggs id=a" in out and "[x] milk id=b" in out


# --- handlers ------------------------------------------------------------


async def test_read_lists_forwards_scope() -> None:
    fake = FakeLists(lists=[lst()])
    out = await handlers(fake)["read_lists"]({}, CTX)
    assert "Groceries" in out and fake.calls[0][0] == "list_lists"
    assert fake.calls[0][1] is CTX.session  # ran under the session's RLS scope


async def test_read_list_found_and_missing() -> None:
    found = await handlers(FakeLists(one=lst(items=[item("a", "eggs")])))["read_list"](
        {"list_id": "L1"}, CTX
    )
    assert "[ ] eggs" in found
    missing = await handlers(FakeLists(one=lst()))["read_list"]({"list_id": "other"}, CTX)
    assert "in scope" in missing


async def test_read_list_surfaces_a_list_card_view() -> None:
    one = lst(items=[item("a", "eggs"), item("b", "milk", checked=True)])
    out = await handlers(FakeLists(one=one))["read_list"]({"list_id": "L1"}, CTX)
    assert isinstance(out, ToolOutput)
    assert out.view is not None and out.view.view == "list_card"
    # Data-only slots the PWA's checklist renders — never model markup.
    assert out.view.data == {
        "list_id": "L1",
        "title": "Groceries",
        "domain": "general",
        "items": [
            {"id": "a", "body": "eggs", "checked": False},
            {"id": "b", "body": "milk", "checked": True},
        ],
    }


async def test_create_list_writes_and_defaults_domain() -> None:
    fake = FakeLists()
    out = await handlers(fake)["create_list"]({"title": "Packing"}, CTX)
    assert "Created list 'Packing' [general] id=new" in out
    # Domain defaulted to the session's first scope.
    assert fake.calls[0] == ("create_list", CTX.session, "general", "Packing")


async def test_create_list_rejects_out_of_scope_and_bad_domain() -> None:
    out_of_scope = await handlers(FakeLists())["create_list"](
        {"title": "x", "domain": "health"}, CTX
    )
    assert "isn't scoped to it" in out_of_scope
    bad = await handlers(FakeLists(bad_domain=True))["create_list"](
        {"title": "x", "domain": "general"}, CTX
    )
    assert "isn't a real domain" in bad


async def test_create_list_needs_a_title() -> None:
    assert "needs a title" in await handlers(FakeLists())["create_list"]({}, CTX)


async def test_add_item_found_and_missing() -> None:
    added = await handlers(FakeLists(item_result=item("i1", "eggs")))["add_list_item"](
        {"list_id": "L1", "body": "eggs"}, CTX
    )
    assert "Added 'eggs' id=i1" in added
    missing = await handlers(FakeLists(item_result=None))["add_list_item"](
        {"list_id": "gone", "body": "eggs"}, CTX
    )
    assert "in scope" in missing


async def test_add_item_needs_list_and_body() -> None:
    assert "needs a list_id and a body" in await handlers(FakeLists())["add_list_item"](
        {"list_id": "L1"}, CTX
    )


async def test_check_item_toggles_and_passes_flag() -> None:
    fake = FakeLists(item_result=item("i1", "eggs", checked=True))
    out = await handlers(fake)["check_list_item"]({"item_id": "i1"}, CTX)
    assert "Checked off 'eggs'" in out
    assert fake.calls[0] == ("set_item_checked", CTX.session, "i1", True)  # default checked=True
    reopened = await handlers(FakeLists(item_result=item("i1", "eggs", checked=False)))[
        "check_list_item"
    ]({"item_id": "i1", "checked": False}, CTX)
    assert "Reopened 'eggs'" in reopened


async def test_remove_item_found_and_missing() -> None:
    gone = await handlers(FakeLists(item_result=item("i1", "x")))["remove_list_item"](
        {"item_id": "i1"}, CTX
    )
    assert "Removed the item" in gone
    missing = await handlers(FakeLists(item_result=None))["remove_list_item"](
        {"item_id": "nope"}, CTX
    )
    assert "in scope" in missing
