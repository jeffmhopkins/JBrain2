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

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from jbrain.agent.contracts import ViewPayload
from jbrain.agent.loop import ToolContext, ToolHandler, ToolOutput
from jbrain.db.session import SessionContext
from jbrain.devices.repo import SqlDeviceRepo
from jbrain.geocode import GeocodeClient
from jbrain.locations import (
    DeviceActivity,
    FixPoint,
    LatestPlace,
    NearbyPlace,
    NearestFix,
    PlaceGeofence,
    RosterEntry,
    SqlLocationRepo,
    require_full_owner,
)
from jbrain.locations.trail import build_trail, trail_view_data

log = structlog.get_logger()

# `location_history` / `location_query` window + radius bounds (the repo clamps
# again, but the tool clamps the parsed args first so the prose agrees). The trail
# fetch cap rides the helper's point budget — a wider window simply downsamples.
_HISTORY_DEFAULT_HOURS = 24.0
_HISTORY_MAX_HOURS = 31 * 24.0
_HISTORY_FIX_LIMIT = 50_000
_QUERY_DEFAULT_HOURS = 24.0
_QUERY_MAX_HOURS = 31 * 24.0
_QUERY_DEFAULT_RADIUS_M = 150.0
_QUERY_MAX_RADIUS_M = 50_000.0

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
) -> tuple[list[str], str | None, str]:
    """Resolve a person/device NAME to its device subject id(s) via the owner-set #1
    binding. The binding lives only on Device entities: a Device entity carries an
    `operatedBy`→Person fact, and the L1 reconciler sets `entities.subject_id` (a
    device subject) on THAT Device — never on the Person. So resolution traverses
    Person entity → `operatedBy` → Device entity → that Device's `subject_id`, and
    also handles the Device being named directly (its own device `subject_id`); see
    `SqlDeviceRepo.device_subjects_for_entity`.

    Returns `(subject_ids, matched_label, status)` where status is one of "ok"
    (>=1 device subject), "none" (no entity matched), "unlinked" (entities matched
    but none reach a device subject). A name matching several owned devices is "ok"
    with multiple ids — the caller picks the most-recently-seen one rather than
    asking which. Never LLM-set — this only reads the deterministic binding back,
    RLS-scoped to the caller. An EXACT canonical-name match (case-insensitive) wins
    over looser substring rows when any exist, so naming a device precisely never
    drags in unrelated substring hits."""
    rows = await entities.list_entities(ctx.session, name, None, 10)
    if not rows:
        return [], None, "none"
    exact = [r for r in rows if str(r["canonical_name"]).lower() == name.lower()]
    used = exact or rows
    subs: list[str] = []
    for r in used:
        for sid in await devices.device_subjects_for_entity(ctx.session, str(r["id"])):
            if sid not in subs:
                subs.append(sid)
    if not subs:
        return [], str(used[0]["canonical_name"]), "unlinked"
    return subs, str(used[0]["canonical_name"]), "ok"


async def _self_subjects(devices: SqlDeviceRepo, ctx: ToolContext) -> list[str]:
    """The owner's own device subjects, resolved DETERMINISTICALLY by the "Me"
    hard-link → operated devices (never a fuzzy "Me" substring search)."""
    return await devices.owner_device_subjects(ctx.session)


async def _pick_latest(locations: SqlLocationRepo, ctx: ToolContext, subs: list[str]) -> str | None:
    """When a person/owner has several devices, answer for the ACTIVE one: the
    subject whose latest fix is newest. One subject returns itself; none returns
    None; a subject with no recorded activity sorts lowest (None last)."""
    if not subs:
        return None
    if len(subs) == 1:
        return subs[0]
    activity = await locations.device_activity(ctx.session)
    floor = datetime.min.replace(tzinfo=UTC)
    return max(subs, key=lambda s: (a.last_seen if (a := activity.get(s)) else None) or floor)


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


