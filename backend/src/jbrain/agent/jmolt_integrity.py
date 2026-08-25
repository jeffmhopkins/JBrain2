"""jmolt's integrity watch (docs/plans/JMOLT_PLAN.md, W4 — M21, M22).

Two premise-independent controls that run as a slow system loop in the web process,
under a NON-jmolt owner context (so they may read jmolt's outbox and flip the kill /
autonomy settings jmolt itself can never touch, per the M7 split):

- **Tamper watch (M21)** — diff jmolt's actually-posted content (its public profile's
  recent posts) against the outbox ledger. A post present on the profile but with no
  matching PUBLISHED outbox row means something wrote as jmolt that did not go through
  the outbox — a key leak. Engage the global kill (M6), revert the autonomy switch to
  OFF (M7), record the `tamper` state, and notify the owner that the key must be rotated.

- **Account-state surfacing (M22)** — read the account's own view (`/agents/me`) and
  normalise it to a state. A platform SUSPENSION/ban auto-pauses the lane + drip (kill +
  switch OFF) and notifies; a softer moderation label or hard rate-limit is surfaced to
  the owner but not auto-paused. jmolt holds no tool that could answer a moderation
  event, so this is the only channel that ever does — and it never auto-answers.

Both dedup on the stored `moltbook_account_state` so the owner is notified (and the kill
engaged) on the TRANSITION into a bad state, not every tick. The watch never raises: a
platform read failure (unregistered, offline, 401) is a "cannot check", never a false
tamper — only a definitive foreign post trips the alarm.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.models.jmolt_outbox import OutboxRepo
from jbrain.notify import Notification, NotifyBus, notify_owner
from jbrain.settings_store import SqlSettingsStore
from jbrain.web.moltbook import MoltbookClient, MoltbookError

log = structlog.get_logger()

# Every 15 minutes — often enough to catch a suspension or a foreign post within a night,
# rare enough not to spend jmolt's local read-rate budget (each pass is two GETs).
JMOLT_INTEGRITY_SECONDS = 900.0

_STATE_OK = "ok"
_STATE_SUSPENDED = "suspended"
_STATE_MODERATED = "moderated"
_STATE_TAMPER = "tamper"
# States that auto-pause the account (kill + switch OFF). "moderated" is surfaced only.
_PAUSE_STATES = frozenset({_STATE_SUSPENDED, _STATE_TAMPER})

_SYSTEM_OWNER = SessionContext(principal_kind="owner")


def _admin_ctx(pid: str) -> SessionContext:
    """A NON-jmolt owner context: reads jmolt's outbox and writes the kill/switch/state
    settings, none of which jmolt's own auth context may touch."""
    return SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))


async def _owner_principal_id(maker: async_sessionmaker[AsyncSession]) -> str | None:
    async with scoped_session(maker, _SYSTEM_OWNER) as s:
        pid = (
            await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner' LIMIT 1"))
        ).scalar()
    return str(pid) if pid is not None else None


# Status strings that mean the account is out of action → auto-pause. A wide net on
# purpose (L6, fail-safe): the platform's exact vocabulary is not contractual, so recognise
# the common synonyms for "suspended/disabled" rather than defaulting an unknown bad state
# to healthy.
_SUSPENDED_WORDS = frozenset(
    {"suspended", "banned", "disabled", "deactivated", "locked", "terminated", "removed", "closed"}
)
_MODERATED_WORDS = frozenset({"moderated", "limited", "restricted", "flagged", "shadowbanned"})


def classify_account(me: dict) -> str:
    """Normalise the platform's `/agents/me` view to one of our states. Defensive about the
    exact schema and biased fail-safe: any recognised suspension/disable shape pauses, a
    softer moderation/limit shape surfaces, and only a view with no bad signal is `ok`.
    Pure — unit-tested against each shape."""
    status = str(me.get("status") or me.get("account_status") or "").strip().lower()
    if (
        me.get("suspended")
        or me.get("banned")
        or me.get("disabled")
        or status in _SUSPENDED_WORDS
    ):
        return _STATE_SUSPENDED
    if status in _MODERATED_WORDS:
        return _STATE_MODERATED
    if me.get("rate_limited") or me.get("moderation") or me.get("labels"):
        return _STATE_MODERATED
    return _STATE_OK


def _profile_item_ids(me: dict) -> list[str]:
    """The platform ids of everything visible on jmolt's public profile — its recent posts
    AND comments (a key leak can write either). Defensive about the schema; an item with no
    id yields '' so the caller treats it as unaccounted-for (fail-safe)."""
    items: list[str] = []
    for key in ("recentPosts", "recent_posts", "recentComments", "recent_comments"):
        seq = me.get(key)
        if isinstance(seq, list):
            items.extend(str(x.get("id") or "").strip() for x in seq if isinstance(x, dict))
    return items


