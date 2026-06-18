"""The owner-only location read tools (Phase 7 / L2): `where_is` / `where_was_i`
(#5), `device_status` (#10), `home_status` (#11), `nearby_now` (#12).

These read the location domain — a domain firewalled in Postgres. But TWO of the
tables behind these answers (`app.events`, `place_geofence`) are gated in RLS by
only `has_domain_scope`, which passes for ANY owner session (including a narrowed
`owner_scoped` agent context) and even a non-owner holding the `location` scope.
RLS therefore does NOT fail-close those reads. So the full-owner gate is the
PRIMARY barrier here, not a backstop — and it is applied as a registration-time
WRAPPER around every handler (`_owner_only`), so a new location tool added later
*cannot forget* it: the structural guarantee is the wrapper, not handler
discipline (mirrors `agent/geocodetools.py::_is_full_owner`, but un-forgettable).

Tools return names / times / distances only. A coordinate never crosses into
model-facing text: where a position is needed (e.g. `nearby_now`) it is resolved
inside the repo query and only the resulting place names + distances come back.
"""

from datetime import UTC, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.locations import (
    DeviceActivity,
    LatestPlace,
    NearbyPlace,
    NearestFix,
    RosterEntry,
    SqlLocationRepo,
    require_full_owner,
)

log = structlog.get_logger()

# A fix older than this (relative to "now") is reported as stale: the position is
# the last KNOWN one, not necessarily where the subject is now. 30 minutes is the
# coarse-presence horizon the rest of the location stack uses.
_STALE_GAP_SECONDS = 30 * 60.0
# A device unheard-from for longer than this is flagged in `device_status`.
_DEVICE_STALE_SECONDS = 60 * 60.0
# Battery at or below this reads as "low" in `device_status`.
_LOW_BATTERY_PCT = 20
# `nearby_now` defaults / bounds — a bounded radius so one call can never sweep
# every fence, and a small result cap (names + distances only).
_NEARBY_DEFAULT_RADIUS_M = 1000.0
_NEARBY_MAX_RADIUS_M = 50_000.0
_NEARBY_DEFAULT_LIMIT = 5
_NEARBY_MAX_LIMIT = 20


class EntityResolver(Protocol):
    """The slice of the analysis repo `where_is` needs: name → candidate entities
    (so a named person/device can be resolved to the entity carrying the device
    binding)."""

    async def list_entities(
        self,
        ctx: SessionContext,
        q: str | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]: ...


def _localize(dt: datetime, tz: str | None) -> datetime:
    """`dt` in the owner's display zone when known, else unchanged (an unknown zone
    name falls back rather than raising mid-render) — so the prose agrees with the
    client-localized cards."""
    if tz is None:
        return dt
    try:
        return dt.astimezone(ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError):
        return dt


def _when(dt: datetime | None, tz: str | None) -> str:
    """A time rendered in the owner's zone, or "unknown" when absent."""
    if dt is None:
        return "unknown"
    return _localize(dt, tz).strftime("%Y-%m-%d %H:%M")


def _age_seconds(dt: datetime | None, *, now: datetime) -> float | None:
    """How long ago `dt` was, in seconds (None when absent)."""
    if dt is None:
        return None
    return (now - dt).total_seconds()


def _staleness_note(last_seen: datetime | None, *, now: datetime, threshold: float) -> str:
    """A trailing freshness flag so an old fix is never read as "here now"."""
    age = _age_seconds(last_seen, now=now)
    if age is None:
        return " (no recent fix — position unknown)"
    if age > threshold:
        return " ⚠ STALE — this is the LAST KNOWN position, not necessarily current"
    return ""


def _place_phrase(place: LatestPlace | None, near: NearestFix | None) -> str:
    """The place clause: the current geofenced place when inside one, else a hint
    that only an ungeofenced fix is known. Names only — no coordinates."""
    if place is not None:
        return f"at {place.place_name}"
    if near is not None:
        return "not inside any saved place (only an ungeofenced fix is known)"
    return "no known position"


