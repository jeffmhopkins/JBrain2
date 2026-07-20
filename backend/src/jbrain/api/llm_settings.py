"""Runtime-editable per-task LLM routing + reasoning effort.

The settings screen reads the catalog of providers/efforts and every task's
EFFECTIVE choice, and writes per-task overrides into app.settings
(LLM_TASK_OVERRIDES_KEY). The router merges those over env/defaults on each call,
so this endpoint is the live control surface — no restart. Owner-only is
implicit pre-P7; the store's RLS enforces it regardless.
"""

import contextlib
from dataclasses import asdict
from typing import Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from jbrain.api.deps import PrincipalDep, SettingsDep
from jbrain.api.notes import ctx_for
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.host_metrics import read_memory_gb
from jbrain.llm import llama_swap_config, local_catalog, local_weights
from jbrain.llm.errors import LlmError
from jbrain.llm.local_gateway import LocalGatewayClient, LocalGatewayError
from jbrain.llm.providers import (
    REASONING_DEFAULT,
    REASONING_EFFORTS,
    id_for_spec,
    provider_choices,
    supports_reasoning,
)
from jbrain.llm.residency import ResidencyCoordinator, ResidencyError
from jbrain.llm.router import TASK_DEFAULTS, TASK_REASONING_BUCKET, _split_spec
from jbrain.settings_store import (
    JCODE_PLANNER_SAME,
    LLM_TASK_OVERRIDES_KEY,
    SqlSettingsStore,
)

log = structlog.get_logger()

router = APIRouter()

# Human labels for each routed task — the screen lists every TASK_DEFAULTS key.
TASK_LABELS: dict[str, str] = {
    "agent.turn": "Agent turn",
    "agent.vision": "Agent image analysis",
    "integrate.note": "Integrate note",
    "intake.materialize": "Intake materialize",
    "fact.adjudicate": "Fact adjudicate",
    "note.extract": "Note extract",
    "entity.disambiguate": "Entity disambiguate",
    "correction_note.extract": "Correction extract",
    "vision.ocr": "Vision OCR",
    "vision.caption": "Vision caption",
    "video.summarize": "Video summary",
    "session.title": "Session title",
    "research.title": "Research report title",
    "wiki.rewrite": "Wiki rewrite",
    "wiki.ground": "Wiki grounding",
    "wiki.lint.contradiction": "Wiki lint — contradiction",
    "wiki.lint.stale": "Wiki lint — stale claim",
    "triage.classify": "Inbox triage",
    "pet.turn": "JPet — reply",
    "pet.thought": "JPet — idle thought",
    "pet.statue": "JPet — statue sculptor",
}


# Tasks that send image content to the model and so require a vision-capable provider:
# the ingest vision.* tasks plus the agent's analyze_image route (agent.vision). The
# screen filters these to vision choices; the PUT enforces it server-side.
def is_vision_task(task: str) -> bool:
    return task.startswith("vision.") or task == "agent.vision"


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


def get_residency(request: Request) -> ResidencyCoordinator | None:
    """The box's evictor/restorer, or None on a build without it wired (never in prod;
    tolerated so the load / plan-load endpoints degrade to a plain warm)."""
    return cast(ResidencyCoordinator | None, getattr(request.app.state, "residency", None))


ResidencyDep = Annotated[ResidencyCoordinator | None, Depends(get_residency)]

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
    # Effort for a reasoning-capable provider (Grok or a local gpt-oss/GLM); null
    # for non-reasoning providers.
    reasoning_effort: str | None


