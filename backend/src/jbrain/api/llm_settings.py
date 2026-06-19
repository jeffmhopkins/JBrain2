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
from jbrain.host_metrics import read_memory_gb
from jbrain.llm import local_catalog, local_weights
from jbrain.llm.errors import LlmError
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.providers import (
    REASONING_DEFAULT,
    REASONING_EFFORTS,
    id_for_spec,
    provider_choices,
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

# Provider ids are no longer a fixed set: enabling local hosting adds one id per
# provisioned catalog model. The PUT validates the id against the live choices
# instead of a Literal — see update_llm_settings.
ReasoningEffort = Literal["none", "low", "medium", "high"]


def get_settings_store(request: Request) -> SqlSettingsStore:
    return cast(SqlSettingsStore, request.app.state.settings_store)


SettingsStoreDep = Annotated[SqlSettingsStore, Depends(get_settings_store)]


def get_local_gateway(request: Request) -> LocalGatewayClient:
    return cast(LocalGatewayClient, request.app.state.local_gateway)


LocalGatewayDep = Annotated[LocalGatewayClient, Depends(get_local_gateway)]

# served_model (what the gateway reports/loads) ↔ catalog id (what the screen uses).
_SERVED_TO_ID = {m.served_model: m.id for m in local_catalog.CATALOG}


async def _loaded_ids(settings: Settings, gateway: LocalGatewayClient) -> set[str]:
    """Catalog ids currently resident in the gateway. Empty when hosting is off or
    the gateway is unreachable — runtime state never blocks the settings screen."""
    if not settings.local_llm_enabled:
        return set()
    return {_SERVED_TO_ID[s] for s in await gateway.running() if s in _SERVED_TO_ID}


class ProviderInfo(BaseModel):
    id: str
    label: str
    supports_reasoning: bool
    # The screen filters vision tasks to vision-capable choices.
    supports_vision: bool


class TaskInfo(BaseModel):
    id: str
    label: str
    # The effective provider id; falls back to the raw spec when it is off-menu.
    provider: str
    # Effort only for grok; null for non-reasoning providers.
    reasoning_effort: str | None


class LocalModelInfo(BaseModel):
    """A catalog model for the 'Manage local models' drawer — what it is and
    whether it is currently offered for routing. Provisioning (the weight
    download) stays a server-side, opt-in step, so this is read-only."""

    id: str
    label: str
    enabled: bool
    # Runtime state from the gateway (best-effort): True when resident in memory.
    # Always False when hosting is off or the gateway can't be reached.
    loaded: bool
    supports_vision: bool
    supports_tools: bool
    tiers: list[str]
    quant: str
    # Catalog's nominal download estimate — always present, drives the un-provisioned
    # rows the operator could still install.
    size_gb: float
    # The REAL measured size of the provisioned weights on disk, or null when the
    # model isn't on this box (so the drawer can show the true footprint for what's
    # installed and the estimate for what isn't).
    disk_gb: float | None
    note: str


class LoadedModelsOut(BaseModel):
    """Result of an unload (and the shape the screen polls): the catalog ids still
    resident, plus whether the gateway answered at all."""

    loaded: list[str]
    reachable: bool


class HostMemory(BaseModel):
    """Unified-memory gauge for the drawer's meter (None off Linux). On Strix Halo
    the iGPU shares system RAM, so this is the real headroom for loading models."""

    total_gb: float
    used_gb: float


class LlmSettingsOut(BaseModel):
    providers: list[ProviderInfo]
    reasoning_efforts: list[str]
    reasoning_default: str
    tasks: list[TaskInfo]
    # Local hosting is off by default; the drawer shows the catalog either way so
    # an operator can see what they could provision (via the install/CLI path).
    local_hosting_enabled: bool
    local_models: list[LocalModelInfo]
    # Live host memory for the drawer meter; None when hosting is off or off-Linux.
    host_memory: HostMemory | None = None


class TaskOverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Validated against the live provider choices in update_llm_settings (an
    # unknown id 422s there) — the set is dynamic once local hosting is on.
    provider: str
    # Only reasoning-capable providers (grok) carry an effort; local models and
    # Claude legitimately omit it (the screen sends just `{provider}`), and the
    # handler drops it for non-grok anyway. Required-here would 422 every
    # non-grok save before the handler runs.
    reasoning_effort: ReasoningEffort | None = None


class LlmSettingsPut(BaseModel):
    # A typo in a task id / provider / effort is a client bug, not forward-compat.
    model_config = ConfigDict(extra="forbid")

    tasks: dict[str, TaskOverrideIn]


def _effective(settings: Settings, task: str, overrides: dict[str, dict[str, str]]) -> TaskInfo:
    """The EFFECTIVE provider/effort for a task after merging stored overrides
    over the task default — the same precedence the router applies."""
    entry = overrides.get(task) or {}
    spec = entry.get("spec") or TASK_DEFAULTS[task]
    provider_id = id_for_spec(settings, spec)
    # Off-menu spec (e.g. an env pin to a model the UI doesn't list): surface the
    # provider half so the screen shows something truthful rather than crashing.
    # Tolerate a malformed stored spec too — show it raw rather than 500.
    if provider_id is None:
        try:
            provider_label = _split_spec(task, spec)[0]
        except LlmError:
            provider_label = spec
        return TaskInfo(
            id=task, label=TASK_LABELS[task], provider=provider_label, reasoning_effort=None
        )
    effort = entry.get("reasoning_effort") or REASONING_DEFAULT if provider_id == "grok" else None
    return TaskInfo(id=task, label=TASK_LABELS[task], provider=provider_id, reasoning_effort=effort)


async def _snapshot(
    settings: Settings,
    store: SqlSettingsStore,
    ctx: SessionContext,
    gateway: LocalGatewayClient,
) -> LlmSettingsOut:
    overrides = await store.llm_task_overrides(ctx)
    loaded = await _loaded_ids(settings, gateway)
    return LlmSettingsOut(
        providers=[
            ProviderInfo(
                id=c.id,
                label=c.label,
                supports_reasoning=c.supports_reasoning,
                supports_vision=c.supports_vision,
            )
            for c in provider_choices(settings)
        ],
        reasoning_efforts=list(REASONING_EFFORTS),
        reasoning_default=REASONING_DEFAULT,
        tasks=[_effective(settings, task, overrides) for task in TASK_DEFAULTS],
        local_hosting_enabled=settings.local_llm_enabled,
        local_models=[
            _local_model_info(settings, m, m.id in loaded) for m in local_catalog.CATALOG
        ],
        host_memory=_host_memory(settings),
    )


def _disk_gb(settings: Settings, model_id: str) -> float | None:
    """Measured weights size for a provisioned model, or None when hosting is off
    or the weights aren't on this box (the read is best-effort, like the meter)."""
    if not settings.local_llm_enabled:
        return None
    return local_weights.weights_size_gb(settings.local_models_dir, model_id)


def _host_memory(settings: Settings) -> HostMemory | None:
    """Live unified-memory reading — only when hosting is on (it drives the drawer
    meter); None off-Linux or when /proc/meminfo can't be read."""
    if not settings.local_llm_enabled:
        return None
    mem = read_memory_gb()
    if mem is None:
        return None
    total, used = mem
    return HostMemory(total_gb=total, used_gb=used)


def _local_model_info(
    settings: Settings, m: local_catalog.LocalModel, loaded: bool
) -> LocalModelInfo:
    enabled = settings.local_llm_enabled and m.id in settings.local_models
    return LocalModelInfo(
        id=m.id,
        label=m.label,
        enabled=enabled,
        loaded=loaded,
        supports_vision=m.supports_vision,
        supports_tools=m.supports_tools,
        tiers=list(m.tiers),
        quant=m.quant,
        size_gb=m.size_gb,
        disk_gb=_disk_gb(settings, m.id),
        note=m.note,
    )


@router.get("/settings/llm")
async def read_llm_settings(
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    return await _snapshot(settings, store, ctx_for(principal), gateway)


@router.post("/settings/llm/local-models/{model_id}/unload")
async def unload_local_model(
    model_id: str,
    principal: PrincipalDep,
    settings: SettingsDep,
    gateway: LocalGatewayDep,
) -> LoadedModelsOut:
    """Evict one resident model from the gateway's memory. 404 for a model that
    isn't a provisioned catalog id; 409 when hosting is off; 502 if the gateway
    rejects or can't be reached."""
    if not settings.local_llm_enabled:
        raise HTTPException(status_code=409, detail="local hosting is not enabled")
    model = local_catalog.get(model_id)
    if model is None or model_id not in settings.local_models:
        raise HTTPException(status_code=404, detail=f"unknown or unprovisioned model: {model_id}")
    try:
        await gateway.unload(model.served_model)
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway unload failed: {exc}") from exc
    loaded = await _loaded_ids(settings, gateway)
    return LoadedModelsOut(loaded=sorted(loaded), reachable=True)


@router.put("/settings/llm")
async def update_llm_settings(
    body: LlmSettingsPut,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    ctx = ctx_for(principal)
    for task in body.tasks:
        if task not in TASK_DEFAULTS:
            raise HTTPException(status_code=422, detail=f"unknown task: {task}")
    overrides = await store.llm_task_overrides(ctx)
    choices = {c.id: c for c in provider_choices(settings)}
    for task, choice in body.tasks.items():
        picked = choices.get(choice.provider)
        # Unknown id, or a local model offered only when local hosting is enabled.
        if picked is None:
            raise HTTPException(status_code=422, detail=f"unknown provider: {choice.provider}")
        # A vision task must draw a vision-capable provider — the UI filters this,
        # but enforce it server-side so a direct PUT can't send images to a
        # text-only local model (the stored override outranks the prompt tier).
        if task.startswith("vision.") and not picked.supports_vision:
            raise HTTPException(
                status_code=422,
                detail=f"{choice.provider} cannot serve vision task {task}",
            )
        entry: dict[str, str] = {"spec": picked.spec}
        # reasoning_effort is meaningful only for grok; drop it otherwise so the
        # stored shape stays clean and the router never misapplies it.
        if choice.provider == "grok":
            entry["reasoning_effort"] = choice.reasoning_effort or REASONING_DEFAULT
        overrides[task] = entry
    await store.upsert(ctx, LLM_TASK_OVERRIDES_KEY, overrides)
    return await _snapshot(settings, store, ctx, gateway)
