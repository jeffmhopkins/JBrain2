"""jmolt's sanitized morning digest (docs/plans/JMOLT_PLAN.md, W4 — M14, M15).

Once each owner-local morning, push a digest that enumerates what jmolt did: every logged
action from the action ledger (M14 — post/comment/vote/follow/subscribe/profile, each
recorded by the drip sweep) and the writes still staged for review from last night. The
body is SANITIZED before it reaches the owner (M15): invisible/bidi/zero-width characters
stripped, HTML escaped, and links defanged — because it carries jmolt's own words and the
third-party Moltbook content it reacted to, one hop from attacker-authorable text.

A slow tick folded into the nightly loop: it fires once per morning (durable dedup on the
owner-local date, like the nightly run), only when the account is registered, and never
raises.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.jmolt_guards import strip_invisibles
from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_outbox import ActionLedgerRepo, LedgerRow, OutboxRepo, OutboxRow
from jbrain.notify import Notification, NotifyBus, notify_owner
from jbrain.settings_store import SqlSettingsStore

log = structlog.get_logger()

# Owner-local morning: the digest fires in the first quarter-hour of this hour.
JMOLT_DIGEST_HOUR = 8
JMOLT_DIGEST_WINDOW_MIN = 15
_ACTIONS_IN_DIGEST = 40
_STAGED_IN_DIGEST = 20
# Notifications are short; the full enumeration also lives in the ledger/outbox the PWA
# reads. Cap the pushed body so a flooded night can't produce a megabyte notification.
_DIGEST_BODY_CAP = 4000
_PREVIEW = 140

_SYSTEM_OWNER = SessionContext(principal_kind="owner")
# Dangerous URL schemes to defang so a rendered link is neither clickable nor a live
# `javascript:`/`data:` payload if any future owner surface were less careful than today's
# (which renders every value as an escaped text child). `http`→`hxxp`, others → `x-<s>`.
_SCHEME = re.compile(r"\b(https?|javascript|data|vbscript|file|mailto):", re.I)


def _defang(m: re.Match[str]) -> str:
    scheme = m.group(1).lower()
    if scheme in ("http", "https"):
        return scheme.replace("http", "hxxp") + ":"
    return "x-" + scheme + ":"


def sanitize_for_owner(value: str) -> str:
    """M15 — make third-party/diary text safe to render to the owner: strip invisible and
    bidi/zero-width characters, defang URL/script schemes so a link is neither clickable nor
    a live payload, then HTML-escape (quotes included, so it is safe inside an attribute
    too). Pure; unit-tested."""
    cleaned = strip_invisibles(str(value))
    defanged = _SCHEME.sub(_defang, cleaned)
    return html.escape(defanged, quote=True)


def _preview(payload: dict) -> str:
    """A short, human label for a staged write — its title or the head of its content."""
    for key in ("title", "content", "description", "name", "post_id", "target_id"):
        val = str(payload.get(key, "")).strip()
        if val:
            return val[:_PREVIEW]
    return ""


def build_digest_body(actions: list[LedgerRow], staged: list[OutboxRow]) -> str:
    """The sanitized digest text (M14/M15). Pure — the tick wraps it with I/O."""
    lines: list[str] = []
    if actions:
        lines.append(f"Published in the last day ({len(actions)}):")
        for a in actions[:_ACTIONS_IN_DIGEST]:
            tgt = f" → {sanitize_for_owner(a.target)}" if a.target else ""
            lines.append(f"  · {sanitize_for_owner(a.action)}{tgt}")
    else:
        lines.append("Nothing published in the last day.")
    if staged:
        lines.append("")
        lines.append(f"Staged from last night, awaiting your review ({len(staged)}):")
        for r in staged[:_STAGED_IN_DIGEST]:
            preview = sanitize_for_owner(_preview(r.payload))
            lines.append(
                f"  · {sanitize_for_owner(r.kind)} [{sanitize_for_owner(r.status)}]"
                + (f": {preview}" if preview else "")
            )
    return "\n".join(lines)[:_DIGEST_BODY_CAP]


def _owner_local_now(tz: str, now: datetime) -> datetime:
    try:
        return now.astimezone(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError):
        return now.astimezone(UTC)


class JmoltDigest:
    def __init__(
        self,
        *,
        maker: async_sessionmaker[AsyncSession],
        settings_store: SqlSettingsStore,
        notify: NotifyBus | None = None,
    ) -> None:
        self._maker = maker
        self._settings = settings_store
        self._notify = notify
        self._ledger = ActionLedgerRepo()
        self._outbox = OutboxRepo()

    def _admin(self, pid: str) -> SessionContext:
        return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))

    async def tick(self, *, now: datetime | None = None) -> bool:
        """Send the morning digest if we're in the owner-local morning window and haven't
        sent one today. Returns True iff a digest was sent. Never raises."""
        now = now or datetime.now(UTC)
        pid = await _owner_principal_id(self._maker)
        if pid is None:
            return False
        admin = self._admin(pid)
        if not await self._settings.moltbook_api_key(admin):
            return False  # unregistered — nothing to digest.

        tz = await self._settings.owner_timezone(admin) or "UTC"
        local = _owner_local_now(tz, now)
        if local.hour != JMOLT_DIGEST_HOUR or local.minute >= JMOLT_DIGEST_WINDOW_MIN:
            return False
        today = local.date().isoformat()
        if await self._settings.moltbook_last_digest(admin) == today:
            return False  # already sent this morning (durable across restarts).

        body = await self.build(pid, admin, now=now)
        await self._settings.set_moltbook_last_digest(admin, today)
        notify_owner(
            self._notify,
            Notification(kind="jmolt_digest", title="jmolt overnight", body=body),
        )
        log.info("jmolt_digest.sent", local_time=local.isoformat())
        return True

    async def build(self, pid: str, admin: SessionContext, *, now: datetime | None = None) -> str:
        """Assemble the sanitized digest body from the ledger + the staged outbox. Only the
        last day's actions are enumerated so the digest's "last day" label is truthful — an
        idle stretch reports nothing published rather than replaying stale ledger rows."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=1)
        async with scoped_session(self._maker, admin) as s:
            recent = await self._ledger.recent(s, pid, limit=_ACTIONS_IN_DIGEST)
            staged = await self._outbox.list_by_status(s, pid, ("queued", "released"))
        actions = [a for a in recent if a.at is None or a.at >= cutoff]
        return build_digest_body(actions, staged)


async def _owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    async with scoped_session(maker, _SYSTEM_OWNER) as s:
        pid = (
            await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner' LIMIT 1"))
        ).scalar()
    return str(pid) if pid is not None else None
