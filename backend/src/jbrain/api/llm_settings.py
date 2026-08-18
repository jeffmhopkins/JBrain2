"""Runtime-editable per-task LLM routing + reasoning effort.

The settings screen reads the catalog of providers/efforts and every task's
EFFECTIVE choice, and writes per-task overrides into app.settings
(LLM_TASK_OVERRIDES_KEY). The router merges those over env/defaults on each call,
so this endpoint is the live control surface — no restart. Owner-only is
implicit pre-P7; the store's RLS enforces it regardless.
"""

import contextlib
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from jbrain.agent.agents import AGENTS
from jbrain.agent.priming import HiddenToolsProbe, jerv_prime_spec
from jbrain.agent.toolregistry import ToolRegistry
from jbrain.api.deps import PrincipalDep, SettingsDep
from jbrain.api.notes import ctx_for
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.host_metrics import read_memory_gb
from jbrain.llm import llama_swap_config, local_catalog, local_weights
from jbrain.llm.errors import LlmError
from jbrain.llm.local_gateway import (
    LocalGateway,
    LocalGatewayClient,
    LocalGatewayError,
    parse_spec_counters,
)
from jbrain.llm.providers import (
    REASONING_DEFAULT,
    REASONING_EFFORTS,
    id_for_spec,
    provider_choices,
    supports_reasoning,
)
from jbrain.llm.residency import ResidencyCoordinator, ResidencyError
from jbrain.llm.router import (
    _FOLLOW_PRIMARY_MODEL,
    _PRIMARY_MODEL_TASK,
    TASK_DEFAULTS,
    TASK_REASONING_BUCKET,
    _split_spec,
)
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

# Auto-generated titles are NOT independently routable: they run as a quick turn on the
# chat's own model (jbrain.agent.titler passes the turn's model; the router also has them
# follow agent.turn via _FOLLOW_PRIMARY_MODEL). Hiding them from the per-task picker avoids
# offering a control that does nothing — and stops a stale pick from swapping in a second
# model just to name a chat. They stay in TASK_DEFAULTS (the router still routes them).
_HIDDEN_TASKS: frozenset[str] = frozenset({"session.title", "research.title"})


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


def get_agent_registry(request: Request) -> ToolRegistry | None:
    """The tool registry, or None on a build without the agent wired — the source of
    jerv's tool schemas for a load's persona+tools prime (a persona-only warm otherwise)."""
    return cast("ToolRegistry | None", getattr(request.app.state, "agent_registry", None))


AgentRegistryDep = Annotated["ToolRegistry | None", Depends(get_agent_registry)]


def get_image_liveness(request: Request) -> HiddenToolsProbe | None:
    """ComfyUI liveness, or None. Hides the image-gen tools from the prime when the
    backend is down so the primed prefix matches a real turn's (which hides them too)."""
    return cast("HiddenToolsProbe | None", getattr(request.app.state, "image_liveness", None))


ImageLivenessDep = Annotated["HiddenToolsProbe | None", Depends(get_image_liveness)]

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
    # Estimated KV-cache size (GB) at the EFFECTIVE window (override or default) AND slot
    # count — the context portion of the model's memory-bar segment. An estimate, not a
    # measurement (see local_catalog.kv_gb_per_128k); a second slot doubles it, and so does
    # `--swa-full` on a sliding-window model. Always derived from local_catalog.footprint_gb
    # so this can never drift from the number the eviction budget uses.
    kv_gb: float
    # llama-server `-np` slot count: 1 (single slot, the default) or 2 (a dedicated
    # interactive keep-warm slot beside the background one, so the jerv prefix isn't evicted
    # by title/background traffic — docs/runbooks/STRIX_HALO_SETUP.md). Editable only while
    # the model isn't resident; a change doubles the model's KV footprint.
    parallel_slots: int
    # `--image-min-tokens`: the FLOOR an image is encoded to, and the knob for whether small
    # text in a photo survives to the model. None on a text-only entry (no projector, so a
    # floor would do nothing) and on a vision entry left at the catalog value.
    image_min_tokens: int | None
    # The catalog's own floor, so the drawer can mark it "(default)" and store null for it
    # rather than persisting a redundant override row.
    image_min_tokens_default: int | None


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


