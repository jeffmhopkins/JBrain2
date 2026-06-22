"""jerv's on-box fish-identification tool: `identify_fish` (docs/FISH_ID_PLAN.md).

A `web`-class, jerv-only, direct-exec tool — the `analyze_image` precedent: on-box
inference, no egress despite the class name (the photo is already an owner attachment
in the RLS-scoped session, so nothing leaves the box). The handler resolves exactly
one image source by id through the shared `ImageSourceResolver` (RLS-scoped), runs the
fishial model, then **frees it** — the model is load → use → unload per call, so it is
never resident between identifications (the `_free_comfyui_model` pattern). A service
failure becomes a clean tool-error string — never a stack trace to the model. The
result rides back as a data-only `fish_identification` view; the app builds the photo
`<img>` src from the id, so the model never authors a URL (invariant #9).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.image_source import ImageSourceResolver
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.fish_id.catalog import FishModel
from jbrain.fish_id.client import DEFAULT_TOP_K, FishIdentifier, FishIdError, FishResult
from jbrain.fish_id.gateway import FishIdGatewayError, FishIdMemory

if TYPE_CHECKING:
    from jbrain.agent.attachments import TurnAttachmentRepo
    from jbrain.models.images import GeneratedImageRepo
    from jbrain.storage import BlobStore

log = structlog.get_logger()

_IDENTIFY_FAILED = (
    "I couldn't identify that fish right now — the identification model didn't respond."
)
_NO_FISH = "I couldn't find a fish to identify in that image."


def _resolve_top_k(raw: object) -> int:
    """The candidate count for a request — the caller's value when a real int, else
    the default (the client clamps it to its accepted range)."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return DEFAULT_TOP_K


def _thumb(arguments: dict) -> tuple[str, str]:
    """Which source the owner named, as (id, kind) for the card's photo — the
    attachment path takes precedence (the resolver already enforced exactly one)."""
    attachment_id = str(arguments.get("source_attachment_id", "")).strip()
    if attachment_id:
        return attachment_id, "attachment"
    return str(arguments.get("source_image_id", "")).strip(), "image"


def _pct(score: float) -> int:
    return round(score * 100)


def _summary(result: FishResult) -> str:
    """The model-facing observation: the top match + confidence and the runners-up,
    so the model can report it (and hedge on a low/close call) in its own words."""
    top = result.matches[0]
    name = f"{top.species} ({top.common_name})" if top.common_name else top.species
    line = (
        f"Identified {name} at {_pct(top.score)}% confidence; the app is showing the owner a"
        " result card."
    )
    others = result.matches[1:]
    if others:
        line += " Also considered: " + ", ".join(f"{m.species} {_pct(m.score)}%" for m in others)
    return line


def fish_identification_view(
    result: FishResult, thumb_id: str, thumb_kind: str, model: FishModel
) -> ViewPayload:
    """The data-only twin of the tool's prose: a `fish_identification` view (the
    hero-verdict card, GUI gate #1). NO url — the component builds the photo `<img>`
    src from `thumb_id` (invariant #9). Confidence is a 0..1 float the component maps
    to a tone enum; the caption names the model arch + species coverage."""
    top = result.matches[0]
    return ViewPayload(
        view="fish_identification",
        surface="inline",
        data={
            "thumb_id": thumb_id,
            "thumb_kind": thumb_kind,
            "top": {"species": top.species, "common": top.common_name, "score": top.score},
            "others": [
                {"species": m.species, "common": m.common_name, "score": m.score}
                for m in result.matches[1:]
            ],
            "arch": model.arch,
            "species_count": model.species_count,
        },
    )


async def _free_fish_model(gateway: FishIdMemory) -> None:
    """Unload the model after the identification (load → use → unload). Best-effort:
    the result is already in hand, so a gateway hiccup is logged, never fatal."""
    try:
        await gateway.free()
    except FishIdGatewayError as exc:
        log.info("fish_id.free_skipped", error=str(exc))


def build_fish_handlers(
    identifier: FishIdentifier,
    gateway: FishIdMemory,
    blob_store: BlobStore,
    repo: GeneratedImageRepo,
    attachments: TurnAttachmentRepo,
    maker: async_sessionmaker[AsyncSession],
    model: FishModel,
) -> dict[str, ToolHandler]:
    """`identify_fish`. Wired only when the fishial service is configured (a localhost
    `fish-id`); the registry omits the sidecar otherwise (graceful degrade). `model` is
    the active catalog entry, for the card's arch/species-count caption.

    `maker` opens the RLS-scoped transaction the source read runs under; the firewall
    is Postgres', applied from `ctx.session` via the shared resolver."""

    resolver = ImageSourceResolver(repo, blob_store, attachments, maker)

    async def identify_fish_tool(arguments: dict, ctx: ToolContext) -> str:
        source = await resolver.source_bytes(arguments, ctx, tool="identify_fish")
        if isinstance(source, str):
            return source  # a clean one-source/miss error — no inference, no load
        image_bytes, _ = source
        top_k = _resolve_top_k(arguments.get("top_k"))
        try:
            result = await identifier.identify(image_bytes, top_k)
        except FishIdError:
            # The service URL/error is already logged in the client — never forward it
            # to the model (it can embed the loopback host:port); a fixed message stands.
            return _IDENTIFY_FAILED
        finally:
            # Load → use → unload: free the model whether or not it found a fish, so it
            # never stays resident between calls.
            await _free_fish_model(gateway)
        if result.top is None:
            return _NO_FISH
        thumb_id, thumb_kind = _thumb(arguments)
        return ToolOutput(
            _summary(result),
            view=fish_identification_view(result, thumb_id, thumb_kind, model),
        )

    return {"identify_fish": identify_fish_tool}