def _window(
    arguments: dict, *, default_hours: float, max_hours: float, now: datetime
) -> tuple[datetime, datetime]:
    """Resolve `[since, until)` from the tool args: an explicit `hours` lookback
    (clamped to `max_hours`), defaulting to `default_hours`. `until` is now. The
    repo clamps again — this clamps first so the prose names the real window."""
    hours = arguments.get("hours")
    span = default_hours if not isinstance(hours, (int, float)) else float(hours)
    span = max(0.1, min(max_hours, span))
    return now - timedelta(hours=span), now


def _freshness(trail_data: dict, *, now: datetime) -> dict:
    """The map's freshness pill: how old the newest fix is, and whether it crosses
    the stale horizon (so an old trail is flagged "last known", never "here now").
    `offline` marks no fix at all. Times/labels only — never a coordinate."""
    legs = trail_data["legs"]
    if not legs:
        return {"freshness": "offline", "fresh_label": "no recent fix"}
    last = _parse_iso(legs[-1]["ended_at"])
    if last is None:
        return {"freshness": "offline", "fresh_label": "no recent fix"}
    age = (now - last).total_seconds()
    mins = round(age / 60)
    label = f"last fix {mins} min ago" if mins < 90 else f"last fix {round(age / 3600)} h ago"
    return {"freshness": "stale" if age > _STALE_GAP_SECONDS else "fresh", "fresh_label": label}


def _trail_view(trail_data: dict, refs: list | None = None) -> ViewPayload:
    """The `location_map` view payload — render-only coordinates live in
    `trail_data["legs"][*]["points"]`, nothing else."""
    return ViewPayload(view="location_map", surface="inline", data=trail_data, refs=refs or [])


def _format_trail_summary(label: str, trail_data: dict, tz: str | None) -> str:
    """The prose lead for `location_history` — names/times/distances only, the gap
    explained in words BEFORE the map (Option B answer-first). Never a coordinate."""
    legs = trail_data["legs"]
    if not legs:
        return f"{label} has no recorded location in that window."
    km = trail_data["total_distance_m"] / 1000
    first_start = _when(_parse_iso(legs[0]["started_at"]), tz)
    last_end = _when(_parse_iso(legs[-1]["ended_at"]), tz)
    head = (
        f"{label} covered ~{km:.1f} km between {first_start} and {last_end}"
        f" ({trail_data['total_fixes']} fixes)."
    )
    gaps = trail_data["gaps"]
    if gaps:
        n = len(gaps)
        longest = max(g["seconds"] for g in gaps) / 3600
        leg_word = "legs" if len(legs) > 1 else "leg"
        head += (
            f" There {'is' if n == 1 else 'are'} {n} gap{'' if n == 1 else 's'}"
            f" (longest ~{longest:.0f}h — no signal), so the trail is {len(legs)}"
            f" separate {leg_word}, never drawn across the gap."
        )
    return head


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _aggregate(fixes: list[FixPoint]) -> dict[str, Any]:
    """The numeric answer `location_query` reports: how many fixes fell inside the
    place + window, the battery range (min and last), and the mean accuracy — all
    derived from the fixes, never a coordinate."""
    batteries = [f.battery_pct for f in fixes if f.battery_pct is not None]
    accuracies = [f.accuracy_m for f in fixes if f.accuracy_m is not None]
    return {
        "count": len(fixes),
        "battery_min": min(batteries) if batteries else None,
        "battery_last": fixes[-1].battery_pct if fixes else None,
        "mean_accuracy_m": round(sum(accuracies) / len(accuracies), 1) if accuracies else None,
    }


