"""User-settings endpoints over app.settings (migration 0012) — the first
server-synced preferences. The response is one extensible object; PUT takes a
partial body and rejects unknown keys/values at validation, so a typo can
never write an unreadable setting. Owner-only is implicit pre-P7 (only the
owner holds a session), and the store's RLS enforces it regardless.
"""

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from jbrain.api.deps import PrincipalDep
from jbrain.api.notes import ctx_for
from jbrain.settings_store import (
    BRAIN_LLM_STREAM_KEY,
    IMAGE_ANALYSIS_KEY,
    OWNER_TIMEZONE_KEY,
    SqlSettingsStore,
    is_valid_timezone,
)

router = APIRouter()


def get_settings_store(request: Request) -> SqlSettingsStore:
    return cast(SqlSettingsStore, request.app.state.settings_store)


SettingsStoreDep = Annotated[SqlSettingsStore, Depends(get_settings_store)]


class SettingsOut(BaseModel):
    image_analysis_mode: Literal["full", "ocr"]
    # The owner's IANA display timezone, or null when unset (server times = UTC).
    owner_timezone: str | None = None
    # Stream real prompt/answer text to the on-box wall display (:8800). OFF by
    # default — see BRAIN_LLM_STREAM_KEY: it puts owner text on the unauthenticated
    # display, so only enable it for a localhost-bound / box-monitor-only display.
    brain_llm_stream: bool = False


class SettingsPatch(BaseModel):
    # Unknown keys are a client bug, not a forward-compat case: reject them.
    model_config = ConfigDict(extra="forbid")

    image_analysis_mode: Literal["full", "ocr"] | None = None
    owner_timezone: str | None = None
    brain_llm_stream: bool | None = None


async def _read(ctx, store: SqlSettingsStore) -> SettingsOut:
    return SettingsOut(
        image_analysis_mode=await store.image_analysis_mode(ctx),
        owner_timezone=await store.owner_timezone(ctx),
        brain_llm_stream=await store.brain_llm_stream(ctx),
    )


@router.get("/settings")
async def read_settings(principal: PrincipalDep, store: SettingsStoreDep) -> SettingsOut:
    return await _read(ctx_for(principal), store)


@router.put("/settings")
async def update_settings(
    body: SettingsPatch, principal: PrincipalDep, store: SettingsStoreDep
) -> SettingsOut:
    ctx = ctx_for(principal)
    if body.image_analysis_mode is not None:
        await store.upsert(ctx, IMAGE_ANALYSIS_KEY, body.image_analysis_mode)
    if body.owner_timezone is not None:
        # Reject an unknown zone rather than store a value that reads as unset.
        if not is_valid_timezone(body.owner_timezone):
            raise HTTPException(status_code=422, detail="unknown timezone")
        await store.upsert(ctx, OWNER_TIMEZONE_KEY, body.owner_timezone)
    if body.brain_llm_stream is not None:
        await store.upsert(ctx, BRAIN_LLM_STREAM_KEY, body.brain_llm_stream)
    return await _read(ctx, store)
