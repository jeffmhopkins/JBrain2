"""The `triage_inbox` engine action: classify untriaged inbox mail into priority
labels, archiving all but `high` (which stays in the inbox) — see
docs/EMAIL_ARCHIVIST_PLAN.md. Driven against the in-memory FakeGmail and a scripted
FakeLlmClient — no network, no real model."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from jbrain.gmail.client import GmailApi, GmailLabel, GmailMessage
from jbrain.gmail.fake import FakeGmail
from jbrain.gmail.triage import _PROMPT, InboxTriage
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
    # message at a time, sorted by id — see InboxTriage.run).
    fake = FakeLlmClient(responses=[json.dumps(v) for v in verdicts])
    return LlmRouter({"xai": fake}, {"triage.classify": ("xai", "m")}), fake


def _names_on(fake: FakeGmail, mid: str) -> set[str]:
    """The label NAMES currently on a message (the fake stores ids)."""
    return {fake._labels[lid].name for lid in fake.labels_on(mid) if lid in fake._labels}


def _with_untriaged(fake: FakeGmail, *mids: str) -> None:
    for mid in mids:
        fake._on[mid].add(_UNTRIAGED_ID)


async def test_sweeps_whole_inbox_keeping_high_and_archiving_the_rest() -> None:
    # m0 is a previous day; the run still sweeps it — there is no "newest day" window
    # anymore, the whole inbox is classified each run.
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
    # Batch is sorted by id, so the calls land in m0, m1, m2, m3 order.
    router, llm = _router(
        [
            {"bucket": "medium", "confidence": 0.9},
            {"bucket": "high", "confidence": 0.9},
            {"bucket": "low", "confidence": 0.95},
            {"bucket": "spam", "confidence": 0.3},
        ]
    )

    await InboxTriage(_factory(fake), router).run({})

    # One LLM call per email — the whole inbox, not just one day; the email rides in
    # user_text, not the system.
    assert len(llm.calls) == 4
    assert "invoice" in llm.calls[1]["user_text"]
    assert "invoice" not in llm.calls[1]["system"]

    # high is filed in place: labeled but KEPT in the inbox, with untriaged cleared.
    assert _names_on(fake, "m1") == {"INBOX", "triaged/high"}
    # Every other bucket is archived out of the inbox (INBOX + untriaged removed).
    assert _names_on(fake, "m0") == {"triaged/medium"}
    assert _names_on(fake, "m2") == {"triaged/low"}
    # The model's spam verdict stands as given — the confidence floor was removed.
    assert _names_on(fake, "m3") == {"triaged/spam"}


async def test_already_filed_high_is_excluded_on_rerun() -> None:
    fake = FakeGmail(messages=[_msg("m1", date="Wed, 25 Jun 2026 09:00:00 +0000")])
    router1, _ = _router([{"bucket": "high", "confidence": 0.9}])
    await InboxTriage(_factory(fake), router1).run({})
    # high stays in the inbox, now carrying its label.
    assert _names_on(fake, "m1") == {"INBOX", "triaged/high"}

    # Second run: m1 still has INBOX, but `in:inbox -label:triaged/high` excludes it, so
    # it is never re-classified (no LLM call) — that exclusion is what makes "keep high
    # in the inbox" terminate instead of looping forever.
    router2, llm2 = _router([])
    await InboxTriage(_factory(fake), router2).run({})
    assert llm2.calls == []
    assert _names_on(fake, "m1") == {"INBOX", "triaged/high"}


async def test_html_body_is_rendered_to_markdown_for_the_model() -> None:
    html = (
        "<html><body><h1>Big Sale</h1><p>Save <strong>50%</strong> on "
        "<a href='https://shop.example/x'>everything</a>.</p>"
        "<script>track()</script></body></html>"
    )
    fake = FakeGmail(messages=[_msg("m1", date="Wed, 25 Jun 2026 09:00:00 +0000", body=html)])
    router, llm = _router([{"bucket": "spam", "confidence": 0.9}])
    await InboxTriage(_factory(fake), router).run({})

    sent = llm.calls[0]["user_text"]
    assert "<h1>" not in sent and "<script>" not in sent  # raw tags gone
    assert "track()" not in sent  # the <script> subtree is dropped, not just its tags
    assert "# Big Sale" in sent  # heading rendered as markdown
    assert "**50%**" in sent  # emphasis rendered as markdown
    assert "everything" in sent  # link text preserved


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
    # m1 classifies (medium → archived); m2's verdict is unusable (no bucket), so m2 is
    # left in the inbox for a later run.
    router, _ = _router([{"bucket": "medium", "confidence": 0.8}, {"confidence": 0.5}])
    await InboxTriage(_factory(fake), router).run({})
    assert _names_on(fake, "m1") == {"triaged/medium"}
    assert _names_on(fake, "m2") == {"INBOX"}


def test_classify_prompt_routes_retail_order_confirmations_to_medium() -> None:
    # The classifier prompt is the only place the owner's rule lives ("an Amazon
    # order confirmation is medium, not high"), and the LLM is faked in every other
    # test — so guard the instruction itself against silent regression. Order
    # confirmations / receipts / shipping updates are transactional, but informational:
    # they must not be pulled up to "high" by the transactional/time-sensitive wording.
    rendered = _PROMPT.render().lower()
    assert "order confirmation" in rendered
    assert "purchase receipt" in rendered
    # "high" is reserved for transactional mail that demands a response, not every
    # transactional message — the order email is explicitly excluded from it.
    assert 'never "high"' in rendered


async def test_archived_mail_drops_out_of_inbox_on_rerun() -> None:
    # Resumability: a filed (non-high) message leaves the inbox, so a second run sees
    # only what remains.
    fake = FakeGmail(
        messages=[
            _msg("m0", date="Tue, 24 Jun 2026 12:00:00 +0000"),
            _msg("m1", date="Wed, 25 Jun 2026 12:00:00 +0000"),
        ]
    )
    router1, llm1 = _router(
        [{"bucket": "low", "confidence": 0.9}, {"bucket": "medium", "confidence": 0.9}]
    )
    await InboxTriage(_factory(fake), router1).run({})
    assert len(llm1.calls) == 2
    assert _names_on(fake, "m0") == {"triaged/low"}
    assert _names_on(fake, "m1") == {"triaged/medium"}

    # Second run: both are gone from the inbox, so there is nothing left to classify.
    router2, llm2 = _router([])
    await InboxTriage(_factory(fake), router2).run({})
    assert llm2.calls == []
