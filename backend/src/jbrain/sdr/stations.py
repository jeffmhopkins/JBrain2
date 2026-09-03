"""The heard log read as a ROSTER — who is out there, rather than what scrolled past.

Binding spec: `docs/mocks/aprs/e-stations.html`. The shape the owner chose is stations
first, most recently heard first, with the type chips at the root narrowing *which
stations are listed* rather than which packets are — "show me who is putting out
weather" is a question about stations, not about frames.

**Why the aggregation is in SQL.** A packet channel produces ~3,000 rows a day on the
measured band and the roster is the screen's first paint. Pulling rows into Python to
group them would move a year of traffic over the wire to render fifteen lines; the
grouping, the counts and the window are all Postgres's job (the Runs-log precedent —
server-side filtering, clamped limits, bounded aggregates).

**It reads `origin_call`, never `source`.** That distinction is the entire feature:
measured on the owner's box, `source` held 5 values while 15 stations were transmitting,
because three quarters of the channel was one IGate relaying APRS-IS traffic and the
AX.25 source of a relayed frame names the RELAY. `sdr/classify.py` derives the true
sender; this groups on it.

**Everything returned is untrusted text off the air.** Callsigns included — a callsign is
plain bytes anyone can forge. Grouping by one is a convenience for reading a log, never a
statement about who someone is, and nothing here may gate an action.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session
from jbrain.sdr.classify import base_call, classify
from jbrain.sdr.explain import collect_definitions, explain, kind_label

# The four ranges from the mock's segmented control. Three NEST — "3 days" contains "1
# day" — and `old` is the complement of a week, the one exclusive bucket and the only one
# that can be empty while the others are full.
#
# A dict of fixed SQL fragments rather than a caller-supplied interval: the window id is
# validated by being a key, so nothing from a query string reaches the statement.
WINDOWS: dict[str, str] = {
    "1d": "heard_at >= now() - interval '1 day'",
    "3d": "heard_at >= now() - interval '3 days'",
    "1w": "heard_at >= now() - interval '7 days'",
    "old": "heard_at < now() - interval '7 days'",
}
DEFAULT_WINDOW = "1d"

# The three that NEST, all inside a week. `old` is the complement, so counting it means
# reading the archive — see the counts query.
_BOUNDED_WINDOWS = ("1d", "3d", "1w")

# Both lists are bounded. The roster's ceiling is generous because a station list IS the
# answer and truncating it silently would hide a station; the packet list's is not,
# because a screen showing a thousand frames is a screen nobody reads.
MAX_STATIONS = 300
MAX_PACKETS = 200
# How far back to look for a station's telemetry definitions. They are re-sent on a slow
# cycle, so a bounded scan of its recent messages finds them without reading its history.
_DEFINITION_SCAN = 40

# Only classified rows can be grouped by sender. Rows the sweep has not reached yet are
# counted separately and reported rather than dropped silently — a roster that is missing
# stations must say so (the same rule that makes a dead receiver look different from a
# quiet channel on this screen).
_CLASSIFIED = "origin_call IS NOT NULL"


def _window(window: str | None) -> tuple[str, str]:
    chosen = window if window in WINDOWS else DEFAULT_WINDOW
    return chosen, WINDOWS[chosen]


def _packet(row: Any, definitions: dict[str, list[str]]) -> dict[str, Any]:
    """One heard frame, decoded into what the card shows.

    Derived on READ rather than stored. The frame itself is in `raw` and never changes,
    so a better decoder improves every row that has ever been logged the moment it ships
    — the same reasoning that made the classifier's columns a cache in the first place.
    Only what the SQL has to group and filter on is persisted."""
    heard = classify(
        str(row["source"] or ""),
        str(row["info"] or ""),
        list(row["path"] or []),
        str(row["raw"] or ""),
    )
    said = explain(heard, definitions=definitions)
    return {
        # The row's identity. Without it the client keys on the array index, and every
        # poll that prepends a frame remounts the whole list — which is invisible until a
        # row holds state, and is already costing a screen-reader user their focus.
        "id": str(row["id"]),
        "heard_at": row["heard_at"].isoformat(),
        # TWO different questions, so two fields. `kind` is what the row's title says —
        # Telemetry, Status, Capabilities — and `bucket` is the coarse stored kind the
        # chips filter on. They differ on purpose: five chips are a control you can aim,
        # and "Other" as a row title tells the reader nothing. One field serving both
        # would force the row to lie or the chips to sprawl.
        "kind": kind_label(heard),
        "bucket": row["kind"],
        "gated": bool(row["gated"]),
        "direct": bool(row["heard_direct"]),
        # The EFFECTIVE info — the payload from inside a third-party wrapper, not the
        # wrapper itself.
        "text": heard.text,
        "summary": said.summary,
        "fields": [[f.name, f.value] for f in said.fields],
        "comment": said.comment,
        "symbol": said.symbol,
        "warnings": said.warnings,
        # As NUMBERS, so the client can work out how far away the station is without
        # parsing a display string back into floats. Null for a frame with no position.
        "lat": said.latitude,
        "lon": said.longitude,
        # The frame as heard, so the sentence above it can always be checked. This is the
        # only place the "gated via N4TDX" claim becomes verifiable, because the row
        # deliberately shows the inner payload rather than the wrapper.
        # Who put it on the air, when that is not who wrote it. The row says "relayed by
        # N4TDX" rather than repeating a callsign the header already carries.
        "relay": _relay(str(row["source"] or ""), heard.origin),
        # Direwolf's own 0-100 reading, or null where nothing was measured — a frame
        # logged before the level was captured, or one whose reading could not be
        # paired. Null is NOT zero: the screen must say "not measured", never "weak".
        "audio_level": row["audio_level"],
        "frame": {
            "source": str(row["source"] or ""),
            "destination": str(row["destination"] or ""),
            "path": list(row["path"] or []),
        },
    }


def _relay(source: str, origin: str) -> str | None:
    """Who put it on the air, when that is not who wrote it."""
    return source if source and source != origin else None


class StationsReader:
    """Reads the roster and one station's traffic, on the caller's RLS scope.

    A service holding the maker, like the other readers a route is handed — the scope
    arrives per call because it belongs to the request. `app.aprs_packets` is owner-only
    in Postgres, so a non-owner gets empty results from RLS rather than a check here
    (CLAUDE.md rule 3)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession]) -> None:
        self._maker = maker

    async def roster(
        self,
        ctx: SessionContext,
        *,
        window: str | None = DEFAULT_WINDOW,
        kinds: list[str] | None = None,
        mine: str | None = None,
        limit: int = MAX_STATIONS,
    ) -> dict[str, Any]:
        """Who has been heard in the window, most recently heard first."""
        chosen, predicate = _window(window)
        wanted = [k for k in (kinds or []) if k]
        bounded = max(1, min(int(limit), MAX_STATIONS))
        # The owner's own callsign, bare. Pinning happens HERE rather than only in the
        # client because the list is capped: over a year's archive the owner's station can
        # fall outside the 300 most recently heard, and a client that pins what it was
        # given cannot pin what it never received — losing exactly the station this
        # feature exists to surface.
        owner = base_call(mine or "") or None
        async with scoped_session(self._maker, ctx) as s:
            # Per-window PACKET counts for the segmented control, all in one pass so the
            # tabs can show what widening the range would actually reveal.
            #
            # BOUNDED TO THE LAST WEEK, which is what makes it affordable. Unbounded this
            # was the only statement here with no WHERE — a sequential scan plus a sort of
            # every row in the table for `count(DISTINCT origin_call)`, measured at 45 ms
            # over 40k rows. At the plan's own ~1.2M rows a year that is over a second and
            # ~140 MB of buffers PER POLL, every five seconds the tab is open, with the
            # DISTINCT sort spilling to disk past `work_mem`. The three nested ranges are
            # all inside a week, so nothing on the screen needed that scan.
            #
            # `old` is the complement of a week and a query bounded to the week cannot see
            # it. Counting it means reading everything the box has ever heard, so the tab
            # gets PRESENCE — an index answers that by stopping at the first row — and an
            # exact count only when the owner has actually opened that range.
            older_count = f"count(*) FILTER (WHERE {WINDOWS['old']})" if chosen == "old" else "NULL"
            counts_sql = (
                "SELECT"
                + ",".join(
                    f" count(*) FILTER (WHERE {WINDOWS[wid]}) AS w_{wid}"
                    for wid in _BOUNDED_WINDOWS
                )
                + f", count(*) FILTER (WHERE {predicate} AND origin_call IS NULL) AS unclassified"
                + f", count(DISTINCT origin_call) FILTER (WHERE {predicate}) AS stations"
                + f", EXISTS (SELECT 1 FROM app.aprs_packets WHERE {WINDOWS['old']}) AS has_older"
                + f", {older_count}::bigint AS older"
                + " FROM app.aprs_packets"
                # Reading `old` needs the archive in scope; every other range is a week.
                + ("" if chosen == "old" else f" WHERE {WINDOWS['1w']}")
            )
            counts = (await s.execute(text(counts_sql))).mappings().one()

            # Station counts per kind, over the window UNFILTERED by the chips — a chip
            # has to keep showing what selecting it would give while another is selected,
            # or the row rearranges itself as you use it.
            per_kind = (
                (
                    await s.execute(
                        text(
                            "SELECT kind, count(DISTINCT origin_call) AS stations"
                            f" FROM app.aprs_packets WHERE {_CLASSIFIED} AND {predicate}"
                            " GROUP BY kind"
                        )
                    )
                )
                .mappings()
                .all()
            )

            # The roster itself. `gated`/`source` are taken from each station's NEWEST
            # frame: how it reached us can change between packets, and the line reads
            # "heard on RF" or "gated via X" about the station as it is now.
            having = " HAVING bool_or(kind = ANY(:kinds))" if wanted else ""
            # `split_part` rather than a LIKE prefix: an owner who saved `KE8XYZ` means
            # every SSID of it and nothing else, and `KE8XYZZ` is a different station.
            pin = " split_part(origin_call, '-', 1) = :mine DESC," if owner else ""
            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT origin_call AS call, count(*) AS packets,"
                            " max(heard_at) AS last_heard_at,"
                            " array_agg(DISTINCT kind) AS kinds,"
                            " (array_agg(gated ORDER BY heard_at DESC))[1] AS gated,"
                            " (array_agg(source ORDER BY heard_at DESC))[1] AS via"
                            f" FROM app.aprs_packets WHERE {_CLASSIFIED} AND {predicate}"
                            f" GROUP BY origin_call{having}"
                            f" ORDER BY{pin} max(heard_at) DESC LIMIT :limit"
                        ),
                        {
                            "limit": bounded,
                            **({"kinds": wanted} if wanted else {}),
                            **({"mine": owner} if owner else {}),
                        },
                    )
                )
                .mappings()
                .all()
            )

        return {
            "window": chosen,
            "window_packets": {wid: counts[f"w_{wid}"] for wid in _BOUNDED_WINDOWS},
            # Whether the archive holds anything at all older than a week, and how much
            # when that is the range being read. Presence rather than a count, because
            # the count is a scan of everything the box has ever heard.
            "has_older": bool(counts["has_older"]),
            "older": counts["older"],
            # How many rows in this window the classifier has not reached. Normally zero;
            # non-zero means the sweep is still working and the roster is INCOMPLETE, and
            # the screen is expected to say so rather than quietly showing fewer stations.
            "unclassified": counts["unclassified"],
            "kind_stations": {r["kind"]: r["stations"] for r in per_kind if r["kind"]},
            # Stations in the window BEFORE the chips narrow it, so the header can read
            # "4 of 15" honestly.
            "stations_total": counts["stations"],
            # The list is capped, and a capped list that does not say so is a list that
            # hides a station. The header is expected to show this rather than printing a
            # confident total it did not return.
            "truncated": len(rows) >= bounded,
            "stations": [
                {
                    "call": r["call"],
                    "packets": r["packets"],
                    "last_heard_at": r["last_heard_at"].isoformat(),
                    "kinds": sorted(k for k in (r["kinds"] or []) if k),
                    "gated": bool(r["gated"]),
                    "relay": _relay(r["via"], r["call"]),
                }
                for r in rows
            ],
        }

    async def station(
        self,
        ctx: SessionContext,
        call: str,
        *,
        window: str | None = DEFAULT_WINDOW,
        kinds: list[str] | None = None,
        limit: int = MAX_PACKETS,
    ) -> dict[str, Any] | None:
        """One station's traffic. None when nothing has ever been heard from it.

        Matched on the EXACT `origin_call`, SSID included, because that is what the
        roster lists and counts. `N1MPR-C` and `N1MPR-S` are two services on one
        machine, and a detail view that silently merged them would contradict the count
        the owner just tapped."""
        chosen, predicate = _window(window)
        wanted = [k for k in (kinds or []) if k]
        bounded = max(1, min(int(limit), MAX_PACKETS))
        wanted_sql = " AND kind = ANY(:kinds)" if wanted else ""
        async with scoped_session(self._maker, ctx) as s:
            # All time, not the window: "142 packets in the log" is the fact that says
            # whether an empty window means quiet or means new.
            overall = (
                (
                    await s.execute(
                        text(
                            "SELECT count(*) AS packets, max(heard_at) AS last_heard_at,"
                            + ", ".join(
                                f" count(*) FILTER (WHERE {sql}) AS w_{wid}"
                                for wid, sql in WINDOWS.items()
                            )
                            + " FROM app.aprs_packets WHERE origin_call = :call"
                        ),
                        {"call": call},
                    )
                )
                .mappings()
                .one()
            )
            if not overall["packets"]:
                return None

            newest = (
                (
                    await s.execute(
                        text(
                            "SELECT gated, source FROM app.aprs_packets"
                            " WHERE origin_call = :call ORDER BY heard_at DESC LIMIT 1"
                        ),
                        {"call": call},
                    )
                )
                .mappings()
                .one()
            )

            # Packets per kind in the window, so the chip row offers only the types this
            # station actually sends. Five chips where a station only ever sends objects
            # is four dead controls.
            kind_rows = (
                (
                    await s.execute(
                        text(
                            "SELECT kind, count(*) AS packets FROM app.aprs_packets"
                            f" WHERE origin_call = :call AND {predicate} GROUP BY kind"
                        ),
                        {"call": call},
                    )
                )
                .mappings()
                .all()
            )

            # Telemetry means nothing without the station's own PARM/UNIT/EQNS messages,
            # and those are ordinary packets in the same table. One extra bounded read
            # per station detail, not per packet.
            definition_rows = (
                (
                    await s.execute(
                        text(
                            "SELECT source, path, info, raw FROM app.aprs_packets"
                            " WHERE origin_call = :call AND kind = 'Message'"
                            " ORDER BY heard_at DESC LIMIT :n"
                        ),
                        {"call": call, "n": _DEFINITION_SCAN},
                    )
                )
                .mappings()
                .all()
            )
            definitions = collect_definitions(
                [
                    classify(
                        str(d["source"] or ""),
                        str(d["info"] or ""),
                        list(d["path"] or []),
                        str(d["raw"] or ""),
                    )
                    for d in definition_rows
                ]
            ).get(call, {})

            rows = (
                (
                    await s.execute(
                        text(
                            "SELECT id, heard_at, source, destination, path, info, raw,"
                            " kind, gated, heard_direct, audio_level"
                            " FROM app.aprs_packets"
                            f" WHERE origin_call = :call AND {predicate}{wanted_sql}"
                            " ORDER BY heard_at DESC LIMIT :limit"
                        ),
                        {
                            "call": call,
                            "limit": bounded,
                            **({"kinds": wanted} if wanted else {}),
                        },
                    )
                )
                .mappings()
                .all()
            )

        return {
            "call": call,
            "packets_total": overall["packets"],
            "last_heard_at": overall["last_heard_at"].isoformat(),
            "gated": bool(newest["gated"]),
            "relay": _relay(newest["source"], call),
            "window": chosen,
            # Scoped to THIS station, unlike the roster's. A time tab showing the whole
            # band's traffic while you are looking at one station is a control that
            # answers a question you did not ask. Cheap at any table size because
            # `aprs_packets_origin_idx` bounds it to one station's rows.
            "window_packets": {wid: overall[f"w_{wid}"] for wid in _BOUNDED_WINDOWS},
            "has_older": overall["w_old"] > 0,
            "older": overall["w_old"],
            "kind_packets": {r["kind"]: r["packets"] for r in kind_rows if r["kind"]},
            "packets": [_packet(r, definitions) for r in rows],
        }