async def _resolve_subject(
    devices: SqlDeviceRepo, entities: EntityResolver, ctx: ToolContext, name: str
) -> tuple[str | None, str | None, str]:
    """Resolve a person/device NAME to a device subject id via the owner-set #1
    binding (`subject_for_person` reads `entities.subject_id`). Returns
    `(subject_id, matched_label, status)` where status is one of "ok", "none"
    (no entity), "unlinked" (entity has no device binding), "ambiguous" (more than
    one bound entity matched). Never LLM-set — this only reads the deterministic
    binding back, RLS-scoped to the caller."""
    rows = await entities.list_entities(ctx.session, name, None, 10)
    if not rows:
        return None, None, "none"
    bound: list[tuple[str, str]] = []
    for r in rows:
        sid = await devices.subject_for_person(ctx.session, str(r["id"]))
        if sid is not None:
            bound.append((sid, str(r["canonical_name"])))
    if not bound:
        return None, str(rows[0]["canonical_name"]), "unlinked"
    if len({sid for sid, _ in bound}) > 1:
        return None, None, "ambiguous"
    return bound[0][0], bound[0][1], "ok"


def _format_where(
    label: str, place: LatestPlace | None, near: NearestFix | None, tz: str | None, *, now: datetime
) -> str:
    """The model-facing answer for where_is/where_was_i: the place + freshness, in
    names/times only. The freshness comes from the nearest-fix gap so a stale fix is
    flagged rather than reported as the current position."""
    last_seen = near.fix.captured_at if near is not None else None
    stale = _staleness_note(last_seen, now=now, threshold=_STALE_GAP_SECONDS)
    when = f" (last fix {_when(last_seen, tz)})" if last_seen is not None else ""
    return f"{label} is {_place_phrase(place, near)}{when}.{stale}"


def _battery_tone(battery: int | None) -> str:
    if battery is None:
        return "unknown"
    if battery <= _LOW_BATTERY_PCT:
        return "low"
    return "ok"


def _freshness_tone(last_seen: datetime | None, *, now: datetime) -> str:
    age = _age_seconds(last_seen, now=now)
    if age is None:
        return "no-fix"
    return "stale" if age > _DEVICE_STALE_SECONDS else "fresh"


def _format_device_status(
    rows: list[tuple[str, DeviceActivity]], tz: str | None, *, now: datetime
) -> str:
    """A computed, never-persisted device table: freshness + battery as enum tones.
    Labels come from the #1 binding when present, else the subject id is omitted
    from the prose (the owner sees the device by its linked name or "unlinked")."""
    if not rows:
        return "No devices have reported a position."
    lines = ["device | last seen | freshness | battery"]
    for label, act in rows:
        lines.append(
            f"{label} | {_when(act.last_seen, tz)} | {_freshness_tone(act.last_seen, now=now)}"
            f" | {act.battery_pct if act.battery_pct is not None else '?'}%"
            f" ({_battery_tone(act.battery_pct)})"
        )
    return "\n".join(lines)


def _format_home_status(roster: list[RosterEntry], tz: str | None, *, now: datetime) -> str:
    """Per-subject current place cross-checked against fix freshness, person-labeled.
    A subject whose latest fix is stale is reported as last-known, never "here now"."""
    if not roster:
        return "No one's location is known."
    lines = []
    for e in roster:
        where = e.place_name if e.place_name else "not at a saved place"
        stale = _staleness_note(e.last_seen, now=now, threshold=_STALE_GAP_SECONDS)
        when = f" (last fix {_when(e.last_seen, tz)})" if e.last_seen is not None else ""
        lines.append(f"{e.subject_label}: {where}{when}.{stale}")
    return "\n".join(lines)


def _format_nearby(places: list[NearbyPlace]) -> str:
    """Names + distances only (rounded to the nearest 10 m — a coarse distance is a
    presence cue, not a coordinate)."""
    if not places:
        return "No saved places nearby."
    return "\n".join(f"- {p.name}: {round(p.distance_m / 10) * 10} m away" for p in places)


