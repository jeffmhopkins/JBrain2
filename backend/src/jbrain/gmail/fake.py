"""An in-memory Gmail for tests (docs/EMAIL_ARCHIVIST_PLAN.md). Same `GmailApi`
surface as `GmailClient`, so the gmail_* handlers and the archivist loop run against
a scripted mailbox with no network — the connector/LLM-adapter testing posture."""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence

from jbrain.gmail.client import GmailError, GmailLabel, GmailMessage

# The one system label the archivist touches — removing it is "archive".
_INBOX = "INBOX"


class FakeGmail:
    """A scripted mailbox. Seed it with messages (each starts in the inbox) and
    optional labels; `labels_on` lets a test assert what a `modify` actually did."""

    def __init__(
        self,
        messages: Iterable[GmailMessage] = (),
        labels: Iterable[GmailLabel] = (),
    ):
        self._messages: dict[str, GmailMessage] = {m.id: m for m in messages}
        self._labels: dict[str, GmailLabel] = {_INBOX: GmailLabel(id=_INBOX, name=_INBOX)}
        for label in labels:
            self._labels[label.id] = label
        self._on: dict[str, set[str]] = {mid: {_INBOX} for mid in self._messages}
        self._ids = itertools.count(1)

    # --- test helpers ------------------------------------------------------

    def labels_on(self, message_id: str) -> set[str]:
        return set(self._on.get(message_id, set()))

    # --- GmailApi ----------------------------------------------------------

    def _match(self, query: str) -> list[str]:
        needle = query.strip().lower()
        return [
            m.id
            for m in self._messages.values()
            if not needle or needle in f"{m.subject}\n{m.body}\n{m.sender}".lower()
        ]

    async def search(self, query: str, *, max_results: int = 25) -> list[str]:
        return self._match(query)[: max(1, max_results)]

    async def count(self, query: str, *, cap: int = 50_000) -> tuple[int, bool]:
        hits = self._match(query)
        return min(len(hits), cap), len(hits) > cap

    async def search_all(self, query: str, *, cap: int = 10_000) -> tuple[list[str], bool]:
        hits = self._match(query)
        return hits[:cap], len(hits) > cap

    async def sender_sample(self, query: str, *, sample: int = 200) -> tuple[list[str], bool]:
        hits = self._match(query)
        sampled = hits[: max(1, sample)]
        return [self._messages[i].sender for i in sampled], len(hits) > len(sampled)

    async def get(self, message_id: str, *, metadata_only: bool = False) -> GmailMessage:
        msg = self._messages.get(message_id)
        if msg is None:
            raise GmailError(f"no such message: {message_id}")
        if metadata_only:
            return GmailMessage(
                id=msg.id,
                thread_id=msg.thread_id,
                sender=msg.sender,
                to=msg.to,
                subject=msg.subject,
                date=msg.date,
                snippet=msg.snippet,
                body="",
            )
        return msg

    async def list_labels(self) -> list[GmailLabel]:
        return list(self._labels.values())

    async def create_label(self, name: str) -> GmailLabel:
        existing = next((lbl for lbl in self._labels.values() if lbl.name == name), None)
        if existing is not None:
            return existing
        label = GmailLabel(id=f"Label_{next(self._ids)}", name=name)
        self._labels[label.id] = label
        return label

    async def modify(
        self,
        message_id: str,
        *,
        add_label_ids: Sequence[str] = (),
        remove_label_ids: Sequence[str] = (),
    ) -> None:
        if message_id not in self._messages:
            raise GmailError(f"no such message: {message_id}")
        on = self._on.setdefault(message_id, set())
        on.update(add_label_ids)
        on.difference_update(remove_label_ids)

    async def batch_modify(
        self,
        message_ids: Sequence[str],
        *,
        add_label_ids: Sequence[str] = (),
        remove_label_ids: Sequence[str] = (),
    ) -> None:
        for message_id in message_ids:
            if message_id not in self._messages:
                continue
            on = self._on.setdefault(message_id, set())
            on.update(add_label_ids)
            on.difference_update(remove_label_ids)