class LocalModelInfo(BaseModel):
    """A catalog model for the 'Manage local models' drawer — what it is, whether
    it is offered for routing, and (for an un-provisioned model) whether the operator
    has queued it for install. Provisioning runs during the next update one-shot; the
    drawer follows it live via download_gb."""

    id: str
    label: str
    # Provisioned on the box (in LOCAL_MODELS) — the weights are installed and it CAN be
    # made available. The Catalogue tab's install/uninstall state.
    enabled: bool
    # Effective-available to the router: provisioned AND not marked unavailable by the
    # operator. Only these show in the Available/Resident tabs and can be staged/loaded. A
    # per-owner runtime toggle (the Catalogue's Available switch) that keeps the weights.
    available: bool
    # Queued for provisioning from the PWA but not yet on the box (in the install
    # queue and not enabled). The next update downloads it and flips it to enabled.
    queued: bool
    # Queued for uninstall from the PWA but still provisioned (in the remove queue
    # and still enabled). The next update drops it from LOCAL_MODELS — and, guarded,
    # prunes its weights — and it leaves the roster on its own once enabled flips.
    remove_queued: bool
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
    # Bytes on disk for this model's directory (partial downloads included), in GB,
    # or null when nothing is downloaded yet / hosting is off. Drives the live
    # install-progress bar: download_gb / size_gb is the percentage while a queued
    # model is being provisioned by an update.
    download_gb: float | None
    note: str
    # The model's catalog default context window — the gateway's `-c` absent an
    # override (the size picker's "no override" value).
    context_window: int
    # The model's native maximum window — the ceiling the drawer caps the size picker
    # at, so the operator can raise `-c` toward what the weights support (not just the
    # conservative default). The picker's KV-cache estimate flags when a big one won't fit.
    max_context_window: int
    # The operator's per-model override (tokens), or null to use the default. Drives
    # the size picker's current value; editable only while the model isn't resident.
    context_window_override: int | None
    # Estimated KV-cache size (GB) at the EFFECTIVE window (override or default) —
    # the context portion of the model's memory-bar segment. An estimate, not a
    # measurement (see local_catalog.kv_gb_per_128k).
    kv_gb: float


class LoadedModelsOut(BaseModel):
    """Result of an unload (and the shape the screen polls): the catalog ids still
    resident, plus whether the gateway answered at all."""

    loaded: list[str]
    reachable: bool


class EvictionVictimOut(BaseModel):
    """One model the staged load would evict — catalog id + label + its resident
    footprint (GB), so the screen can mark it on the memory bar."""

    id: str
    label: str
    gb: float


class LoadPlanOut(BaseModel):
    """The dry-run for the settings screen's stage preview: what loading `model_id`
    would evict right now, and where the box would land — no side effects. The Load
    button then commits it (the load endpoint runs the same eviction for real)."""

    model_id: str
    # False when the box can't be measured (hosting off / gateway or meminfo
    # unreadable): the screen can't show an eviction preview, only offer the load.
    measured: bool
    # Already resident → loading is a no-op; fits → loads with no eviction.
    already_resident: bool
    fits: bool
    # Even evicting everything leaves it over the free-RAM floor (it takes the box).
    over: bool
    # Even evicting everything, the model can't fit total RAM — the load is refused (a
    # commit would 409). The screen disables "Load" and says why.
    over_box: bool
    victims: list[EvictionVictimOut]
    # Measured used memory now, projected used after the load, the free-RAM floor, total.
    resident_gb: float
    projected_gb: float
    ceiling_gb: float
    total_gb: float


class HostMemory(BaseModel):
    """Unified-memory gauge for the drawer's meter (None off Linux). On Strix Halo
    the iGPU shares system RAM, so this is the real headroom for loading models."""

    total_gb: float
    used_gb: float


class JcodeModelChoice(BaseModel):
    id: str
    label: str