def _format_query(
    place_name: str, agg: dict[str, Any], tz: str | None, fixes: list[FixPoint]
) -> str:
    """The aggregate prose for `location_query` — counts/battery/accuracy + place
    name, no coordinate. Empty result reads as "no fixes there in that window"."""
    if agg["count"] == 0:
        return f"No fixes recorded at {place_name} in that window."
    parts = [f"{agg['count']} fixes at {place_name}"]
    if agg["battery_last"] is not None:
        low = (
            f" (low {agg['battery_min']}%)"
            if agg["battery_min"] is not None and agg["battery_min"] != agg["battery_last"]
            else ""
        )
        parts.append(f"battery last {agg['battery_last']}%{low}")
    if agg["mean_accuracy_m"] is not None:
        parts.append(f"~{agg['mean_accuracy_m']} m mean accuracy")
    last_when = _when(fixes[-1].captured_at, tz)
    return f"{'; '.join(parts)}. Last fix {last_when}."


def _match_fence(places: list[PlaceGeofence], name: str) -> PlaceGeofence | None:
    """The saved circular fence whose name matches `name`: an exact (case-insensitive)
    name wins, else the first substring hit. Polygon-only fences are skipped (the
    spatial query needs a center+radius)."""
    circles = [p for p in places if p.center is not None and p.radius_m is not None]
    lowered = name.lower()
    exact = [p for p in circles if p.name.lower() == lowered]
    if exact:
        return exact[0]
    loose = [p for p in circles if lowered in p.name.lower()]
    return loose[0] if loose else None