class FreeRamInfo(BaseModel):
    """The residency free-RAM floor for the settings card: the EFFECTIVE fraction kept
    free (the operator override when set, else the config default), the config default
    itself (so the card can offer a 'revert to default' and label it), and whether an
    override is in force. A change re-budgets every subsequent local model load."""

    # Effective fraction kept free (0.15 = 15% headroom): `override` when set, else `default`.
    fraction: float
    # The JBRAIN_LOCAL_LLM_FREE_RAM_FRACTION config default — the value when nothing is stored.
    default: float
    # The operator's stored override, or null when the effective value is the config default.
    override: float | None


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
    # The residency free-RAM floor (headroom the evictor keeps free) — the card's current
    # value + config default. Always present; the screen renders its card only when hosting
    # is on (it's meaningless without a box to budget).
    free_ram: FreeRamInfo
    # The end-of-turn RESTORE switch. True (the default) = after a displacement, the box puts
    # the displaced models back once the turn ends. False = it stops, and models come back
    # only when a turn actually needs one. The owner's "nothing loads unless I ask" control
    # while diagnosing the box, reachable from the PWA because the owner has no terminal.
    auto_restore: bool = True
    # Code mode's model selector (the dropdown card). Always present; `enabled`
    # gates whether the screen renders it.
    jcode: JcodeModelInfo
    # The client-side ceiling on a local call, in seconds (`JBRAIN_LOCAL_LLM_TIMEOUT`).
    # REPORTED, not settable: it is env-only, so an operator cannot change it without a host
    # step. Surfaced because it is otherwise invisible and it masquerades as a hung model — a
    # cold prefill at a large window can exceed it, and the turn then fails as a client timeout
    # with nothing saying the model was still working. An investigator has to be able to rule
    # that in or out before spending a day on the gateway.
    local_llm_timeout_s: float | None = None


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
    spec = entry.get("spec")
    if spec is None and task in _FOLLOW_PRIMARY_MODEL:
        # A title with no override of its own FOLLOWS the primary chat model (agent.turn) at the
        # router, so the screen shows THAT route, not the raw title default — otherwise it would
        # claim the title runs somewhere it doesn't. An explicit title override (handled above)
        # still wins. (Env pins aren't reflected here, the same simplification every task has.)
        agent_entry = overrides.get(_PRIMARY_MODEL_TASK) or {}
        spec = agent_entry.get("spec") or TASK_DEFAULTS[_PRIMARY_MODEL_TASK]
    spec = spec or TASK_DEFAULTS[task]
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
    slots = await store.llm_local_parallel_slots(ctx)
    image_floors = await store.llm_local_image_min_tokens(ctx)
    free_ram_override = await store.llm_local_free_ram_fraction(ctx)
    auto_restore = await store.llm_local_auto_restore(ctx)
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
        tasks=[
            _effective(settings, task, overrides)
            for task in TASK_DEFAULTS
            if task not in _HIDDEN_TASKS
        ],
        local_hosting_enabled=settings.local_llm_enabled,
        local_models=[
            _local_model_info(
                settings,
                m,
                m.id in loaded,
                windows,
                slots,
                image_floors,
                m.id in unavailable,
                m.id in requested,
                m.id in removing,
            )
            for m in local_catalog.CATALOG
        ],
        host_memory=_host_memory(settings),
        free_ram=FreeRamInfo(
            fraction=free_ram_override
            if free_ram_override is not None
            else settings.local_llm_free_ram_fraction,
            default=settings.local_llm_free_ram_fraction,
            override=free_ram_override,
        ),
        auto_restore=auto_restore,
        jcode=await _jcode_info(settings, store, ctx),
        local_llm_timeout_s=settings.local_llm_timeout,
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
    slots: dict[str, int],
    image_floors: dict[str, int],
    unavailable: bool,
    requested: bool,
    removing: bool,
) -> LocalModelInfo:
    enabled = settings.local_llm_enabled and m.id in settings.local_models
    # Effective-available: provisioned AND not toggled off by the operator.
    available = enabled and not unavailable
    override = windows.get(m.id)
    effective_window = override if override is not None else m.context_window
    # What the gateway will REALLY serve: a speculative model is pinned to one slot whatever
    # override is stored, so the drawer shows the served value rather than a saved one the
    # engine ignores (and sizes its KV bar off the same number).
    n_slots = m.effective_slots(slots.get(m.id, 1))
    # Derive from footprint_gb rather than re-deriving the KV formula here. The two drifted
    # the moment `kv_full_history` landed: footprint_gb doubled gpt-oss's KV for `--swa-full`
    # and this line did not, so the eviction budget was right while the meter the owner reads
    # under-reported that model by 9 GB — the exact dishonesty the doubling exists to prevent.
    # `disk_gb=0` strips the weights term, leaving the KV this field is meant to report.
    kv_gb = round(local_catalog.footprint_gb(m, effective_window, disk_gb=0.0, slots=n_slots), 2)
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
        parallel_slots=n_slots,
        # Only meaningful with a projector: a floor on a text-only entry would never be read,
        # so the drawer gets None and renders no control rather than a dead one.
        # The override wins; otherwise the catalog's own field. Both None on a text-only entry,
        # so the drawer renders no control rather than a dead one.
        image_min_tokens=image_floors.get(m.id) or m.image_min_tokens,
        image_min_tokens_default=m.image_min_tokens,
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


