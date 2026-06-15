"""The app.settings repo (migration 0012): server-synced user preferences.

Key -> jsonb value, owner-only RLS. Absent rows mean "default" — readers fall
back in code rather than seeding rows, so adding a setting is a constant here,
never a migration. `image_analysis_mode` is the first key: "full" (OCR + a
salient description the fact pipeline mines) or "ocr" (transcription only);
the OcrPipeline reads it per job and the Settings screen round-trips it.
"""

import json
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

# Embedding-assisted predicate canonicalization (docs/PREDICATE_CANONICALIZATION.md
# Phase 3): when on, the integrate pipeline cosine-matches an unknown predicate
# against the canonical index and either rewrites it (STRONG) or files a
# new_predicate review card. DB-backed; default ON now that the Phase-4 eval has
# calibrated the bands — flip off live (a settings upsert) to disable without a
# redeploy. Inert anyway unless the integrate pipeline is the active path and an
# embedder is configured (the worker seeds the index at boot).
PREDICATE_CANON_KEY = "predicate_canonicalization"
PREDICATE_CANON_DEFAULT = True

# Typed value-shape enforcement (docs/PREDICATE_CANONICALIZATION.md Phase 1/4):
# when off a value_json that violates its predicate's declared shape is only
# logged; when ON (default) it is DROPPED (the fact survives on its statement,
# per the storage invariant). DB-backed; flip off live (a settings upsert) to
# revert to log-only without a redeploy.
# Flip-time note: enabling this over a DB that ALREADY holds shape-invalid facts
# re-commits those value_json as None, which decide() won't see as an idempotent
# refresh of the stored bad-value row — so each such fact may churn (supersede /
# duplicate) once until rewritten. A fresh DB has none, so this is a no-op there;
# over an existing corpus a one-time backfill-drop can precede enabling it.
VALUE_SHAPE_ENFORCE_KEY = "value_shape_enforce"
VALUE_SHAPE_ENFORCE_DEFAULT = True

# Runtime-editable per-task LLM routing + reasoning effort (the settings screen's
# live control surface). A JSON map task → {"spec": "provider:model"?,
# "reasoning_effort": "none|low|medium|high"?}. The router reads this each call
# and merges it OVER env/defaults so the operator can re-route a task without a
# redeploy — see jbrain.llm.router._resolve_live. Absent = use static config.
LLM_TASK_OVERRIDES_KEY = "llm_task_overrides"
_VALID_REASONING_EFFORTS = ("none", "low", "medium", "high")

# The owner's IANA display timezone (e.g. "America/New_York"). Absent = UTC.
# Server-rendered times — the agent's appointment prose — localize to it so they
# agree with the cards the client localizes to the browser zone; the frontend
# syncs the browser's detected zone here. Stored as an IANA name, not an offset,
# so a future instant reads correctly across a DST boundary.
OWNER_TIMEZONE_KEY = "owner_timezone"


def is_valid_timezone(tz: str) -> bool:
    """Whether `tz` names a known IANA zone — the gate for storing/trusting one."""
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


# Provisional -> confirmed entity promotion (docs/entity.md "Entity lifecycle"):
# when on, an entity corroborated by >= CORROBORATION_THRESHOLD distinct
# same-domain notes is auto-confirmed; if its identity is contested (a live
# namesake), a `confirm_entity` review card is filed instead of auto-confirming.
# DB-backed; flip live. Default OFF until the goldens are migrated to expect
# confirmation (the rule deliberately changes entity status across notes).
ENTITY_PROMOTION_KEY = "entity_promotion"
ENTITY_PROMOTION_DEFAULT = False

# Reflexion (agent self-improvement Loop 1, docs/ASSISTANT.md): when on, the
# non-streaming agent turn verifies its answer with the deterministic grounding
# verifier and may re-run (hard-capped at N=2, adopted only on a strict score
# improvement, fully ephemeral). DB-backed; flip live. Default OFF — conservative
# until the verifier is calibrated on the eval corpus, and it spends extra model
# turns. Inert on the streamed /chat path (deltas can't be un-sent); see
# AgentLoop.run.
REFLEXION_KEY = "agent_reflexion"
REFLEXION_DEFAULT = False


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

    async def predicate_canonicalization(self, ctx: SessionContext) -> bool:
        """Whether embedding-assisted predicate canonicalization is on (Phase 3).
        Defaults ON; an explicit `false` (or any non-true value) disables it."""
        return await self.get(ctx, PREDICATE_CANON_KEY, PREDICATE_CANON_DEFAULT) is True

    async def value_shape_enforce(self, ctx: SessionContext) -> bool:
        """Whether a shape-violating value_json is DROPPED (vs only logged).
        Defaults ON; an explicit `false` (or any non-true value) disables it."""
        return await self.get(ctx, VALUE_SHAPE_ENFORCE_KEY, VALUE_SHAPE_ENFORCE_DEFAULT) is True

    async def owner_timezone(self, ctx: SessionContext) -> str | None:
        """The owner's IANA display timezone, or None when unset or unrecognized
        (callers fall back to UTC). An unknown stored value is treated as unset
        rather than trusted — a bad zone must never crash a render."""
        tz = await self.get(ctx, OWNER_TIMEZONE_KEY, None)
        return tz if isinstance(tz, str) and is_valid_timezone(tz) else None

    async def entity_promotion(self, ctx: SessionContext) -> bool:
        """Whether provisional->confirmed entity promotion is on (docs/entity.md).
        Defaults OFF; an explicit `true` enables it."""
        return await self.get(ctx, ENTITY_PROMOTION_KEY, ENTITY_PROMOTION_DEFAULT) is True

    async def agent_reflexion(self, ctx: SessionContext) -> bool:
        """Whether the non-streaming agent turn runs the Reflexion verify/retry pass
        (docs/ASSISTANT.md Loop 1). Defaults OFF; an explicit `true` enables it."""
        return await self.get(ctx, REFLEXION_KEY, REFLEXION_DEFAULT) is True

    async def llm_task_overrides(self, ctx: SessionContext) -> dict[str, dict[str, str]]:
        """The live per-task LLM routing/reasoning overrides, sanitized.

        Defensive on read — this feeds every LLM call, so a malformed stored
        value (wrong types, bad effort, junk keys) is dropped rather than allowed
        to crash a call. Only `spec` (str) and `reasoning_effort` (a known effort)
        survive; a task entry with neither is omitted entirely."""
        raw = await self.get(ctx, LLM_TASK_OVERRIDES_KEY, {})
        if not isinstance(raw, dict):
            return {}
        clean: dict[str, dict[str, str]] = {}
        for task, entry in raw.items():
            if not isinstance(task, str) or not isinstance(entry, dict):
                continue
            sane: dict[str, str] = {}
            spec = entry.get("spec")
            if isinstance(spec, str) and spec:
                sane["spec"] = spec
            effort = entry.get("reasoning_effort")
            if effort in _VALID_REASONING_EFFORTS:
                sane["reasoning_effort"] = effort
            if sane:
                clean[task] = sane
        return clean
