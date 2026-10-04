"""Whether the owner's Tavily key is still working — and telling him when it stops.

Tavily is the box's last web-search tier (after SearXNG and Brave) and the last web_fetch
recovery tier, on a free plan with a monthly credit allowance. When that runs out every call
fails the same way until the month resets, and before this the only trace was a
`web.tavily_failed` log line the owner (no terminal) never sees: search quietly fell back to the
scraper engines it was brought in to replace. This module turns those failures into a STATE:
kept in owner-only app.settings so the Settings panel can show it, announced once per change on
the owner's devices, and used to stop calling a key that cannot answer (a cooldown) instead of
paying a round trip per search to learn it again.

Tavily's documented statuses: 429 is a per-minute rate limit, 432 the plan's credit limit, 433
the pay-as-you-go spending cap, 401/403 a rejected key. Anything else is a transient error and
is not a state worth announcing.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

import structlog

log = structlog.get_logger()

OK = "ok"
QUOTA = "quota"
RATE_LIMITED = "rate_limited"
KEY_REJECTED = "key_rejected"

_STATES: dict[int, tuple[str, str]] = {
    432: (QUOTA, "Tavily's plan credit limit is used up (HTTP 432)"),
    433: (QUOTA, "Tavily's pay-as-you-go spending limit is reached (HTTP 433)"),
    429: (RATE_LIMITED, "Tavily is rate-limiting this key (HTTP 429)"),
    401: (KEY_REJECTED, "Tavily rejected the API key (HTTP 401)"),
    403: (KEY_REJECTED, "Tavily rejected the API key (HTTP 403)"),
}

# How long to stop calling after each failure. A spent allowance only returns at the monthly
# reset, so an hourly re-probe costs one failed call an hour; a rate limit clears in a minute.
# A rejected key is not cooled: the owner fixes it in Settings and the next call should try it.
_COOLDOWN_S: dict[str, float] = {QUOTA: 3600.0, RATE_LIMITED: 60.0}

# The notification kind the owner app routes on.
NOTIFY_KIND = "tavily_health"


@dataclass(frozen=True)
class HealthState:
    """The stored record: the current state, when it began (ISO, UTC), why, and which leg
    (`search` or `fetch`) observed it."""

    state: str = OK
    since: str = ""
    detail: str = ""
    leg: str = ""

    @classmethod
    def parse(cls, raw: object) -> HealthState:
        if not isinstance(raw, dict):
            return cls()
        state = raw.get("state")
        if state not in (OK, QUOTA, RATE_LIMITED, KEY_REJECTED):
            return cls()
        return cls(
            state=str(state),
            since=str(raw.get("since") or ""),
            detail=str(raw.get("detail") or ""),
            leg=str(raw.get("leg") or ""),
        )


class TavilyHealth:
    """Shared by the search and fetch legs (one per process). `load`/`save` read and write the
    stored record; `notify(title, body)` reaches the owner's devices. All three are best-effort:
    a failure to record or announce never fails the search that observed it."""

    def __init__(
        self,
        *,
        load: Callable[[], Awaitable[object]],
        save: Callable[[dict[str, str]], Awaitable[None]],
        notify: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._load = load
        self._save = save
        self._notify = notify
        self._clock = clock
        self._now = now
        self._state: HealthState | None = None  # loaded on first use, then kept in step
        self._cooldown_until = 0.0

    def cooling_down(self) -> bool:
        """True while a recent quota/rate failure says the next call would fail too."""
        return self._clock() < self._cooldown_until

    async def current(self) -> HealthState:
        if self._state is None:
            try:
                self._state = HealthState.parse(await self._load())
            except Exception:  # noqa: BLE001
                log.warning("web.tavily_health_unreadable", exc_info=True)
                self._state = HealthState()
        return self._state

    async def failed(self, status: int, leg: str) -> str:
        """Record a failed call; returns the human reason, or "" for a status that is not a
        health state (a 5xx, a timeout) and so changes nothing."""
        mapped = _STATES.get(status)
        if mapped is None:
            return ""
        state, detail = mapped
        self._cooldown_until = self._clock() + _COOLDOWN_S.get(state, 0.0)
        prior = await self.current()
        if prior.state != state:
            await self._transition(HealthState(state, self._now().isoformat(), detail, leg))
            if state != RATE_LIMITED:  # a minute-long throttle is not worth a phone buzz
                self._announce(
                    "Tavily search is failing",
                    f"{detail}. Web search has fallen back to the box's own engines, which are "
                    "mostly blocked — results will be poor until this clears.",
                )
        return detail

    async def succeeded(self, leg: str) -> None:
        prior = await self.current()
        self._cooldown_until = 0.0
        if prior.state == OK:
            return
        await self._transition(HealthState(OK, self._now().isoformat(), "", leg))
        if prior.state != RATE_LIMITED:
            self._announce("Tavily search is working again", "Web search is back on Tavily.")

    async def _transition(self, new: HealthState) -> None:
        self._state = new
        log.info("web.tavily_health", state=new.state, leg=new.leg, detail=new.detail)
        try:
            await self._save(asdict(new))
        except Exception:  # noqa: BLE001
            log.warning("web.tavily_health_save_failed", exc_info=True)

    def _announce(self, title: str, body: str) -> None:
        if self._notify is None:
            return
        try:
            self._notify(title, body)
        except Exception:  # noqa: BLE001
            log.warning("web.tavily_health_notify_failed", exc_info=True)