def build_location_handlers(
    locations: SqlLocationRepo,
    devices: SqlDeviceRepo,
    entities: EntityResolver,
) -> dict[str, ToolHandler]:
    """The location read tools, each bound with the registration-time full-owner
    wrapper so a narrowed/`owner_scoped`/non-owner session is refused BEFORE any
    location read runs — the un-forgettable primary barrier the weak-table reads
    (`app.events`, `place_geofence`) depend on."""

    async def where_is_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        name = str(arguments.get("subject", "")).strip()
        if not name:
            return ToolOutput("where_is needs a subject (a person or device name).")
        sid, label, status = await _resolve_subject(devices, entities, ctx, name)
        if status == "none":
            return ToolOutput(f"No person or device named '{name}' is in scope.")
        if status == "unlinked":
            return ToolOutput(f"'{label or name}' has no linked device, so I can't locate it.")
        if status == "ambiguous":
            return ToolOutput(f"'{name}' matches more than one linked device — which one?")
        return await _answer_where(label or name, sid, ctx)

    async def where_was_i_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        sid, label, status = await _resolve_subject(devices, entities, ctx, "Me")
        if status != "ok" or sid is None:
            return ToolOutput("Your own device isn't linked yet, so I can't locate you.")
        return await _answer_where("You", sid, ctx)

    async def _answer_where(label: str, sid: str | None, ctx: ToolContext) -> ToolOutput:
        if sid is None:
            return ToolOutput(f"'{label}' has no linked device, so I can't locate it.")
        now = datetime.now(UTC)
        place = await locations.latest_place(ctx.session, subject_id=sid)
        near = await locations.nearest_fix(
            ctx.session, subject_id=sid, at=now, max_gap_seconds=_STALE_GAP_SECONDS
        )
        return ToolOutput(_format_where(label, place, near, ctx.timezone, now=now))

    async def device_status_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        now = datetime.now(UTC)
        activity = await locations.device_activity(ctx.session)
        labeled: list[tuple[str, DeviceActivity]] = []
        for sid, act in activity.items():
            linked = await devices.linked_person(ctx.session, sid)
            label = linked.canonical_name if linked is not None else "unlinked device"
            labeled.append((label, act))
        labeled.sort(key=lambda t: t[0])
        return ToolOutput(_format_device_status(labeled, ctx.timezone, now=now))

    async def home_status_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        now = datetime.now(UTC)
        roster = await locations.home_roster(ctx.session)
        return ToolOutput(_format_home_status(roster, ctx.timezone, now=now))

    async def nearby_now_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        radius = min(
            _NEARBY_MAX_RADIUS_M,
            max(1.0, float(arguments.get("radius_m", _NEARBY_DEFAULT_RADIUS_M))),
        )
        limit = min(_NEARBY_MAX_LIMIT, max(1, int(arguments.get("limit", _NEARBY_DEFAULT_LIMIT))))
        # Center on the owner's own device (resolved to a subject; the position never
        # surfaces — `nearby` reads it inside the query and returns names/distances).
        sid, _label, status = await _resolve_subject(devices, entities, ctx, "Me")
        if status != "ok" or sid is None:
            return ToolOutput("Your own device isn't linked yet, so I can't find nearby places.")
        places = await locations.nearby(ctx.session, subject_id=sid, radius_m=radius, limit=limit)
        return ToolOutput(_format_nearby(places))

    handlers: dict[str, ToolHandler] = {
        "where_is": where_is_tool,
        "where_was_i": where_was_i_tool,
        "device_status": device_status_tool,
        "home_status": home_status_tool,
        "nearby_now": nearby_now_tool,
    }
    return {name: _owner_only(handler) for name, handler in handlers.items()}


def _owner_only(handler: ToolHandler) -> ToolHandler:
    """Wrap a location handler so it refuses any non-full-owner session BEFORE
    running — the registration-time gate that makes the full-owner barrier
    un-forgettable. `require_full_owner` raises `LocationToolRefusal`, which the
    loop's dispatcher surfaces to the model as a safe, recoverable observation
    (the message is owner-safe: location is simply not available in this session)."""

    async def gated(arguments: dict, ctx: ToolContext) -> str:
        require_full_owner(ctx.session)
        return await handler(arguments, ctx)

    return gated
