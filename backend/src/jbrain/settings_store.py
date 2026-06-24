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

# Per-model context window (tokens) the operator has overridden for a local catalog
# model, keyed by catalog id. An absent id uses the catalog default
# (local_catalog.LocalModel.context_window). DB-backed and read live so the meter
# and the regenerated llama-swap `-c` track without a redeploy. Defensive on read:
# a non-int / non-positive / bool value is dropped — a junk value must never read
# as a window.
LLM_LOCAL_CONTEXT_WINDOWS_KEY = "llm_local_context_windows"
# Catalog ids the operator has STAGED — the intent layer of the stage→load→unload
# lifecycle: a model the operator wants the gateway to serve (and that the bar
# projects RAM for) but that isn't necessarily resident yet. A list of catalog ids;
# non-string and duplicate entries are dropped on read (order preserved).
LLM_LOCAL_STAGED_KEY = "llm_local_staged"
# Catalog ids the operator has asked to PROVISION (download + enable) from the PWA,
# but that aren't on the box yet — the install queue. The update one-shot reads this
# (owner-scoped, via jbrain.cli) and provisions the union of it, the current
# LOCAL_MODELS, and the recommended set, then clears it. A list of catalog ids;
# non-string and duplicate entries are dropped on read (first-seen order preserved).
LLM_LOCAL_PROVISION_REQUESTED_KEY = "llm_local_provision_requested"
# Catalog ids the operator has asked to UNINSTALL (remove from LOCAL_MODELS, and —
# guarded — delete the downloaded weights) on the next update. The mirror of the
# install queue: the update one-shot reads this (owner-scoped, via jbrain.cli),
# subtracts it from the kept set so the model stops being served/enabled, prunes its
# weights behind hard guards, then clears it. A list of catalog ids; non-string and
# duplicate entries are dropped on read (first-seen order preserved).
LLM_LOCAL_REMOVE_REQUESTED_KEY = "llm_local_remove_requested"

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


# The wiki-build token budget (Phase-6 §3b) — SEPARATE from self-improvement so a runaway
# rewrite loop can't starve eval spend (and vice-versa). Same constant-not-a-migration store:
# the per-day spend tally lives in a settings row keyed by UTC date.
WIKI_BUILD_BUDGET_KEY = "wiki_build_daily_tokens"
WIKI_BUILD_BUDGET_DEFAULT = 500_000
WIKI_BUILD_KILL_SWITCH_KEY = "wiki_build_kill_switch"
WIKI_BUILD_KILL_SWITCH_DEFAULT = False
WIKI_BUILD_SPEND_PREFIX = "wiki_build_spend:"


# Provisional -> confirmed entity promotion (docs/entity.md "Entity lifecycle"):
# when on, an entity corroborated by >= CORROBORATION_THRESHOLD distinct
# same-domain notes is auto-confirmed; if its identity is contested (a live
# namesake), a `confirm_entity` review card is filed instead of auto-confirming.
# DB-backed; flip live. Default OFF until the goldens are migrated to expect
# confirmation (the rule deliberately changes entity status across notes).
ENTITY_PROMOTION_KEY = "entity_promotion"
ENTITY_PROMOTION_DEFAULT = False

# Reflexion (Loop 1) buffer-then-retry mode (docs/ASSISTANT.md "Self-improvement
# loops", Phase-5 Track R). DEFAULT is verify-and-annotate (mode b): a
# critique-worthy /chat turn streams normally and emits a `verdict` event after
# `done` (no retry, zero extra model calls — the verifiers are pure). This flag,
# when ON, opts into mode (a): the turn is produced non-streaming, the verifiers
# run, and the answer may be re-produced (strict-improvement, capped at N=2)
# BEFORE any token streams — so the user sees a spinner instead of a live token
# stream until verification clears. That latency tradeoff is why it is off by
# default. Reflexion spend is bound by the ordinary per-turn cost guardrail
# (Guardrails.max_cost_tokens), NOT the self-improvement budget. DB-backed; flip
# live (a settings upsert) with no redeploy.
REFLEXION_BUFFER_RETRY_KEY = "reflexion_buffer_retry"
REFLEXION_BUFFER_RETRY_DEFAULT = False

# Integration run + resolution-pin persistence (docs/WORKFLOW_ENGINE_PLAN.md §E7b,
# Wave 1 Track A): when on, integrate_note writes an `app.runs` row
# (kind='integration') and UPSERTs the Integrator's committed identity/predicate-key
# decisions into `app.resolution_pin` (the pure analysis.pins). Net-new (the loop
# logged to structlog only before), so it ships behind this flag and is validated by
# convergence, not diff-against-old. DB-backed; flip off live (a settings upsert) to
# disable the writes without a redeploy. Default ON: the writes are purely additive
# (a separate run row + pins, no change to the committed graph) and idempotent, so
# enabling them cannot corrupt existing data — only the persisted audit/pin trail.
INTEGRATION_PERSIST_KEY = "integration_persist"
INTEGRATION_PERSIST_DEFAULT = True

