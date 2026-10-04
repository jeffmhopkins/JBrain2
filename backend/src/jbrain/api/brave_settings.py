"""Brave Search tier settings (docs/plans/BROWSER_AGENT_PLAN.md B3).

The Settings panel writes the Brave tier's toggle, API key and monthly query budget into
owner-only app.settings, and `BraveSearch` reads them live per search — no restart (the stored key
takes precedence over the JBRAIN_BRAVE_API_KEY env fallback). Mirrors the Tavily panel
(`api/tavily_settings.py`): the KEY is a secret NEVER echoed back — the GET reports only whether
one is present and where it comes from. The GET also carries this month's usage against the
budget, so the owner sees how close the free credit is with no terminal (non-negotiable #10).
`POST /settings/brave/test` spends ONE real, counted query to prove a freshly-pasted key works.
Owner-gated by `OwnerDep` on every route, on top of the store's owner-only RLS.
"""

from typing import Literal, cast

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from jbrain.api.deps import OwnerDep, PrincipalInfo, SettingsDep
from jbrain.api.notes import ctx_for
from jbrain.api.settings import SettingsStoreDep
from jbrain.config import Settings
from jbrain.settings_store import BRAVE_BUDGET_MAX, BRAVE_BUDGET_MIN, SqlSettingsStore
from jbrain.web.search import BraveSearch, usage_count, utc_month

router = APIRouter()


def _brave(request: Request) -> BraveSearch:
    return cast(BraveSearch, request.app.state.brave_search)


class BraveStatusOut(BaseModel):
    # The KEY is never returned: `key_present` is whether one is effectively set (stored OR the
    # env fallback) and `key_source` which of the two wins. `wired` = the tier exists (a base URL
    # is pinned); `effective` = a search would actually reach Brave right now (wired, enabled,
    # keyed, under this month's budget, and not `blocked`). `blocked` is what this process has
    # learned from Brave itself: "key_rejected" (this key was refused) or "credit_spent" (the
    # plan ran out this month), "" otherwise.
    enabled: bool
    key_present: bool
    key_source: Literal["stored", "env", "none"]
    wired: bool
    effective: bool
    blocked: Literal["", "key_rejected", "credit_spent"] = ""
    budget: int
    used_this_month: int
    month: str
    # The last key/credit failure Brave reported ("" = none since the last success) and when.
    last_error: str = ""
    last_error_at: str = ""


class BravePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Absent = unchanged for every field.
    enabled: bool | None = None
    # A non-empty string SETS the key; "" (or blank) REMOVES the stored key, reverting to the env
    # fallback. Bounded so the field can't carry an unbounded body.
    api_key: str | None = Field(default=None, max_length=512)
    # Queries per calendar month. No "unlimited": the floor is one query.
    monthly_budget: int | None = Field(default=None, ge=BRAVE_BUDGET_MIN, le=BRAVE_BUDGET_MAX)


class BraveTestOut(BaseModel):
    ok: bool
    hits: int
    detail: str


async def _status(
    request: Request, principal: PrincipalInfo, store: SqlSettingsStore, settings: Settings
) -> BraveStatusOut:
    ctx = ctx_for(principal)
    enabled = await store.brave_enabled(ctx)
    stored = await store.brave_api_key(ctx)
    source: Literal["stored", "env", "none"] = (
        "stored" if stored else "env" if settings.brave_api_key else "none"
    )
    budget = await store.brave_monthly_budget(ctx)
    month = utc_month()
    used = usage_count(await store.brave_usage(ctx), month)
    raw_error = await store.brave_last_error(ctx)
    error = raw_error if isinstance(raw_error, dict) else {}
    brave = _brave(request)
    wired = brave.wired
    blocked = cast(
        Literal["", "key_rejected", "credit_spent"],
        brave.blocked(stored or settings.brave_api_key),
    )
    return BraveStatusOut(
        enabled=enabled,
        key_present=source != "none",
        key_source=source,
        wired=wired,
        effective=wired and enabled and source != "none" and used < budget and not blocked,
        blocked=blocked,
        budget=budget,
        used_this_month=used,
        month=month,
        last_error=str(error.get("detail") or ""),
        last_error_at=str(error.get("at") or ""),
    )


@router.get("/settings/brave")
async def read_brave_settings(
    request: Request, principal: OwnerDep, store: SettingsStoreDep, settings: SettingsDep
) -> BraveStatusOut:
    return await _status(request, principal, store, settings)


@router.put("/settings/brave")
async def update_brave_settings(
    body: BravePatch,
    request: Request,
    principal: OwnerDep,
    store: SettingsStoreDep,
    settings: SettingsDep,
) -> BraveStatusOut:
    ctx = ctx_for(principal)
    if body.enabled is not None:
        await store.set_brave_enabled(ctx, body.enabled)
    if body.api_key is not None:
        await store.set_brave_api_key(ctx, body.api_key.strip())
        # The last error was about the key being replaced; a new key starts with a clean slate.
        await store.set_brave_last_error(ctx, {"detail": "", "at": ""})
    if body.monthly_budget is not None:
        await store.set_brave_monthly_budget(ctx, body.monthly_budget)
    return await _status(request, principal, store, settings)


@router.post("/settings/brave/test")
async def test_brave_settings(request: Request, principal: OwnerDep) -> BraveTestOut:
    """Run ONE live Brave query (the "Test key" button). It is a real, billed query, so it is
    counted against the month like any other — and refused, not sent, once the budget is
    reached. Each failure reads as itself: unwired, keyless, off, budget spent, a rejected key,
    a spent plan, a rate limit, another HTTP error or no connection."""
    ok, hits, detail = await _brave(request).probe()
    return BraveTestOut(ok=ok, hits=hits, detail=detail)