class JmoltIntegrity:
    def __init__(
        self,
        *,
        maker: async_sessionmaker[AsyncSession],
        client: MoltbookClient,
        settings_store: SqlSettingsStore,
        notify: NotifyBus | None = None,
    ) -> None:
        self._maker = maker
        self._client = client
        self._settings = settings_store
        self._notify = notify
        self._outbox = OutboxRepo()

    async def check(self, *, now: datetime | None = None) -> str:
        """One integrity pass. Returns the observed state ("ok" when healthy or
        uncheckable). Never raises."""
        now = now or datetime.now(UTC)
        pid = await _owner_principal_id(self._maker)
        if pid is None:
            return _STATE_OK
        admin = _admin_ctx(pid)

        # Tamper takes precedence over account-state: a foreign post is the more serious
        # finding, and its message names the rotation the owner must do.
        state = await self._observe(pid, admin)
        if state is None:  # could not read the platform — leave prior state untouched.
            return await self._settings.moltbook_account_state(admin)

        previous = await self._settings.moltbook_account_state(admin)
        # Enforce the pause on EVERY tick while a bad state persists (idempotent), not only
        # on the transition: a suspended or tampered (key-leaked) account must not become
        # writable again just because the owner cleared the kill to investigate — only a
        # recovery to a healthy state lifts the auto-pause. This is the security-critical
        # half; the notification below is deduped to the transition so it never spams.
        if state in _PAUSE_STATES:
            await self._settings.set_moltbook_killed(admin, True)  # M6
            await self._settings.set_moltbook_autonomy(admin, False)  # M7 auto-revert

        if state == previous:
            return state  # steady state — pause already re-enforced above; don't re-notify.

        await self._settings.set_moltbook_account_state(admin, state)
        self._notify_transition(state, previous)
        return state

    async def _observe(self, pid: str, admin: SessionContext) -> str | None:
        """The observed state, or None if the platform could not be read (never a false
        tamper). One `/agents/me` read feeds both checks: tamper first, then account state."""
        try:
            me = await self._client.me()
        except MoltbookError:
            return None
        if await self._tampered(pid, admin, me):
            return _STATE_TAMPER
        return classify_account(me)

    async def _tampered(self, pid: str, admin: SessionContext, me: dict) -> bool:
        """True iff the public profile carries a post or comment whose platform id no
        PUBLISHED outbox row accounts for — i.e. something wrote as jmolt outside the review
        queue (a key leak). Matches on the platform id ONLY, never the (attacker-controllable)
        title, and treats an item with no id as unaccounted-for: the fail-safe direction,
        since a false positive only pauses + asks the owner to rotate, while a false negative
        misses a real leak. Best-effort by nature (the profile shows a bounded recent window
        the platform controls); a flood that pushes an item out of view is a residual gap."""
        items = _profile_item_ids(me)
        if not items:
            return False
        async with scoped_session(self._maker, admin) as s:
            published = await self._outbox.list_by_status(s, pid, ("published",))
        # Every published write (post OR comment) records its platform id in the outbox row.
        known_ids = {r.moltbook_id for r in published if r.moltbook_id}
        for item_id in items:
            if item_id and item_id in known_ids:
                continue
            log.warning("jmolt_integrity.tamper_item", moltbook_id=item_id or None)
            return True
        return False

    def _notify_transition(self, state: str, previous: str) -> None:
        if state == _STATE_TAMPER:
            note = Notification(
                kind="jmolt_alert",
                title="jmolt: possible key leak",
                body="A post appeared on jmolt's profile that did not go through the review "
                "queue. Moltbook writing is stopped and autonomy is off. Rotate the Moltbook "
                "key and check the account.",
            )
        elif state == _STATE_SUSPENDED:
            note = Notification(
                kind="jmolt_alert",
                title="jmolt account suspended",
                body="Moltbook reports the account as suspended. The nightly run and drip are "
                "paused and autonomy is off until you sort it out with the platform.",
            )
        elif state == _STATE_MODERATED:
            note = Notification(
                kind="jmolt_alert",
                title="jmolt account flagged",
                body="Moltbook has flagged or rate-limited jmolt's account. Nothing is paused "
                "automatically — take a look when you can.",
            )
        else:  # recovered to ok
            note = Notification(
                kind="jmolt_status",
                title="jmolt account healthy again",
                body=f"jmolt's account state returned to normal (was {previous}). The kill "
                "switch stays engaged until you clear it.",
            )
        notify_owner(self._notify, note)


async def run_jmolt_integrity_loop(
    integrity: JmoltIntegrity, *, interval: float = JMOLT_INTEGRITY_SECONDS
) -> None:
    while True:
        try:
            await integrity.check()
        except Exception as exc:  # noqa: BLE001 — the watch must not kill the loop
            log.warning("jmolt_integrity.check_error", error=repr(exc))
        await asyncio.sleep(interval)