# The dispatcher's enqueue mode (docs/WORKFLOW_ENGINE_PLAN.md §5 Wave 2, §E7a):
# "shadow" computes the would-be enqueue + diffs it but never enqueues; "live"
# (the default since the Wave-2 cutover, Sub-task C) actually enqueues the engine's
# resolved jobs — the engine now OWNS the note->ingest, ingest->integrate, and
# resolution->consolidate paths, and the three hardcoded enqueues that twinned those
# events have been removed; "off" silences the dispatcher tick entirely. Separate
# from the master `workflow_dispatch` on/off switch (dispatcher.WORKFLOW_DISPATCH_KEY):
# that gate, when false, stops the tick regardless of mode. DB-backed, read live so
# an operator can ROLL BACK live with a single settings upsert (no redeploy): set
# mode "shadow" to stop the engine enqueuing, or set the master switch false to stop
# the tick — but with the hardcoded enqueues gone, only the reconcilers
# (backfill_pending_notes / backfill_pending_integration, now recurring) would then
# pick up new notes, so a rollback to shadow is a degraded mode, not the old path.
# Default flipped shadow->live at the cutover: the diff was clean and the engine is
# the live path; an unrecognized stored value still falls back to "shadow"
# (workflow_dispatch_mode getter) — a junk value never reads as the active enqueue.
WorkflowDispatchMode = Literal["shadow", "live", "off"]
WORKFLOW_DISPATCH_MODES: tuple[WorkflowDispatchMode, ...] = ("shadow", "live", "off")
# The deploy default when NO row is stored (an absent setting): the cutover made this
# "live" — the engine is the active enqueue path.
WORKFLOW_DISPATCH_MODE_DEFAULT: WorkflowDispatchMode = "live"
# The fail-closed fallback for a PRESENT but unrecognized stored value — distinct
# from the absent-row default: a junk/corrupt mode must read diff-only ("shadow"),
# never the active enqueue, even though the deploy default is now "live". Only an
# absent row earns the live default; corrupt input degrades to shadow until fixed.
WORKFLOW_DISPATCH_MODE_FALLBACK: WorkflowDispatchMode = "shadow"
WORKFLOW_DISPATCH_MODE_KEY = "workflow_dispatch_mode"