class JcodeModelInfo(BaseModel):
    """The code-mode (jcode) agent's model selector. The card is shown only when
    code mode is enabled; the dropdown offers installed, tool-capable local models
    (jcode is a tool-using agent on the on-box gateway)."""

    enabled: bool
    # The effective EXECUTOR model id the agent runs (grok's `[models] default`): the
    # stored override, else the config default.
    model: str
    # The JBRAIN_JCODE_MODEL config default — the value when no override is stored.
    default: str
    # The PLANNER selection (grok's `plan` subagent): a model id, or the "same" sentinel
    # meaning single-model (planner == executor, no separate model). The stored override
    # resolved against the config default; the card renders "Same as executor" for the
    # sentinel and marks `planner_default` as the suggested split model.
    planner: str
    # The JBRAIN_JCODE_PLANNER_MODEL config default — the split planner the card suggests
    # (gpt-oss-120b) and the value when nothing is stored.
    planner_default: str
    # The single-model sentinel value the planner select uses for its "Same as executor"
    # option — surfaced so the client and server agree on the one magic string.
    planner_same: str = JCODE_PLANNER_SAME
    options: list[JcodeModelChoice]


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
    # Code mode's model selector (the dropdown card). Always present; `enabled`
    # gates whether the screen renders it.
    jcode: JcodeModelInfo


class TaskOverrideIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Validated against the live provider choices in update_llm_settings (an
    # unknown id 422s there) — the set is dynamic once local hosting is on.
    provider: str
    # Only reasoning-capable providers (Grok, local gpt-oss/GLM) carry an effort;
    # Claude and non-reasoning local models legitimately omit it (the screen sends
    # just `{provider}`), and the handler drops it for them anyway. Required-here
    # would 422 every non-reasoning save before the handler runs.
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
    # Effective effort for the screen: a stored override wins; else the task's bucket
    # default (high/medium/low); else the global fallback for a task with no bucket
    # (the vision tasks, when routed to a reasoning-capable cloud provider).
    effort = (
        (entry.get("reasoning_effort") or TASK_REASONING_BUCKET.get(task) or REASONING_DEFAULT)
        if supports_reasoning(settings, provider_id)
        else None
    )
    return TaskInfo(id=task, label=TASK_LABELS[task], provider=provider_id, reasoning_effort=effort)


async def _snapshot(
    settings: Settings,
    store: SqlSettingsStore,
    ctx: SessionContext,
    gateway: LocalGatewayClient,
) -> LlmSettingsOut:
    overrides = await store.llm_task_overrides(ctx)
    windows = await store.llm_local_context_windows(ctx)
    unavailable = set(await store.llm_local_unavailable(ctx))
    requested = set(await store.llm_local_provision_requested(ctx))
    removing = set(await store.llm_local_remove_requested(ctx))
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
            _local_model_info(
                settings,
                m,
                m.id in loaded,
                windows,
                m.id in unavailable,
                m.id in requested,
                m.id in removing,
            )
            for m in local_catalog.CATALOG
        ],
        host_memory=_host_memory(settings),
        jcode=await _jcode_info(settings, store, ctx),
    )


def _jcode_options(settings: Settings) -> list[JcodeModelChoice]:
    """Installed, tool-capable local models the jcode dropdown offers — jcode is a
    tool-using agent on the on-box gateway, so non-tool or uninstalled models are
    excluded. Empty when local hosting is off (nothing is installed to serve). Shares the
    one source of truth with the sandbox's grok `/model` list + the residency-aware proxy
    (jbrain.llm.local_catalog.jcode_models)."""
    return [
        JcodeModelChoice(id=m.id, label=m.label)
        for m in local_catalog.jcode_models(settings.local_llm_enabled, settings.local_models)
    ]


async def _jcode_info(
    settings: Settings, store: SqlSettingsStore, ctx: SessionContext
) -> JcodeModelInfo:
    stored = await store.jcode_model(ctx)
    stored_planner = await store.jcode_planner_model(ctx)
    # The sentinel is preserved as-is (single-model); otherwise the stored override wins
    # and "" falls back to the config default — same rule as the executor.
    planner = (
        stored_planner
        if stored_planner == JCODE_PLANNER_SAME
        else (stored_planner or settings.jcode_planner_model)
    )
    return JcodeModelInfo(
        enabled=settings.jcode_enabled,
        # The stored override wins; "" falls back to the config default.
        model=stored or settings.jcode_model,
        default=settings.jcode_model,
        planner=planner,
        planner_default=settings.jcode_planner_model,
        options=_jcode_options(settings),
    )