async def _saved_override_maps(
    store: SqlSettingsStore, ctx: SessionContext
) -> tuple[dict[str, int], dict[str, int], dict[str, list[str]], dict[str, int]]:
    """Every per-model override the served config depends on, loaded together.

    A helper rather than four ad-hoc reads because forgetting one is silent and costs a real
    investigation: a regenerate that omits an override kind re-stamps that flag away, the model
    reloads without it, and nothing reports the difference. Every one of these maps had at least
    one call site that dropped it — image floors were reset by the window, slot and flag setters
    AND by the boot reconcile; extra args were reset by the slot setter and the boot reconcile.
    Loading them as a set means a NEW override kind is added here once, not in five places."""
    return (
        await store.llm_local_context_windows(ctx),
        await store.llm_local_parallel_slots(ctx),
        await store.llm_local_extra_args(ctx),
        await store.llm_local_image_min_tokens(ctx),
    )


def _try_regenerate(
    settings: Settings,
    windows: dict[str, int],
    slots: dict[str, int],
    extra: dict[str, list[str]] | None = None,
    image_min_tokens: dict[str, int] | None = None,
) -> None:
    """Re-stamp llama-swap.yaml with the current per-model windows AND slot counts so the
    gateway (run with --watch-config) reloads at the configured `-c`/`-np`. Every model is a
    non-swapping group member regardless of staging (the app is the sole evictor), so this is
    driven only by window/slot edits. Best-effort: the settings are already persisted (so the
    meter is correct), and the weights dir may not be writable/complete in every deploy — a
    regen failure only delays the gateway catching up, it must never fail the edit."""
    try:
        manifest = [asdict(m) for m in local_catalog.selected(settings.local_models)]
        llama_swap_config.write(
            settings.local_models_dir,
            manifest,
            windows=windows,
            slots=slots,
            extra_args=extra,
            image_min_tokens=image_min_tokens,
        )
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


async def reconcile_gateway_config(
    models_dir: str,
    manifest: Sequence[Mapping[str, object]],
    *,
    windows: Mapping[str, int],
    slots: Mapping[str, int],
    gateway: LocalGateway,
    extra_args: Mapping[str, Sequence[str]] | None = None,
    image_min_tokens: Mapping[str, int] | None = None,
) -> bool:
    """Re-stamp llama-swap.yaml with the operator's SAVED per-model overrides — context window,
    `-np` slots, extra launch flags and the image floor — and
    ONLY if the served config actually changed, evict any resident local model so its next
    request reloads at the corrected `-c`. Returns True when it re-stamped, False on a no-op.

    Why this exists: the DEPLOY re-stamp (`deploy/local-models-sync.sh` step 5 →
    `python -m jbrain.llm.llama_swap_config`) regenerates the config from the BASE catalog manifest
    and passes NO overrides, so a model whose saved window exceeds its catalog default (Nemotron 3.5
    Lightning at 500k over a 32k base) is silently reset to that base on every deploy — and the
    agent's own system+tools prefix (~33k tokens) then overflows every turn ('ran out of context').
    The runtime settings path (`_try_regenerate`) DOES apply the overrides; this reconciles them
    back at boot so a deploy self-heals. Idempotent: when the on-disk config already matches the
    saved overrides it is a no-op and nothing is evicted, so a plain restart keeps its warm model.
    ALL FOUR override kinds have to come in here, not just the window. This rendered with
    windows+slots only, so `desired` could never match an on-disk config that carried an operator
    flag or a raised image floor: every boot re-stamped both of them AWAY and then evicted every
    resident model to "correct" a config that had just been made wrong. A launch-flag experiment
    therefore silently reverted on the next restart, which is worse than not having the knob —
    the flag reads as ineffective rather than absent, and the measurement taken after it is a lie.

    Best-effort — a render/glob miss or a down gateway is logged, never raised into boot."""
    try:
        desired = llama_swap_config.render(
            list(manifest),
            models_dir,
            windows=windows,
            slots=slots,
            extra_args=extra_args,
            image_min_tokens=image_min_tokens,
        )
    except Exception as exc:  # noqa: BLE001 — a missing weight/glob must never fail boot
        log.warning("llm_settings.gateway_reconcile_render_failed", error=str(exc))
        return False
    path = Path(models_dir) / "llama-swap.yaml"
    with contextlib.suppress(OSError):
        if path.read_text() == desired:
            return False  # already correct — the common case; leave any resident model warm
    llama_swap_config.write(
        models_dir,
        list(manifest),
        windows=windows,
        slots=slots,
        extra_args=extra_args,
        image_min_tokens=image_min_tokens,
    )
    log.info("llm_settings.gateway_config_reconciled")
    # The served `-c` changed under a possibly-resident gateway (an app restart with the gateway
    # still up, or a deploy race): evict resident local models so their next request reloads at the
    # corrected window (a running llama-server can't resize its KV cache live). On a fresh
    # post-deploy boot nothing is resident yet, so this loop is a no-op there.
    try:
        running = await gateway.running()
    except LocalGatewayError:
        return True
    served = {str(m["served_model"]) for m in manifest}
    for name in sorted(running & served):
        with contextlib.suppress(LocalGatewayError):
            await gateway.unload(name)
    return True


