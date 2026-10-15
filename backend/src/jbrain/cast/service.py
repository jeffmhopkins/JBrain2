"""Cast a notice to the house's TVs, and keep the record of how each took it (JPANEL_CAST_PLAN.md
K1).

One instance lives on `app.state.cast_notices`. `send` draws the frame once (`cast.frame`, on a
thread), stores it in the blob store (CLAUDE.md #2), writes the notice, then asks the sidecar to
cast it to each chosen TV at once. The sidecar re-checks each TV's gate at that instant, so
every TV's outcome is what the TV was doing then, not when the PWA listed it: `shown`, `woke`,
`skipped` (with the gate's words) or `failed`. Every query runs as the owner: both tables are
owner-only (CLAUDE.md #3, migration 0250).
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from jbrain.blob_refs import blob_referenced
from jbrain.cast import frame as F
from jbrain.cast.sidecar import Sidecar
from jbrain.db.session import SessionContext, scoped_session
from jbrain.storage import BlobStore

log = structlog.get_logger()

_OWNER = SessionContext(principal_kind="owner")
#: The owner's record of what he cast, kept a week like the panel's (owner, 2026-10-14).
KEEP_S = 7 * 24 * 3600
TITLE_MAX = 120
BODY_MAX = 600
SECONDS_MIN, SECONDS_MAX = 5, 300


class CastRefused(Exception):
    """A send turned away before anything was cast; `status` and `reason` are the reply."""

    def __init__(self, reason: str, status: int = 422, **extra: Any) -> None:
        super().__init__(reason)
        self.reason, self.status, self.extra = reason, status, extra

    def detail(self) -> dict[str, Any]:
        return {"reason": self.reason, **self.extra}


@dataclass(frozen=True)
class TvOutcome:
    tv_id: str
    name: str
    outcome: str
    why: str
    cast_id: str | None = None


@dataclass(frozen=True)
class CastView:
    id: str
    sender: str
    title: str
    body: str
    seconds: int
    created_at: datetime
    stopped_at: datetime | None
    tvs: list[TvOutcome] = field(default_factory=list)


class CastService:
    def __init__(self, maker: async_sessionmaker, blobs: BlobStore, sidecar: Sidecar) -> None:
        self._maker = maker
        self._blobs = blobs
        self._sidecar = sidecar

    async def tvs(self) -> dict[str, Any]:
        return await self._sidecar.call("GET", "/tvs")

    async def send(
        self,
        *,
        sender: str,
        title: str,
        body: str,
        picture: bytes | None,
        tv_ids: list[str],
        seconds: int,
        meta: str,
        frame: bytes | None = None,
    ) -> CastView:
        """`frame` is a picture the phone drew whole (a photo with the words placed over it);
        without one, the box draws the frame from `title`, `body` and `picture`."""
        title, body = title.strip(), body.strip()
        if not title:
            raise CastRefused("bad_field", field="title", want="1-120 bytes")
        if len(title.encode()) > TITLE_MAX:
            raise CastRefused("too_long", field="title", max_bytes=TITLE_MAX)
        if len(body.encode()) > BODY_MAX:
            raise CastRefused("too_long", field="body", max_bytes=BODY_MAX)
        if not tv_ids:
            raise CastRefused("no_tvs")
        seconds = max(SECONDS_MIN, min(SECONDS_MAX, seconds))
        await self.sweep()
        names = {str(t["id"]): str(t.get("name") or "TV") for t in (await self.tvs())["tvs"]}
        try:
            if frame is not None:
                png = await asyncio.to_thread(F.take, frame)
            else:
                png = await asyncio.to_thread(F.render, title, body, picture, meta=meta)
        except F.FrameError:
            raise CastRefused("bad_picture", status=415) from None
        sha = await self._blobs.put(png)
        async with scoped_session(self._maker, _OWNER) as s:
            notice_id = (
                await s.execute(
                    text(
                        """
                        INSERT INTO app.cast_notice (sender, title, body, frame_sha256, seconds)
                        VALUES (:sender, :title, :body, :sha, :seconds) RETURNING id
                        """
                    ),
                    {
                        "sender": sender,
                        "title": title,
                        "body": body,
                        "sha": sha,
                        "seconds": seconds,
                    },
                )
            ).scalar_one()
            await s.commit()
        image_b64 = base64.b64encode(png).decode()
        unique = list(dict.fromkeys(tv_ids))
        outcomes = await asyncio.gather(
            *(self._cast_one(tv, names.get(tv), image_b64, seconds, title) for tv in unique)
        )
        async with scoped_session(self._maker, _OWNER) as s:
            for o in outcomes:
                await s.execute(
                    text(
                        """
                        INSERT INTO app.cast_notice_tv
                            (notice_id, tv_id, tv_name, outcome, why, cast_id)
                        VALUES (:n, :tv, :name, :outcome, :why, :cast_id)
                        """
                    ),
                    {
                        "n": notice_id,
                        "tv": o.tv_id,
                        "name": o.name,
                        "outcome": o.outcome,
                        "why": o.why,
                        "cast_id": o.cast_id,
                    },
                )
            await s.commit()
        log.info(
            "cast.sent",
            notice=str(notice_id),
            sender=sender,
            outcomes={o.tv_id: o.outcome for o in outcomes},
        )
        view = await self.get(str(notice_id))
        assert view is not None
        return view

    async def _cast_one(
        self, tv_id: str, name: str | None, image_b64: str, seconds: int, title: str
    ) -> TvOutcome:
        if name is None:
            return TvOutcome(tv_id, "TV", "skipped", "not on the network")
        try:
            out = await self._sidecar.call(
                "POST",
                f"/tvs/{tv_id}/cast",
                json={"image_b64": image_b64, "seconds": seconds, "title": title},
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
            if exc.status_code == 409:
                return TvOutcome(tv_id, name, "skipped", str(detail.get("why") or "in use"))
            if exc.status_code == 404:
                return TvOutcome(tv_id, name, "skipped", "not on the network")
            return TvOutcome(tv_id, name, "failed", str(detail.get("error") or detail)[:200])
        woke = out.get("state_before") == "standby"
        return TvOutcome(
            tv_id,
            name,
            "woke" if woke else "shown",
            "was off" if woke else str(out.get("why_before") or ""),
            str(out.get("cast_id") or "") or None,
        )

    async def get(self, notice_id: str) -> CastView | None:
        views = await self._read("WHERE n.id = CAST(:id AS uuid)", {"id": notice_id})
        return views[0] if views else None

    async def history(self, limit: int) -> list[CastView]:
        return await self._read(
            "WHERE n.id IN (SELECT id FROM app.cast_notice ORDER BY created_at DESC LIMIT :limit)",
            {"limit": limit},
        )

    async def _read(self, where: str, params: dict[str, Any]) -> list[CastView]:
        async with scoped_session(self._maker, _OWNER) as s:
            rows = (
                await s.execute(
                    text(
                        f"""
                        SELECT n.id, n.sender, n.title, n.body, n.seconds, n.created_at,
                               n.stopped_at, t.tv_id, t.tv_name, t.outcome, t.why, t.cast_id
                        FROM app.cast_notice n
                        LEFT JOIN app.cast_notice_tv t ON t.notice_id = n.id
                        {where}
                        ORDER BY n.created_at DESC, t.tv_name
                        """
                    ),
                    params,
                )
            ).all()
        views: dict[str, CastView] = {}
        for r in rows:
            key = str(r.id)
            if key not in views:
                views[key] = CastView(
                    key, r.sender, r.title, r.body, r.seconds, r.created_at, r.stopped_at
                )
            if r.tv_id is not None:
                views[key].tvs.append(TvOutcome(r.tv_id, r.tv_name, r.outcome, r.why, r.cast_id))
        return list(views.values())

    async def frame(self, notice_id: str) -> bytes | None:
        async with scoped_session(self._maker, _OWNER) as s:
            sha = (
                await s.execute(
                    text("SELECT frame_sha256 FROM app.cast_notice WHERE id = CAST(:id AS uuid)"),
                    {"id": notice_id},
                )
            ).scalar_one_or_none()
        return await self._blobs.get(str(sha)) if sha else None

    async def stop(self, notice_id: str) -> dict[str, bool] | None:
        """Take it down: quit the notice on every TV still showing it. A TV somebody took
        over since is left alone (the sidecar checks the cast is still its own)."""
        view = await self.get(notice_id)
        if view is None:
            return None
        stopped: dict[str, bool] = {}
        for tv in view.tvs:
            if tv.cast_id is None:
                continue
            try:
                out = await self._sidecar.call(
                    "POST", f"/tvs/{tv.tv_id}/stop", json={"cast_id": tv.cast_id}
                )
                stopped[tv.tv_id] = bool(out.get("stopped"))
            except HTTPException:
                stopped[tv.tv_id] = False
        async with scoped_session(self._maker, _OWNER) as s:
            await s.execute(
                text(
                    "UPDATE app.cast_notice SET stopped_at = now()"
                    " WHERE id = CAST(:id AS uuid) AND stopped_at IS NULL"
                ),
                {"id": notice_id},
            )
            await s.commit()
        return stopped

    async def sweep(self) -> int:
        """Delete notices older than the week they are kept, with their frames, each blob only
        once `blob_refs` says nothing else holds the same bytes. Lazy, on every send and read,
        because nothing else touches these tables."""
        async with scoped_session(self._maker, _OWNER) as s:
            gone = (
                await s.execute(
                    text(
                        """
                        DELETE FROM app.cast_notice
                        WHERE created_at <= now()
                            - make_interval(secs => CAST(:keep AS double precision))
                        RETURNING frame_sha256
                        """
                    ),
                    {"keep": KEEP_S},
                )
            ).all()
            shas = {str(r.frame_sha256) for r in gone}
            free = [sha for sha in shas if not await blob_referenced(s, sha)]
            await s.commit()
        for sha in free:
            await self._blobs.delete(sha)
        return len(gone)
