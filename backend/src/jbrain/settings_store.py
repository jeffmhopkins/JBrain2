"""The app.settings repo (migration 0012): server-synced user preferences.

Key -> jsonb value, owner-only RLS. Absent rows mean "default" — readers fall
back in code rather than seeding rows, so adding a setting is a constant here,
never a migration. `image_analysis_mode` is the first key: "full" (OCR + a
salient description the fact pipeline mines) or "ocr" (transcription only);
the OcrPipeline reads it per job and the Settings screen round-trips it.
"""

import json
from typing import Any, Literal, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session

ImageAnalysisMode = Literal["full", "ocr"]
IMAGE_ANALYSIS_MODES: tuple[ImageAnalysisMode, ...] = ("full", "ocr")
IMAGE_ANALYSIS_DEFAULT: ImageAnalysisMode = "full"
IMAGE_ANALYSIS_KEY = "image_analysis_mode"

# The secret in the read-only appointments ICS feed URL. Absent/null = no feed
# (disabled); rotating it instantly invalidates the old subscribe URL.
FEED_TOKEN_KEY = "appointments_feed_token"

# The note→graph pipeline the analysis trigger enqueues — the W3.3 cutover
# toggle. "analyze" = the v1 single-shot path (analyze_note); "integrate" = the
# v3 graph-aware path (integrate_note). DB-backed so the cutover is reversible
# LIVE (no redeploy). Defaults to "analyze" until the flip; absent/unrecognized
# falls back to the default.
NotePipeline = Literal["analyze", "integrate"]
NOTE_PIPELINES: tuple[NotePipeline, ...] = ("analyze", "integrate")
NOTE_PIPELINE_DEFAULT: NotePipeline = "analyze"
NOTE_PIPELINE_KEY = "note_analysis_pipeline"

# The two analysis job kinds the toggle selects between.
ANALYZE_JOB = "analyze_note"
INTEGRATE_JOB = "integrate_note"

# Embedding-assisted predicate canonicalization (docs/PREDICATE_CANONICALIZATION.md
# Phase 3): when on, the integrate pipeline cosine-matches an unknown predicate
# against the canonical index and either rewrites it (STRONG) or files a
# new_predicate review card. DB-backed, default OFF — the feature ships inert and
# is flipped live after the Phase-4 eval calibrates the bands.
PREDICATE_CANON_KEY = "predicate_canonicalization"
PREDICATE_CANON_DEFAULT = False

# Typed value-shape enforcement (docs/PREDICATE_CANONICALIZATION.md Phase 1/4):
# when off (default) a value_json that violates its predicate's declared shape is
# only logged; when on, it is DROPPED (the fact survives on its statement, per
# the storage invariant). DB-backed + default-off so it ships inert and is
# flipped live only after the Phase-4 eval confirms the conservative validator
# never drops a sound value — and is reversible without a redeploy.
VALUE_SHAPE_ENFORCE_KEY = "value_shape_enforce"
VALUE_SHAPE_ENFORCE_DEFAULT = False


class SqlSettingsStore:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def get(self, ctx: SessionContext, key: str, default: Any = None) -> Any:
        # value::text + json.loads — the queue's jsonb pattern; raw asyncpg
        # rows would otherwise hand back the JSON as an undecoded string.
        async with scoped_session(self._maker, ctx) as session:
            raw = (
                await session.execute(
                    text("SELECT value::text FROM app.settings WHERE key = :key"),
                    {"key": key},
                )
            ).scalar_one_or_none()
        return default if raw is None else json.loads(raw)

    async def upsert(self, ctx: SessionContext, key: str, value: Any) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "INSERT INTO app.settings (key, value)"
                    " VALUES (:key, cast(:value AS jsonb))"
                    " ON CONFLICT (key) DO UPDATE"
                    " SET value = excluded.value, updated_at = now()"
                ),
                {"key": key, "value": json.dumps(value)},
            )

    async def image_analysis_mode(self, ctx: SessionContext) -> ImageAnalysisMode:
        """The configured mode, defaulting (and falling back on any
        unrecognized stored value) to full analysis."""
        mode = await self.get(ctx, IMAGE_ANALYSIS_KEY, IMAGE_ANALYSIS_DEFAULT)
        return (
            cast(ImageAnalysisMode, mode)
            if mode in IMAGE_ANALYSIS_MODES
            else (IMAGE_ANALYSIS_DEFAULT)
        )

    async def note_pipeline(self, ctx: SessionContext) -> NotePipeline:
        """The configured note→graph pipeline (cutover toggle), defaulting (and
        falling back on any unrecognized stored value) to the v1 path."""
        mode = await self.get(ctx, NOTE_PIPELINE_KEY, NOTE_PIPELINE_DEFAULT)
        return cast(NotePipeline, mode) if mode in NOTE_PIPELINES else NOTE_PIPELINE_DEFAULT

    async def analysis_job_kind(self, ctx: SessionContext) -> str:
        """The job kind the analysis trigger should enqueue right now — the one
        source of truth every enqueue site shares so the cutover is atomic."""
        return INTEGRATE_JOB if await self.note_pipeline(ctx) == "integrate" else ANALYZE_JOB

    async def predicate_canonicalization(self, ctx: SessionContext) -> bool:
        """Whether embedding-assisted predicate canonicalization is on (Phase 3).
        Defaults OFF; only an explicit `true` enables it."""
        return await self.get(ctx, PREDICATE_CANON_KEY, PREDICATE_CANON_DEFAULT) is True

    async def value_shape_enforce(self, ctx: SessionContext) -> bool:
        """Whether a shape-violating value_json is DROPPED (vs only logged).
        Defaults OFF; only an explicit `true` enables enforcement."""
        return await self.get(ctx, VALUE_SHAPE_ENFORCE_KEY, VALUE_SHAPE_ENFORCE_DEFAULT) is True
