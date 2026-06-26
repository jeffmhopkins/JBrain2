"""The `triage_inbox` engine action: classify the newest day of inbox mail into
priority labels and archive it (docs/EMAIL_ARCHIVIST_PLAN.md).

A mostly-deterministic sweep. The Gmail mechanics — find the inbox, read each
message, apply labels, drop INBOX — are direct API calls through the `GmailApi`; the
LLM is invoked once per email to classify it into one of four priority buckets,
reading that message's sender, subject, and full body (the body is the strongest
signal for separating real, actionable mail from marketing fluff or junk). One call
per email — each gets the model's whole attention and its full body, rather than
sharing one prompt's context with nine others.

One run triages a single calendar day — the newest day still present in the inbox —
so repeated runs walk back through history, and because each filed message leaves
the inbox the sweep is naturally resumable after a crash or a cap. It NEVER deletes
(the gmail.modify scope cannot), low-confidence spam is downgraded to a visible
bucket, and a message the classifier omits is left in the inbox for the next run.

Runs under SYSTEM_CTX like the other scheduled sweeps; the schedule ships disabled
and is Ops-fireable. The one LLM call goes through the adapter (`triage.classify`),
never a provider SDK (CLAUDE.md rule 1).
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, tzinfo
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from jbrain.gmail.client import GmailApi, GmailMessage
from jbrain.htmltext import html_to_markdown
from jbrain.llm.promptfile import load_prompt
from jbrain.llm.router import LlmRouter
from jbrain.workflow.registry import ActionSpec

log = structlog.get_logger()

_PROMPT = load_prompt(Path(__file__).parent / "prompts" / "triage_classify.prompt")

# The four priority buckets, filed as nested Gmail labels (`triaged/<bucket>`; Gmail
# nests on "/"). Order is high→spam so a "when torn, pick the safer one" tie-break has
# a defined direction.
BUCKETS: tuple[str, ...] = ("high", "medium", "low", "spam")
_TRIAGED_PREFIX = "triaged"
# The owner's pre-triage label, removed alongside INBOX as a message is filed. Absent
# on most mailboxes — resolved once and skipped when it doesn't exist.
_UNTRIAGED_LABEL = "untriaged"
# INBOX is Gmail's system label id; removing it IS "archive".
_INBOX = "INBOX"

# The newest inbox messages a single run considers. A day with more than this many
# messages is filed across successive runs (the excess simply stays in the inbox), so
# this bounds one run's work without stranding mail.
_SEARCH_CAP = 200
# Full-message reads run in bounded-concurrency chunks (mirrors GmailClient.sender_sample)
# rather than one slow id-at-a-time loop.
_FETCH_CHUNK = 10
# A cheap signal that a body is HTML (a full-document tag or any closing tag), so we
# render it to markdown before classifying rather than feeding the model raw markup.
# A text/plain body that happens to contain a stray "<" is left untouched.
_HTML_HINT = re.compile(
    r"<(?:html|head|body|div|p|table|td|tr|a|br|span|ul|ol|li|img|font)\b|</[a-zA-Z]+>", re.I
)

# Returned by the classifier for ONE email; defined here (not only in the prompt)
# because the handler passes it to router.complete and reads the result back.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["bucket", "confidence"],
    "properties": {
        "bucket": {"type": "string", "enum": list(BUCKETS)},
        "confidence": {"type": "number"},
    },
}

TRIAGE_INBOX_SPEC = ActionSpec(
    name="triage_inbox",
    version=1,
    handler="triage_inbox",
    domain_optional=True,
    mutating=True,  # relabels + archives inbox mail (never deletes)
    cost_class="expensive",  # one LLM classification call per email
    dedup_key_expr=None,
    description="Classify the newest day of inbox mail into triaged/* labels and archive it.",
)

# The provider hands back a configured client; the handler holds the bound
# `provider.client` method, exactly as the gmail_* tools do, so it picks up a live
# credential change with no restart.
ClientFactory = Callable[[], Awaitable[GmailApi]]

# A live-progress sink the worker injects (the run-log's progress_note); None when the
# handler runs outside the worker (a direct test call).
ProgressFn = Callable[[str], Awaitable[None]]

# Resolves the owner's IANA timezone at run time (live from settings, like the Gmail
# credentials); None when the handler runs outside the worker (a direct test call).
TimezoneProvider = Callable[[], Awaitable[str | None]]


def _zone(name: str | None) -> tzinfo:
    """The owner's timezone for day-bucketing, falling back to UTC for an unset or
    unrecognized name. 'A day' is the OWNER's calendar day — bucketing in UTC split a
    US-evening email (already the next UTC day) off from the rest of its local day, so
    an evening run missed part of 'today'."""
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _message_day(msg: GmailMessage, zone: tzinfo) -> date | None:
    """The message's calendar day in `zone`, from its Date header, or None when it
    can't be parsed — a malformed header must not stop the sweep, so an undated message
    is folded into the current run's batch rather than blocking it."""
    try:
        dt = parsedate_to_datetime(msg.date)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone).date()


