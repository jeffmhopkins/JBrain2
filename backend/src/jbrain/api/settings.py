"""User-settings endpoints over app.settings (migration 0012) — the first
server-synced preferences. The response is one extensible object; PUT takes a
partial body and rejects unknown keys/values at validation, so a typo can
never write an unreadable setting. Owner-only is implicit pre-P7 (only the
owner holds a session), and the store's RLS enforces it regardless.
"""

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from jbrain.api.deps import PrincipalDep
from jbrain.api.notes import ctx_for
from jbrain.settings_store import (
    BRAIN_ANSWER_CHORUS_DEFAULT,
    BRAIN_ANSWER_CHORUS_KEY,
    BRAIN_ANSWER_PITCH_DEFAULT,
    BRAIN_ANSWER_PITCH_KEY,
    BRAIN_ANSWER_PITCH_MAX_ST,
    BRAIN_ANSWER_ROBOT_DEFAULT,
    BRAIN_ANSWER_ROBOT_KEY,
    BRAIN_ANSWER_SPEED_DEFAULT,
    BRAIN_ANSWER_SPEED_KEY,
    BRAIN_ANSWER_SPEED_MAX,
    BRAIN_ANSWER_SPEED_MIN,
    BRAIN_ANSWER_VOICE_DEFAULT,
    BRAIN_ANSWER_VOICE_KEY,
    BRAIN_LLM_STREAM_KEY,
    BRAIN_READ_ALOUD_ENGINE_KEY,
    BRAIN_READ_ALOUD_KEY,
    IMAGE_ANALYSIS_KEY,
    LOCAL_LLM_AUTO_UPDATE_DEFAULT,
    LOCAL_LLM_AUTO_UPDATE_KEY,
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
    # Read the streamed wall-display turns aloud (piper TTS on the box). OFF by
    # default — the runtime companion to brain_llm_stream (BRAIN_READ_ALOUD_KEY),
    # same localhost-bound / box-monitor-only caveat.
    brain_read_aloud: bool = False
    # The Kokoro voice id the read-aloud speaks answers in (BRAIN_ANSWER_VOICE_KEY) — the
    # PWA renders its per-turn read-aloud on the box in this voice, and its Settings picker
    # writes it. Defaults to af_heart.
    brain_answer_voice: str = BRAIN_ANSWER_VOICE_DEFAULT
    # Which engine the PWA read-aloud renders with — "piper" (a legacy marker meaning on-box
    # Kokoro, with a device-native fallback) or "native" (the browser's own voice). Defaults to
    # on-box.
    brain_read_aloud_engine: Literal["piper", "native"] = "piper"
    # Read-aloud voice effects (applied to the PWA read-aloud AND the wall display). speed is
    # Kokoro's native rate; pitch (semitones) + chorus are post-render ffmpeg effects on the box.
    # Defaults are no-ops (1.0× / 0 st / off).
    brain_answer_speed: float = BRAIN_ANSWER_SPEED_DEFAULT
    brain_answer_pitch: float = BRAIN_ANSWER_PITCH_DEFAULT
    brain_answer_chorus: bool = BRAIN_ANSWER_CHORUS_DEFAULT
    brain_answer_robot: bool = BRAIN_ANSWER_ROBOT_DEFAULT
    # Whether an update rebuilds the model gateway onto the newest llama.cpp and smoke-tests
    # it by loading a model. ON by default; surfaced here because it lived only in `.env`,
    # which the owner has no terminal to reach.
    local_llm_auto_update: bool = LOCAL_LLM_AUTO_UPDATE_DEFAULT
    # The owner's read-aloud respelling map {word: "say it like"} — applied as a whole-word text
    # substitution before a clip is rendered (jbrain.api.brain). Empty by default.
    pronunciation_lexicon: dict[str, str] = {}


class SettingsPatch(BaseModel):
    # Unknown keys are a client bug, not a forward-compat case: reject them.
    model_config = ConfigDict(extra="forbid")

    image_analysis_mode: Literal["full", "ocr"] | None = None
    owner_timezone: str | None = None
    brain_llm_stream: bool | None = None
    brain_read_aloud: bool | None = None
    # A voice id from the live installed picker; bounded so a junk value can't bloat the
    # row. Empty/blank is rejected below rather than stored (it would read as the default).
    brain_answer_voice: Annotated[str, Field(max_length=128)] | None = None
    brain_read_aloud_engine: Literal["piper", "native"] | None = None
    # Voice effects — bounded to the box's ranges at the edge so a junk value is a 422, not a
    # clamp-on-read surprise. Speed 0.5–2.0×, pitch ±12 semitones, chorus on/off.
    brain_answer_speed: (
        Annotated[float, Field(ge=BRAIN_ANSWER_SPEED_MIN, le=BRAIN_ANSWER_SPEED_MAX)] | None
    ) = None
    brain_answer_pitch: (
        Annotated[float, Field(ge=-BRAIN_ANSWER_PITCH_MAX_ST, le=BRAIN_ANSWER_PITCH_MAX_ST)] | None
    ) = None
    brain_answer_chorus: bool | None = None
    brain_answer_robot: bool | None = None
    local_llm_auto_update: bool | None = None
    # The full respelling map to store (replace semantics). The store sanitizes/bounds it; the
    # Field caps the raw payload so a client can't post an unbounded body.
    pronunciation_lexicon: Annotated[dict[str, str], Field(max_length=200)] | None = None


async def _read(ctx, store: SqlSettingsStore) -> SettingsOut:
    return SettingsOut(
        image_analysis_mode=await store.image_analysis_mode(ctx),
        owner_timezone=await store.owner_timezone(ctx),
        brain_llm_stream=await store.brain_llm_stream(ctx),
        brain_read_aloud=await store.brain_read_aloud(ctx),
        brain_answer_voice=await store.brain_answer_voice(ctx),
        brain_read_aloud_engine=await store.brain_read_aloud_engine(ctx),
        brain_answer_speed=await store.brain_answer_speed(ctx),
        brain_answer_pitch=await store.brain_answer_pitch(ctx),
        brain_answer_chorus=await store.brain_answer_chorus(ctx),
        brain_answer_robot=await store.brain_answer_robot(ctx),
        local_llm_auto_update=await store.local_llm_auto_update(ctx),
        pronunciation_lexicon=await store.pronunciation_lexicon(ctx),
    )


@router.get("/settings")
async def read_settings(principal: PrincipalDep, store: SettingsStoreDep) -> SettingsOut:
    return await _read(ctx_for(principal), store)


@router.put("/settings")
async def update_settings(
    body: SettingsPatch, request: Request, principal: PrincipalDep, store: SettingsStoreDep
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
    if body.brain_read_aloud is not None:
        await store.upsert(ctx, BRAIN_READ_ALOUD_KEY, body.brain_read_aloud)
        # Push the read-aloud flag to the wall now so the voice panel shows/hides on the
        # toggle without waiting for the next chat turn (which re-syncs it anyway). Best-
        # effort display config, never owner text — a hiccup must not fail the save.
        flag_emit = getattr(request.app.state, "brain_flag_emit", None)
        if flag_emit is not None:
            flag_emit("read_aloud", body.brain_read_aloud)
    if body.brain_answer_voice is not None:
        # A blank id would read back as the default anyway — reject it rather than store
        # a value that silently means "unset".
        voice = body.brain_answer_voice.strip()
        if not voice:
            raise HTTPException(status_code=422, detail="empty voice")
        await store.upsert(ctx, BRAIN_ANSWER_VOICE_KEY, voice)
    if body.brain_read_aloud_engine is not None:
        await store.upsert(ctx, BRAIN_READ_ALOUD_ENGINE_KEY, body.brain_read_aloud_engine)
    # Voice effects: store, then push to the wall (best-effort, like read_aloud) so the on-box
    # display reads at the chosen speed/pitch/chorus live, without waiting for a redeploy.
    value_emit = getattr(request.app.state, "brain_value_emit", None)
    if body.brain_answer_speed is not None:
        await store.upsert(ctx, BRAIN_ANSWER_SPEED_KEY, body.brain_answer_speed)
        if value_emit is not None:
            value_emit("answer_speed", body.brain_answer_speed)
    if body.brain_answer_pitch is not None:
        await store.upsert(ctx, BRAIN_ANSWER_PITCH_KEY, body.brain_answer_pitch)
        if value_emit is not None:
            value_emit("answer_pitch", body.brain_answer_pitch)
    if body.brain_answer_chorus is not None:
        await store.upsert(ctx, BRAIN_ANSWER_CHORUS_KEY, body.brain_answer_chorus)
        if value_emit is not None:
            value_emit("answer_chorus", body.brain_answer_chorus)
    if body.brain_answer_robot is not None:
        await store.upsert(ctx, BRAIN_ANSWER_ROBOT_KEY, body.brain_answer_robot)
        if value_emit is not None:
            value_emit("answer_robot", body.brain_answer_robot)
    if body.local_llm_auto_update is not None:
        await store.upsert(ctx, LOCAL_LLM_AUTO_UPDATE_KEY, body.local_llm_auto_update)
    if body.pronunciation_lexicon is not None:
        # Replace semantics; the store sanitizes + bounds it, so a junk entry is dropped rather
        # than stored (an empty map clears the lexicon).
        await store.set_pronunciation_lexicon(ctx, body.pronunciation_lexicon)
    return await _read(ctx, store)
