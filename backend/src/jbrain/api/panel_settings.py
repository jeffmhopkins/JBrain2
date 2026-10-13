"""The desk panel's settings from the PWA: asked for here, taken by the panel, reported back.

docs/mocks/jpanel-c6/README.md (owner, approved 2026-10-13): the display panel's card shows the
panel's settings as it last reported them, and a change made there says "Waiting for the panel"
until the panel reports the new value back, then "On the panel". The rules are
`jpanel/panel_settings.py`'s; the table and its RLS are migration 0245.

Three doors:

- **The owner's** (`/endpoint/panels/{device_id}/settings`): read what the panel reported and
  what waits; ask for changes. A `display` panel only (the twins' S3 panels have none of these
  settings, 409), and only a setting its firmware reports (409 `not_reported`): a request the
  panel would never apply is not queued.
- **The pickup** (`record_report`, called by `GET /endpoint/debug/next`): the panel's report
  is stored (when it changed, or a minute on), the queue reconciled against it, and the answer
  carries `X-Panel-Settings: <version>` while anything waits.
- **The panel's** (`GET /endpoint/panel-settings`): the queue as the panel applies it. Its own
  row only, under its own RLS context; 403 to anything but a `display`.

Rules: every query on an RLS-scoped session (#3); no LLM here; nothing in a file.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.api.deps import OwnerDep, PanelDep
from jbrain.api.notes import ctx_for
from jbrain.api.panel_alarms import _display_subject, _own_display
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, panel_context, scoped_session
from jbrain.jpanel import debug_relay as relay
from jbrain.jpanel import panel_settings as S
from jbrain.jpanel.notice import PANEL_ZONES

log = structlog.get_logger()

router = APIRouter(prefix="/endpoint", tags=["endpoint"])

#: An unchanged report is stored again at most this often: often enough that "reported 2 h ago"
#: is right to the minute once the panel goes quiet, rarely enough that a pickup every 3 s is
#: not a write every 3 s.
REFRESH_S = 60.0

#: subject_id -> (the report last stored, when). The pickup compares against it before touching
#: the table; a box restart costs one write.
_stored: dict[str, tuple[dict[str, int], float]] = {}


class PanelSettings(BaseModel):
    """What the PWA's section draws. `reported` is None until a panel that reports its settings
    (firmware 0.0.21) has picked up; `pending` is what waits for the panel, by key."""

    supported: bool
    reported: dict[str, int] | None
    reported_s: int | None
    online: bool
    pending: dict[str, int]
    version: int
    zones: list[str]
    night_colors: list[str]


class SettingsIn(BaseModel):
    changes: dict[str, Any]


def _maker(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_maker)


def _desired(raw: Any) -> S.Desired:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return {str(k): dict(v) for k, v in (raw or {}).items() if isinstance(v, dict)}


def _report(raw: Any) -> dict[str, int] | None:
    if isinstance(raw, str):
        raw = json.loads(raw)
    return None if raw is None else {str(k): int(v) for k, v in raw.items()}


async def _row(s: AsyncSession, sid: str) -> Any:
    return (
        await s.execute(
            text(
                "SELECT desired, version, reported, reported_at FROM app.endpoint_panel_setting"
                " WHERE subject_id = CAST(:sid AS uuid)"
            ),
            {"sid": sid},
        )
    ).first()


def _seen_s(device_id: str, row: Any) -> tuple[int | None, bool]:
    """Seconds since the panel last reported, and whether it is online. The pickup's own clock
    while this box has seen it; the stored time otherwise (a box restarted while it was off)."""
    seen = relay.last_seen(device_id)
    if seen is not None:
        return int(seen[0]), seen[0] <= relay.OFFLINE_AFTER_S
    if row is not None and row.reported_at is not None:
        return int((datetime.now(UTC) - row.reported_at).total_seconds()), False
    return None, False


def _view(device_id: str, row: Any) -> PanelSettings:
    reported = _report(row.reported) if row is not None else None
    desired = _desired(row.desired) if row is not None else {}
    ago, online = _seen_s(device_id, row)
    return PanelSettings(
        supported=reported is not None,
        reported=reported,
        reported_s=ago if reported is not None else None,
        online=online,
        pending={k: int(v["value"]) for k, v in desired.items()},
        version=int(row.version) if row is not None else 0,
        zones=[label for label, _ in PANEL_ZONES],
        night_colors=list(S.NIGHT_COLORS),
    )


# ---- the owner's door ------------------------------------------------------------------------


async def read_settings(
    maker: async_sessionmaker[AsyncSession], ctx: SessionContext, device_id: str
) -> PanelSettings:
    sid = await _display_subject(maker, ctx, device_id)
    async with scoped_session(maker, ctx) as s:
        row = await _row(s, sid)
    return _view(device_id, row)


async def ask(
    maker: async_sessionmaker[AsyncSession],
    ctx: SessionContext,
    device_id: str,
    changes: dict[str, Any],
) -> PanelSettings:
    sid = await _display_subject(maker, ctx, device_id)
    try:
        wanted = S.check(changes)
    except S.BadSetting as exc:
        raise HTTPException(
            status_code=422, detail={"reason": "bad_setting", "key": exc.key, "want": exc.why}
        ) from None
    async with scoped_session(maker, ctx) as s:
        row = (
            await s.execute(
                text(
                    "SELECT desired, version, reported, reported_at"
                    " FROM app.endpoint_panel_setting WHERE subject_id = CAST(:sid AS uuid)"
                    " FOR UPDATE"
                ),
                {"sid": sid},
            )
        ).first()
        reported = _report(row.reported) if row is not None else None
        missing = [k for k in wanted if reported is None or k not in reported]
        if row is None or reported is None or missing:
            raise HTTPException(
                status_code=409, detail={"reason": "not_reported", "keys": missing or list(wanted)}
            )
        before = _desired(row.desired)
        after = S.request(before, reported, wanted)
        if after != before:
            await s.execute(
                text(
                    "UPDATE app.endpoint_panel_setting"
                    " SET desired = CAST(:d AS jsonb), version = version + 1"
                    " WHERE subject_id = CAST(:sid AS uuid)"
                ),
                {"d": json.dumps(after), "sid": sid},
            )
            await s.commit()
    async with scoped_session(maker, ctx) as s:
        row = await _row(s, sid)
    log.info("panel_settings.asked", device=device_id, keys=sorted(wanted), waiting=len(after))
    return _view(device_id, row)


@router.get("/panels/{device_id}/settings")
async def get_settings(device_id: str, owner: OwnerDep, request: Request) -> PanelSettings:
    """The desk panel's settings as it last reported them, and what waits for it. Owner only."""
    return await read_settings(_maker(request), ctx_for(owner), device_id)


