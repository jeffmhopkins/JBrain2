"""Lists — owner-managed structured records the agent maintains directly. The
repository runs every query on an RLS-scoped session, so domain isolation is
Postgres', not these methods' (same pattern as notes/auth)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from jbrain.db.session import SessionContext


class UnknownDomain(Exception):
    """A list was created in a domain code that doesn't exist (FK violation)."""


@dataclass(frozen=True)
class ListItemInfo:
    id: str
    body: str
    checked: bool
    position: int
    source_note_id: str | None
    created_at: datetime


@dataclass(frozen=True)
class ListInfo:
    id: str
    domain: str
    title: str
    archived: bool
    created_at: datetime
    updated_at: datetime
    items: list[ListItemInfo] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return len(self.items)

    @property
    def open_count(self) -> int:
        return sum(1 for i in self.items if not i.checked)


class ListsRepo(Protocol):
    async def create_list(self, ctx: SessionContext, *, domain: str, title: str) -> ListInfo:
        """Create an empty list in `domain`; raises UnknownDomain for a bad code."""
        ...

    async def list_lists(
        self, ctx: SessionContext, *, include_archived: bool = False
    ) -> list[ListInfo]:
        """In-scope lists (newest activity first), each with its items."""
        ...

    async def get_list(self, ctx: SessionContext, list_id: str) -> ListInfo | None:
        """One list with its items; None when missing or out of scope."""
        ...

    async def rename_list(self, ctx: SessionContext, list_id: str, title: str) -> ListInfo | None:
        """None when the list is missing or out of scope."""
        ...

    async def archive_list(
        self, ctx: SessionContext, list_id: str, *, archived: bool
    ) -> ListInfo | None:
        """Retire/restore a list; None when missing or out of scope."""
        ...

    async def add_item(
        self,
        ctx: SessionContext,
        list_id: str,
        body: str,
        *,
        source_note_id: str | None = None,
    ) -> ListItemInfo | None:
        """Append an item to the end; None when the list is missing/out of scope."""
        ...

    async def set_item_checked(
        self, ctx: SessionContext, item_id: str, *, checked: bool
    ) -> ListItemInfo | None:
        """Check/uncheck an item; None when missing or out of scope."""
        ...

    async def remove_item(self, ctx: SessionContext, item_id: str) -> bool:
        """Delete an item; False when missing or out of scope."""
        ...
