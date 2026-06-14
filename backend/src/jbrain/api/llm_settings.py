"""Runtime-editable per-task LLM routing + reasoning effort.

The settings screen reads the catalog of providers/efforts and every task's
EFFECTIVE choice, and writes per-task overrides into app.settings
(LLM_TASK_OVERRIDES_KEY). The router merges those over env/defaults on each call,
so this endpoint is the live control surface — no restart. Owner-only is
implicit pre-P7; the store's RLS enforces it regardless.
"""

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from jbrain.api.deps import PrincipalDep, SettingsDep
from jbrain.api.notes import ctx_for
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.llm.providers import (
    REASONING_DEFAULT,
    REASONING_EFFORTS,
    id_for_spec,
    provider_choices,
    spec_for_id,
)
from jbrain.llm.router import TASK_DEFAULTS, _split_spec
from jbrain.settings_store import LLM_TASK_OVERRIDES_KEY, SqlSettingsStore

router = APIRouter()

# Human labels for each routed task — the screen lists every TASK_DEFAULTS key.
TASK_LABELS: dict[str, str] = {
    "agent.turn": "Agent turn",
    "integrate.note": "Integrate note",
    "fact.adjudicate": "Fact adjudicate",
    "note.extract": "Note extract",
    "entity.disambiguate": "Entity disambiguate",
    "correction_note.extract": "Correction extract",
    "vision.ocr": "Vision OCR",
    "vision.caption": "Vision caption",
    "session.title": "Session title",
}

ProviderId = Literal["grok", "claude", "local"]
ReasoningEffort = Literal["none", "low", "medium", "high"]


def get_settings_store(request: Request) -> SqlSettingsStore:
    return cast(SqlSettingsStore, request.app.state.settings_store)


SettingsStoreDep = Annotated[SqlSettingsStore, Depends(get_settings_store)]


class ProviderInfo(BaseModel):
    id: str
    label: str
    supports_reasoning: bool


class TaskInfo(BaseModel):
    id: str
    label: str
    # The effective provider id; falls back to the raw spec when it is off-menu.
    provider: str
    # Effort only for grok; null for non-reasoning providers.
    reasoning_effort: str | None


class LlmSettingsOut(BaseModel):
    providers: list[ProviderInfo]
    reasoning_efforts: list[str]
    reasoning_default: str
    tasks: list[TaskInfo]


class TaskOverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ProviderId
    reasoning_effort: ReasoningEffort


class LlmSettingsPut(BaseModel):
    # A typo in a task id / provider / effort is a client bug, not forward-compat.
    model_config = ConfigDict(extra="forbid")

    tasks: dict[str, TaskOverrideIn]


def _effective(
    settings: Settings, task: str, overrides: dict[str, dict[str, str]]
) -> TaskInfo:
    """The EFFECTIVE provider/effort for a task after merging stored overrides
    over the task default — the same precedence the router applies."""
    entry = overrides.get(task) or {}
    spec = entry.get("spec") or TASK_DEFAULTS[task]
    provider_id = id_for_spec(settings, spec)
    # Off-menu spec (e.g. an env pin to a model the UI doesn't list): surface the
    # provider half so the screen shows something truthful rather than crashing.
    if provider_id is None:
        provider_label = _split_spec(task, spec)[0]
        return TaskInfo(
            id=task, label=TASK_LABELS[task], provider=provider_label, reasoning_effort=None
        )
    effort = entry.get("reasoning_effort") or REASONING_DEFAULT if provider_id == "grok" else None
    return TaskInfo(
        id=task, label=TASK_LABELS[task], provider=provider_id, reasoning_effort=effort
    )


async def _snapshot(
    settings: Settings, store: SqlSettingsStore, ctx: SessionContext
) -> LlmSettingsOut:
    overrides = await store.llm_task_overrides(ctx)
    return LlmSettingsOut(
        providers=[
            ProviderInfo(id=c.id, label=c.label, supports_reasoning=c.supports_reasoning)
            for c in provider_choices(settings)
        ],
        reasoning_efforts=list(REASONING_EFFORTS),
        reasoning_default=REASONING_DEFAULT,
        tasks=[_effective(settings, task, overrides) for task in TASK_DEFAULTS],
    )


@router.get("/settings/llm")
async def read_llm_settings(
    principal: PrincipalDep, settings: SettingsDep, store: SettingsStoreDep
) -> LlmSettingsOut:
    return await _snapshot(settings, store, ctx_for(principal))


@router.put("/settings/llm")
async def update_llm_settings(
    body: LlmSettingsPut,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
) -> LlmSettingsOut:
    ctx = ctx_for(principal)
    for task in body.tasks:
        if task not in TASK_DEFAULTS:
            raise HTTPException(status_code=422, detail=f"unknown task: {task}")
    overrides = await store.llm_task_overrides(ctx)
    for task, choice in body.tasks.items():
        spec = spec_for_id(settings, choice.provider)
        if spec is None:  # unreachable given the Literal, but keep the store honest
            raise HTTPException(status_code=422, detail=f"unknown provider: {choice.provider}")
        entry: dict[str, str] = {"spec": spec}
        # reasoning_effort is meaningful only for grok; drop it otherwise so the
        # stored shape stays clean and the router never misapplies it.
        if choice.provider == "grok":
            entry["reasoning_effort"] = choice.reasoning_effort
        overrides[task] = entry
    await store.upsert(ctx, LLM_TASK_OVERRIDES_KEY, overrides)
    return await _snapshot(settings, store, ctx)