def _dedup_str_list(raw: object) -> list[str]:
    """The sanitize shared by the catalog-id list settings (staged, install queue):
    a non-list store, or non-string / duplicate entries, are dropped — first-seen
    order preserved. A junk store must never read as a model id."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item not in seen:
            seen.add(item)
            out.append(item)
    return out


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

    async def reflexion_buffer_retry(self, ctx: SessionContext) -> bool:
        """Whether Reflexion's opt-in buffer-then-retry mode (a) is on (Track R).
        Defaults OFF — the default is verify-and-annotate (mode b), which streams
        normally. An explicit `true` enables the spinner-latency retry path; any
        non-true value reads as off."""
        return (
            await self.get(ctx, REFLEXION_BUFFER_RETRY_KEY, REFLEXION_BUFFER_RETRY_DEFAULT) is True
        )

    async def wiki_build_kill_switch(self, ctx: SessionContext) -> bool:
        """Whether the wiki-build kill-switch is engaged. When on, the builder refuses to
        spend. Defaults OFF; only an explicit `true` engages it."""
        return (
            await self.get(ctx, WIKI_BUILD_KILL_SWITCH_KEY, WIKI_BUILD_KILL_SWITCH_DEFAULT) is True
        )

    async def wiki_build_daily_budget(self, ctx: SessionContext) -> int:
        """The per-day wiki-build TOKEN budget, separate from self-improvement. A malformed
        or non-positive stored value falls back to the default (fail-closed: junk is never
        unlimited)."""
        raw = await self.get(ctx, WIKI_BUILD_BUDGET_KEY, WIKI_BUILD_BUDGET_DEFAULT)
        return (
            raw
            if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0
            else WIKI_BUILD_BUDGET_DEFAULT
        )

    async def wiki_build_spent_today(self, ctx: SessionContext, *, day: str) -> int:
        """Tokens spent on wiki builds on UTC date `day` (a per-day settings row, no table).
        Absent/malformed = 0."""
        raw = await self.get(ctx, WIKI_BUILD_SPEND_PREFIX + day, 0)
        return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else 0

    async def record_wiki_build_spend(self, ctx: SessionContext, *, day: str, tokens: int) -> None:
        """Add `tokens` to UTC date `day`'s wiki-build tally (read-modify-write). A negative
        delta is clamped to 0 so a bad caller can never refund the budget."""
        current = await self.wiki_build_spent_today(ctx, day=day)
        await self.upsert(ctx, WIKI_BUILD_SPEND_PREFIX + day, current + max(tokens, 0))

    async def integration_persist(self, ctx: SessionContext) -> bool:
        """Whether the Integrator persists its run + resolution pins (§E7b).
        Defaults ON; an explicit `false` (or any non-true value) disables it."""
        return await self.get(ctx, INTEGRATION_PERSIST_KEY, INTEGRATION_PERSIST_DEFAULT) is True

    async def workflow_dispatch_mode(self, ctx: SessionContext) -> WorkflowDispatchMode:
        """The dispatcher's enqueue mode: "live" (the default since the Wave-2 cutover
        — actually enqueue, the engine owns the path), "shadow" (diff only, never
        enqueue — now a degraded rollback, see WORKFLOW_DISPATCH_MODE_DEFAULT), or
        "off". An unrecognized stored value falls back to "shadow": a junk mode must
        never read as "live" off corrupt input (it stays diff-only until corrected)."""
        mode = await self.get(ctx, WORKFLOW_DISPATCH_MODE_KEY, WORKFLOW_DISPATCH_MODE_DEFAULT)
        return (
            cast(WorkflowDispatchMode, mode)
            if mode in WORKFLOW_DISPATCH_MODES
            else WORKFLOW_DISPATCH_MODE_FALLBACK
        )

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

    async def llm_local_context_windows(self, ctx: SessionContext) -> dict[str, int]:
        """Per-model context-window overrides, keyed by catalog id, sanitized.

        Defensive on read — this feeds both the context meter and the regenerated
        gateway `-c`, so a non-dict store, or any entry whose value isn't a positive
        int (bool excluded), is dropped rather than trusted as a window."""
        raw = await self.get(ctx, LLM_LOCAL_CONTEXT_WINDOWS_KEY, {})
        if not isinstance(raw, dict):
            return {}
        clean: dict[str, int] = {}
        for mid, win in raw.items():
            if (
                isinstance(mid, str)
                and isinstance(win, int)
                and not isinstance(win, bool)
                and win > 0
            ):
                clean[mid] = win
        return clean

    async def set_llm_local_context_window(
        self, ctx: SessionContext, *, model_id: str, window: int | None
    ) -> dict[str, int]:
        """Set (window is a positive int) or clear (window is None) one model's
        override; returns the sanitized map. Read-modify-write on the single row.
        Bounds/validity are the API's job — the store stays a dumb sanitizer."""
        current = await self.llm_local_context_windows(ctx)
        if window is None:
            current.pop(model_id, None)
        else:
            current[model_id] = window
        await self.upsert(ctx, LLM_LOCAL_CONTEXT_WINDOWS_KEY, current)
        return current

    async def llm_local_staged(self, ctx: SessionContext) -> list[str]:
        """Catalog ids the operator has staged, sanitized: a non-list store, or
        non-string / duplicate entries, are dropped (first-seen order preserved)."""
        return _dedup_str_list(await self.get(ctx, LLM_LOCAL_STAGED_KEY, []))

    async def set_llm_local_staged(self, ctx: SessionContext, ids: list[str]) -> list[str]:
        """Replace the staged set with `ids` (sanitized like the reader); returns it."""
        clean = _dedup_str_list(ids)
        await self.upsert(ctx, LLM_LOCAL_STAGED_KEY, clean)
        return clean

    async def llm_local_provision_requested(self, ctx: SessionContext) -> list[str]:
        """Catalog ids queued for provisioning from the PWA, sanitized like the
        staged set (non-list store / non-string / duplicates dropped, order kept)."""
        return _dedup_str_list(await self.get(ctx, LLM_LOCAL_PROVISION_REQUESTED_KEY, []))

    async def set_llm_local_provision_requested(
        self, ctx: SessionContext, ids: list[str]
    ) -> list[str]:
        """Replace the install queue with `ids` (sanitized like the reader); returns it."""
        clean = _dedup_str_list(ids)
        await self.upsert(ctx, LLM_LOCAL_PROVISION_REQUESTED_KEY, clean)
        return clean

    async def llm_local_remove_requested(self, ctx: SessionContext) -> list[str]:
        """Catalog ids queued for uninstall from the PWA, sanitized like the install
        queue (non-list store / non-string / duplicates dropped, order kept)."""
        return _dedup_str_list(await self.get(ctx, LLM_LOCAL_REMOVE_REQUESTED_KEY, []))

    async def set_llm_local_remove_requested(
        self, ctx: SessionContext, ids: list[str]
    ) -> list[str]:
        """Replace the uninstall queue with `ids` (sanitized like the reader); returns it."""
        clean = _dedup_str_list(ids)
        await self.upsert(ctx, LLM_LOCAL_REMOVE_REQUESTED_KEY, clean)
        return clean
