"""The read-only appointments ICS feed and its token management.

Two surfaces with different auth:
  - GET /feed/appointments.ics?token=… is PUBLIC (a calendar app can't hold an
    owner session) — the high-entropy token IS the credential, compared in
    constant time; a missing/wrong/disabled token is an indistinguishable 404.
  - the token management endpoints are owner-only (the Settings surface): show,
    rotate (invalidating the old URL), and disable the feed.

On a valid token the feed reads under an owner context (all domains, full titles
— the recorded owner decision), so the subscribe URL carries health/finance
titles off-box; it is revocable, and Settings labels it as such.
"""

import secrets
from typing import cast

from fastapi import APIRouter, Depends, Request, Response

from jbrain.api.deps import owner_only
from jbrain.appointments.ics import to_ics
from jbrain.appointments.repo import SqlAppointmentsRepo
from jbrain.db.session import SessionContext
from jbrain.settings_store import FEED_TOKEN_KEY, SqlSettingsStore

router = APIRouter()

# The feed serves the owner's own data with no request principal — an owner
# context (unrestricted: all domains) gated entirely by the token check above it.
_FEED_CTX = SessionContext(principal_kind="owner")


def _settings(request: Request) -> SqlSettingsStore:
    return cast(SqlSettingsStore, request.app.state.settings_store)


def _appointments(request: Request) -> SqlAppointmentsRepo:
    return cast(SqlAppointmentsRepo, request.app.state.appointments_repo)


async def _stored_token(request: Request) -> str | None:
    token = await _settings(request).get(_FEED_CTX, FEED_TOKEN_KEY)
    return token if isinstance(token, str) and token else None


@router.get("/feed/appointments.ics")
async def appointments_ics(request: Request, token: str = "") -> Response:
    stored = await _stored_token(request)
    # Disabled feed or any token mismatch → an opaque 404 (never reveal which).
    if stored is None or not token or not secrets.compare_digest(token, stored):
        return Response(status_code=404)
    # Past + future, cancelled included (emitted as STATUS:CANCELLED so a
    # subscribed calendar removes them) — a faithful mirror of the calendar.
    appts = await _appointments(request).list_appointments(_FEED_CTX, include_cancelled=True)
    return Response(
        content=to_ics(appts),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="appointments.ics"'},
    )


# --- owner-only token management (the Settings surface) ---------------------


@router.get("/feed/appointments", dependencies=[Depends(owner_only)])
async def feed_config(request: Request) -> dict:
    """Whether the feed is enabled, and the token the PWA builds the URL from."""
    token = await _stored_token(request)
    return {"enabled": token is not None, "token": token}


@router.post("/feed/appointments/rotate", dependencies=[Depends(owner_only)])
async def rotate_feed(request: Request) -> dict:
    """Issue a fresh token (enabling the feed, or invalidating the old URL)."""
    token = secrets.token_urlsafe(32)
    await _settings(request).upsert(_FEED_CTX, FEED_TOKEN_KEY, token)
    return {"enabled": True, "token": token}


@router.delete("/feed/appointments", status_code=204, dependencies=[Depends(owner_only)])
async def disable_feed(request: Request) -> Response:
    """Disable the feed — the subscribe URL stops working immediately."""
    await _settings(request).upsert(_FEED_CTX, FEED_TOKEN_KEY, None)
    return Response(status_code=204)
