"""jmolt outbox + action-ledger repos (migration 0174, docs/plans/JMOLT_PLAN.md W3).

`OutboxRepo` stages Moltbook writes and drives their lifecycle; `ActionLedgerRepo` is the
append-only record of everything jmolt did (M14). Both run on caller-supplied RLS-scoped
sessions — the authority split (jmolt stages, a non-jmolt owner advances/prunes, the domain
scope reads) is Postgres', not these methods'.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Ledger retention (M13-style bound): keep the last N actions per principal.
LEDGER_RETENTION = 5000


@dataclass(frozen=True)
class OutboxRow:
    id: str
    kind: str
    payload: dict[str, Any]
    status: str
    publish_at: datetime | None
    moltbook_id: str | None
    error: str | None
    created_at: datetime
    published_at: datetime | None
    # The outbox's own monotonic key — the keyset cursor for the activity feed's "show older".
    seq: int = 0
    # A simulated write (JMOLT_LEDGER_ENGINE_PLAN.md, S1). `due` cannot see it, so the drip
    # sweep can never publish it to the real Moltbook.
    sim: bool = False


@dataclass(frozen=True)
class LedgerRow:
    action: str
    target: str | None
    reacted_to: str | None
    detail: dict[str, Any] | None
    at: datetime
    # The ledger's own monotonic key — the keyset cursor for "show older" paging. 0 when the
    # reader (e.g. `recent`) does not select it.
    seq: int = 0


# The two action FAMILIES (the `action` prefix before the first underscore): `stage_*` rows
# are what jmolt DRAFTED, `publish_*` rows are what the drip actually sent. Splitting on this
# separates the signal (drafts, which carry `reacted_to` content) from the drip's bookkeeping.
LEDGER_FAMILIES = ("stage", "publish")


def _row_to_outbox(r: Any) -> OutboxRow:
    payload = r.payload if isinstance(r.payload, dict) else json.loads(r.payload or "{}")
    return OutboxRow(
        id=str(r.id),
        kind=r.kind,
        payload=payload,
        status=r.status,
        publish_at=r.publish_at,
        moltbook_id=r.moltbook_id,
        error=r.error,
        created_at=r.created_at,
        published_at=r.published_at,
        seq=int(r.seq),
        sim=bool(getattr(r, "sim", False)),
    )


class OutboxRepo:
    async def stage(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        kind: str,
        payload: dict[str, Any],
        publish_at: datetime | None = None,
        dedup_key: str | None = None,
        sim: bool = False,
    ) -> str | None:
        """Stage a write (jmolt context). Returns the new row id — or None when `dedup_key`
        is set and an identical write is already staged for this principal, which the partial
        unique index `jmolt_outbox_dedup` (migration 0177) turns into a no-op via ON CONFLICT.
        A fresh-context sitting cannot see its own pending queue, so this is what stops it from
        re-staging the same vote/follow/comment it staged an hour earlier.

        `sim=True` marks the row a simulated write: `due` cannot see it, so no sweep will
        ever publish it (migration 0180). Everything else about the row is identical, which
        is the point — the simulator measures the production staging path, guards included."""
        row = (
            await session.execute(
                text(
                    "INSERT INTO app.jmolt_outbox"
                    " (principal_id, kind, payload, publish_at, dedup_key, sim)"
                    " VALUES (:pid, :kind, cast(:payload AS jsonb), :pub, :dk, :sim)"
                    " ON CONFLICT (principal_id, dedup_key)"
                    " WHERE dedup_key IS NOT NULL DO NOTHING"
                    " RETURNING id"
                ),
                {
                    "pid": principal_id,
                    "kind": kind,
                    "payload": json.dumps(payload),
                    "pub": publish_at,
                    "dk": dedup_key,
                    "sim": sim,
                },
            )
        ).scalar_one_or_none()
        return str(row) if row is not None else None

    async def staged_count_since(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        kinds: tuple[str, ...],
        since: datetime,
    ) -> int:
        """How many live writes of `kinds` this principal has staged since `since` (a UTC
        instant — the caller passes the start of the owner-local day). Counts everything not
        owner-rejected or failed, so a released/published write still consumes the night's
        budget. Backs the per-night comment/vote/follow caps."""
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.jmolt_outbox"
                    " WHERE principal_id = :pid AND kind = ANY(:kinds)"
                    " AND status IN ('queued', 'released', 'published')"
                    " AND created_at >= :since"
                ),
                {"pid": principal_id, "kinds": list(kinds), "since": since},
            )
        ).scalar_one()
        return int(count)

    async def comment_counts_on_post(
        self, session: AsyncSession, principal_id: str, *, post_id: str, since: datetime
    ) -> tuple[int, int]:
        """(total, top_level) comments staged on one post since an instant.

        Read from the OUTBOX rather than the action ledger because only the outbox carries
        the payload, and `parent_id` is what separates a genuine threaded reply from a second
        opening remark on the same post.

        Counts `queued`, `released` and `published` — a published comment is still one jmolt
        made, and the drip publishes within a minute of staging, so filtering to unpublished
        rows would blind the cap almost immediately. Excludes `failed` and `discarded`,
        because neither is a comment jmolt actually placed: on the live box six of seven
        failures were the platform rate-limiting us, and counting those would let one 429
        permanently burn a post's top-level allowance and then tell jmolt "you already
        commented here" about something nobody can see. A draft the owner discarded is the
        owner's decision, not jmolt's quota."""
        row = (
            await session.execute(
                text(
                    "SELECT count(*) AS total,"
                    " count(*) FILTER (WHERE payload->>'parent_id' IS NULL) AS top_level"
                    " FROM app.jmolt_outbox WHERE principal_id = :pid AND kind = 'comment'"
                    " AND payload->>'post_id' = :post AND created_at >= :since"
                    " AND status IN ('queued', 'released', 'published')"
                ),
                {"pid": principal_id, "post": post_id, "since": since},
            )
        ).one()
        return int(row.total), int(row.top_level)

    async def get(self, session: AsyncSession, row_id: str) -> OutboxRow | None:
        """One row by id, or None. For the direct-publish path, which stages a row and then
        publishes that same row rather than sweeping the whole queue."""
        row = (
            await session.execute(
                text("SELECT * FROM app.jmolt_outbox WHERE id = :id"), {"id": row_id}
            )
        ).first()
        return _row_to_outbox(row) if row is not None else None

    async def list_by_status(
        self, session: AsyncSession, principal_id: str, statuses: tuple[str, ...]
    ) -> list[OutboxRow]:
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM app.jmolt_outbox"
                    " WHERE principal_id = :pid AND status = ANY(:st)"
                    " ORDER BY coalesce(publish_at, created_at)"
                ),
                {"pid": principal_id, "st": list(statuses)},
            )
        ).all()
        return [_row_to_outbox(r) for r in rows]

    async def list_activity(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        statuses: tuple[str, ...],
        kinds: tuple[str, ...] | None = None,
        cursor: int | None = None,
        limit: int = 100,
    ) -> list[OutboxRow]:
        """The owner-facing activity feed, newest first — one row per thing jmolt did, each
        carrying its own status (Drafted → Scheduled → Published, or Failed). Unlike the action
        ledger (two look-alike log rows per action, no status, no id), the outbox is the source
        that can show per-row status and a link to the published item. `statuses` selects the
        lifecycle slice (the drafted/published segment filter), `kinds` narrows the row kinds,
        and `cursor` (a `seq`) pages older."""
        clauses = ["principal_id = :pid", "status = ANY(:st)"]
        params: dict[str, Any] = {"pid": principal_id, "st": list(statuses), "lim": limit}
        if kinds:
            clauses.append("kind = ANY(:kinds)")
            params["kinds"] = list(kinds)
        if cursor:
            clauses.append("seq < :cursor")
            params["cursor"] = cursor
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM app.jmolt_outbox WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY seq DESC LIMIT :lim"
                ),
                params,
            )
        ).all()
        return [_row_to_outbox(r) for r in rows]

    async def activity_counts(
        self, session: AsyncSession, principal_id: str, *, statuses: tuple[str, ...]
    ) -> list[tuple[str, int]]:
        """Per-kind counts over the same status slice — so the filter chips show honest totals
        independent of the segment currently selected (the runs-log `/stats` pattern). Returns
        (kind, count) rows."""
        rows = (
            await session.execute(
                text(
                    "SELECT kind, count(*) AS n FROM app.jmolt_outbox"
                    " WHERE principal_id = :pid AND status = ANY(:st) GROUP BY kind"
                ),
                {"pid": principal_id, "st": list(statuses)},
            )
        ).all()
        return [(r.kind, int(r.n)) for r in rows]

    async def due(
        self, session: AsyncSession, *, now: datetime, sim: bool = False
    ) -> list[OutboxRow]:
        """Released rows ready to publish: comments/votes/social/profile (no publish_at) and
        posts whose publish_at has arrived. For the drip sweep (system owner context).

        This is the ONE outbox query with no principal filter — the sweep runs box-wide — so
        it is also the one place a simulated write could reach the real Moltbook. The `sim`
        predicate is that fence — it DEFAULTS to false, so every production caller is fenced
        without knowing the simulator exists — and it is the query the
        `jmolt_outbox_publishable` index serves. The simulator's own sweep passes True and
        gets the disjoint half: the two worlds are never mixed in one result."""
        rows = (
            await session.execute(
                text(
                    "SELECT * FROM app.jmolt_outbox"
                    " WHERE status = 'released' AND sim = :sim"
                    " AND (publish_at IS NULL OR publish_at <= :now)"
                    " ORDER BY seq"
                ),
                {"now": now, "sim": sim},
            )
        ).all()
        return [_row_to_outbox(r) for r in rows]

    async def set_status(
        self,
        session: AsyncSession,
        row_id: str,
        status: str,
        *,
        moltbook_id: str | None = None,
        error: str | None = None,
        published: bool = False,
    ) -> None:
        """Advance a row (non-jmolt owner context: PWA release/discard, or sweep publish)."""
        await session.execute(
            text(
                "UPDATE app.jmolt_outbox SET status = :st, moltbook_id = :mid, error = :err,"
                " published_at = CASE WHEN :pub THEN now() ELSE published_at END"
                " WHERE id = :id"
            ),
            {"st": status, "mid": moltbook_id, "err": error, "pub": published, "id": row_id},
        )

    async def recent_published_posts(
        self, session: AsyncSession, principal_id: str, *, limit: int = 30
    ) -> list[str]:
        """Titles+bodies of recently published posts, for the near-dup check (M9)."""
        rows = (
            await session.execute(
                text(
                    "SELECT payload FROM app.jmolt_outbox"
                    " WHERE principal_id = :pid AND kind = 'post'"
                    " AND status IN ('published', 'released', 'queued')"
                    " ORDER BY seq DESC LIMIT :lim"
                ),
                {"pid": principal_id, "lim": limit},
            )
        ).all()
        out = []
        for r in rows:
            p = r.payload if isinstance(r.payload, dict) else json.loads(r.payload or "{}")
            out.append(f"{p.get('title', '')} {p.get('content', '')}".strip())
        return out

    async def staged_post_times(self, session: AsyncSession, principal_id: str) -> list[datetime]:
        """publish_at of posts not yet failed/discarded — for the M10 clamp."""
        rows = (
            await session.execute(
                text(
                    "SELECT publish_at FROM app.jmolt_outbox"
                    " WHERE principal_id = :pid AND kind = 'post'"
                    " AND status IN ('queued', 'released') AND publish_at IS NOT NULL"
                ),
                {"pid": principal_id},
            )
        ).all()
        return [r.publish_at for r in rows]

    async def staged_post_titles_since(
        self, session: AsyncSession, principal_id: str, *, since: datetime
    ) -> list[tuple[datetime, str, str]]:
        """(staged_at, submolt, title) for tonight's posts, oldest first.

        The action ledger records a post's TARGET, which for a post is the submolt — so the
        done-tonight block could say "post 2x on aithoughts" and no more. On 2026-08-28 jmolt
        wrote three posts in one night that were three rewordings of one argument, saw
        exactly that line, and could not tell from it that it had already made the point. All
        three went out on the drip that afternoon, half an hour apart.

        A count cannot stop a duplicate — the same reasoning that put targets in the block in
        the first place. The title is where a post's IDEA lives, and it is in the outbox
        rather than the ledger, so it is fetched here."""
        rows = (
            await session.execute(
                text(
                    "SELECT created_at, payload->>'submolt_name' AS submolt,"
                    " payload->>'title' AS title FROM app.jmolt_outbox"
                    " WHERE principal_id = :pid AND kind = 'post' AND created_at >= :since"
                    " AND status <> 'discarded' ORDER BY created_at ASC LIMIT 20"
                ),
                {"pid": principal_id, "since": since},
            )
        ).all()
        return [(r.created_at, r.submolt or "", r.title or "") for r in rows]


class ActionLedgerRepo:
    async def record(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        action: str,
        target: str | None = None,
        reacted_to: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append one action (jmolt or system context). `reacted_to` is truncated to bound
        the row (the fenced content that prompted the action, for injection forensics)."""
        await session.execute(
            text(
                "INSERT INTO app.jmolt_action_ledger"
                " (principal_id, action, target, reacted_to, detail)"
                " VALUES (:pid, :action, :target, :reacted, cast(:detail AS jsonb))"
            ),
            {
                "pid": principal_id,
                "action": action,
                "target": (target or None) and str(target)[:200],
                "reacted": (reacted_to or None) and str(reacted_to)[:2000],
                "detail": json.dumps(detail) if detail is not None else None,
            },
        )

    async def recent(
        self, session: AsyncSession, principal_id: str, *, limit: int = 200
    ) -> list[LedgerRow]:
        rows = (
            await session.execute(
                text(
                    "SELECT action, target, reacted_to, detail, at FROM app.jmolt_action_ledger"
                    " WHERE principal_id = :pid ORDER BY seq DESC LIMIT :lim"
                ),
                {"pid": principal_id, "lim": limit},
            )
        ).all()
        return [
            LedgerRow(
                action=r.action,
                target=r.target,
                reacted_to=r.reacted_to,
                detail=r.detail if isinstance(r.detail, dict) or r.detail is None else None,
                at=r.at,
            )
            for r in rows
        ]

    async def since(
        self, session: AsyncSession, principal_id: str, *, since: datetime, limit: int = 200
    ) -> list[LedgerRow]:
        """Every action jmolt has taken since an instant, OLDEST first — the night's own
        record of what it did.

        This exists because jmolt cannot see its own work any other way. Its writes go
        stage -> release -> drip while its reads go to the live site, so within a night
        everything it writes is invisible to everything it reads: on 2026-08-26 it read one
        thread nine times, was shown two comments by other agents every time, and never once
        saw the seventeen of its own it had accumulated there. The ledger is the only exact
        account of what happened, and it was right the whole time — jmolt just had no way to
        read it. Only `stage_*` rows: system bookkeeping is not something jmolt did."""
        rows = (
            await session.execute(
                text(
                    "SELECT action, target, reacted_to, detail, at FROM app.jmolt_action_ledger"
                    " WHERE principal_id = :pid AND at >= :since AND action LIKE 'stage\\_%'"
                    " ORDER BY seq ASC LIMIT :lim"
                ),
                {"pid": principal_id, "since": since, "lim": limit},
            )
        ).all()
        return [
            LedgerRow(
                action=r.action,
                target=r.target,
                reacted_to=None,  # forensics only; never re-injected into a prompt
                detail=r.detail if isinstance(r.detail, dict) or r.detail is None else None,
                at=r.at,
            )
            for r in rows
        ]

    async def list_filtered(
        self,
        session: AsyncSession,
        principal_id: str,
        *,
        family: str | None = None,
        kinds: tuple[str, ...] | None = None,
        since_days: int | None = None,
        cursor: int | None = None,
        limit: int = 200,
    ) -> list[LedgerRow]:
        """The owner-facing activity feed with server-side filtering (mirrors the runs-log
        pattern so a busy night stays legible). `family` keeps only `stage_*` or `publish_*`;
        `kinds` keeps only those action kinds (the suffix after the family — comment/vote/…);
        `since_days` bounds the window; `cursor` (a `seq`) pages older. Newest first."""
        clauses = ["principal_id = :pid"]
        params: dict[str, Any] = {"pid": principal_id, "lim": limit}
        if family in LEDGER_FAMILIES:
            clauses.append("starts_with(action, :fam || '_')")
            params["fam"] = family
        if kinds:
            clauses.append("substring(action from position('_' in action) + 1) = ANY(:kinds)")
            params["kinds"] = list(kinds)
        if since_days:
            clauses.append("at >= now() - make_interval(days => :days)")
            params["days"] = since_days
        if cursor:
            clauses.append("seq < :cursor")
            params["cursor"] = cursor
        rows = (
            await session.execute(
                text(
                    "SELECT seq, action, target, reacted_to, detail, at"
                    " FROM app.jmolt_action_ledger"
                    " WHERE " + " AND ".join(clauses) + " ORDER BY seq DESC LIMIT :lim"
                ),
                params,
            )
        ).all()
        return [
            LedgerRow(
                seq=int(r.seq),
                action=r.action,
                target=r.target,
                reacted_to=r.reacted_to,
                detail=r.detail if isinstance(r.detail, dict) or r.detail is None else None,
                at=r.at,
            )
            for r in rows
        ]

    async def stats(
        self, session: AsyncSession, principal_id: str, *, since_days: int | None = None
    ) -> list[tuple[str, str, int]]:
        """Counts grouped by (family, kind) over the same optional window — so the filter
        chips show honest totals independent of what the filtered list is currently showing
        (the runs-log `/stats` pattern). Returns (family, kind, count) rows."""
        clauses = ["principal_id = :pid"]
        params: dict[str, Any] = {"pid": principal_id}
        if since_days:
            clauses.append("at >= now() - make_interval(days => :days)")
            params["days"] = since_days
        rows = (
            await session.execute(
                text(
                    "SELECT split_part(action, '_', 1) AS family,"
                    " substring(action from position('_' in action) + 1) AS kind,"
                    " count(*) AS n FROM app.jmolt_action_ledger"
                    " WHERE " + " AND ".join(clauses) + " GROUP BY 1, 2"
                ),
                params,
            )
        ).all()
        return [(r.family, r.kind, int(r.n)) for r in rows]

    async def prune(
        self, session: AsyncSession, principal_id: str, *, keep: int = LEDGER_RETENTION
    ) -> None:
        """Bound the ledger (non-jmolt owner context) — keep the last `keep` actions."""
        await session.execute(
            text(
                "DELETE FROM app.jmolt_action_ledger"
                " WHERE principal_id = :pid AND seq NOT IN ("
                "   SELECT seq FROM app.jmolt_action_ledger"
                "   WHERE principal_id = :pid ORDER BY seq DESC LIMIT :keep"
                " )"
            ),
            {"pid": principal_id, "keep": keep},
        )
