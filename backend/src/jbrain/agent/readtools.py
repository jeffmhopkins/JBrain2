"""The first read-only agent tools: `search` and `read_note`, thin handlers over
the existing search and notes services.

Each handler runs under the session's RLS scope (`ToolContext.session`), so a
narrowed session only ever sees in-scope data — the firewall is the services',
not the handler's. `build_registry` binds these handlers to their `.tool`
sidecars (docs/archive/ASSISTANT_PLAN.md P4.4c).
"""

from collections.abc import Awaitable, Callable, Collection
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import structlog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from jbrain.citygeocode import CityGeocoder
    from jbrain.embed import EmbedClient
    from jbrain.geocode import NominatimReverseClient
    from jbrain.llm.router import LlmRouter
    from jbrain.notify import NotifyBus
    from jbrain.push.repo import FcmTokenRepo
    from jbrain.push.sender import PushNotifier
    from jbrain.settings_store import SqlSettingsStore
    from jbrain.web.federal_register import FederalRegisterClient
    from jbrain.web.feeds import FeedClient
    from jbrain.web.fetch import WebFetcher
    from jbrain.web.nppes import NppesClient
    from jbrain.web.public_records import CourtListenerClient
    from jbrain.web.search import SearxngClient
    from jbrain.web.wikidata import WikidataClient

from jbrain.agent.appointmenttools import (
    build_appointment_handlers,
    build_appointment_write_handlers,
)
from jbrain.agent.archivisttools import build_archivist_memory_handlers
from jbrain.agent.bartools import build_bar_handlers
from jbrain.agent.charttools import build_chart_handlers
from jbrain.agent.clock import build_clock_handlers
from jbrain.agent.connectortools import build_connector_handlers
from jbrain.agent.contracts import EntityRef, NoteSource
from jbrain.agent.deep_research import DeepProduceRef, DeepResearchRef, DeepResearchService
from jbrain.agent.deepest_lane import DeepestRunLane
from jbrain.agent.deepest_progress import DeepestProgressChannel
from jbrain.agent.deepest_tool import (
    DeepestHandle,
    DeepestKickoffService,
    DeepestResearchRef,
    _owner_principal_id,
)
from jbrain.agent.geocodetools import build_geocode_handlers
from jbrain.agent.jmoltjournaltools import build_jmolt_journal_handlers
from jbrain.agent.jmoltobservetools import build_jmolt_observe_handlers
from jbrain.agent.jmoltscratchtools import build_jmolt_scratch_handlers
from jbrain.agent.jmolttimetools import build_jmolt_time_handlers
from jbrain.agent.labtools import build_lab_handlers
from jbrain.agent.listtools import build_list_handlers
from jbrain.agent.locationtools import build_location_handlers
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.agent.memory import MemoryService
from jbrain.agent.memorytools import build_memory_handlers
from jbrain.agent.mergetools import build_merge_handlers
from jbrain.agent.metricstools import build_metrics_handlers
from jbrain.agent.plantools import build_plan_handlers
from jbrain.agent.presencetools import build_presence_handlers
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.proposaltools import build_intake_link_handlers, build_proposal_handlers
from jbrain.agent.runlog import AgentRunLog
from jbrain.agent.session import AgentSessionRepo
from jbrain.agent.sessiontools import build_session_handlers
from jbrain.agent.spawn import DecomposeRef, SpawnRef, SpawnService
from jbrain.agent.toolregistry import ToolRegistry, load_registry
from jbrain.agent.transcript_store import AgentTranscript
from jbrain.analysis.neighborhood import (
    DEFAULT_DEPTH,
    DEFAULT_TOTAL_CAP,
    MAX_DEPTH,
    EdgeKinds,
)
from jbrain.analysis.relationships import predicate_candidates
from jbrain.appointments.service import AppointmentsRepo
from jbrain.connectors.base import ConnectorRegistry
from jbrain.db.session import SessionContext
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.lists.service import ListsRepo
from jbrain.locations import SqlLocationRepo
from jbrain.notes.service import NoteInfo, NotesRepo
from jbrain.sdr.aprslog import RECENT_DEFAULT, AprsReader
from jbrain.search.service import (
    SearchResponse,
    SearchResult,
    SearchService,
    WikiSearchResult,
)

log = structlog.get_logger()

TOOLS_DIR = Path(__file__).parent / "tools"
_DEFAULT_LIMIT = 8

# jerv's read-only `analyze_image` vision sidecar (image GENERATION is not an agent
# tool — the owner drives ComfyUI through the Images launcher, api/images_render.py).
# Optional: dropped from the registry when no ComfyUI/vision wiring is configured (no
# handlers passed), so an unconfigured box silently lacks the feature.
OPTIONAL_IMAGE_TOOLS = frozenset({"analyze_image"})
# jerv's on-box audio transcription sidecar, dropped from the registry when the
# whisper gateway is unconfigured (graceful degrade, like the image tools).
OPTIONAL_TRANSCRIBE_TOOL = frozenset({"transcribe"})
# jerv's radio tools, dropped from the registry on a box with no SDR — most boxes.
# sdr_listen is load-bearing for the GUI as well as the chat: the composer's radio
# icon exists only while a session holds the tuner, so dropping the tool on a
# radio-less box also correctly means that surface can never appear.
OPTIONAL_SDR_TOOLS = frozenset({"sdr_listen", "sdr_stop", "aprs_recent"})
# jerv's on-box video analysis sidecar, dropped from the registry when ffmpeg is
# absent (graceful degrade, like the image/whisper tools).
OPTIONAL_VIDEO_TOOL = frozenset({"analyze_video"})
# jerv's single-frame grab (VIDEO_IMAGE_TOOLS_PLAN.md), dropped from the registry when
# ffmpeg is absent (the URL path also needs yt-dlp, which degrades cleanly at call time).
OPTIONAL_GRAB_TOOL = frozenset({"grab_frame"})
# jerv's web-image fetch (VIDEO_IMAGE_TOOLS_PLAN.md), dropped when the web fetcher is
# unconfigured — it gives jerv eyes on a web image (web_fetch is text-only).
OPTIONAL_FETCH_IMAGE_TOOL = frozenset({"fetch_image"})
# jerv's multi-image compare (VIDEO_IMAGE_TOOLS_PLAN.md), dropped when no vision router
# is configured. Router-gated, NOT ComfyUI-gated — a vision read needs no image-gen.
OPTIONAL_COMPARE_TOOL = frozenset({"compare_images"})
# The standalone HTML render (AGENT_CANVAS_PLAN §3b): dropped when no htmlrender sidecar
# is configured, since without it the tool has nothing to rasterize with. Deliberately NOT
# in the model-gated set below — the canvas gate exists for GROUNDING coordinates, and a
# render with no photograph under it has no coordinates to place wrong, so any model that
# can write HTML may call this one.
OPTIONAL_HTML_TOOL = frozenset({"render_html"})
# The canvas pair (AGENT_CANVAS_PLAN.md): optional because the handlers are only wired
# when the image/attachment stores exist, and because a box with no htmlrender sidecar
# still gets the shape ops — the `html` op degrades with a note rather than vanishing.
OPTIONAL_CANVAS_TOOLS = frozenset({"canvas", "show_canvas"})
# The crop lane (AGENT_CANVAS_PLAN W4). Model-gated with the canvas pair: it grounds
# regions with the vision model, so an unmeasured coordinate base would cut confidently
# wrong crops — the same silent failure the canvas gate exists to prevent.
OPTIONAL_CROP_TOOLS = frozenset({"crop_regions"})
# The SERVED models qualified to hold the canvas (AGENT_CANVAS_PLAN §7, owner decision
# §10.3). Deliberately an allowlist rather than a bare `supports_vision` check: a
# vision-capable model with a DIFFERENT grounding coordinate base would not fail loudly,
# it would silently place every box wrong. Each entry earns its place by passing the
# `POST /api/debug/grounding` probe, and `agent/grounding.py` refuses anything not here.
# The two Qwen3.8 twins share weights, repo and projector, so one probe qualifies both.
CANVAS_MODELS = frozenset({"qwen3.8-27b", "qwen3.8-27b-q4"})


