"""User-settings endpoints over app.settings (migration 0012) — the first
server-synced preferences. The response is one extensible object; PUT takes a
partial body and rejects unknown keys/values at validation, so a typo can
never write an unreadable setting. Owner-only is implicit pre-P7 (only the
owner holds a session), and the store's RLS enforces it regardless.
"""

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict

from jbrain.api.deps import PrincipalDep
from jbrain.api.notes import ctx_for
from jbrain.settings_store import IMAGE_ANALYSIS_KEY, SqlSettingsStore

router = APIRouter()


def get_settings_store(request: Request) -> SqlSettingsStore:
    return cast(SqlSettingsStore, request.app.state.settings_store)


SettingsStoreDep = Annotated[SqlSettingsStore, Depends(get_settings_store)]


class SettingsOut(BaseModel):
    image_analysis_mode: Literal["full", "ocr"]


class SettingsPatch(BaseModel):
    # Unknown keys are a client bug, not a forward-compat case: reject them.
    model_config = ConfigDict(extra="forbid")

    image_analysis_mode: Literal["full", "ocr"] | None = None


@router.get("/settings")
async def read_settings(principal: PrincipalDep, store: SettingsStoreDep) -> SettingsOut:
    return SettingsOut(image_analysis_mode=await store.image_analysis_mode(ctx_for(principal)))


@router.put("/settings")
async def update_settings(
    body: SettingsPatch, principal: PrincipalDep, store: SettingsStoreDep
) -> SettingsOut:
    ctx = ctx_for(principal)
    if body.image_analysis_mode is not None:
        await store.upsert(ctx, IMAGE_ANALYSIS_KEY, body.image_analysis_mode)
    return SettingsOut(image_analysis_mode=await store.image_analysis_mode(ctx))