def _disk_gb(settings: Settings, model_id: str) -> float | None:
    """Measured weights size for a provisioned model, or None when hosting is off
    or the weights aren't on this box (the read is best-effort, like the meter)."""
    if not settings.local_llm_enabled:
        return None
    return local_weights.weights_size_gb(settings.local_models_dir, model_id)


def _download_gb(settings: Settings, model_id: str) -> float | None:
    """Bytes on disk for a model's dir (partial shards included), or None when
    hosting is off or nothing has been downloaded — the numerator of the live
    install-progress bar."""
    if not settings.local_llm_enabled:
        return None
    return local_weights.dir_size_gb(settings.local_models_dir, model_id)


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
    settings: Settings,
    m: local_catalog.LocalModel,
    loaded: bool,
    windows: dict[str, int],
    unavailable: bool,
    requested: bool,
    removing: bool,
) -> LocalModelInfo:
    enabled = settings.local_llm_enabled and m.id in settings.local_models
    # Effective-available: provisioned AND not toggled off by the operator.
    available = enabled and not unavailable
    override = windows.get(m.id)
    effective_window = override if override is not None else m.context_window
    kv_gb = round(m.kv_gb_per_128k * effective_window / 131072, 2)
    return LocalModelInfo(
        id=m.id,
        label=m.label,
        enabled=enabled,
        available=available,
        # Queued only while not yet provisioned — once an install completes the model
        # is enabled, so it leaves the "available to install" list on its own.
        queued=requested and not enabled,
        # The mirror: queued for uninstall only while still provisioned — once the
        # update drops it from LOCAL_MODELS it stops being enabled, so the flag
        # clears on its own.
        remove_queued=removing and enabled,
        loaded=loaded,
        supports_vision=m.supports_vision,
        supports_tools=m.supports_tools,
        tiers=list(m.tiers),
        quant=m.quant,
        size_gb=m.size_gb,
        disk_gb=_disk_gb(settings, m.id),
        download_gb=_download_gb(settings, m.id),
        note=m.note,
        context_window=m.context_window,
        max_context_window=m.max_context_window,
        context_window_override=override,
        kv_gb=kv_gb,
    )


def _require_provisioned(settings: Settings, model_id: str) -> local_catalog.LocalModel:
    """The catalog model for `model_id`, or raise: 409 when hosting is off, 404 when
    the id isn't a provisioned catalog model. The gate for every per-model action."""
    if not settings.local_llm_enabled:
        raise HTTPException(status_code=409, detail="local hosting is not enabled")
    model = local_catalog.get(model_id)
    if model is None or model_id not in settings.local_models:
        raise HTTPException(status_code=404, detail=f"unknown or unprovisioned model: {model_id}")
    return model


def _require_installable(settings: Settings, model_id: str) -> local_catalog.LocalModel:
    """The catalog model for `model_id` when it can be queued for install, or raise:
    409 when hosting is off (the gateway/GPU env is a one-time host setup the PWA
    can't bootstrap), 404 for an id outside the catalog, 409 when it is already
    provisioned (enabled). The gate for the install-queue endpoints."""
    if not settings.local_llm_enabled:
        raise HTTPException(status_code=409, detail="local hosting is not enabled")
    model = local_catalog.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"unknown model: {model_id}")
    if model_id in settings.local_models:
        raise HTTPException(status_code=409, detail=f"already provisioned: {model_id}")
    return model


def _require_uninstallable(settings: Settings, model_id: str) -> local_catalog.LocalModel:
    """The catalog model for `model_id` when it can be queued for uninstall, or raise:
    409 when hosting is off, 404 for an id outside the catalog, 409 when it has NOTHING
    to remove (neither enabled nor weights on disk). The gate for the uninstall-queue
    endpoints — the mirror of _require_installable.

    Weights-on-disk counts even when the model is NOT enabled: a model dropped from
    LOCAL_MODELS (e.g. an alt the sync's roster recompute disabled) leaves its weights
    orphaned on disk with no other way to reclaim them, so the drawer must be able to
    queue their removal. The sync prunes any id in the remove queue regardless of the
    roster."""
    if not settings.local_llm_enabled:
        raise HTTPException(status_code=409, detail="local hosting is not enabled")
    model = local_catalog.get(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail=f"unknown model: {model_id}")
    on_disk = _disk_gb(settings, model_id) is not None
    if model_id not in settings.local_models and not on_disk:
        raise HTTPException(status_code=409, detail=f"nothing to remove: {model_id}")
    return model