def canvas_hidden_for_model(
    served_model: str | None, profile_tools: frozenset[str]
) -> frozenset[str]:
    """The canvas names to withhold for a known served model. Pure — no router.

    Two failure modes this prevents, both silent. A text-only pick (e.g. the MTP twin,
    which cannot run beside the vision projector) would leave the model drawing blind:
    it could neither aim at a photo nor check what it drew, and `api.agent` drops image
    bytes for it anyway. An unmeasured VISION model is worse — it answers confidently in
    whatever coordinate base it was trained on, so every box lands wrong with no error.
    An unknown model therefore hides the pair rather than betting on it."""
    gated = OPTIONAL_CANVAS_TOOLS | OPTIONAL_CROP_TOOLS
    if not (profile_tools & gated):
        return frozenset()
    return frozenset() if served_model in CANVAS_MODELS else gated


async def canvas_hidden_tools(
    router: "LlmRouter | None", model_override: str | None, profile_tools: frozenset[str]
) -> frozenset[str]:
    """`canvas_hidden_for_model` for a turn whose model still has to be resolved."""
    gated = OPTIONAL_CANVAS_TOOLS | OPTIONAL_CROP_TOOLS
    if not (profile_tools & gated):
        return frozenset()
    if router is None:
        return gated
    try:
        _provider, model = await router.effective_spec("agent.turn", spec_override=model_override)
    except Exception:  # noqa: BLE001 — a routing probe failure must not cost the turn
        log.warning("canvas.model_probe_failed", exc_info=True)
        return gated
    return canvas_hidden_for_model(model, profile_tools)


def compose_hidden_tools(
    extra: frozenset[str],
) -> "Callable[[], Awaitable[Collection[str]]] | None":
    """One per-turn hidden-tools provider from a static hidden set (the model-gated
    canvas pair today).

    The loop computes the tool array ONCE per turn, before the step loop, so everything
    that hides a tool has to be folded in here rather than armed later. Returns None when
    nothing is hidden, which keeps the common path allocation-free and byte-identical."""
    return (lambda: _ready(extra)) if extra else None


async def _ready(names: frozenset[str]) -> Collection[str]:
    return names


# jerv's deterministic OCR read (docs/plans/RAPIDOCR_PLAN.md), present only when the
# RapidOCR sidecar is configured; otherwise the `ocr` sidecar is dropped.
OPTIONAL_OCR_TOOL = frozenset({"ocr"})
# jerv's URL-sourced stream/video analysis sidecar, dropped from the registry when
# ffmpeg OR yt-dlp is absent (docs/archive/STREAM_ANALYSIS_PLAN.md).
OPTIONAL_STREAM_TOOL = frozenset({"analyze_stream"})
# jerv's cross-turn re-read of a fetched page (docs/plans/CROSS_TURN_TOOL_RESULTS_PLAN.md),
# present only when the artifact store + blob store are wired into build_web_handlers;
# otherwise its sidecar has no handler and is dropped.
OPTIONAL_READ_ARTIFACT_TOOL = frozenset({"read_artifact"})
# The archivist persona's Gmail sidecars (`web`-class, opt-in), dropped from the
# registry when Gmail is unconfigured — no refresh token, so no handlers are passed
# (graceful degrade, docs/archive/EMAIL_ARCHIVIST_PLAN.md).
OPTIONAL_GMAIL_TOOLS = frozenset(
    {
        "gmail_search",
        "gmail_read",
        "gmail_list_labels",
        "gmail_create_label",
        "gmail_label",
        "gmail_archive",
        "gmail_count",
        "gmail_sender_breakdown",
        "gmail_bulk_label",
    }
)

# The jmolt persona's Moltbook read umbrella (`web`-class, opt-in). Always wired in
# main.py (the client refuses at call time when unregistered), but marked optional so a
# build_registry() call without moltbook_handlers (e.g. a unit test) drops the sidecar
# rather than failing the sidecar↔handler pairing (docs/plans/JMOLT_PLAN.md).
OPTIONAL_MOLTBOOK_TOOLS = frozenset({"moltbook"})
# jmolt's Moltbook WRITE tools (`web`-class, jmolt-only). Built in main.py over the outbox
# + settings store; optional so a build_registry() call without them drops the sidecars.
OPTIONAL_MOLTBOOK_WRITE_TOOLS = frozenset(
    {
        "moltbook_post",
        "moltbook_comment",
        "moltbook_vote",
        "moltbook_social",
        "moltbook_profile_update",
    }
)


