"""Tavily Extract tier settings (docs/plans/TAVILY_FETCH_TIER_PLAN.md).

The Settings panel writes the hosted Tavily tier's on/off toggle + API key into owner-only
app.settings, and the WebFetcher picks them up live per fetch — no restart (the stored key takes
precedence over the JBRAIN_TAVILY_API_KEY env fallback). Mirrors the Gmail-credentials panel: the
KEY is a secret NEVER echoed back — the GET reports only whether one is set (stored or via the env
fallback). `POST /settings/tavily/test` runs the live Tavily tier against a URL (the "Test key"
button), so the owner verifies a freshly-pasted key from the PWA with no terminal (non-negotiable
#10). Owner-only via the store's RLS (and the router's owner gate).
"""

from typing import cast

import structlog
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from jbrain.api.deps import PrincipalDep, PrincipalInfo, SettingsDep
from jbrain.api.notes import ctx_for
from jbrain.api.settings import SettingsStoreDep
from jbrain.config import Settings
from jbrain.settings_store import SqlSettingsStore
from jbrain.web.fetch import WebFetcher, WebFetchError

log = structlog.get_logger()

router = APIRouter()

# The URL the "Test key" probe extracts when the owner gives none — a stable, boring public page,
# so a green test proves the key + connectivity without depending on any particular walled site.
_DEFAULT_TEST_URL = "https://example.com"


def _fetcher(request: Request) -> WebFetcher:
    return cast(WebFetcher, request.app.state.web_fetcher)


class TavilyStatusOut(BaseModel):
    # The KEY is never returned — only whether one is effectively present (stored OR via the env
    # fallback), plus the toggle. `wired` is whether the tier exists at all (a base URL is set);
    # `effective` is whether it would actually run a fetch (wired AND enabled AND a key present).
    enabled: bool
    key_set: bool
    wired: bool
    effective: bool


class TavilyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # The toggle. Absent = unchanged.
    enabled: bool | None = None
    # A non-empty string SETS the key; absent/blank leaves it unchanged (so toggling doesn't wipe
    # the key). To remove the stored key (revert to the env fallback) send clear_key=true.
    api_key: str | None = None
    clear_key: bool = False


class TavilyTestOut(BaseModel):
    ok: bool
    detail: str


class TavilyTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # An optional URL to probe; defaults to a stable public page. Bounded so the field can't carry
    # an unbounded body — the fetcher's SSRF guard still refuses a private/non-http target.
    url: str = _DEFAULT_TEST_URL


async def _status(
    request: Request, principal: PrincipalInfo, store: SqlSettingsStore, settings: Settings
) -> TavilyStatusOut:
    ctx = ctx_for(principal)
    enabled = await store.tavily_enabled(ctx)
    # Effective key = the stored key OR the env fallback (the same precedence the WebFetcher uses).
    stored = await store.tavily_api_key(ctx)
    key_present = bool(stored or settings.tavily_api_key)
    fetcher = _fetcher(request)
    return TavilyStatusOut(
        enabled=enabled,
        key_set=key_present,
        wired=fetcher.tavily_wired,
        effective=fetcher.tavily_wired and enabled and key_present,
    )


@router.get("/settings/tavily")
async def read_tavily_settings(
    request: Request, principal: PrincipalDep, store: SettingsStoreDep, settings: SettingsDep
) -> TavilyStatusOut:
    return await _status(request, principal, store, settings)


@router.put("/settings/tavily")
async def update_tavily_settings(
    body: TavilyPatch,
    request: Request,
    principal: PrincipalDep,
    store: SettingsStoreDep,
    settings: SettingsDep,
) -> TavilyStatusOut:
    ctx = ctx_for(principal)
    if body.enabled is not None:
        await store.set_tavily_enabled(ctx, body.enabled)
    if body.clear_key:
        await store.set_tavily_api_key(ctx, "")  # revert to the env fallback
    elif body.api_key is not None and body.api_key.strip():
        await store.set_tavily_api_key(ctx, body.api_key.strip())
    return await _status(request, principal, store, settings)


@router.post("/settings/tavily/test")
async def test_tavily_settings(
    body: TavilyTestIn, request: Request, principal: PrincipalDep
) -> TavilyTestOut:
    """Run the LIVE Tavily tier against `url` (the "Test key" button) so the owner verifies a
    freshly-saved key with no terminal. Reports the failure modes distinctly: the tier unwired,
    disabled/keyless, a bad target (SSRF/scheme), or a genuine miss (bad key / Tavily error)."""
    fetcher = _fetcher(request)
    if not fetcher.tavily_wired:
        return TavilyTestOut(ok=False, detail="The Tavily tier is not configured on this box.")
    try:
        result = await fetcher.tavily(body.url.strip() or _DEFAULT_TEST_URL)
    except WebFetchError as exc:
        return TavilyTestOut(ok=False, detail=str(exc))
    if result is None:
        return TavilyTestOut(
            ok=False,
            detail="No page came back — Tavily is disabled or the key is missing/invalid, "
            "or it couldn't read that URL. Check the toggle and key above.",
        )
    return TavilyTestOut(
        ok=True, detail=f"Tavily read {result.total_chars} chars from {result.url} — key works."
    )
