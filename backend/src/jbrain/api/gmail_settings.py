"""Gmail credentials settings (docs/EMAIL_ARCHIVIST_PLAN.md "OAuth setup").

The settings panel writes the archivist's OAuth credentials (client id, client
secret, refresh token) into owner-only app.settings, and the provider picks them up
live — no restart. Secrets are NEVER echoed back: the GET reports only which fields
are set and whether Gmail is connected; the POST /test verifies the saved credentials
by listing labels. Owner-only via the store's RLS (and the router's owner gate).
"""

from typing import cast

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from jbrain.api.deps import PrincipalDep
from jbrain.api.notes import ctx_for
from jbrain.api.settings import SettingsStoreDep
from jbrain.gmail import GmailClientProvider, GmailError

log = structlog.get_logger()

router = APIRouter()


def get_gmail_provider(request: Request) -> GmailClientProvider:
    return cast(GmailClientProvider, request.app.state.gmail_provider)


class GmailStatusOut(BaseModel):
    # Secrets are never returned — only whether each field is populated, plus the
    # derived "connected" (a refresh token is present, so the tools can run).
    client_id_set: bool
    client_secret_set: bool
    refresh_token_set: bool
    connected: bool


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


async def _status(provider: GmailClientProvider) -> GmailStatusOut:
    cid, secret, rt = await provider.credentials()
    return GmailStatusOut(
        client_id_set=bool(cid),
        client_secret_set=bool(secret),
        refresh_token_set=bool(rt),
        connected=bool(rt),
    )


@router.get("/settings/gmail")
async def read_gmail_settings(request: Request, principal: PrincipalDep) -> GmailStatusOut:
    return await _status(get_gmail_provider(request))


@router.put("/settings/gmail")
async def update_gmail_settings(
    body: GmailCredsPatch, request: Request, principal: PrincipalDep, store: SettingsStoreDep
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
    return await _status(get_gmail_provider(request))


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