async def reconcile_gateway_windows_on_boot(
    settings: Settings,
    store: SqlSettingsStore,
    gateway: LocalGateway,
    ctx: SessionContext,
) -> bool:
    """Boot hook: load the saved window/slot overrides and reconcile the gateway config against
    them (see `reconcile_gateway_config`). Inert when local hosting is off. Best-effort — a settings
    read hiccup or missing weights simply skips the reconcile rather than blocking startup."""
    if not settings.local_llm_enabled:
        return False
    try:
        windows, slots, extra, floors = await _saved_override_maps(store, ctx)
        manifest = [asdict(m) for m in local_catalog.selected(settings.local_models)]
    except Exception as exc:  # noqa: BLE001 — never fail boot on a reconcile-setup hiccup
        log.warning("llm_settings.gateway_reconcile_load_failed", error=str(exc))
        return False
    return await reconcile_gateway_config(
        settings.local_models_dir,
        manifest,
        windows=windows,
        slots=slots,
        gateway=gateway,
        extra_args=extra,
        image_min_tokens=floors,
    )


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
    registry: AgentRegistryDep,
    liveness: ImageLivenessDep,
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
    return await gateway_load(model_id, settings, gateway, registry=registry, liveness=liveness)


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
    return await set_local_context_window_value(
        model_id, body.context_window, settings, store, ctx_for(principal), gateway
    )


async def set_local_context_window_value(
    model_id: str,
    window: int | None,
    settings: Settings,
    store: SqlSettingsStore,
    ctx: SessionContext,
    gateway: LocalGatewayClient,
) -> LlmSettingsOut:
    """The window edit itself, shared by the owner screen and the debug console — so the two
    surfaces cannot drift on validation, regeneration or eviction."""
    model = _require_provisioned(settings, model_id)
    ceiling = model.max_context_window
    if window is not None and not (1 <= window <= ceiling):
        raise HTTPException(status_code=422, detail=f"context window must be 1..{ceiling}")
    windows = await store.set_llm_local_context_window(ctx, model_id=model_id, window=window)
    _, slots, extra, floors = await _saved_override_maps(store, ctx)
    _try_regenerate(settings, windows, slots, extra, floors)
    await _unload_if_loaded(settings, gateway, model)
    return await _snapshot(settings, store, ctx, gateway)


class ImageMinTokensIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # null clears the override (back to the catalog floor); else 1..IMAGE_TOKENS_MAX.
    image_min_tokens: int | None = None


# llama.cpp's own ceiling for this projector family (`set_limit_image_tokens(8, 4096)`), so a
# floor above it could never be honoured and would only mislead whoever set it.
IMAGE_TOKENS_MAX = 4096