class EntityReader(Protocol):
    """The slice of the analysis repo the read/entity tools need — the entity-page
    view behind read_entity, the name/alias search behind find_entity, the
    relationship traversal behind relate, the n-hop vicinity walk behind
    neighborhood, and the note-currency overlay that tells the retrieval tools
    which of a note's facts are no longer live."""

    async def entity_view(self, ctx: SessionContext, entity_id: str) -> dict[str, Any] | None: ...

    async def owner_entity_id(self, ctx: SessionContext) -> str | None: ...

    async def list_entities(
        self,
        ctx: SessionContext,
        q: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...

    async def relate(
        self,
        ctx: SessionContext,
        anchor_id: str | None,
        predicates: Any,
        limit: int = 8,
    ) -> list[dict[str, Any]]: ...

    async def neighborhood(
        self,
        ctx: SessionContext,
        entity_id: str,
        *,
        depth: int = DEFAULT_DEPTH,
        kinds: EdgeKinds = "both",
        total_cap: int = DEFAULT_TOTAL_CAP,
    ) -> dict[str, Any] | None: ...

    async def note_currency(
        self, ctx: SessionContext, note_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]: ...

    async def analyte_currency(
        self, ctx: SessionContext, chunk_ids: list[str]
    ) -> dict[str, list[dict[str, Any]]]: ...


def _search_flag(stale: list[dict[str, Any]]) -> str:
    """A compact currency flag under a hit whose note has non-live facts — so the
    agent knows BEFORE acting on the snippet, and where the current value lives."""
    statuses = sorted({f["status"].replace("_review", "") for f in stale})
    ids = sorted({f["entity_id"] for f in stale})
    return (
        f"  ⚠ {len(stale)} fact(s) here are no longer current ({', '.join(statuses)})"
        f" — read_entity {', '.join(ids)} for current values"
    )


def _analyte_flag(stale: list[dict[str, Any]]) -> str:
    """A currency flag under a health hit whose prose quotes an analyte value that is
    no longer the current reading — the value was superseded by a correction, or it is
    a contested/preliminary result held for review (EMR plan §7.2). Names the analyte
    so the agent confirms the live number via read_labs before relying on the snippet."""
    statuses = sorted({f["status"].replace("_review", "") for f in stale})
    analytes = sorted({f["analyte"] for f in stale})
    ids = sorted({f["entity_id"] for f in stale})
    return (
        f"  ⚠ a value here is no longer the current reading ({', '.join(statuses)}):"
        f" {', '.join(analytes)} — read_labs (or read_entity {', '.join(ids)}) for the"
        " current value"
    )


def format_search(
    resp: SearchResponse,
    currency: dict[str, list[dict[str, Any]]] | None = None,
    analyte: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    if not resp.results:
        return "No matching notes in scope."
    lines = ["(keyword-only search — semantic ranking unavailable)"] if resp.degraded else []
    for r in resp.results:
        if isinstance(r, WikiSearchResult):
            # A wiki article is the answer layer: surface it as a read_wiki target above notes.
            lines.append(f'- wiki "{r.title}" [{r.domain}]: {r.snippet.strip()}')
            continue
        line = f"- note {r.note_id} [{r.domain}] {r.created_at:%Y-%m-%d}: {r.snippet.strip()}"
        stale = (currency or {}).get(r.note_id)
        if stale:
            line += "\n" + _search_flag(stale)
        # The chunk-precise value flag: this hit's snippet quotes a stale analyte reading.
        stale_value = (analyte or {}).get(r.chunk_id)
        if stale_value:
            line += "\n" + _analyte_flag(stale_value)
        lines.append(line)
    return "\n".join(lines)


def _currency_address(f: dict[str, Any]) -> str:
    qualifier = f.get("qualifier")
    return f"{f['entity_name']}.{f['predicate']}" + (f".{qualifier}" if qualifier else "")


def _currency_line(f: dict[str, Any]) -> str:
    pointer = f" → read_entity {f['entity_id']} for the current value."
    if f["status"] == "superseded":
        current = f.get("current_value")
        now = f" Current value: {current}." if current else " No current value is recorded."
        return (
            f"- {_currency_address(f)}: SUPERSEDED — this note's value was replaced"
            f" by a newer note.{now}{pointer}"
        )
    if f["status"] == "retracted":
        return (
            f"- {_currency_address(f)}: RETRACTED — no longer asserted (an extraction"
            f" error or a correction)."
            f"{pointer}"
        )
    return (
        f"- {_currency_address(f)}: PENDING REVIEW — unverified, contested by the"
        f" review process."
        f"{pointer}"
    )


def format_currency(stale: list[dict[str, Any]]) -> str:
    """The currency overlay appended to a note's prose: which facts the note
    states are no longer the live value, and where the current value lives. The
    note above is the original record; the graph knows what has since changed, so
    the agent should prefer the current values (or read_entity to confirm)."""
    if not stale:
        return ""
    header = (
        "\n\n⚠ currency overlay (from the fact graph — the note text above is the"
        " original record, but these facts are no longer current; prefer the values"
        " below):"
    )
    return header + "\n" + "\n".join(_currency_line(f) for f in stale)


def format_note(note: NoteInfo) -> str:
    return f"note {note.id} [{note.domain}] {note.created_at:%Y-%m-%d}\n{note.body}"


def search_sources(resp: SearchResponse) -> tuple[NoteSource, ...]:
    """The structured twin of format_search: a note source per note hit for the UI's cards.
    Wiki hits are surfaced in the prose (read_wiki targets); a wiki source card is a later UI."""
    return tuple(
        NoteSource(note_id=r.note_id, domain=r.domain, snippet=r.snippet.strip())
        for r in resp.results
        if isinstance(r, SearchResult)
    )


def _note_snippet(body: str, limit: int = 140) -> str:
    """A one-line preview of a note's body for its source card."""
    line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    return line[:limit]


def _edge_line(f: dict[str, Any]) -> str:
    """One current fact as an edge. A relationship edge names another entity —
    surface its id so the model can read_entity it and follow the relationship
    one hop further (the chain behind "my wife's name")."""
    base = f"- {f['predicate']}: {f['statement']}"
    if obj := f.get("object_entity_id"):
        name = f.get("object_entity_name") or ""
        return f"{base} → {name} (id={obj})"
    return base


def _current_facts(view: dict[str, Any]) -> list[dict[str, Any]]:
    return [p["current"] for p in view.get("predicates", []) if p.get("current")]


# How many source notes the entity view lists (and offers as cards). The graph
# is the spine, notes hold the richness (docs/reference/ENTITY_GRAPH_REFOCUS_PLAN.md), so
# the entity page must be a doorway into its sources — but a bounded one; the
# model chains read_note only where it actually needs the prose.
_SOURCE_NOTE_LIMIT = 5


def _source_notes(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The entity's distinct mentioning notes, newest-first (mentions arrive in
    that order; the first mention per note wins, so its snippet is the freshest)."""
    seen: dict[str, dict[str, Any]] = {}
    for m in view.get("mentions", []):
        seen.setdefault(m["note_id"], m)
    return list(seen.values())


def _source_note_line(m: dict[str, Any]) -> str:
    stamp = m.get("note_created_at") or m.get("created_at")
    date = f" {stamp:%Y-%m-%d}" if stamp else ""
    domain = f" [{m['domain']}]" if m.get("domain") else ""
    snippet = " ".join(str(m.get("snippet") or "").split())[:140]
    return f"- note {m['note_id']}{domain}{date}: {snippet}"


def format_entity(view: dict[str, Any]) -> str:
    """The structured/graph view: schema.org kind, names, facts-as-edges (with the
    target entity's id on relationship edges, so they can be chained through),
    inbound edges, and the most recent source notes WITH ids — the graph is the
    spine and notes hold the richness, so the entity page is also the doorway
    into read_note. Text-only now; an entity_card view comes with the component
    registry (the text-first tool path, docs/archive/ASSISTANT_PLAN.md)."""
    lines = [f"{view['canonical_name']} [{view['kind']}] ({view['domain']})"]
    if aliases := view.get("aliases"):
        lines.append("also known as: " + ", ".join(aliases))
    if current := _current_facts(view):
        lines.append("facts:")
        lines += [_edge_line(f) for f in current]
    if inbound := view.get("inbound"):
        lines.append("referenced by:")
        lines += [f"- {r['name']} {r['predicate']} this" for r in inbound]
    if notes := _source_notes(view):
        shown = notes[:_SOURCE_NOTE_LIMIT]
        lines.append(f"source notes ({len(notes)} total, newest first — read_note for the prose):")
        lines += [_source_note_line(m) for m in shown]
        if len(notes) > len(shown):
            lines.append(
                f"  (+{len(notes) - len(shown)} more — search the entity name for the rest)"
            )
    return "\n".join(lines)


def entity_view_sources(view: dict[str, Any]) -> tuple[NoteSource, ...]:
    """The listed source notes as cards — the structured twin of the note lines
    format_entity prints, so the PWA renders openable sources alongside the
    entity and the ids the model cites stay tappable for the owner."""
    return tuple(
        NoteSource(
            note_id=str(m["note_id"]),
            domain=str(m.get("domain") or view.get("domain", "general")),
            snippet=" ".join(str(m.get("snippet") or "").split())[:140],
        )
        for m in _source_notes(view)[:_SOURCE_NOTE_LIMIT]
    )


def entity_view_objects(view: dict[str, Any]) -> tuple[EntityRef, ...]:
    """The entities this one points at via its relationship edges — tappable chips
    so the PWA linkifies a related name the agent mentions, and a structured twin
    of the ids `format_entity` prints for the model to chain on."""
    return tuple(
        EntityRef(
            entity_id=str(f["object_entity_id"]),
            label=str(f.get("object_entity_name") or f["object_entity_id"]),
            domain=f.get("object_entity_domain") or view.get("domain", "general"),
        )
        for f in _current_facts(view)
        if f.get("object_entity_id")
    )


def entity_view_ref(view: dict[str, Any]) -> EntityRef:
    """The entity that was READ, as a citable ref carrying its current-fact
    statements. read_entity only surfaced the entities this one *points at*
    (`entity_view_objects`), never the subject itself — so an answer drawn from one
    of the subject's own facts ("born in 1986", read off Me's birthDate edge) had no
    matching text in the grounding corpus and was falsely flagged "not in your
    notes". Surfacing the subject with its fact statements gives the verifier the
    text the answer actually grounds in, and the PWA a chip to open the entity."""
    return EntityRef(
        entity_id=str(view.get("id") or ""),
        label=str(view["canonical_name"]),
        domain=view.get("domain", "general"),
        aliases=[str(a) for a in view.get("aliases", [])],
        facts=[str(f["statement"]) for f in _current_facts(view) if f.get("statement")],
    )


def build_read_handlers(
    search: SearchService,
    notes: NotesRepo,
    entities: EntityReader,
    aprs: AprsReader | None = None,
) -> dict[str, ToolHandler]:
    """`aprs` is None on a box with no radio, and the heard-log tool is then simply
    absent — the same graceful degrade the image and transcription sidecars use."""
    async def search_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolOutput("search needs a non-empty query.")
        limit = int(arguments.get("limit", _DEFAULT_LIMIT))
        resp = await search.search(ctx.session, query, None, limit)
        hits = [r for r in resp.results if isinstance(r, SearchResult)]
        # Overlay the supersession/review outcome the snippet's prose can't show. A
        # stale analyte `value` is flagged chunk-precisely (which reading the snippet
        # quotes, §7.2); every other predicate stays note-scoped. Partition by predicate
        # so the two flags never double-report the same reading.
        note_ids = list({r.note_id for r in hits})
        currency = await entities.note_currency(ctx.session, note_ids) if note_ids else {}
        currency = {
            n: [f for f in facts if f["predicate"] != "value"] for n, facts in currency.items()
        }
        chunk_ids = list({r.chunk_id for r in hits if r.chunk_id})
        analyte = await entities.analyte_currency(ctx.session, chunk_ids) if chunk_ids else {}
        return ToolOutput(format_search(resp, currency, analyte), search_sources(resp))

    async def aprs_recent_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        if aprs is None:
            return ToolOutput("This box has no radio, so there is no APRS log.")
        rows = await aprs.recent(
            ctx.session,
            limit=int(arguments.get("limit") or RECENT_DEFAULT),
            source=(str(arguments.get("source") or "").strip() or None),
        )
        if not rows:
            return ToolOutput("Nothing heard — APRS logging has not been running.")
        # Rendered as plain lines rather than JSON, and deliberately WITHOUT any framing
        # that could read as a system message: every field below is a stranger's text.
        lines = [
            f"{row['heard_at']:%H:%M} {row['source']} -> {row['destination']}: {row['info']}"
            for row in rows
        ]
        return ToolOutput("\n".join(lines))

    async def read_note_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        note_id = str(arguments.get("note_id", "")).strip()
        if not note_id:
            return ToolOutput("read_note needs a note_id.")
        note = await notes.get_note(ctx.session, note_id)
        if note is None:
            return ToolOutput("No note with that id is in scope.")
        # The note body is the original record; the graph knows what has since
        # changed — append the currency overlay so the agent doesn't quote a value
        # a later note superseded or a correction retracted.
        currency = await entities.note_currency(ctx.session, [note.id])
        body = format_note(note) + format_currency(currency.get(note.id, []))
        source = NoteSource(note_id=note.id, domain=note.domain, snippet=_note_snippet(note.body))
        return ToolOutput(body, (source,))

    return {
        "search": search_tool,
        "read_note": read_note_tool,
        # The APRS heard log. `read` because it only reads a table — but what it
        # reads is UNTRUSTED: packets are transmissions from anyone in range, and a
        # callsign forges trivially (APRS_CONTROL_PLAN.md, the two trust tiers).
        **({"aprs_recent": aprs_recent_tool} if aprs is not None else {}),
    }


_ENTITY_LIMIT = 8

# The owner's own entity is "Me". The model's natural reach for it is read_entity
# with a literal "me"/"owner"/"myself" rather than the uuid — so resolve those
# sentinels to the owner entity and make that instinct one successful call, instead
# of a failed guess ("No entity with that id is in scope") that falls back to a noisy
# find_entity("Me") (whose substring match also returns every "appointMEnt").
_OWNER_SENTINELS = frozenset({"me", "owner", "myself"})


def entity_refs(rows: list[dict[str, Any]]) -> tuple[EntityRef, ...]:
    """Map entity rows to refs for the response's tappable entity chips —
    carrying aliases so a name in the prose links even when it isn't the label."""
    return tuple(
        EntityRef(
            entity_id=str(r["id"]),
            label=str(r["canonical_name"]),
            domain=r["domain"],
            aliases=[str(a) for a in r.get("aliases", [])],
        )
        for r in rows
    )


def format_entities(rows: list[dict[str, Any]]) -> str:
    """The model-facing list — names + ids so it can chain into read_entity."""
    return "\n".join(
        f"- {r['canonical_name']} [{r['kind']}] ({r['domain']}) id={r['id']}" for r in rows
    )


def format_relations(rows: list[dict[str, Any]]) -> str:
    """The model-facing list for relate: which edge led to which entity, with ids
    to chain into read_entity (e.g. read the spouse for their name)."""
    return "\n".join(
        f"- {r['predicate']} → {r['canonical_name']} [{r['kind']}] ({r['domain']}) id={r['id']}"
        for r in rows
    )


def format_neighborhood(result: dict[str, Any]) -> str:
    """The model-facing vicinity map: neighbors grouped per hop (format_relations'
    line idiom — ids to chain into read_entity — plus the single connecting path
    the traversal kept), then the notes tying the set together with ids the model
    can read_note."""
    entities = result["entities"]
    anchor, neighbors = entities[0], entities[1:]
    lines = [
        f"{anchor['name']} [{anchor['kind']}] ({anchor['domain']}) id={anchor['id']}"
        f" — neighborhood within {result['depth']} hop(s):"
    ]
    if not neighbors:
        lines.append("no connected entities in scope.")
    hop = 0
    for e in neighbors:  # hop-then-rank order, so the group headers are sequential
        if e["hop"] != hop:
            hop = e["hop"]
            lines.append(f"hop {hop}:")
        lines.append(f"- {e['name']} [{e['kind']}] ({e['domain']}) id={e['id']} — {e['path']}")
    if notes := result["notes"]:
        lines.append("connecting notes:")
        lines += [
            f"- note {n['note_id']} (hop {n['hop']}) — {', '.join(n['connects'])}" for n in notes
        ]
    return "\n".join(lines)


def neighborhood_entities(result: dict[str, Any]) -> tuple[EntityRef, ...]:
    """Every traversed entity (anchor first) as a tappable chip, the structured
    twin of the ids `format_neighborhood` prints."""
    return tuple(
        EntityRef(entity_id=str(e["id"]), label=str(e["name"]), domain=e["domain"])
        for e in result["entities"]
    )


def neighborhood_sources(result: dict[str, Any]) -> tuple[NoteSource, ...]:
    """A source card per connecting note. The snippet names WHO the note connects
    (the traversal's reason for surfacing it) — the body was never fetched."""
    return tuple(
        NoteSource(
            note_id=n["note_id"], domain=n["domain"], snippet="connects " + ", ".join(n["connects"])
        )
        for n in result["notes"]
    )


# The neighborhood tool's kinds argument mapped onto the typed traversal
# vocabulary — one lookup both validates the model's string and narrows it.
_EDGE_KINDS: dict[str, EdgeKinds] = {
    "relationships": "relationships",
    "co-mentions": "co-mentions",
    "both": "both",
}


def build_entity_handlers(entities: EntityReader) -> dict[str, ToolHandler]:
    async def read_entity_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        entity_id = str(arguments.get("entity_id", "")).strip()
        if not entity_id:
            return ToolOutput("read_entity needs an entity_id.")
        if entity_id.lower() in _OWNER_SENTINELS:
            owner_id = await entities.owner_entity_id(ctx.session)
            if owner_id is None:
                return ToolOutput("No entity with that id is in scope.")
            entity_id = owner_id
        view = await entities.entity_view(ctx.session, entity_id)
        if view is None:
            return ToolOutput("No entity with that id is in scope.")
        # The subject (with its facts) leads, then the entities it points at — so a
        # claim from the subject's own fact grounds, and related names still linkify.
        # Source-note cards ride along so the doorway into the prose is tappable.
        return ToolOutput(
            format_entity(view),
            entity_view_sources(view),
            entities=(entity_view_ref(view), *entity_view_objects(view)),
        )

    async def find_entity_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        name = str(arguments.get("name", "")).strip()
        if not name:
            return ToolOutput("find_entity needs a name.")
        kind = str(arguments.get("kind", "")).strip() or None
        rows = (await entities.list_entities(ctx.session, name, kind, _ENTITY_LIMIT))[
            :_ENTITY_LIMIT
        ]
        if not rows:
            return ToolOutput(f"No entity matching '{name}' in scope.")
        return ToolOutput(format_entities(rows), entities=entity_refs(rows))

    async def relate_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        relationship = str(arguments.get("relationship", "")).strip()
        if not relationship:
            return ToolOutput("relate needs a relationship.")
        anchor = str(arguments.get("from", "")).strip() or None
        rows = await entities.relate(
            ctx.session, anchor, predicate_candidates(relationship), _ENTITY_LIMIT
        )
        if not rows:
            whose = "the owner" if anchor is None else "that entity"
            return ToolOutput(f"No '{relationship}' relationship for {whose} in scope.")
        return ToolOutput(format_relations(rows), entities=entity_refs(rows))

    async def neighborhood_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        anchor = str(arguments.get("anchor", "")).strip()
        if not anchor or anchor.lower() in _OWNER_SENTINELS:
            resolved = await entities.owner_entity_id(ctx.session)
            if resolved is None:
                return ToolOutput("No entity with that id is in scope.")
            anchor = resolved
        kinds = _EDGE_KINDS.get(str(arguments.get("kinds", "both")).strip() or "both")
        if kinds is None:
            return ToolOutput(
                "neighborhood kinds must be 'relationships', 'co-mentions', or 'both'."
            )
        hops = max(1, min(int(arguments.get("hops", DEFAULT_DEPTH)), MAX_DEPTH))
        # The model may narrow the entity budget, never widen past the ratified cap.
        limit = max(1, min(int(arguments.get("limit", DEFAULT_TOTAL_CAP)), DEFAULT_TOTAL_CAP))
        result = await entities.neighborhood(
            ctx.session, anchor, depth=hops, kinds=kinds, total_cap=limit
        )
        if result is None:
            return ToolOutput("No entity with that id is in scope.")
        return ToolOutput(
            format_neighborhood(result),
            neighborhood_sources(result),
            entities=neighborhood_entities(result),
        )

    return {
        "read_entity": read_entity_tool,
        "find_entity": find_entity_tool,
        "relate": relate_tool,
        "neighborhood": neighborhood_tool,
    }


class WikiReader(Protocol):
    async def get_article(self, ctx: SessionContext, article_id: str) -> dict[str, Any] | None:
        """The assembled article (lead + sections + references), RLS-scoped; None if not visible."""
        ...


def format_wiki_article(article: dict[str, Any]) -> str:
    """Render an article for the agent to discuss/cite: the lead + each section's prose, then the
    numbered References (the [n] markers in the prose index into them)."""
    lines = [f"# {article['title']}", str(article.get("subtitle", ""))]
    for para in article.get("lead", []):
        lines.append(str(para.get("text", "")))
    for section in article.get("sections", []):
        lines.append(f"\n## {section['heading']} [{section['domain']}]")
        for block in section.get("blocks", []):
            lines.append(str(block.get("text", "")))
        for sub in section.get("subsections", []):
            lines.append(f"### {sub['heading']}")
            for block in sub.get("blocks", []):
                lines.append(str(block.get("text", "")))
    refs = article.get("references", [])
    if refs:
        lines.append("\nReferences:")
        lines.extend(f"[{r['n']}] {r['meta']} — {r['snippet']}" for r in refs)
    return "\n".join(line for line in lines if line.strip())


def build_wiki_handlers(wiki: WikiReader) -> dict[str, ToolHandler]:
    """The read-only wiki-editorial tool: read a machine-written article (with its sources) so the
    agent can explain or discuss it in Talk. Read-only — the wiki is never edited directly; the
    write levers (correction note, source exclusion, rebuild) are separate."""

    async def read_wiki_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        article_id = str(arguments.get("article_id", "")).strip()
        if not article_id:
            return ToolOutput("read_wiki needs an article_id.")
        article = await wiki.get_article(ctx.session, article_id)
        if article is None:
            return ToolOutput("No wiki article with that id is in scope.")
        return ToolOutput(format_wiki_article(article))

    return {"read_wiki": read_wiki_tool}


def build_registry(
    search: SearchService,
    notes: NotesRepo,
    entities: EntityReader,
    memory: MemoryService,
    proposals: ProposalRepo,
    connectors: ConnectorRegistry,
    lists: ListsRepo,
    appointments: AppointmentsRepo,
    wiki: WikiReader,
    wiki_write: dict[str, ToolHandler],
    locations: SqlLocationRepo,
    devices: SqlDeviceRepo,
    web_handlers: dict[str, ToolHandler],
    city_geocoder: "CityGeocoder",
    maker: "async_sessionmaker[AsyncSession]",
    external_reverse: "NominatimReverseClient | None" = None,
    router: "LlmRouter | None" = None,
    settings: "SqlSettingsStore | None" = None,
    embed: "EmbedClient | None" = None,
    feeds: "FeedClient | None" = None,
    searxng: "SearxngClient | None" = None,
    fetcher: "WebFetcher | None" = None,
    wikidata: "WikidataClient | None" = None,
    courtlistener: "CourtListenerClient | None" = None,
    nppes: "NppesClient | None" = None,
    federal_register: "FederalRegisterClient | None" = None,
    image_handlers: dict[str, ToolHandler] | None = None,
    transcribe_handlers: dict[str, ToolHandler] | None = None,
    sdr_handlers: dict[str, ToolHandler] | None = None,
    video_handlers: dict[str, ToolHandler] | None = None,
    stream_handlers: dict[str, ToolHandler] | None = None,
    grab_handlers: dict[str, ToolHandler] | None = None,
    fetch_image_handlers: dict[str, ToolHandler] | None = None,
    compare_handlers: dict[str, ToolHandler] | None = None,
    ocr_handlers: dict[str, ToolHandler] | None = None,
    html_handlers: dict[str, ToolHandler] | None = None,
    canvas_handlers: dict[str, ToolHandler] | None = None,
    crop_handlers: dict[str, ToolHandler] | None = None,
    gmail_handlers: dict[str, ToolHandler] | None = None,
    external_handlers: dict[str, ToolHandler] | None = None,
    research_report_handlers: dict[str, ToolHandler] | None = None,
    moltbook_handlers: dict[str, ToolHandler] | None = None,
    moltbook_write_handlers: dict[str, ToolHandler] | None = None,
    notify_bus: "NotifyBus | None" = None,
    push: "PushNotifier | None" = None,
    fcm_token_repo: "FcmTokenRepo | None" = None,
    deepest_handle: DeepestHandle | None = None,
) -> ToolRegistry:
    """The agent's tool registry: every shipped sidecar bound to its handler — the
    read tools, the Tier-A memory tools, the list tools (which write the owner's
    own data directly), the appointment read tools (over the notes-derived
    projection), propose_correction and propose_merge (which stage a Proposal,
    never write), and the egress connector tools (which stage an egress Proposal,
    never call out).
    `image_handlers` is jerv's local `analyze_image` vision read, present only when a
    ComfyUI is configured; when absent the sidecar is dropped (graceful degrade,
    docs/archive/IMAGE_GEN_PLAN.md).
    Fails at startup if a sidecar and handler don't match exactly, so a new .tool
    can never ship unwired."""
    spawn_ref = SpawnRef()
    # Deep research is late-bound like the spawn primitive: its service needs the spawn
    # service (which needs the very registry being built), so it is wired below once both
    # exist (docs/plans/DEEP_RESEARCH_TOOL_PLAN.md).
    deep_research_ref = DeepResearchRef()
    # The deep_produce verb shares the same DeepResearchService (one engine, two verbs);
    # late-bound below alongside deep_research (DEEP_PRODUCE_PLAN.md, W1).
    deep_produce_ref = DeepProduceRef()
    # The two-tier decomposition primitive (a task agent's one-shot sub-fan), late-bound
    # to the same spawn service (DEEPEST_RESEARCH_TOOL_PLAN.md, R2).
    decompose_ref = DecomposeRef()
    # The no-holds background research kickoff (enqueue-and-return), late-bound to the lane
    # + the deep-research service below (DEEPEST_RESEARCH_TOOL_PLAN.md, R7).
    deepest_research_ref = DeepestResearchRef()
    registry = load_registry(
        TOOLS_DIR,
        {
            # The heard-log reader is built only when the box has a radio, so the
            # tool is simply absent otherwise — the same degrade as the sidecars below.
            **build_read_handlers(
                search, notes, entities, AprsReader(maker) if sdr_handlers else None
            ),
            # A clock read — no owner data, no domain — so every agent that holds it
            # (the curator by default; jerv by allowlist) can ground time-relative talk.
            **build_clock_handlers(),
            # `name_session`: the chat names itself from inside its own turn, replacing the
            # separate `session.title` completion that evicted the primed prefix to do it.
            **build_session_handlers(AgentSessionRepo(maker)),
            **build_entity_handlers(entities),
            **build_list_handlers(lists),
            **build_appointment_handlers(appointments),
            **build_appointment_write_handlers(proposals, appointments),
            **build_lab_handlers(maker),
            # The generic charting tools: chart_measurements (grounded, reads app.facts
            # under RLS, cited) and render_chart (the model passes a general series).
            **build_chart_handlers(maker),
            # render_bars: the categorical twin of render_chart — a model-supplied
            # breakdown/ranking rendered as the general-domain `bar_chart` view.
            **build_bar_handlers(),
            **build_memory_handlers(memory),
            **build_proposal_handlers(proposals),
            **build_intake_link_handlers(proposals),
            **build_merge_handlers(proposals, entities),
            **build_connector_handlers(connectors, proposals),
            **build_geocode_handlers(city_geocoder),
            **build_location_handlers(locations, devices, entities, proposals),
            # jerv's owner-approved, jerv-only location read (a `web`-gated, opt-in
            # tool, never offered to the curator). It names the live PWA fix the turn
            # carried via the offline city geocoder (no saved-place / device read),
            # escalating to the external geocoder only for a requested street address.
            **build_presence_handlers(city_geocoder, external_reverse),
            **build_wiki_handlers(wiki),
            # The owner-only host-telemetry read (query_server_metrics): RLS-gated
            # by the metrics tables' owner policy, so a non-owner session sees nothing.
            **build_metrics_handlers(maker),
            **wiki_write,
            # The jerv chatbot's internet tools (`web` permission), opt-in per agent.
            **web_handlers,
            # jerv's local image-gen tools (`web`-gated, on-box), present only when a
            # ComfyUI is configured; otherwise their sidecars are dropped below.
            **(image_handlers or {}),
            # jerv's local audio transcription (`web`-gated, on-box), present only when
            # the whisper gateway is configured; otherwise its sidecar is dropped below.
            **(transcribe_handlers or {}),
            # jerv's radio tools, present only when the box has an SDR; otherwise
            # their sidecars are dropped below. sdr_listen is what puts the tuner
            # icon in the composer, so without it that surface is unreachable.
            **(sdr_handlers or {}),
            # jerv's local video analysis (`web`-gated, on-box), present only when
            # ffmpeg is available; otherwise its sidecar is dropped below.
            **(video_handlers or {}),
            # jerv's URL-sourced stream/video analysis (`web`-gated), present only when
            # ffmpeg AND yt-dlp are available; otherwise its sidecar is dropped below.
            **(stream_handlers or {}),
            # jerv's single-frame grab (`web`-gated, on-box), present only when ffmpeg is
            # available; otherwise its sidecar is dropped below. A grabbed still becomes a
            # first-class chat image analyze_image/compare_images can read by id.
            **(grab_handlers or {}),
            # jerv's web-image fetch (`web`-gated): fetch a web image's bytes through the
            # SSRF-guarded fetcher and persist it as a chat image — web_fetch is text-only,
            # so this is jerv's only way to actually see a picture on the web.
            **(fetch_image_handlers or {}),
            # jerv's multi-image compare (`web`-gated, vision-router-backed): compare N chat
            # images and show the owner a side-by-side. Not ComfyUI-gated (a vision read).
            **(compare_handlers or {}),
            # jerv's deterministic OCR read (`web`-gated, on-box RapidOCR): the verbatim,
            # hallucination-free counterpart to analyze_image. Present only when the sidecar
            # is configured; otherwise dropped below (docs/plans/RAPIDOCR_PLAN.md).
            **(ocr_handlers or {}),
            # jerv's standalone HTML render (`web`-gated, on-box): model-authored markup
            # rasterized to an image card. Present only when the htmlrender sidecar is
            # configured; otherwise its sidecar is dropped below.
            **(html_handlers or {}),
            **(canvas_handlers or {}),
            **(crop_handlers or {}),
            # jerv's search over the external-source video corpus (`web`-gated). Reads the
            # general-domain corpus via a purpose-built scope (EXTERNAL_VIDEO_INGESTION_PLAN.md).
            **(external_handlers or {}),
            **(research_report_handlers or {}),
            # The archivist persona's Gmail tools (`web`-gated), present only when a
            # Gmail refresh token is configured; otherwise their sidecars are dropped.
            **(gmail_handlers or {}),
            # The jmolt persona's Moltbook read umbrella (`web`-gated, jmolt-only), built
            # in main.py over the pinned client + live key provider (docs/plans/JMOLT_PLAN.md).
            **(moltbook_handlers or {}),
            # The jmolt persona's Moltbook WRITE tools (`web`-gated, jmolt-only): stage into
            # the outbox with the M8/M9/M10 guards; built in main.py over the outbox + store.
            **(moltbook_write_handlers or {}),
            # The archivist's cross-session memory (`web`-gated, archivist-only) over
            # the owner-only `archivist_memory` table — always wired (the table always
            # exists); curator never sees it (the opt-in web class).
            **build_archivist_memory_handlers(maker),
            # jmolt's scratchpad tools (`web`-gated, jmolt-only) over the `jmolt_scratch`
            # table — always wired (the table always exists); the M19 RLS split, not this
            # code, is the firewall (docs/plans/JMOLT_PLAN.md, W2).
            **build_jmolt_scratch_handlers(maker),
            # jmolt's journal tool (`web`-gated, jmolt-only) over the `jmolt_journal` table
            # — jmolt's append-only line to its human, surfaced in the digest + PWA. Always
            # wired; the M19 RLS split is the firewall (docs/plans/JMOLT_PLAN.md).
            **build_jmolt_journal_handlers(maker),
            # jmolt's `time_left` tool (`web`-gated, jmolt-only): reports how much of its
            # nightly hour remains, computed from the trusted local clock — always wired.
            **build_jmolt_time_handlers(maker),
            # jerv's read-only lens on jmolt (`web`-gated, jmolt_observer-only): the
            # `jmolt_observe` umbrella over jmolt's nights/transcripts/actions/scratchpad/
            # outbox — always wired (the tables always exist). Every read runs a
            # jmolt-READ context (owner + jmolt domain, no auth_context), so the M19 RLS
            # split grants SELECT and denies every write; the handler also refuses to run
            # alongside any egress tool (M16). docs/plans/JMOLT_PLAN.md, W4.
            **build_jmolt_observe_handlers(maker),
            # jerv's per-conversation planning tools (`web`-gated, jerv-only) over the
            # owner-only `agent_session_plans` table — always wired (the table always
            # exists); curator never sees them (the opt-in web class). read_plan/write_plan
            # let jerv draft a plan the owner approves, then execute against it
            # (docs/archive/JERV_PLANNING_TOOL_PLAN.md).
            **build_plan_handlers(maker),
            # The sub-agent spawn primitive (docs/archive/SUBAGENT_SPAWNING_PLAN.md). A
            # late-bound handler: the service it forwards to needs the very registry
            # being built (it launches children on it), so it is wired below once the
            # registry exists. jerv (+ research/review children) reach it by
            # allowlist; curator's tools=None never does (NEVER_DEFAULT).
            "spawn_subagent": spawn_ref,
            # The deep-research primitive: a bounded plan→gather→reflect→refill→
            # synthesize→critique run over the same fan. jerv-only + NEVER_DEFAULT
            # (curator's tools=None never absorbs it), wired below once the spawn
            # service exists (deep research runs its fans through it).
            "deep_research": deep_research_ref,
            # The deep-produce verb — the same engine as deep_research, jerv-only +
            # NEVER_DEFAULT, wired to the shared service below (DEEP_PRODUCE_PLAN.md, W1).
            "deep_produce": deep_produce_ref,
            # The task-agent decomposition tool: a research_deep child reaches it by
            # allowlist (jerv holds it only for the parent⊆child clamp); NEVER_DEFAULT, so
            # curator's tools=None never absorbs it. Wired below with the spawn service.
            "decompose_research": decompose_ref,
            # The deepest-research kickoff: enqueue-and-return, jerv-only + NEVER_DEFAULT,
            # wired below with the lane + the deep-research service it drives.
            "deepest_research": deepest_research_ref,
        },
        optional=(
            OPTIONAL_IMAGE_TOOLS
            | OPTIONAL_TRANSCRIBE_TOOL
            | OPTIONAL_SDR_TOOLS
            | OPTIONAL_VIDEO_TOOL
            | OPTIONAL_STREAM_TOOL
            | OPTIONAL_GRAB_TOOL
            | OPTIONAL_FETCH_IMAGE_TOOL
            | OPTIONAL_COMPARE_TOOL
            | OPTIONAL_OCR_TOOL
            | OPTIONAL_HTML_TOOL
            | OPTIONAL_CANVAS_TOOLS
            | OPTIONAL_CROP_TOOLS
            | OPTIONAL_READ_ARTIFACT_TOOL
            | OPTIONAL_GMAIL_TOOLS
            | OPTIONAL_MOLTBOOK_TOOLS
            | OPTIONAL_MOLTBOOK_WRITE_TOOLS
        ),
    )
    # Wire the spawn service now that the registry exists (children run on it). It
    # needs the LLM router; when none is configured the ref stays unbound and a spawn
    # call refuses cleanly rather than erroring.
    if router is not None:
        spawn_ref.service = SpawnService(
            router=router,
            registry=registry,
            sessions=AgentSessionRepo(maker),
            runlog=AgentRunLog(maker),
            transcript=AgentTranscript(maker),
        )
        # Deep research runs its gather/refill/critique fans through the spawn service,
        # so it is wired once that exists. Same guard: no router → the ref stays unbound
        # and a deep_research call refuses cleanly.
        deep_research_ref.service = DeepResearchService(
            router=router,
            spawn=spawn_ref.service,
            maker=maker,
            embed=embed,
            feeds=feeds,
            searxng=searxng,
            fetcher=fetcher,
            # The free public-records clients backing the deterministic pre-gather
            # (CANDIDATE_PROFILE_V2_PLAN.md) — the same instances the `public_records` tool uses.
            wikidata=wikidata,
            courtlistener=courtlistener,
            nppes=nppes,
            federal_register=federal_register,
        )
        # deep_produce is the same engine, a different verb: share the one service instance.
        deep_produce_ref.service = deep_research_ref.service
        # decompose_research forwards to the same spawn service (it spawns the sub-fan).
        decompose_ref.service = spawn_ref.service

        # Deepest research: a single background lane (one run at a time by default) and a
        # progress channel with all three off-turn legs wired — the durable chat transcript,
        # the NotifyBus deep-link nudge, and (when a real push notifier is deployed) an FCM
        # poke to owner devices whose tokens are resolved live per tick. The kickoff service
        # drives the deep-research service in deepest mode and also owns resume/drain.
        async def _owner_push_tokens() -> list[str]:
            # The owner's live device tokens, resolved per progress tick (best-effort). Empty
            # when no push notifier / token repo is configured, so the poke leg no-ops.
            if fcm_token_repo is None:
                return []
            owner_pid = await _owner_principal_id(maker)
            if owner_pid is None:
                return []
            owner_ctx = SessionContext(principal_id=owner_pid, principal_kind="owner")
            return await fcm_token_repo.tokens_for_subjects(owner_ctx, [owner_pid])

        deepest_kickoff = DeepestKickoffService(
            lane=DeepestRunLane(),
            service=deep_research_ref.service,
            progress=DeepestProgressChannel(
                transcript=AgentTranscript(maker),
                notify=notify_bus,
                push=push,
                push_tokens_provider=_owner_push_tokens,
            ),
            maker=maker,
        )
        deepest_research_ref.service = deepest_kickoff
        # Expose the wired service so the app lifespan can resume interrupted runs at startup
        # and drain in-flight ones at shutdown (it can't build the service — that needs the
        # registry-backed DeepResearchService created just above).
        if deepest_handle is not None:
            deepest_handle.service = deepest_kickoff
    return registry
