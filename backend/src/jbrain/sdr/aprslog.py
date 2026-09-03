"""The APRS heard log: drain the sidecar's packet stream into `app.aprs_packets`.

docs/plans/APRS_CONTROL_PLAN.md P1.

**Why a background loop rather than a route the PWA opens.** A log that only records
while someone is looking is not a log. The owner enables APRS logging and drives away;
the point is that what arrived while nobody watched is there afterwards. So this runs
for as long as a logging session holds the tuner, and the screen reads rows out of the
table rather than being the thing that fills it.

**Idle is the normal state.** Most of the time no session is logging and this costs one
cheap status check per tick. It attaches only when a session with `purpose == "aprs"`
appears, and lets go the moment it ends.

**Heard text is untrusted.** These rows came off the air from anyone with a transmitter,
and a source callsign is plain bytes that forge trivially. Nothing here interprets a
packet — it is stored, and the two trust tiers in the plan govern what may read it. A
packet reaching a model as a prompt is prompt injection with an antenna.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.sdr.classify import classify

log = structlog.get_logger(__name__)

# How long to wait before looking for a logging session again. A packet channel is not
# urgent and the check is a cheap GET, so this is slow on purpose.
IDLE_POLL_SECONDS = 5.0
# A frame's info field is bounded well under this; the cap exists so a malformed or
# hostile frame cannot write an unbounded row.
MAX_INFO = 512
MAX_RAW = 2048

# The columns `classify` fills in. Cached derivations over `raw`, so every one is
# nullable and a NULL `kind` is the sweep's claim check rather than a broken row.
DERIVED = ("origin_call", "data_type", "kind", "gated", "heard_direct", "addressee")

INSERT_SQL = (
    "INSERT INTO app.aprs_packets"
    " (heard_at, frequency_hz, source, destination, path, info, raw,"
    " origin_call, data_type, kind, gated, heard_direct, addressee, audio_level)"
    " VALUES (to_timestamp(:heard_at), :hz, :src, :dst, :path, :info, :raw,"
    " :origin_call, :data_type, :kind, :gated, :heard_direct, :addressee, :audio_level)"
)

# The sweep's claim, as a constant so a test can EXPLAIN the STATEMENT THE SWEEP RUNS
# rather than asserting that an index with a matching name happens to exist. Newest first
# because the screen shows recent traffic: the owner watches the log become filterable
# from the top down while an old backlog is still working.
BACKFILL_CLAIM_SQL = (
    "SELECT id, source AS src, info, path, raw"
    " FROM app.aprs_packets WHERE kind IS NULL"
    " ORDER BY heard_at DESC LIMIT :batch"
    " FOR UPDATE SKIP LOCKED"
)

BACKFILL_SQL = (
    "UPDATE app.aprs_packets SET origin_call = :origin_call, data_type = :data_type,"
    " kind = :kind, gated = :gated, heard_direct = :heard_direct, addressee = :addressee"
    " WHERE id = :id"
)

# How many unclassified rows one sweep claims. Small on purpose: the sweep runs beside a
# live drain, and a backlog cleared over several minutes is indistinguishable from one
# cleared at once to anyone reading the screen.
BACKFILL_BATCH = 200


class AprsLog:
    """Drains one logging session's packets into the table, then waits for the next."""

    def __init__(
        self,
        maker: async_sessionmaker[AsyncSession],
        base_url: str,
        *,
        on_packet: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._maker = maker
        self._base = base_url
        # Consecutive failed stores. `_store` swallows its errors so one bad row cannot
        # end the drain — correct for liveness, and it means a BROKEN INSERT stops the
        # log silently: new code against an un-migrated schema fails every row, forever,
        # with nothing on any screen saying so. The owner has no terminal (CLAUDE.md rule
        # 10), so the count is surfaced on the APRS tab instead. Reset by any success,
        # because the number that matters is "still failing", not "ever failed".
        self.store_failures = 0
        # The command gate, when one is wired. It is a callback rather than an import so
        # the log keeps knowing nothing about tasks: storing what was heard and acting on
        # it are the plan's two trust tiers, and they stay separable here too.
        self._on_packet = on_packet

    async def tick(self) -> None:
        """Attach to a logging session if there is one, and drain it until it ends."""
        if not self._base:
            return
        async with httpx.AsyncClient(base_url=self._base, timeout=None) as client:
            if not await self._is_logging(client):
                return
            await self._drain(client)

    async def _is_logging(self, client: httpx.AsyncClient) -> bool:
        try:
            resp = await client.get("/healthz", timeout=5.0)
            health: dict[str, Any] = resp.json()
        except (httpx.HTTPError, ValueError):
            return False
        session = health.get("listening") or {}
        return bool(session.get("purpose") == "aprs")

    async def _drain(self, client: httpx.AsyncClient) -> None:
        """Read frames until the stream ends, storing each one.

        The stream ending is the normal exit: the owner released the radio, or it was
        retuned. Errors are swallowed to the log for the same reason — this loop
        outlives any one session, and a sidecar restart must not end it."""
        try:
            async with client.stream("GET", "/listen/packets") as upstream:
                if upstream.status_code != 200:
                    return
                async for line in upstream.aiter_lines():
                    row = _parse(line)
                    if row is None:
                        continue
                    await self._store(row)
                    await self._offer(row)
        except (httpx.HTTPError, ValueError) as exc:
            log.info("aprs_log.stream_ended", error=repr(exc))

    async def _offer(self, row: dict[str, Any]) -> None:
        """Hand the frame to the command gate, if any.

        After the store and never instead of it: a frame that could not be logged is
        still a frame that was heard, and a bookkeeping failure must not decide whether
        the owner's gate opens. Errors stay here — a gate that raises would otherwise
        end the drain, which is the failure mode where the radio goes quiet and nothing
        says so."""
        if self._on_packet is None:
            return
        try:
            await self._on_packet(row)
        except Exception as exc:  # noqa: BLE001 — one frame must not end the drain
            log.warning("aprs_log.gate_failed", error=repr(exc))

    async def backfill(self, *, batch: int = BACKFILL_BATCH) -> int:
        """Classify rows that have none yet, newest first. Returns how many it filled.

        The derived columns are a cache over `raw`, and this is what makes that claim
        true: rows written before the columns existed, and rows written by a classifier
        that has since been improved, are re-derived here rather than staying wrong.
        Re-deriving costs nothing but CPU because `raw` is stored losslessly.

        Costs one index-only probe when there is nothing to do: the partial index this
        reads is EMPTY once the table is fully derived."""
        try:
            async with scoped_session(self._maker, SessionContext(principal_kind="owner")) as s:
                rows = (
                    await s.execute(text(BACKFILL_CLAIM_SQL), {"batch": max(1, int(batch))})
                ).mappings()
                # A row the classifier cannot read is LEFT NULL rather than labelled
                # wrong, which means a genuine classifier bug leaves that row for the
                # next sweep. That is the intended failure: the batch shrinks to just
                # the unreadable rows, they cost one bounded query per interval, and
                # they heal the moment the classifier is fixed.
                filled = 0
                for row in rows:
                    values = derive(dict(row))
                    if values["kind"] is None:
                        continue
                    # A SAVEPOINT per row, because the batch is claimed from a channel
                    # anyone can transmit on. One row Postgres refuses — a byte that
                    # cannot live in a text column is the reachable case — would
                    # otherwise abort the whole statement, leave every row in the batch
                    # unclassified, and be re-selected every minute forever. One bad
                    # frame, once, would permanently stop the archive from ever being
                    # classified, and the only trace is a log line the owner has no
                    # terminal to read (CLAUDE.md rule 10).
                    try:
                        async with s.begin_nested():
                            await s.execute(text(BACKFILL_SQL), {"id": row["id"], **values})
                    except Exception as exc:  # noqa: BLE001 — one row, not the sweep
                        log.warning("aprs_log.backfill_row_failed", error=repr(exc))
                        continue
                    filled += 1
                await s.commit()
        except Exception as exc:  # noqa: BLE001 — the sweep retries; it never ends a loop
            log.warning("aprs_log.backfill_failed", error=repr(exc))
            return 0
        if filled:
            log.info("aprs_log.backfilled", rows=filled)
        return filled

    async def _store(self, row: dict[str, Any]) -> None:
        try:
            async with scoped_session(self._maker, SessionContext(principal_kind="owner")) as s:
                await s.execute(
                    text(INSERT_SQL),
                    row,
                )
                await s.commit()
            self.store_failures = 0
        except Exception as exc:  # noqa: BLE001 — one bad row must not end the log
            self.store_failures += 1
            log.warning("aprs_log.store_failed", error=repr(exc), consecutive=self.store_failures)


def _parse(line: str) -> dict[str, Any] | None:
    """One newline-framed row from the sidecar, bounded, or None to skip it.

    Every field is treated as hostile: the sidecar decoded it, but what it decoded came
    off a shared channel. Lengths are capped here rather than trusted from the wire."""
    if not line.strip():
        return None
    try:
        payload = json.loads(line)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("keepalive"):
        return None
    source = str(payload.get("source") or "")
    info = str(payload.get("info") or "")
    if not source:
        return None  # a frame with no sender is not attributable, so it is not a row
    try:
        hz = int(payload.get("frequency_hz") or 0)
    except (TypeError, ValueError):
        # A crafted frequency must SKIP one row, not end the drain. `_drain` catches
        # ValueError around the whole read loop, so raising here would let a single
        # hostile frame stop the log until the session was restarted.
        return None
    path = payload.get("path")
    try:
        heard_at = float(payload.get("heard_at") or 0.0)
    except (TypeError, ValueError):
        heard_at = 0.0
    if heard_at <= 0:
        # A sidecar too old to stamp the frame, or a crafted one. Falling back to now is
        # honest — it is when we learned of it — and far better than storing 1970.
        heard_at = time.time()
    row = {
        "heard_at": heard_at,
        "hz": hz,
        "src": _clean(source)[:16],
        "dst": _clean(str(payload.get("destination") or ""))[:16],
        "path": [_clean(str(p))[:16] for p in path][:8] if isinstance(path, list) else [],
        "info": _clean(info)[:MAX_INFO],
        "raw": _clean(str(payload.get("raw") or ""))[:MAX_RAW],
        "audio_level": _level(payload.get("audio_level")),
    }
    return row | derive(row)


def _level(raw: Any) -> int | None:
    """How strong the transmission was, or None where nothing was measured.

    Unlike every other column here this one CANNOT be recovered from `raw` later — the
    reading exists only at decode time — so a sidecar too old to send it, or a frame
    whose level could not be paired, leaves NULL for ever. NULL therefore has to mean
    "not measured" and never "weak"; 0 is a real reading and stays one.

    Out-of-range is dropped rather than clamped. The sidecar already clamps what
    direwolf says, so a value outside 0-100 arriving here means the two are out of step,
    and a made-up number is worse than a blank."""
    if raw is None:
        return None
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return None
    return level if 0 <= level <= 100 else None


def derive(row: dict[str, Any]) -> dict[str, Any]:
    """The classifier's output as columns, or empty values if it cannot read the frame.

    Deriving here rather than in SQL keeps one implementation for the live path and the
    backfill, and keeps it testable without a database. It is wrapped because the whole
    point of the derived columns being nullable is that a classification failure costs a
    label and never a packet — `raw` is stored losslessly either way, so an unclassified
    row is one the sweep picks up again after the classifier improves."""
    try:
        heard = classify(
            str(row.get("src") or ""),
            str(row.get("info") or ""),
            list(row.get("path") or []),
            str(row.get("raw") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — a label is never worth losing a frame
        log.warning("aprs_log.classify_failed", error=repr(exc))
        return dict.fromkeys(DERIVED)
    return {
        "origin_call": heard.origin,
        "data_type": heard.dti,
        "kind": heard.kind,
        "gated": heard.gated,
        "heard_direct": heard.direct,
        "addressee": heard.addressee,
    }


def _clean(text: str) -> str:
    """Drop what Postgres `text` cannot hold, and what a reader would be confused by.

    A NUL is the one that matters. Postgres rejects it in a text column, both INSERTs on
    this path swallow their errors so the log keeps running, and the code comparison
    strips it before matching — so a NUL-suffixed code behaves exactly like the clean one
    while leaving NO row in either table. Five of those lock the command with no packet
    logged and no attempt recorded: one byte, and the evidence the whole design leans on
    is gone.

    Other control characters go with it. They mean nothing in an AX.25 info field, and
    this text is quoted, rendered and read back by a person."""
    return "".join(ch for ch in text if ch == "\t" or ch >= " ")


async def run_aprs_log_loop(logger: AprsLog, *, interval: float = IDLE_POLL_SECONDS) -> None:
    """Forever: attach to a logging session when one exists, drain it, wait, repeat."""
    while True:
        try:
            await logger.tick()
        except Exception as exc:  # noqa: BLE001 — the loop outlives every session
            log.warning("aprs_log.tick_error", error=repr(exc))
        await asyncio.sleep(interval)


# Slow on purpose. Nothing waits on the sweep: live frames are classified as they are
# stored, so this only ever works a backlog, and a backlog is by definition old.
BACKFILL_POLL_SECONDS = 60.0


async def run_aprs_backfill_loop(
    logger: AprsLog, *, interval: float = BACKFILL_POLL_SECONDS
) -> None:
    """Forever: fill in rows the classifier has not seen.

    Its own loop rather than a step inside the drain, because `tick` stays attached for
    as long as the owner is logging — which is the point of the log, and can be days. A
    backlog must not wait on the radio being idle."""
    while True:
        try:
            await logger.backfill()
        except Exception as exc:  # noqa: BLE001 — the loop outlives every sweep
            log.warning("aprs_log.backfill_error", error=repr(exc))
        await asyncio.sleep(interval)


# --- reading the log back ------------------------------------------------------------

# What jerv gets by default, and the ceiling however large a limit it asks for. A packet
# channel produces a lot of rows and a turn does not need most of them.
RECENT_DEFAULT = 20
RECENT_MAX = 100
# Stations in a digest. A busy channel has tens, not hundreds, and a turn that needs
# more than this wants the frames rather than the summary.
DIGEST_MAX = 40


class AprsReader:
    """Reads the heard log back, on the caller's RLS scope.

    A service holding the maker, like `SearchService` and the other readers a tool is
    handed — the scope arrives per call because it belongs to the turn, not to the
    reader. The owner-only policy on the table decides visibility, so a non-owner gets
    an empty list from Postgres rather than a check here (CLAUDE.md rule 3)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker

    async def recent(
        self,
        ctx: SessionContext,
        *,
        limit: int = RECENT_DEFAULT,
        source: str | None = None,
        station: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """The most recently heard packets, newest first.

        `station` and `source` are NOT the same question, and the difference is the
        whole reason F1 exists. `source` is the AX.25 sender — for three quarters of
        this channel that is the IGate, not whoever wrote the packet. `station` matches
        `origin_call`, the true sender, so "has KD4WLE been heard" finally answers about
        KD4WLE. `source` is kept because "what has this RELAY put on the air" is a real
        question too, just a different one.

        Selects `raw` and `path` as well, because the caller renders the frame into
        something readable rather than pasting the info field at a model."""
        bounded = max(1, min(int(limit), RECENT_MAX))
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": bounded}
        if source:
            clauses.append("source = :source")
            params["source"] = source
        if station:
            # An unclassified row (the sweep has not reached it) has a NULL
            # `origin_call`, and for a DIRECT frame the sender IS `source` — so falling
            # back keeps a station's own traffic findable while the backlog fills in,
            # rather than reporting it as never heard.
            clauses.append("COALESCE(origin_call, source) = :station")
            params["station"] = station
        if kind:
            clauses.append("kind = :kind")
            params["kind"] = kind
        if since is not None:
            clauses.append("heard_at >= :since")
            params["since"] = since
        if until is not None:
            clauses.append("heard_at <= :until")
            params["until"] = until
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT heard_at, frequency_hz, source, destination, path, info,"
                        " raw, origin_call, kind, gated, heard_direct, audio_level"
                        f" FROM app.aprs_packets {where}"
                        " ORDER BY heard_at DESC LIMIT :limit"
                    ),
                    params,
                )
            ).mappings()
            return [dict(row) for row in rows]

    async def digest(
        self,
        ctx: SessionContext,
        *,
        station: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Who was heard and how much, one row per station, busiest first.

        "Who is around" is a question about STATIONS, and answering it with a list of
        frames makes a model read fifty lines to count six callsigns — on this channel
        one relay would fill the whole window on its own. Grouped on `origin_call` for
        the same reason `recent` filters on it.

        The count is over the whole window, not over a page of it, so it does not change
        meaning when a limit truncates the frames."""
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if station:
            clauses.append("COALESCE(origin_call, source) = :station")
            params["station"] = station
        if kind:
            clauses.append("kind = :kind")
            params["kind"] = kind
        if since is not None:
            clauses.append("heard_at >= :since")
            params["since"] = since
        if until is not None:
            clauses.append("heard_at <= :until")
            params["until"] = until
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT COALESCE(origin_call, source) AS station,"
                        " count(*) AS packets,"
                        " max(heard_at) AS last_heard_at,"
                        " bool_or(heard_direct) AS direct,"
                        " bool_and(gated) AS gated,"
                        " max(audio_level) AS best_level,"
                        " array_agg(DISTINCT kind) FILTER (WHERE kind IS NOT NULL)"
                        " AS kinds"
                        f" FROM app.aprs_packets {where}"
                        " GROUP BY 1 ORDER BY packets DESC, last_heard_at DESC"
                        f" LIMIT {DIGEST_MAX}"
                    ),
                    params,
                )
            ).mappings()
            return [dict(row) for row in rows]