@router.put("/settings/llm/local-models/{model_id}/image-min-tokens")
async def set_local_image_min_tokens(
    model_id: str,
    body: ImageMinTokensIn,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Set (or clear, with null) one model's `--image-min-tokens` floor — how much of an image
    the model actually gets to see.

    This is the knob for small text: a curved bottle label or a receipt comes back garbled at
    a low floor and legible at a high one, and the right value is only findable against real
    photos. 409 when hosting is off, 404 for an unprovisioned id, 422 outside 1..4096 or on a
    model with no projector — a floor on a text-only model would silently do nothing.

    Costs prefill and KV, never weights, so unlike the context window it does not move the
    residency budget. Unloads the model if resident: the floor is a launch flag, so it takes
    effect on the next load."""
    model = _require_provisioned(settings, model_id)
    if not model.supports_vision:
        raise HTTPException(status_code=422, detail=f"{model_id} has no vision projector")
    tokens = body.image_min_tokens
    if tokens is not None and not (1 <= tokens <= IMAGE_TOKENS_MAX):
        raise HTTPException(status_code=422, detail=f"image floor must be 1..{IMAGE_TOKENS_MAX}")
    ctx = ctx_for(principal)
    floors = await store.set_llm_local_image_min_tokens(ctx, model_id=model_id, tokens=tokens)
    windows, slots, extra, _ = await _saved_override_maps(store, ctx)
    _try_regenerate(settings, windows, slots, extra, floors)
    await _unload_if_loaded(settings, gateway, model)
    return await _snapshot(settings, store, ctx, gateway)


class ParallelSlotsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 1 (or null) clears the override → a single slot; 2 opts into the dedicated interactive
    # keep-warm slot. Bounded 1..2 by the API — a third slot buys nothing here and only burns KV.
    slots: int | None = None


# One extra slot is the whole feature: a dedicated interactive slot beside the background one.
# More than two just multiplies KV with no benefit on a single-GPU box, so the API caps it.
PARALLEL_SLOTS_MAX = 2


@router.put("/settings/llm/local-models/{model_id}/parallel-slots")
async def set_local_parallel_slots(
    model_id: str,
    body: ParallelSlotsIn,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Set (2) or clear (1/null) one model's llama-server `-np` slot count — the operator's
    opt-in to a dedicated interactive keep-warm slot, so a primed jerv prefix isn't evicted by
    title/background traffic (docs/runbooks/STRIX_HALO_SETUP.md). 409 when hosting is off; 404
    for an unprovisioned id; 422 outside 1..2. A second slot roughly doubles the model's KV
    cost — persists the override (the meter reflects it at once), re-stamps the gateway config,
    and unloads the model if resident so its next request reloads with the new `-np`/`-c`."""
    model = _require_provisioned(settings, model_id)
    if body.slots is not None and not (1 <= body.slots <= PARALLEL_SLOTS_MAX):
        raise HTTPException(status_code=422, detail=f"slots must be 1..{PARALLEL_SLOTS_MAX}")
    ctx = ctx_for(principal)
    slots = await store.set_llm_local_parallel_slots(ctx, model_id=model_id, slots=body.slots)
    windows, _, extra, floors = await _saved_override_maps(store, ctx)
    _try_regenerate(settings, windows, slots, extra, floors)
    await _unload_if_loaded(settings, gateway, model)
    return await _snapshot(settings, store, ctx, gateway)


# The operator may set the floor between 5% and 50% free. Below 5% invites the
# kernel-reclaim hard-freeze the floor exists to prevent (STRIX_HALO_SETUP.md); above 50%
# leaves too little of the box usable to co-reside anything worthwhile. Outside this band the
# knob does more harm than good, so the API refuses it (the store would still sanitize (0,1),
# but the UI never offers out-of-band values and a hand-rolled request shouldn't foot-gun).
FREE_RAM_FRACTION_MIN = 0.05
FREE_RAM_FRACTION_MAX = 0.5


class FreeRamFractionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # null clears the override (revert to the config default); else 0.05..0.5.
    fraction: float | None = None


@router.put("/settings/llm/free-ram-fraction")
async def set_free_ram_fraction(
    body: FreeRamFractionIn,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Set (or clear, with null) the residency free-RAM floor — the fraction of RAM the
    evictor keeps free before every local model load. null reverts to the config default;
    else the fraction must be 0.05..0.5 (422 otherwise). Persisted and read live by the
    evictor in both the api and worker processes, so it takes effect on the next model load
    with no restart — no gateway restamp or unload is needed (the floor only sizes evictions,
    not any model's `-c`)."""
    if body.fraction is not None and not (
        FREE_RAM_FRACTION_MIN <= body.fraction <= FREE_RAM_FRACTION_MAX
    ):
        raise HTTPException(
            status_code=422,
            detail=f"fraction must be {FREE_RAM_FRACTION_MIN}..{FREE_RAM_FRACTION_MAX}",
        )
    ctx = ctx_for(principal)
    await store.set_llm_local_free_ram_fraction(ctx, body.fraction)
    return await _snapshot(settings, store, ctx, gateway)


class AutoRestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.put("/settings/llm/auto-restore")
async def set_auto_restore(
    body: AutoRestoreIn,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    """Turn the end-of-turn restore on or off.

    ON (the default) is the long-standing behaviour: when a displacement (an image render, a
    code session, a big one-off) evicts models, the box puts them back once the turn ends, so
    it drifts to a steady state instead of cold-loading on the next turn. OFF stops that, and
    a model comes back only when a turn actually needs it.

    This is a SURPRISE control, not a safety one: every load — restore included — goes through
    the device-memory guard (jbrain.llm.gpu_guard) regardless. What it buys is a box that does
    nothing on its own while the owner is diagnosing it. Read live by the evictor in the api
    process, so it applies to the next turn with no restart."""
    ctx = ctx_for(principal)
    await store.set_llm_local_auto_restore(ctx, body.enabled)
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
        # The auto-title tasks are hidden from the picker and follow the chat model (the
        # router ignores their own overrides); reject a direct write so a pin can't be
        # created that the router would only ignore anyway.
        if task in _HIDDEN_TASKS:
            raise HTTPException(
                status_code=422, detail=f"task is not independently routable: {task}"
            )
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
    model_id: str,
    settings: Settings,
    gateway: LocalGatewayClient,
    *,
    registry: ToolRegistry | None = None,
    liveness: HiddenToolsProbe | None = None,
) -> LoadedModelsOut:
    """Warm one provisioned model into the gateway. Shared by the owner screen and
    the debug console. 404/409 for unprovisioned/off; 502 if the gateway rejects.

    The warm-up primes the interactive chat persona (jerv) — its system prompt AND its
    tool schemas — into the gateway KV cache so the operator's first conversation turn
    reuses that prefix instead of paying the cold persona+tools prefill (the 60-90s
    first-response latency owners hit right after Load,
    docs/archive/LLM_PROMPT_CACHE_PLAN.md). The tools MUST be primed too: under the
    gateway's `--jinja` the template renders them into the prompt's leading tokens, so a
    persona-only warm diverges from a real (tool-carrying) turn before the reusable prefix
    ends and the reuse misses. `registry` supplies those schemas (via `jerv_prime_spec`);
    without it — a build with no agent wired — the warm falls back to persona-only."""
    model = _require_provisioned(settings, model_id)
    warm_system: str | None = AGENTS["jerv"].prompt
    warm_tools: list[dict[str, object]] | None = None
    if registry is not None:
        # Pass the model being loaded so the model-gated canvas pair is resolved exactly
        # here — a prefix primed with a different tool block than the turn will send is
        # worse than no prime, since the reuse misses from the tools block onward.
        warm_system, warm_tools = await jerv_prime_spec(registry, liveness, model.served_model)
    try:
        await gateway.load(model.served_model, warm_system=warm_system, warm_tools=warm_tools)
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


# The llama-server flags an operator may set remotely. An ALLOWLIST, not a filter: llama-server
# REFUSES TO START on an unknown flag, and the flag lands in that model's launch command, so an
# unrestricted argv would let one API call make a model permanently unloadable — on a box with no
# terminal to fix it from. Each entry is a flag we have a reason to want to try live:
#   --swa-full        keep full history on sliding-window layers (a precondition for KV-slot
#                     restore doing anything on gpt-oss; roughly doubles that model's KV)
#   --slot-save-path  where llama-server reads/writes KV-slot state files
#   -b / -ub          logical / physical prompt batch — the prompt-processing throughput knobs
#   --spec-type            which speculative-decoding mode to serve with (e.g. draft-mtp)
#   --spec-draft-n-max     how many tokens a draft proposes per round
#   --spec-draft-n-min     the floor below which a draft is discarded
#   --spec-draft-p-min     confidence gate that stops a draft early; llama.cpp's own default is
#                          0.00 (ungated), so the useful value is one nobody can guess in advance
# The speculative four are here because their right values are EMPIRICAL and hardware-specific:
# published Strix Halo numbers disagree on n-max, and p-min's payoff depends on generation
# length. Without them a single tuning iteration costs a catalog edit, a release and an
# Ops → Update — which is how a knob ends up never being tuned at all. Turning speculation on
# also pins the model to one slot, and the config generator derives that from the flags it is
# about to write (`llama_swap_config._is_speculative`), so an operator flag gets the same clamp
# a catalog flag does. A bad VALUE here can stop a model loading, same as a bad `-ub`; clearing
# is the same call with no args and does not require the model to be loadable.
#   --image-min-tokens     FLOOR on how many tokens an image is encoded to
#   --image-max-tokens     CEILING on the same (llama.cpp defaults to 4096 for this projector
#                          family and the catalog pins only the floor, at 2048)
# The image pair is here for the same reason as the speculative four: the right value is
# empirical and only observable against real images. The floor is what decides whether small
# text in a photo survives to the model — raise it and OCR on a curved bottle label or a
# receipt gets legible, at the cost of prefill and KV — and no amount of reading can say where
# that threshold sits for a given camera and subject. Pinning the ceiling bounds the CLIP
# workspace, which matters much less now that flash attention is confirmed on (the term is
# linear in patches, not quadratic), but it remains the lever if a build ever loses `-fa`.
#   --ctx-checkpoints  how many per-slot context checkpoints llama-server keeps
#   --cache-reuse      minimum chunk size worth salvaging from a matching prompt prefix
# The cache pair is here because the SLOW-PREFILL investigation cannot start without it, and on
# a HYBRID model our shipped values are the prime suspects. Qwen3.8 runs 48 of its 65 layers as
# Gated DeltaNet, which carries a recurrent state: that state cannot be KV-shifted or partially
# rewound, so `--cache-reuse` can only ever salvage the 16 attention layers, and checkpoints are
# the ONLY mechanism that lets such a model resume mid-sequence at all. We serve
# `--ctx-checkpoints 2` (down from llama.cpp's 32, to save ~4.7 GiB/slot), which is close to
# "no restore points" — plausibly why every turn re-prefills. Whether that trade is right is an
# empirical question about THIS box, and without these two flags answering it costs a catalog
# edit, a release and an Ops → Update, i.e. it never gets answered.
#
# `--ctx-checkpoints` carries a WORSE failure mode than the rest of this list, so raise it in
# small steps. A checkpoint on a hybrid is a full copy of the recurrent state (~150 MiB for
# Qwen3.8) and is device-resident, so a large value costs GB per slot — and `footprint_gb` does
# NOT model checkpoint memory, so the residency budget will not see it coming. On a box that has
# hard-locked under memory pressure the risk is not "the model fails to load" (the recoverable
# failure the flags above assume) but the host going down. Clearing is still the same call with
# no args, which does not require the model to be loadable.
#   -ngl               how many layers are offloaded to the iGPU
#   -fa                flash attention on/off/auto
#   --reasoning-format how llama.cpp splits a thinking trace out of `content`
# The first two are the "is it the GPU?" bisect. When a model emits garbage or dies on this
# gfx1151 — the exact failure class behind our `-ub 1024` (llama.cpp #27237) — the first move is
# "does it still happen with fewer layers offloaded, or with flash attention off?", and that move
# was unavailable. Neither can make a model unloadable: a wrong value costs speed or a CPU
# fallback. `--reasoning-format` is the remedy for the OTHER common breakage after a llama.cpp
# rebuild on master — `<think>` tags leaking into `content`, or an empty reasoning channel —
# which is a one-string fix (`deepseek` vs `auto`) that otherwise costs a release.
#
# `--no-mmap` is deliberately NOT here and cannot be: llama.cpp has no positive `--mmap`
# counterpart, so an allowlist entry could not undo the flag we already pass. An entry would be a
# silent no-op, which is worse than an absent one. Same for `--jinja`, which is unconditional and
# would need a `--chat-template-file` (a file on the box) to be worth overriding.
# Flags taking a value are allowed to carry one; the value itself is NOT interpreted here.
EXTRA_ARG_FLAGS: frozenset[str] = frozenset(
    {
        "--swa-full",
        "--slot-save-path",
        "-b",
        "-ub",
        "--spec-type",
        "--spec-draft-n-max",
        "--spec-draft-n-min",
        "--spec-draft-p-min",
        "--image-min-tokens",
        "--image-max-tokens",
        "--ctx-checkpoints",
        "--cache-reuse",
        "-ngl",
        "-fa",
        "--reasoning-format",
    }
)


# The one flag on the list whose bad value is not self-limiting. Everything else fails by not
# loading — recoverable, because clearing does not need the model to be loadable. A checkpoint on
# a hybrid is a full copy of the recurrent state (~150 MiB for Qwen3.8), device-resident and per
# slot, and `footprint_gb` does not model it, so the residency evictor cannot see it coming. At
# llama.cpp's own default of 32 that is ~4.7 GiB/slot of unbudgeted device memory on a box whose
# documented failure mode is an unrecoverable host hang under GTT pressure — and `32` is the most
# likely typo, being the value every llama.cpp doc names. 8 is above any value this investigation
# needs and well under the cliff.
_EXTRA_ARG_BOUNDS: dict[str, tuple[int, int]] = {"--ctx-checkpoints": (0, 8)}


def _validate_extra_args(args: list[str]) -> list[str]:
    """Reject anything not on EXTRA_ARG_FLAGS. Values are accepted positionally (a token
    following a flag that takes one), so `--slot-save-path /tmp/kv/` passes while a bare
    `/tmp/kv/` or an unknown `--foo` is refused. 422 rather than a silent drop — a caller that
    thinks it set a flag and did not would misread every measurement that follows.

    Values are otherwise NOT interpreted — a bad one costs a model that will not load, which is
    recoverable from the console. `_EXTRA_ARG_BOUNDS` is the exception, for the flag whose bad
    value takes the host down instead."""
    cleaned: list[str] = []
    expect_value = False
    flag = ""
    for raw in args:
        token = raw.strip()
        if not token:
            continue
        if expect_value and not token.startswith("-"):
            bounds = _EXTRA_ARG_BOUNDS.get(flag)
            if bounds is not None:
                low, high = bounds
                try:
                    value = int(token)
                except ValueError:
                    raise HTTPException(
                        status_code=422, detail=f"{flag} takes an integer, got {token!r}"
                    ) from None
                if not low <= value <= high:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"{flag} must be {low}..{high} — a larger value costs device memory "
                            "the residency budget does not model, and can hang the box"
                        ),
                    )
            cleaned.append(token)
            expect_value = False
            continue
        if token not in EXTRA_ARG_FLAGS:
            raise HTTPException(
                status_code=422,
                detail=f"flag {token!r} is not settable; allowed: {sorted(EXTRA_ARG_FLAGS)}",
            )
        cleaned.append(token)
        flag = token
        expect_value = token != "--swa-full"  # the only boolean flag on the list
    return cleaned


async def set_local_extra_args(
    model_id: str,
    args: list[str],
    settings: Settings,
    store: SqlSettingsStore,
    ctx: SessionContext,
    gateway: LocalGatewayClient,
) -> LlmSettingsOut:
    """Set (or clear, with an empty list) one model's extra llama-server flags, re-stamp the
    gateway config, and unload the model so its next request relaunches with them.

    Same shape as the context-window and slot setters — persist, regenerate, evict — because a
    running llama-server cannot change its launch flags any more than it can resize its KV."""
    model = _require_provisioned(settings, model_id)
    validated = _validate_extra_args(args)
    extra = await store.set_llm_local_extra_args(ctx, model_id=model_id, args=validated)
    windows, slots, _, floors = await _saved_override_maps(store, ctx)
    _try_regenerate(settings, windows, slots, extra, floors)
    await _unload_if_loaded(settings, gateway, model)
    return await _snapshot(settings, store, ctx, gateway)


async def gateway_props(
    model_id: str, settings: Settings, gateway: LocalGatewayClient
) -> dict[str, object]:
    """llama-server's `/props` for one model — build identity, real `n_ctx`, slot count."""
    model = _require_provisioned(settings, model_id)
    try:
        return await gateway.props(model.served_model)
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway props failed: {exc}") from exc


async def gateway_slots(
    model_id: str, settings: Settings, gateway: LocalGatewayClient
) -> dict[str, object]:
    """llama-server's `/slots` for one model — the only reliable answer to "is speculation
    actually drafting?", since `/props`'s `speculative.types` reads "none" on every build."""
    model = _require_provisioned(settings, model_id)
    try:
        return {"slots": await gateway.slots(model.served_model)}
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway slots failed: {exc}") from exc


async def gateway_metrics(
    model_id: str, settings: Settings, gateway: LocalGatewayClient
) -> dict[str, object]:
    """llama-server's Prometheus `/metrics` for one model, plus the speculative counters
    pulled out of it.

    `spec` is the point: drafted vs accepted tokens, and the derived accept rate. That is the
    measure of whether MTP is earning its keep and whether `--spec-draft-n-max` sits at the
    right depth — which until now could only be inferred from wall-clock timings. `raw` is
    kept so a counter this parser doesn't know about is still reachable."""
    model = _require_provisioned(settings, model_id)
    try:
        text = await gateway.metrics(model.served_model)
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway metrics failed: {exc}") from exc
    return {"spec": parse_spec_counters(text), "raw": text}


async def gateway_slot_action(
    model_id: str,
    slot_id: int,
    action: str,
    filename: str | None,
    settings: Settings,
    gateway: LocalGatewayClient,
) -> dict[str, object]:
    """Save / restore / erase one KV slot. 422 on an unknown action so a typo can't read as a
    no-op success; 502 when llama-server rejects (including the 501 it returns when the server
    was started without `--slot-save-path`)."""
    if action not in ("save", "restore", "erase"):
        raise HTTPException(status_code=422, detail="action must be save, restore or erase")
    model = _require_provisioned(settings, model_id)
    try:
        return await gateway.slot_action(model.served_model, slot_id, action, filename=filename)
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway slot {action} failed: {exc}") from exc


async def gateway_prime(
    model_id: str,
    settings: Settings,
    gateway: LocalGatewayClient,
    *,
    registry: ToolRegistry | None = None,
    liveness: HiddenToolsProbe | None = None,
) -> dict[str, object]:
    """Prime one model with the real jerv prefix and TIME it — the measurement instrument for
    prefill experiments. `elapsed_ms` is the number that matters: a cold prefill and a
    post-restore prefill differ by ~50x, which is the only reliable way to tell whether a KV
    restore actually took effect (a restore returns 200 either way)."""
    model = _require_provisioned(settings, model_id)
    warm_system: str | None = AGENTS["jerv"].prompt
    warm_tools: list[dict[str, object]] | None = None
    if registry is not None:
        warm_system, warm_tools = await jerv_prime_spec(registry, liveness, model.served_model)
    started = time.monotonic()
    try:
        await gateway.load(model.served_model, warm_system=warm_system, warm_tools=warm_tools)
    except LocalGatewayError as exc:
        raise HTTPException(status_code=502, detail=f"gateway prime failed: {exc}") from exc
    return {
        "model": model.served_model,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "tool_count": len(warm_tools or []),
        "system_chars": len(warm_system or ""),
    }


@router.put("/settings/llm")
async def update_llm_settings(
    body: LlmSettingsPut,
    principal: PrincipalDep,
    settings: SettingsDep,
    store: SettingsStoreDep,
    gateway: LocalGatewayDep,
) -> LlmSettingsOut:
    return await apply_overrides(body, settings, store, ctx_for(principal), gateway)
