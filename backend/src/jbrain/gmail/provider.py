"""Runtime Gmail credentials → a live client (docs/EMAIL_ARCHIVIST_PLAN.md).

The credentials come from the owner-only settings store (set via the GUI panel),
falling back to the JBRAIN_GMAIL_* env values for any blank field — so the panel is
the live control surface and a saved change takes effect with no restart (the same
posture as the LLM routing settings). The provider caches one `GmailClient` and
rebuilds it only when the credentials actually change, so the in-memory access-token
cache survives across tool calls. Until a refresh token exists, `client()` raises a
recoverable GmailError the handlers surface as "connect Gmail in Settings".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from jbrain.db.session import SessionContext
from jbrain.gmail.client import GmailApi, GmailClient, GmailError

if TYPE_CHECKING:
    from jbrain.config import Settings
    from jbrain.settings_store import SqlSettingsStore

# Gmail credentials are owner-only settings; reading them needs only the owner
# identity, not a specific principal id (app.is_owner() is principal-kind based).
_OWNER = SessionContext(principal_kind="owner")


class GmailClientProvider:
    """Resolves current Gmail credentials and hands back a configured client."""

    def __init__(
        self,
        store: SqlSettingsStore,
        settings: Settings,
        *,
        base_url: str,
        token_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._store = store
        self._settings = settings
        self._base_url = base_url
        self._token_url = token_url
        self._transport = transport
        self._cached: GmailClient | None = None
        self._creds: tuple[str, str, str] | None = None

    async def credentials(self) -> tuple[str, str, str]:
        """(client_id, client_secret, refresh_token); each stored value falls back to
        its JBRAIN_GMAIL_* env value when blank."""
        cid, secret, rt = await self._store.gmail_credentials(_OWNER)
        return (
            cid or self._settings.gmail_client_id,
            secret or self._settings.gmail_client_secret,
            rt or self._settings.gmail_refresh_token,
        )

    async def configured(self) -> bool:
        """Whether a refresh token is present — the one credential that gates the
        feature (the gmail_* tools report "not connected" until then)."""
        return bool((await self.credentials())[2])

    async def client(self) -> GmailApi:
        creds = await self.credentials()
        if not creds[2]:
            raise GmailError("Gmail isn't connected yet — add your OAuth credentials in Settings.")
        client = self._cached
        if client is None or creds != self._creds:
            client = GmailClient(
                creds[0],
                creds[1],
                creds[2],
                base_url=self._base_url,
                token_url=self._token_url,
                transport=self._transport,
            )
            self._cached = client
            self._creds = creds
        return client
