"""The `triage_inbox` engine action: classify the newest day of inbox mail into
priority labels and archive it (docs/EMAIL_ARCHIVIST_PLAN.md). Driven against the
in-memory FakeGmail and a scripted FakeLlmClient — no network, no real model."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from jbrain.gmail.client import GmailApi, GmailLabel, GmailMessage
from jbrain.gmail.fake import FakeGmail
from jbrain.gmail.triage import InboxTriage
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter

_UNTRIAGED_ID = "Label_untriaged"


def _msg(
    mid: str, *, date: str, subject: str = "s", body: str = "b", sender: str = "a@x"
) -> GmailMessage:
    return GmailMessage(
        id=mid,
        thread_id=f"t{mid}",
        sender=sender,
        to="me@x",
        subject=subject,
        date=date,
        snippet=body[:50],
        body=body,
    )


def _factory(fake: FakeGmail) -> Callable[[], Awaitable[GmailApi]]:
    async def client() -> GmailApi:
        return fake

    return client


def _router(verdicts: list[dict]) -> tuple[LlmRouter, FakeLlmClient]:
    # One verdict per email, replayed in call order (the batch is classified one
    # message at a time, sorted by id — see InboxTriage._newest_day).
    fake = FakeLlmClient(responses=[json.dumps(v) for v in verdicts])
    return LlmRouter({"xai": fake}, {"triage.classify": ("xai", "m")}), fake


def _names_on(fake: FakeGmail, mid: str) -> set[str]:
    """The label NAMES currently on a message (the fake stores ids)."""
    return {fake._labels[lid].name for lid in fake.labels_on(mid) if lid in fake._labels}


def _with_untriaged(fake: FakeGmail, *mids: str) -> None:
    for mid in mids:
        fake._on[mid].add(_UNTRIAGED_ID)


async def test_files_newest_day_and_leaves_older_mail_in_inbox() -> None:
    # m0 is the previous day; m1/m2/m3 are the newest day and the only ones triaged.
    fake = FakeGmail(
        messages=[
            _msg("m0", date="Tue, 24 Jun 2026 23:00:00 +0000", subject="yesterday"),
            _msg("m1", date="Wed, 25 Jun 2026 09:00:00 +0000", subject="invoice"),
            _msg("m2", date="Wed, 25 Jun 2026 10:00:00 +0000", subject="50% off"),
            _msg("m3", date="Wed, 25 Jun 2026 11:00:00 +0000", subject="weird"),
        ],
        labels=[GmailLabel(id=_UNTRIAGED_ID, name="untriaged")],
    )
    _with_untriaged(fake, "m0", "m1", "m2", "m3")
    # Batch is sorted by id, so the calls land in m1, m2, m3 order. m3 is spam below
    # the floor.
    router, llm = _router(
        [
            {"bucket": "high", "confidence": 0.9},
            {"bucket": "low", "confidence": 0.95},
            {"bucket": "spam", "confidence": 0.3},
        ]
    )

    await InboxTriage(_factory(fake), router).run({})

    # One LLM call per newest-day email; the email rides in user_text, not the system.
    assert len(llm.calls) == 3
    assert "invoice" in llm.calls[0]["user_text"]
    assert "invoice" not in llm.calls[0]["system"]

    # Newest-day mail filed: triaged/* added, INBOX + untriaged removed.
    assert _names_on(fake, "m1") == {"triaged/high"}
    assert _names_on(fake, "m2") == {"triaged/low"}
    # Low-confidence spam downgraded to the safe, visible bucket.
    assert _names_on(fake, "m3") == {"triaged/medium"}
    # Previous day untouched — still in the inbox for a later run.
    assert _names_on(fake, "m0") == {"INBOX", "untriaged"}


async def test_empty_inbox_makes_no_llm_call() -> None:
    fake = FakeGmail(messages=[])
    router, llm = _router([])
    await InboxTriage(_factory(fake), router).run({})
    assert llm.calls == []


async def test_unclassified_message_stays_in_inbox() -> None:
    fake = FakeGmail(
        messages=[
            _msg("m1", date="Wed, 25 Jun 2026 09:00:00 +0000"),
            _msg("m2", date="Wed, 25 Jun 2026 10:00:00 +0000"),
        ]
    )
    # m1 classifies; m2's verdict is unusable (no bucket), so m2 is left in the inbox.
    router, _ = _router([{"bucket": "high", "confidence": 0.8}, {"confidence": 0.5}])
    await InboxTriage(_factory(fake), router).run({})
    assert _names_on(fake, "m1") == {"triaged/high"}
    assert _names_on(fake, "m2") == {"INBOX"}


async def test_archived_mail_drops_out_of_inbox_on_rerun() -> None:
    # Resumability: after filing, the newest-day mail leaves the inbox, so a second
    # run sees only what remains (here, the older day's message).
    fake = FakeGmail(
        messages=[
            _msg("m0", date="Tue, 24 Jun 2026 12:00:00 +0000"),
            _msg("m1", date="Wed, 25 Jun 2026 12:00:00 +0000"),
        ]
    )
    router1, _ = _router([{"bucket": "high", "confidence": 0.9}])
    await InboxTriage(_factory(fake), router1).run({})
    assert _names_on(fake, "m1") == {"triaged/high"}
    assert _names_on(fake, "m0") == {"INBOX"}

    # Second run: m1 is gone from the inbox; m0 (now the newest remaining) is filed.
    router2, llm2 = _router([{"bucket": "low", "confidence": 0.9}])
    await InboxTriage(_factory(fake), router2).run({})
    assert len(llm2.calls) == 1
    assert _names_on(fake, "m0") == {"triaged/low"}