def _chunks(items: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


class InboxTriage:
    """Classify and file the newest day of inbox mail. Stateless across runs — its
    only persistence is the Gmail labels themselves, so a re-run picks up wherever the
    last one left off."""

    def __init__(
        self,
        client_factory: ClientFactory,
        router: LlmRouter,
        timezone_provider: TimezoneProvider | None = None,
    ):
        self._client_factory = client_factory
        self._router = router
        self._timezone_provider = timezone_provider

    async def run(self, _payload: dict[str, Any], *, progress: ProgressFn | None = None) -> None:
        gmail = await self._client_factory()
        ids = await gmail.search("in:inbox", max_results=_SEARCH_CAP)
        if not ids:
            log.info("triage_inbox.empty")
            return

        zone = _zone(await self._timezone_provider() if self._timezone_provider else None)
        messages = await self._fetch_messages(gmail, ids)
        day, batch = self._newest_day(messages, zone)
        if not batch:
            log.info("triage_inbox.nothing_to_triage", fetched=len(messages))
            return

        await self._note(progress, f"reading {len(batch)} emails from {day}")
        verdicts = await self._classify(batch, progress)
        if not verdicts:
            log.warning("triage_inbox.no_verdicts", day=str(day), considered=len(batch))
            return

        counts = await self._file(gmail, batch, verdicts)
        await self._note(progress, f"filed {sum(counts.values())} of {len(batch)} emails")
        log.info(
            "triage_inbox.filed",
            day=str(day),
            considered=len(batch),
            filed=sum(counts.values()),
            skipped=len(batch) - sum(counts.values()),
            **{f"bucket_{b}": counts.get(b, 0) for b in BUCKETS},
        )

    async def _fetch_messages(self, gmail: GmailApi, ids: Sequence[str]) -> list[GmailMessage]:
        """Read each id in full (sender/subject/date + decoded body) in bounded-
        concurrency chunks."""
        out: list[GmailMessage] = []
        for chunk in _chunks(ids, _FETCH_CHUNK):
            out.extend(await asyncio.gather(*(gmail.get(mid) for mid in chunk)))
        return out

    def _newest_day(
        self, messages: Sequence[GmailMessage], zone: tzinfo
    ) -> tuple[date | None, list[GmailMessage]]:
        """The newest calendar day present (in the owner's `zone`) and that day's
        messages (plus any undated ones), sorted by id for a deterministic
        classification order. When nothing carries a parseable date, the whole fetched
        page is the batch."""
        days = [d for d in (_message_day(m, zone) for m in messages) if d is not None]
        target = max(days) if days else None
        batch = [m for m in messages if target is None or _message_day(m, zone) in (None, target)]
        batch.sort(key=lambda m: m.id)
        return target, batch

    async def _classify(
        self, batch: Sequence[GmailMessage], progress: ProgressFn | None
    ) -> dict[str, str]:
        """Map each message id to its bucket via a SEPARATE LLM call per email,
        reporting "processed X of Y" as it goes. A verdict the model omits or labels
        with an unknown bucket is dropped (that message stays in the inbox); the
        model's bucket otherwise stands as given (no confidence override)."""
        verdicts: dict[str, str] = {}
        for i, msg in enumerate(batch):
            result = await self._router.complete(
                task="triage.classify",
                system=_PROMPT.render(),
                user_text=self._render_email(msg),
                json_schema=_SCHEMA,
                max_tokens=int(_PROMPT.config.get("max_tokens", 4096)),
            )
            bucket = self._resolve_bucket(result.parsed)
            if bucket is not None:
                verdicts[msg.id] = bucket
            await self._note(progress, f"processed {i + 1} of {len(batch)} emails")
        return verdicts

    @staticmethod
    async def _note(progress: ProgressFn | None, note: str) -> None:
        """Emit a live progress note when the worker injected a sink (no-op in tests)."""
        if progress is not None:
            await progress(note)

    @staticmethod
    def _render_email(msg: GmailMessage) -> str:
        """One email as the classifier sees it: sender, subject, and the FULL body as
        clean text. Gmail hands us the text/plain part when present, else raw HTML; an
        HTML body is rendered to markdown (tags/boilerplate stripped) so the model reads
        content, not markup. No length cap — the whole message goes, and the markdown
        pass keeps a marketing email's real size far below its raw-HTML bulk."""
        body = msg.body
        if _HTML_HINT.search(body):
            body = html_to_markdown(body) or body
        return f"From: {msg.sender}\nSubject: {msg.subject}\n\n{body}".strip()

    @staticmethod
    def _resolve_bucket(parsed: Any) -> str | None:
        # The model's bucket stands as given (the `confidence` field still anchors its
        # judgment, but no longer downgrades a low-confidence spam verdict — the owner
        # asked for promotional/announcement noise to be filed as spam, not surfaced).
        if not isinstance(parsed, dict):
            return None
        bucket = parsed.get("bucket")
        return bucket if bucket in BUCKETS else None

    async def _file(
        self, gmail: GmailApi, batch: Sequence[GmailMessage], verdicts: dict[str, str]
    ) -> dict[str, int]:
        """Apply one batchModify per bucket: add `triaged/<bucket>`, remove INBOX and
        (if it exists) `untriaged`. Returns the count filed per bucket."""
        by_bucket: dict[str, list[str]] = defaultdict(list)
        for mid, bucket in verdicts.items():
            by_bucket[bucket].append(mid)

        label_ids = {b: (await gmail.create_label(f"{_TRIAGED_PREFIX}/{b}")).id for b in by_bucket}
        existing = {lbl.name: lbl.id for lbl in await gmail.list_labels()}
        remove = [_INBOX] + ([existing[_UNTRIAGED_LABEL]] if _UNTRIAGED_LABEL in existing else [])

        for bucket, mids in by_bucket.items():
            await gmail.batch_modify(
                mids, add_label_ids=[label_ids[bucket]], remove_label_ids=remove
            )
        return {bucket: len(mids) for bucket, mids in by_bucket.items()}


def triage_inbox_handler(
    client_factory: ClientFactory,
    router: LlmRouter,
    timezone_provider: TimezoneProvider | None = None,
) -> Any:
    """Worker dispatch entry for `triage_inbox` (payload-only Handler)."""
    return InboxTriage(client_factory, router, timezone_provider).run