@router.put("/panels/{device_id}/settings")
async def put_settings(
    device_id: str, owner: OwnerDep, request: Request, body: SettingsIn
) -> PanelSettings:
    """Ask the panel to change some of its settings. Queued until it takes them, however long it
    is offline; asking for what it already holds withdraws a request. Owner only."""
    return await ask(_maker(request), ctx_for(owner), device_id, body.changes)


# ---- the pickup -------------------------------------------------------------------------------


async def record_report(
    maker: async_sessionmaker[AsyncSession], principal: PrincipalInfo, params: dict[str, str]
) -> str | None:
    """A pickup's report: stored, and the queue settled against it. Returns the queue's version
    for the answer's header while anything waits, else None. Never raises: the pickup is the
    panel's proof of life, and a settings sync is not worth it."""
    report = S.parse_report(params)
    sid = principal.subject_id
    if report is None or principal.kind != "device_key" or not sid:
        return None
    try:
        return await _record(maker, principal, sid, report)
    except Exception as exc:  # noqa: BLE001 — the pickup answers whatever this does
        log.warning("panel_settings.report_failed", device=principal.id, error=repr(exc))
        return None


async def _record(
    maker: async_sessionmaker[AsyncSession],
    principal: PrincipalInfo,
    sid: str,
    report: dict[str, int],
) -> str | None:
    now = time.monotonic()
    async with scoped_session(maker, panel_context(principal.id, sid)) as s:
        role = (
            await s.execute(
                text("SELECT device_role FROM app.subjects WHERE id = CAST(:sid AS uuid)"),
                {"sid": sid},
            )
        ).scalar_one_or_none()
        if role != "display":
            return None
        row = await _row(s, sid)
        desired = _desired(row.desired) if row is not None else {}
        keep, settled, dropped = S.reconcile(desired, report)
        last = _stored.get(sid)
        stale = last is None or last[0] != report or now - last[1] >= REFRESH_S
        if row is None:
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_panel_setting (subject_id, reported, reported_at)"
                    " VALUES (CAST(:sid AS uuid), CAST(:r AS jsonb), now())"
                    " ON CONFLICT (subject_id) DO NOTHING"
                ),
                {"sid": sid, "r": json.dumps(report)},
            )
        elif keep != desired or stale or _report(row.reported) != report:
            await s.execute(
                text(
                    "UPDATE app.endpoint_panel_setting SET reported = CAST(:r AS jsonb),"
                    " reported_at = now(), desired = CAST(:d AS jsonb)"
                    " WHERE subject_id = CAST(:sid AS uuid)"
                ),
                {"sid": sid, "r": json.dumps(report), "d": json.dumps(keep)},
            )
        else:
            return str(row.version) if keep else None
        await s.commit()
    _stored[sid] = (report, now)
    if settled or dropped:
        log.info("panel_settings.reported", device=principal.id, settled=settled, dropped=dropped)
    return str(row.version) if row is not None and keep else None


def forget(subject_id: str | None = None) -> None:
    if subject_id is None:
        _stored.clear()
    else:
        _stored.pop(subject_id, None)


# ---- the panel's door -------------------------------------------------------------------------


@router.get("/panel-settings")
async def panel_pending(principal: PanelDep, request: Request) -> dict[str, Any]:
    """What waits for the calling desk panel: `{"v": version, "s": {wire: [value, base]}}`. The
    panel applies each one only while it still holds `base`, then reports on its next pickup."""
    maker = _maker(request)
    sid = await _own_display(maker, principal)
    async with scoped_session(maker, panel_context(principal.id, sid)) as s:
        row = await _row(s, sid)
    if row is None:
        return S.wire_pending(0, {})
    return S.wire_pending(int(row.version), _desired(row.desired))
