"""Gmail credentials settings (docs/EMAIL_ARCHIVIST_PLAN.md "OAuth setup").

The settings panel writes the archivist's OAuth credentials (client id, client
secret, refresh token) into owner-only app.settings, and the provider picks them up
live — no restart. Secrets are NEVER echoed back: the GET reports only which fields
are set and whether Gmail is connected; the POST /test verifies the saved credentials
by listing labels. Owner-only via the store's RLS (and the router's owner gate).

The refresh token can be set two ways: pasted directly (from the bootstrap script),
or minted by the in-app **Connect** flow — `/connect` redirects the owner to Google's
consent, and `/callback` exchanges the returned code for the refresh token and stores
it. The callback is owner-gated (the Lax session cookie rides Google's top-level
redirect) AND validated against a single-use `state` (CSRF), so only the owner who
started the connect can complete it. The redirect_uri is `public_base_url` when set,
else derived from the request the browser hit — so a tunneled box needs no env edit,
just a redeploy (connect from the public hostname so it matches the Google client).
"""

import secrets
from typing import cast
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict

from jbrain.api.deps import OwnerDep, PrincipalDep, SettingsDep
from jbrain.api.notes import ctx_for
from jbrain.api.settings import SettingsStoreDep
from jbrain.config import Settings
from jbrain.gmail import GmailClientProvider, GmailError

log = structlog.get_logger()

router = APIRouter()

# The owner-consent endpoints. `gmail.modify` only (read/label/archive, never delete);
# offline + consent so Google returns a refresh token. `_STATE_KEY` holds the
# single-use CSRF state across the round-trip.
_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
_STATE_KEY = "gmail_oauth_state"


def get_gmail_provider(request: Request) -> GmailClientProvider:
    return cast(GmailClientProvider, request.app.state.gmail_provider)


def _public_base(request: Request, settings: Settings) -> str:
    """The box's public origin: the explicit `public_base_url` when set (preferred, so
    the redirect_uri is guaranteed to match the Google client), else derived from the
    request the owner's browser actually hit — so a tunneled box works after a plain
    redeploy with no env editing. Mirrors api/debug_tokens._public_base and pairing."""
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    host = request.headers.get("host", request.url.netloc)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{host}".rstrip("/")


def _redirect_uri(request: Request, settings: Settings) -> str:
    """The callback URL Google redirects to — must match the Web client's registered
    Authorized redirect URI exactly (so connect from the public hostname)."""
    return f"{_public_base(request, settings)}/api/settings/gmail/callback"


class GmailStatusOut(BaseModel):
    # Secrets are never returned — only whether each field is populated, plus the
    # derived "connected" (a refresh token is present, so the tools can run).
    client_id_set: bool
    client_secret_set: bool
    refresh_token_set: bool
    connected: bool
    # The client_id is NOT a secret (Google puts it in every auth URL), so we echo it
    # back, plus the exact redirect_uri the box will send — the two values that cause
    # `invalid_client` / `redirect_uri_mismatch` when they don't match the Google client.
    client_id: str
    redirect_uri: str


class GmailCredsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Each field is optional: a blank/absent field is left unchanged, so re-saving one
    # value never wipes the others. Send a non-empty string to set a field.
    client_id: str | None = None
    client_secret: str | None = None
    refresh_token: str | None = None


class GmailTestOut(BaseModel):
    ok: bool
    detail: str


async def _status(request: Request, settings: Settings) -> GmailStatusOut:
    cid, secret, rt = await get_gmail_provider(request).credentials()
    return GmailStatusOut(
        client_id_set=bool(cid),
        client_secret_set=bool(secret),
        refresh_token_set=bool(rt),
        connected=bool(rt),
        client_id=cid,
        redirect_uri=_redirect_uri(request, settings),
    )


@router.get("/settings/gmail")
async def read_gmail_settings(
    request: Request, principal: PrincipalDep, settings: SettingsDep
) -> GmailStatusOut:
    return await _status(request, settings)


@router.put("/settings/gmail")
async def update_gmail_settings(
    body: GmailCredsPatch,
    request: Request,
    principal: PrincipalDep,
    store: SettingsStoreDep,
    settings: SettingsDep,
) -> GmailStatusOut:
    # Only set fields the caller actually provided (non-empty after strip).
    def clean(v: str | None) -> str | None:
        return v.strip() if isinstance(v, str) and v.strip() else None

    await store.set_gmail_credentials(
        ctx_for(principal),
        client_id=clean(body.client_id),
        client_secret=clean(body.client_secret),
        refresh_token=clean(body.refresh_token),
    )
    return await _status(request, settings)


@router.post("/settings/gmail/test")
async def test_gmail_settings(request: Request, principal: PrincipalDep) -> GmailTestOut:
    """Verify the saved credentials by minting a token and listing labels — the
    feedback the panel shows after a save."""
    provider = get_gmail_provider(request)
    try:
        client = await provider.client()
        labels = await client.list_labels()
    except GmailError as exc:
        return GmailTestOut(ok=False, detail=str(exc))
    return GmailTestOut(ok=True, detail=f"Connected — {len(labels)} labels visible.")


@router.get("/settings/gmail/connect")
async def gmail_connect(
    request: Request, principal: OwnerDep, store: SettingsStoreDep, settings: SettingsDep
) -> RedirectResponse:
    """Start the in-app Connect flow: stash a CSRF state, then redirect the owner to
    Google's consent screen. The owner must have saved a client id + secret first."""
    client_id, client_secret, _ = await get_gmail_provider(request).credentials()
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="Save your Client ID and secret first.")
    state = secrets.token_urlsafe(24)
    await store.upsert(ctx_for(principal), _STATE_KEY, state)
    params = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": _redirect_uri(request, settings),
            "response_type": "code",
            "scope": _SCOPE,
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",  # force it so a refresh token is always returned
            "state": state,
        }
    )
    return RedirectResponse(f"{_AUTH_URL}?{params}")


@router.get("/settings/gmail/callback")
async def gmail_callback(
    request: Request,
    principal: OwnerDep,
    store: SettingsStoreDep,
    settings: SettingsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Google's redirect target: validate the single-use state, exchange the code for a
    refresh token, store it, and bounce back to the settings screen."""
    ctx = ctx_for(principal)
    expected = await store.get(ctx, _STATE_KEY, "")
    await store.upsert(ctx, _STATE_KEY, "")  # single-use, cleared regardless of outcome
    settings_url = f"{_public_base(request, settings)}/settings"
    if error or not code or not state or not expected or state != expected:
        return RedirectResponse(f"{settings_url}?gmail=error")
    try:
        refresh_token = await get_gmail_provider(request).exchange_code(
            code, _redirect_uri(request, settings)
        )
    except GmailError:
        return RedirectResponse(f"{settings_url}?gmail=error")
    await store.set_gmail_credentials(ctx, refresh_token=refresh_token)
    return RedirectResponse(f"{settings_url}?gmail=connected")