def build_location_handlers(
    locations: SqlLocationRepo,
    devices: SqlDeviceRepo,
    entities: EntityResolver,
    geocoder: GeocodeClient | None = None,
) -> dict[str, ToolHandler]:
    """The location read tools, each bound with the registration-time full-owner
    wrapper so a narrowed/`owner_scoped`/non-owner session is refused BEFORE any
    location read runs — the un-forgettable primary barrier the weak-table reads
    (`app.events`, `place_geofence`) depend on."""

    async def where_is_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        name = str(arguments.get("subject", "")).strip()
        if not name:
            return ToolOutput("where_is needs a subject (a person or device name).")
        subs, label, status = await _resolve_subject(devices, entities, ctx, name)
        if status == "none":
            return ToolOutput(f"No person or device named '{name}' is in scope.")
        if status == "unlinked":
            return ToolOutput(f"'{label or name}' has no linked device, so I can't locate it.")
        sid = await _pick_latest(locations, ctx, subs)
        return await _answer_where(label or name, sid, ctx)

    async def where_was_i_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        subs = await _self_subjects(devices, ctx)
        if not subs:
            return ToolOutput("Your own device isn't linked yet, so I can't locate you.")
        sid = await _pick_latest(locations, ctx, subs)
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
        # Center on the owner's own device (resolved deterministically to a device
        # subject; the position never surfaces — `nearby` reads it inside the query
        # and returns names/distances). With several owned devices, the active one.
        subs = await _self_subjects(devices, ctx)
        if not subs:
            return ToolOutput("Your own device isn't linked yet, so I can't find nearby places.")
        sid = await _pick_latest(locations, ctx, subs)
        places = await locations.nearby(ctx.session, subject_id=sid, radius_m=radius, limit=limit)
        return ToolOutput(_format_nearby(places))

    async def location_history_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        # Resolve the subject (a named person/device, or self) to its active device,
        # exactly as where_is does — fail-closed if unlinked.
        name = str(arguments.get("subject", "")).strip()
        now = datetime.now(UTC)
        if name and name.lower() not in ("me", "i", "you"):
            subs, label, status = await _resolve_subject(devices, entities, ctx, name)
            if status == "none":
                return ToolOutput(f"No person or device named '{name}' is in scope.")
            if status == "unlinked":
                return ToolOutput(f"'{label or name}' has no linked device, so I can't map it.")
            label = label or name
        else:
            subs = await _self_subjects(devices, ctx)
            label = "You"
            if not subs:
                return ToolOutput("Your own device isn't linked yet, so I can't map your history.")
        sid = await _pick_latest(locations, ctx, subs)
        if sid is None:
            return ToolOutput(f"'{label}' has no linked device, so I can't map it.")
        since, until = _window(
            arguments, default_hours=_HISTORY_DEFAULT_HOURS, max_hours=_HISTORY_MAX_HOURS, now=now
        )
        fixes = await locations.fixes_within(
            ctx.session, subject_id=sid, since=since, until=until, limit=_HISTORY_FIX_LIMIT
        )
        trail = build_trail(fixes)
        data = trail_view_data(trail, timezone=ctx.timezone)
        summary = _format_trail_summary(label, data, ctx.timezone)
        if trail.is_empty:
            # No fixes → answer in prose, no empty map (the view would draw nothing).
            return ToolOutput(summary)
        data.update(_freshness(data, now=now))
        return ToolOutput(summary, view=_trail_view(data))

    async def location_query_tool(arguments: dict, ctx: ToolContext) -> ToolOutput:
        place_q = str(arguments.get("place", "")).strip()
        if not place_q:
            return ToolOutput("location_query needs a place name.")
        now = datetime.now(UTC)
        since, until = _window(
            arguments, default_hours=_QUERY_DEFAULT_HOURS, max_hours=_QUERY_MAX_HOURS, now=now
        )
        radius = max(
            1.0, min(_QUERY_MAX_RADIUS_M, float(arguments.get("radius_m", _QUERY_DEFAULT_RADIUS_M)))
        )
        # Resolve the place to a center+radius: the saved-fence mirror first (a
        # named place the owner already keeps), falling back to an on-box
        # forward-geocode ONLY on a miss — the same full-owner path geocode_forward
        # is gated by (no Proposal; Photon is a local read on a no-egress network).
        places = await locations.places(ctx.session)
        fence = _match_fence(places, place_q)
        if fence is not None and fence.center is not None:
            center = fence.center
            fence_radius = fence.radius_m if fence.radius_m is not None else radius
            place_name = fence.name
        else:
            resolved = await _geocode_center(geocoder, place_q)
            if resolved is None:
                return ToolOutput(f'No saved place or address found for "{place_q}".')
            center, place_name = resolved
            fence_radius = radius
        sid = await _pick_latest(locations, ctx, await _self_subjects(devices, ctx))
        if sid is None:
            return ToolOutput("Your own device isn't linked yet, so I can't answer that.")
        fixes = await locations.fixes_within(
            ctx.session,
            subject_id=sid,
            since=since,
            until=until,
            center=center,
            radius_m=fence_radius,
            limit=_HISTORY_FIX_LIMIT,
        )
        agg = _aggregate(fixes)
        text = _format_query(place_name, agg, ctx.timezone, fixes)
        if agg["count"] == 0:
            return ToolOutput(text)
        trail = build_trail(fixes)
        data = trail_view_data(trail, timezone=ctx.timezone)
        data.update(_freshness(data, now=now))
        return ToolOutput(text, view=_trail_view(data))

    handlers: dict[str, ToolHandler] = {
        "where_is": where_is_tool,
        "where_was_i": where_was_i_tool,
        "device_status": device_status_tool,
        "home_status": home_status_tool,
        "nearby_now": nearby_now_tool,
        "location_history": location_history_tool,
        "location_query": location_query_tool,
    }
    return {name: _owner_only(handler) for name, handler in handlers.items()}


async def _geocode_center(
    geocoder: GeocodeClient | None, query: str
) -> tuple[tuple[float, float], str] | None:
    """On-box forward-geocode `query` to a (center, label), routed through the same
    local-read path `geocode_forward` uses (Photon, no egress, no Proposal). None on
    no geocoder, no hit, or an outage — the caller answers "no place found"."""
    if geocoder is None:
        return None
    try:
        results = await geocoder.forward(query, 1)
    except Exception as exc:  # noqa: BLE001 - a geocoder outage is a recoverable observation
        log.warning("location_query.geocode_failed", error=repr(exc))
        return None
    if not results:
        return None
    hit = results[0]
    return (hit.latitude, hit.longitude), hit.label


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