def _try_regenerate(settings: Settings, windows: dict[str, int]) -> None:
    """Re-stamp llama-swap.yaml with the current per-model windows so the gateway (run
    with --watch-config) reloads at the configured `-c`. Every model is a non-swapping
    group member regardless of staging (the app is the sole evictor), so this is driven
    only by window edits. Best-effort: the settings are already persisted (so the meter
    is correct), and the weights dir may not be writable/complete in every deploy — a
    regen failure only delays the gateway catching up, it must never fail the edit."""
    try:
        manifest = [asdict(m) for m in local_catalog.selected(settings.local_models)]
        llama_swap_config.write(settings.local_models_dir, manifest, windows=windows)
    except Exception as exc:  # noqa: BLE001 — best-effort; the override is saved either way
        log.warning("llm_settings.gateway_config_regen_failed", error=str(exc))


async def _unload_if_loaded(
    settings: Settings, gateway: LocalGatewayClient, model: local_catalog.LocalModel
) -> None:
    """Evict `model` if resident so its next request reloads at the new `-c` (a
    running llama-server can't resize its KV cache live). Best-effort: a gateway
    that's down just means the stale process lingers until it's next swapped."""
    try:
        if model.served_model in await gateway.running():
            await gateway.unload(model.served_model)
    except LocalGatewayError as exc:
        log.warning("llm_settings.reload_unload_failed", model=model.id, error=str(exc))


