"""The `triage_inbox` engine action: classify untriaged inbox mail into priority
labels, archiving everything except `high` — which stays in the inbox so it stays in
front of the owner (docs/archive/EMAIL_ARCHIVIST_PLAN.md).

A mostly-deterministic sweep. The Gmail mechanics — find the inbox, read each
message, apply labels, drop INBOX — are direct API calls through the `GmailApi`; the
LLM is invoked once per email to classify it into one of four priority buckets,
reading that message's sender, subject, and full body (the body is the strongest
signal for separating real, actionable mail from marketing fluff or junk). One call
per email — each gets the model's whole attention and its full body, rather than
sharing one prompt's context with nine others.

One run sweeps the whole inbox (up to a per-run cap), classifying every message that
isn't already filed as `high`. The search excludes `triaged/high` because a `high`
verdict is filed in place — labeled but left in the inbox — so without the exclusion
each run would re-classify it forever. Because every other bucket leaves the inbox as
it's filed, the sweep is naturally resumable after a crash or a cap. It NEVER deletes
(the gmail.modify scope cannot), and a message the classifier omits is left in the
inbox for the next run.

Runs under SYSTEM_CTX like the other scheduled sweeps. The one LLM call goes through
the adapter (`triage.classify`), never a provider SDK (CLAUDE.md rule 1).
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from jbrain.db.session import scoped_session
from jbrain.gmail.client import GmailApi, GmailMessage
from jbrain.htmltext import html_to_markdown
from jbrain.llm.promptfile import load_prompt
from jbrain.llm.router import LlmRouter
from jbrain.models.archivist import ArchivistMemoryRepo
from jbrain.queue import SYSTEM_CTX
from jbrain.workflow.registry import ActionSpec

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger()

_PROMPT = load_prompt(Path(__file__).parent / "prompts" / "triage_classify.prompt")

# The four priority buckets, filed as nested Gmail labels (`triaged/<bucket>`; Gmail
# nests on "/"). Order is high→spam so a "when torn, pick the safer one" tie-break has
# a defined direction.
BUCKETS: tuple[str, ...] = ("high", "medium", "low", "spam")
_TRIAGED_PREFIX = "triaged"
# `high` mail is filed in place: labeled but kept in the inbox so it stays in front of
# the owner. Every other bucket is archived (INBOX dropped) as it's filed.
_KEEP_IN_INBOX = "high"
# The owner's pre-triage label, removed alongside INBOX as a message is filed. Absent
# on most mailboxes — resolved once and skipped when it doesn't exist.
_UNTRIAGED_LABEL = "untriaged"
# INBOX is Gmail's system label id; removing it IS "archive".
_INBOX = "INBOX"

# Untriaged inbox mail: in the inbox but not yet filed as `high`. `high` is filed in
# place (kept in the inbox), so the exclusion stops each run from re-classifying it.
_SEARCH_QUERY = f"in:inbox -label:{_TRIAGED_PREFIX}/{_KEEP_IN_INBOX}"
# The newest inbox messages a single run considers. An inbox with more untriaged
# messages than this is filed across successive runs (the excess simply stays in the
# inbox), so this bounds one run's work without stranding mail.
_SEARCH_CAP = 200
# Full-message reads run in bounded-concurrency chunks (mirrors GmailClient.sender_sample)
# rather than one slow id-at-a-time loop.
_FETCH_CHUNK = 10
# The archivist keeps owner-authored corrections to the bucket rules between these
# markers in its cross-session memory; the sweep reads ONLY what sits between them and
# ignores the rest of the scratchpad (taxonomy, filing rules, progress). Kept loose —
# any text on the marker lines after the keyword is tolerated — so the agent-authored
# document doesn't have to be byte-exact for the section to be found.
_CLARIFICATIONS_RE = re.compile(
    r"===\s*TRIAGE CLARIFICATIONS.*?===\s*\n(.*?)\n?===\s*END TRIAGE CLARIFICATIONS\s*===",
    re.IGNORECASE | re.DOTALL,
)

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
    description="Classify untriaged inbox mail into triaged/* labels; archive all but high.",
    category="note",  # email → notes; groups with the note pipeline on the Ops surface
    # Run only when the model triage routes to is already resident: classification
    # makes one local LLM call per email, so firing while that model is cold would
    # swap out whatever the owner is actively using (a code model, an image session).
    # When unmet the run defers (5m, no attempt burned); the scheduler coalesces
    # re-fires so deferred runs never pile up. Inert on a cloud route (always met).
    precondition="reasoning_model_loaded",
)

# The provider hands back a configured client; the handler holds the bound
# `provider.client` method, exactly as the gmail_* tools do, so it picks up a live
# credential change with no restart.
ClientFactory = Callable[[], Awaitable[GmailApi]]

# A live-progress sink the worker injects (the run-log's progress_note); None when the
# handler runs outside the worker (a direct test call).
ProgressFn = Callable[[str], Awaitable[None]]


def _chunks(items: Sequence[Any], size: int) -> list[list[Any]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def _extract_clarifications(memory: str) -> str:
    """The owner's bucket corrections — the text between the TRIAGE CLARIFICATIONS
    markers in the archivist's memory, stripped. Empty when the section is absent or
    blank, so a mailbox whose owner never recorded a correction triages exactly as
    before."""
    match = _CLARIFICATIONS_RE.search(memory)
    return match.group(1).strip() if match else ""


def _clarifications_block(corrections: str) -> str:
    """The injected `{{ clarifications }}` block for the classifier prompt, or empty
    string when there are none. Framed as owner overrides so the model gives them
    priority over the general bucket rules."""
    if not corrections:
        return ""
    return (
        "Owner corrections — the mailbox owner recorded these after catching the sweep "
        "misfiling mail. They take priority over the general rules above; when one "
        f"applies to this email, follow it exactly:\n{corrections}\n"
    )


class InboxTriage:
    """Classify and file untriaged inbox mail. Stateless across runs — its only
    persistence is the Gmail labels themselves, so a re-run picks up wherever the last
    one left off."""

    def __init__(
        self,
        client_factory: ClientFactory,
        router: LlmRouter,
        maker: async_sessionmaker[AsyncSession] | None = None,
    ):
        self._client_factory = client_factory
        self._router = router
        # The app sessionmaker, used only to read the owner's archivist memory for its
        # triage corrections. None in unit tests (no DB) — the sweep then runs with no
        # owner overrides, exactly as it did before the memory was wired in.
        self._maker = maker
        self._memory = ArchivistMemoryRepo()

    async def run(self, _payload: dict[str, Any], *, progress: ProgressFn | None = None) -> None:
        gmail = await self._client_factory()
        ids = await gmail.search(_SEARCH_QUERY, max_results=_SEARCH_CAP)
        if not ids:
            log.info("triage_inbox.empty")
            return

        batch = await self._fetch_messages(gmail, ids)
        # A deterministic classification order (and so a deterministic test order).
        batch.sort(key=lambda m: m.id)

        # Read the owner's corrections ONCE per run and reuse the rendered system prompt
        # across every per-email call — not once per email.
        clarifications = _clarifications_block(await self._load_corrections())
        await self._note(progress, f"reading {len(batch)} emails")
        verdicts = await self._classify(batch, clarifications, progress)
        if not verdicts:
            log.warning("triage_inbox.no_verdicts", considered=len(batch))
            return

        counts = await self._file(gmail, batch, verdicts)
        await self._note(progress, f"filed {sum(counts.values())} of {len(batch)} emails")
        log.info(
            "triage_inbox.filed",
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

    async def _load_corrections(self) -> str:
        """The owner's triage corrections from the archivist's memory, or empty string.
        Reading runs under SYSTEM_CTX (a worker context, `is_owner()`); the row is keyed
        by the owner principal (the interactive archivist writes under its own principal,
        not "worker"), so resolve that id first. NEVER raises — a memory read failure
        must not break the sweep, so it logs and falls back to no corrections."""
        if self._maker is None:
            return ""
        try:
            async with scoped_session(self._maker, SYSTEM_CTX) as session:
                principal = (
                    await session.execute(
                        text("SELECT id::text FROM app.principals WHERE kind = 'owner'")
                    )
                ).scalar()
                if not principal:
                    return ""
                memory = await self._memory.read(session, principal)
        except Exception:  # noqa: BLE001 — best-effort; the sweep proceeds without overrides
            log.warning("triage_inbox.memory_read_failed", exc_info=True)
            return ""
        return _extract_clarifications(memory)

    async def _classify(
        self, batch: Sequence[GmailMessage], clarifications: str, progress: ProgressFn | None
    ) -> dict[str, str]:
        """Map each message id to its bucket via a SEPARATE LLM call per email,
        reporting "processed X of Y" as it goes. A verdict the model omits or labels
        with an unknown bucket is dropped (that message stays in the inbox); the
        model's bucket otherwise stands as given (no confidence override). `clarifications`
        is the owner's correction block (possibly empty), injected into every call's
        system prompt."""
        verdicts: dict[str, str] = {}
        for i, msg in enumerate(batch):
            result = await self._router.complete(
                task="triage.classify",
                system=_PROMPT.render(clarifications=clarifications),
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
        """Apply one batchModify per bucket: add `triaged/<bucket>` and clear the
        `untriaged` label (if it exists). Every bucket EXCEPT `high` also drops INBOX
        (the archive); `high` is filed in place so it stays in the inbox. Returns the
        count filed per bucket."""
        by_bucket: dict[str, list[str]] = defaultdict(list)
        for mid, bucket in verdicts.items():
            by_bucket[bucket].append(mid)

        label_ids = {b: (await gmail.create_label(f"{_TRIAGED_PREFIX}/{b}")).id for b in by_bucket}
        existing = {lbl.name: lbl.id for lbl in await gmail.list_labels()}
        untriaged = [existing[_UNTRIAGED_LABEL]] if _UNTRIAGED_LABEL in existing else []

        for bucket, mids in by_bucket.items():
            remove = untriaged + ([] if bucket == _KEEP_IN_INBOX else [_INBOX])
            await gmail.batch_modify(
                mids, add_label_ids=[label_ids[bucket]], remove_label_ids=remove
            )
        return {bucket: len(mids) for bucket, mids in by_bucket.items()}


def triage_inbox_handler(
    client_factory: ClientFactory,
    router: LlmRouter,
    maker: async_sessionmaker[AsyncSession] | None = None,
) -> Any:
    """Worker dispatch entry for `triage_inbox` (payload-only Handler). `maker` is the
    app sessionmaker, used to read the owner's archivist-memory triage corrections."""
    return InboxTriage(client_factory, router, maker).run