@router.get("/settings/llm")
async def read_llm_settings(
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    return await _snapshot(settings, store, ctx_for(principal), gateway)


class JcodeModelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "" reverts to the JBRAIN_JCODE_MODEL default; any other value must be an
    # installed, tool-capable local model id (validated server-side below).
    model: str


@router.put("/settings/llm/jcode-model")
async def set_jcode_model(
    body: JcodeModelIn,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Choose the model the code-mode (jcode) agent runs. "" reverts to the config
    default; any other value must be an installed, tool-capable local model (422
    otherwise) — the same set the dropdown shows. New jcode sessions pick up the
    change; an in-flight session keeps the model it started with."""
    valid = {c.id for c in _jcode_options(settings)}
    if body.model and body.model not in valid:
        raise HTTPException(
            status_code=422, detail="model must be an installed, tool-capable local model"
        )
    ctx = ctx_for(principal)
    await store.set_jcode_model(ctx, body.model)
    return await _snapshot(settings, store, ctx, gateway)


class JcodePlannerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # "" reverts to the JBRAIN_JCODE_PLANNER_MODEL default; the "same" sentinel means
    # single-model (planner == executor); any other value must be an installed,
    # tool-capable local model id (validated server-side below).
    planner: str


@router.put("/settings/llm/jcode-planner")
async def set_jcode_planner(
    body: JcodePlannerIn,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Choose the PLANNER model for code mode's grok `plan` subagent. "" reverts to the
    config default (the split planner); "same" collapses the card to a single model (the
    executor plans too); any other value must be an installed, tool-capable local model
    (422 otherwise) — the same set the executor dropdown shows. New jcode sessions pick up
    the change; an in-flight session keeps the planner it started with."""
    valid = {c.id for c in _jcode_options(settings)}
    if body.planner and body.planner != JCODE_PLANNER_SAME and body.planner not in valid:
        raise HTTPException(
            status_code=422,
            detail="planner must be an installed, tool-capable local model or 'same'",
        )
    ctx = ctx_for(principal)
    await store.set_jcode_planner_model(ctx, body.planner)
    return await _snapshot(settings, store, ctx, gateway)


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
    return await gateway_unload(model_id, settings, gateway)


@router.post("/settings/llm/local-models/{model_id}/load")
async def load_local_model(
    model_id: str,
    principal: PrincipalDep,
    settings: SettingsDep,
    gateway: LocalGatewayDep,
    residency: ResidencyDep,
) -> LoadedModelsOut:
    """Make the gateway load one model into memory (the settings screen's stage → Load).
    First frees room the deliberate way — evict the fewest, biggest resident models to hold
    the free-RAM floor, WITHOUT scheduling them for restore (a manual load is a steady-state
    change, not a transient displacement) — then warms the model. The eviction is exactly
    what the stage preview (plan-load) showed. A model that can't fit the box even after
    evicting everything is REFUSED with a 409 (loading it would OOM-crash the box) — nothing
    is evicted in that case. 404 for an unprovisioned id; 409 when hosting is off or the
    model can't fit; 502 if the gateway rejects or can't be reached."""
    model = _require_provisioned(settings, model_id)
    if residency is not None:
        try:
            await residency.free_room(model.served_model)  # evict-to-fit, or refuse if impossible
        except ResidencyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await gateway_load(model_id, settings, gateway)


class ContextWindowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # null clears the override (revert to the catalog default); else 1..native-max.
    context_window: int | None = None


@router.put("/settings/llm/local-models/{model_id}/context-window")
async def set_local_context_window(
    model_id: str,
    body: ContextWindowIn,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Set (or clear, with null) one model's context window. 409 when hosting is
    off; 404 for an unprovisioned id; 422 for a window outside 1..native-max (the
    model's full architectural window, not the conservative served default).
    Persists the override (so the meter updates at once), re-stamps the gateway
    config, and unloads the model if resident so its next request reloads at the
    new `-c` — a running process can't resize its KV cache live."""
    model = _require_provisioned(settings, model_id)
    ceiling = model.max_context_window
    if body.context_window is not None and not (1 <= body.context_window <= ceiling):
        raise HTTPException(status_code=422, detail=f"context window must be 1..{ceiling}")
    ctx = ctx_for(principal)
    windows = await store.set_llm_local_context_window(
        ctx, model_id=model_id, window=body.context_window
    )
    _try_regenerate(settings, windows)
    await _unload_if_loaded(settings, gateway, model)
    return await _snapshot(settings, store, ctx, gateway)


class AvailableIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool


@router.put("/settings/llm/local-models/{model_id}/available")
async def set_local_available(
    model_id: str,
    body: AvailableIn,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Mark a provisioned model available / unavailable to the router — a per-owner runtime
    toggle that keeps the weights (unlike Uninstall). Unavailable models drop out of the
    Available/Resident tabs and can't be staged/loaded; making one unavailable also unloads
    it if resident, to free the memory. The weights stay on disk, so it flips back instantly.
    404 for an unprovisioned id; 409 when hosting is off. No gateway re-stamp — the model
    stays a swap-group member; availability is an app-side roster filter."""
    model = _require_provisioned(settings, model_id)
    ctx = ctx_for(principal)
    unavailable = await store.llm_local_unavailable(ctx)
    if body.available:
        unavailable = [u for u in unavailable if u != model_id]
    elif model_id not in unavailable:
        unavailable.append(model_id)
    await store.set_llm_local_unavailable(ctx, unavailable)
    # Making it unavailable frees its memory now — an unroutable model shouldn't hold RAM.
    if not body.available:
        with contextlib.suppress(LocalGatewayError):
            if model.served_model in await gateway.running():
                await gateway.unload(model.served_model)
    return await _snapshot(settings, store, ctx, gateway)


@router.post("/settings/llm/local-models/{model_id}/plan-load")
async def plan_load_local_model(
    model_id: str,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    residency: ResidencyDep,
) -> LoadPlanOut:
    """Dry-run: what would loading `model_id` evict right now, and where would the box land?
    No side effects — the settings screen's "stage" preview calls this so the operator sees
    the eviction before committing the load. 404 for an unprovisioned id; 409 when hosting is
    off. `measured` is false when the box can't be read (gateway/meminfo down): the screen
    then just offers the load without an eviction preview."""
    model = _require_provisioned(settings, model_id)
    ctx = ctx_for(principal)
    windows = await store.llm_local_context_windows(ctx)
    plan = await residency.plan_load(model.served_model) if residency is not None else None
    if plan is None:
        return LoadPlanOut(
            model_id=model_id,
            measured=False,
            already_resident=False,
            fits=True,
            over=False,
            over_box=False,
            victims=[],
            resident_gb=0.0,
            projected_gb=0.0,
            ceiling_gb=0.0,
            total_gb=0.0,
        )
    victims: list[EvictionVictimOut] = []
    for served in plan.victims:
        victim = local_catalog.get_by_served(served)
        if victim is None:
            continue  # a served name outside the catalog can't be sized/labelled — skip it
        window = windows.get(victim.id, victim.context_window)
        gb = local_catalog.footprint_gb(victim, window, disk_gb=_disk_gb(settings, victim.id))
        victims.append(EvictionVictimOut(id=victim.id, label=victim.label, gb=round(gb, 1)))
    return LoadPlanOut(
        model_id=model_id,
        measured=True,
        already_resident=plan.already_resident,
        fits=plan.fits,
        over=plan.over,
        over_box=plan.over_box,
        victims=victims,
        resident_gb=round(plan.resident_gb, 1),
        projected_gb=round(plan.projected_gb, 1),
        ceiling_gb=round(plan.ceiling_gb, 1),
        total_gb=round(plan.total_gb, 1),
    )


@router.post("/settings/llm/local-models/{model_id}/install")
async def queue_local_install(
    model_id: str,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Queue an un-provisioned catalog model for install — the next update one-shot
    downloads its weights, adds it to LOCAL_MODELS, and restarts the gateway. 409
    when hosting is off or the model is already provisioned; 404 for an unknown id.
    Pure settings write (no download here), so it can't fail on an unreachable
    gateway; the download is followed live via each model's download_gb."""
    _require_installable(settings, model_id)
    ctx = ctx_for(principal)
    requested = await store.llm_local_provision_requested(ctx)
    if model_id not in requested:
        requested.append(model_id)
        await store.set_llm_local_provision_requested(ctx, requested)
    # Disjoint-set guard: an id can't be queued for both install and uninstall, or
    # the sync's set algebra is ambiguous. Strip it from the remove queue here.
    removing = await store.llm_local_remove_requested(ctx)
    if model_id in removing:
        await store.set_llm_local_remove_requested(ctx, [r for r in removing if r != model_id])
    return await _snapshot(settings, store, ctx, gateway)


@router.delete("/settings/llm/local-models/{model_id}/install")
async def cancel_local_install(
    model_id: str,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Remove a model from the install queue. Tolerant of an id no longer in the
    queue (a concurrent update may have just provisioned and cleared it) — returns
    the current snapshot rather than 404 so the drawer always reconciles."""
    ctx = ctx_for(principal)
    requested = [r for r in await store.llm_local_provision_requested(ctx) if r != model_id]
    await store.set_llm_local_provision_requested(ctx, requested)
    return await _snapshot(settings, store, ctx, gateway)


@router.post("/settings/llm/local-models/{model_id}/uninstall")
async def queue_local_uninstall(
    model_id: str,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Queue a provisioned catalog model for uninstall — the next update one-shot
    drops it from LOCAL_MODELS (so it stops being served/enabled) and, behind hard
    guards, prunes its weights. 409 when hosting is off or the model isn't
    provisioned; 404 for an unknown id. Pure settings write (no disk/gateway action
    here), so it can't fail on an unreachable gateway; the removal lands on update."""
    _require_uninstallable(settings, model_id)
    ctx = ctx_for(principal)
    removing = await store.llm_local_remove_requested(ctx)
    if model_id not in removing:
        removing.append(model_id)
        await store.set_llm_local_remove_requested(ctx, removing)
    # Disjoint-set guard: an id can't be queued for both install and uninstall, or
    # the sync's set algebra is ambiguous. Strip it from the install queue here.
    requested = await store.llm_local_provision_requested(ctx)
    if model_id in requested:
        await store.set_llm_local_provision_requested(ctx, [r for r in requested if r != model_id])
    return await _snapshot(settings, store, ctx, gateway)


@router.delete("/settings/llm/local-models/{model_id}/uninstall")
async def cancel_local_uninstall(
    model_id: str,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Remove a model from the uninstall queue. Tolerant of an id no longer in the
    queue (a concurrent update may have just removed and cleared it) — returns the
    current snapshot rather than 404 so the drawer always reconciles."""
    ctx = ctx_for(principal)
    removing = [r for r in await store.llm_local_remove_requested(ctx) if r != model_id]
    await store.set_llm_local_remove_requested(ctx, removing)
    return await _snapshot(settings, store, ctx, gateway)


async def snapshot(
    settings: Settings,
    store: SqlSettingsStore,
    ctx: SessionContext,
    gateway: LocalGatewayClient,
) -> LlmSettingsOut:
    """The full LLM-settings snapshot under `ctx`. Public entry shared by the
    owner settings screen and the owner debug console (api/debug.py)."""
    return await _snapshot(settings, store, ctx, gateway)


async def apply_overrides(
    body: LlmSettingsPut,
    settings: Settings,
    store: SqlSettingsStore,
    ctx: SessionContext,
    gateway: LocalGatewayClient,
) -> LlmSettingsOut:
    """Validate and persist per-task routing overrides, then return the snapshot.
    Shared by the owner PUT and the debug console so both enforce the same rules
    (known task, known provider, vision-capable for vision tasks)."""
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
        if is_vision_task(task) and not picked.supports_vision:
            raise HTTPException(
                status_code=422,
                detail=f"{choice.provider} cannot serve vision task {task}",
            )
        entry: dict[str, str] = {"spec": picked.spec}
        # reasoning_effort is meaningful only for a reasoning-capable provider (Grok
        # or a local gpt-oss/GLM); drop it otherwise so the stored shape stays clean
        # and the router never misapplies it to a model with no thinking channel.
        if picked.supports_reasoning:
            entry["reasoning_effort"] = choice.reasoning_effort or REASONING_DEFAULT
        overrides[task] = entry
    await store.upsert(ctx, LLM_TASK_OVERRIDES_KEY, overrides)
    return await _snapshot(settings, store, ctx, gateway)


async def gateway_load(
    model_id: str, settings: Settings, gateway: LocalGatewayClient
) -> LoadedModelsOut:
    """Warm one provisioned model into the gateway. Shared by the owner screen and
    the debug console. 404/409 for unprovisioned/off; 502 if the gateway rejects."""
    model = _require_provisioned(settings, model_id)
    try:
        await gateway.load(model.served_model)
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway load failed: {exc}") from exc
    return LoadedModelsOut(loaded=sorted(await _loaded_ids(settings, gateway)), reachable=True)


async def gateway_unload(
    model_id: str, settings: Settings, gateway: LocalGatewayClient
) -> LoadedModelsOut:
    """Evict one provisioned model from the gateway. Shared by the owner screen and
    the debug console. 404/409 for unprovisioned/off; 502 if the gateway rejects."""
    model = _require_provisioned(settings, model_id)
    try:
        await gateway.unload(model.served_model)
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway unload failed: {exc}") from exc
    return LoadedModelsOut(loaded=sorted(await _loaded_ids(settings, gateway)), reachable=True)


@router.put("/settings/llm")
async def update_llm_settings(
    body: LlmSettingsPut,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    return await apply_overrides(body, settings, store, ctx_for(principal), gateway)
