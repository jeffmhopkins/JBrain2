"""The owner-only radio control API — what the omnibox tuner drives.

`GET /sdr/status` is the one the composer polls: its `listening` field is what lights
the radio icon, so "a session is running" and "the owner sees a radio icon" are the
same fact rather than two that can drift apart. Start/tune/stop move the radio;
`GET /sdr/audio` proxies the sidecar's live Opus so the browser fetches it
same-origin (the `radio` network is internal — the PWA has no route to the sidecar).

Every route is `OwnerDep`. The radio is a physical device on the owner's box: there
is no scoped-token or family case for it, so the surface is simply unreachable by
anything but the owner.

**No URL ever crosses this boundary.** Routes take a frequency and a mode, bounded
here as well as in the sidecar, and the sidecar's base URL is pinned in settings.
That is what keeps `stream.py`'s SSRF guard untouched rather than widened
(docs/plans/SDR_RADIO_PLAN.md §4.4).

This module touches no LLM (rule 1 n/a). It does touch storage and the database, but
only through the recordings library at the bottom of the file: everything about the live
radio is process state in the sidecar rather than a row, and only a RECORDING becomes
one. Those routes go through `BlobStore` for the audio (rule 2) and an RLS-scoped
session for the row (rule 3) — `app.sdr_recordings` is owner-only, and a recording's
blob is resolved from the row the caller could read, never from a sha in a URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import wave
from typing import Annotated, Any, cast
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from jbrain.api.deps import OwnerDep, SettingsDep
from jbrain.api.llm_settings import get_settings_store
from jbrain.api.notes import BlobStoreDep, SessionMakerDep, ctx_for
from jbrain.db.session import scoped_session
from jbrain.sdr import bands
from jbrain.sdr.aprslog import AprsReader
from jbrain.sdr.audio import cut_clip
from jbrain.sdr.classify import looks_like_station
from jbrain.sdr.command import MAX_FAILURES
from jbrain.sdr.health import session_for, shown
from jbrain.sdr.recorder import RecorderRefused, SdrRecorder
from jbrain.sdr.recordings import RECENT_DEFAULT, RECENT_MAX, RecordingsRepo
from jbrain.sdr.resolve import attached_serials, for_purpose, refusal
from jbrain.sdr.roles import GENERAL, Choice, Radio, conflicts
from jbrain.sdr.stations import PROVENANCE, WINDOWS, StationsReader
from jbrain.sdr.tuner import (
    MAX_MHZ,
    MIN_MHZ,
    TUNABLE_MIN_MHZ,
    nodes_in,
    out_of_range,
    viewable,
)
from jbrain.transcribe import WhisperCppClient

router = APIRouter(prefix="/sdr", tags=["sdr"])

# The demodulators rtl_fm offers. Bounded here as well as in the sidecar because a
# bound that lives only in the caller is not a bound — and the tuner range, which used
# to sit beside this, is now `jbrain.sdr.tuner` rather than a third copy of itself.
MODES = ("fm", "nfm", "wbfm", "am", "usb", "lsb")

#: The widest and narrowest filter the sidecar will accept, in Hz. Only a BOUND, not the
#: ladder: the ladder is per-mode and lives in `deploy/sdr/demod.py`, and duplicating it
#: here would be a second place to forget a rung. This exists so an absurd value is
#: refused as a 422 by the schema rather than travelling to the sidecar to come back a
#: 400 — the sidecar remains the authority on which exact widths are real.
MIN_BANDWIDTH_HZ = 1_000
MAX_BANDWIDTH_HZ = 200_000

# The lease purpose a logging session holds, and where APRS lives in North America
# when the owner does not say otherwise (APRS_CONTROL_PLAN.md §7 holds the private
# command frequency open).
APRS_PURPOSE = "aprs"
#: From the band table, which is the one place that knows where a service lives — it was
#: this literal in two files. Kept in MHz here because that is the unit the route's query
#: parameter takes, and converting at the boundary is better than a second constant.
APRS_DEFAULT_MHZ = bands.APRS_HZ / 1_000_000

# How far back a single caption may reach. Segments that pile up behind a busy whisper
# are transcribed TOGETHER rather than one at a time (see _Backlog), and this bounds how
# much audio one merged clip may carry: past it the oldest is given up, because a caption
# for something said half a minute ago is not a live caption any more.
CAPTION_BACKLOG_S = 24.0
# How long the caption stream waits with nothing to say before sending a comment to hold
# the socket open. Proxies close an idle event stream, and a quiet band is normal.
CAPTION_IDLE_S = 15.0

# Long enough for a slow tuner open, short enough that a wedged sidecar surfaces as
# an error rather than a hung composer.
_TIMEOUT = 30.0

#: How long a USB port reset may take before the api stops waiting. MEASURED: the ioctl
#: outran `_TIMEOUT` on a device that was in trouble — which is exactly the device anyone
#: resets, because the kernel waits on a port that may never answer and that wait IS the
#: operation rather than a hang. Generous, and still bounded.
RESET_TIMEOUT_S = 120.0

#: A radio the owner pointed at, or nothing at all. The pattern is narrow because this
#: value becomes an argv token in the sidecar (`-d <serial>`), and the sidecar bounds it
#: again for the same reason — a bound that lives only in the caller is not a bound once
#: there is a second caller.
SerialQuery = Annotated[str | None, Query(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

# How many command attempts the radio tab shows. Enough to see a burst of failures at a
# glance; the whole history stays in the table.
ATTEMPTS_SHOWN = 20


class SdrStatusOut(BaseModel):
    """`listening` is None when the radio is idle — which is exactly the condition
    the omnibox uses to decide whether the icon exists at all.

    `sessions` is every radio the box is holding. It exists because `listening` is now
    the ONE the omnibox should draw and prefers the tuner: with APRS on one dongle and
    the tuner on another, a screen reading `listening.purpose` to ask "is APRS logging?"
    is told no while it is running — which is how the APRS tab put up a contention panel
    and an inert button in front of the owner.

    Which one that is gets decided HERE, by `health.shown`, out of `sessions` — not by
    the sidecar, which sends its own `listening` meaning only the tuner's session."""

    available: bool
    listening: dict[str, Any] | None
    sessions: list[dict[str, Any]] = []
    #: The recording in progress, or None. Here rather than on a route of its own so the
    #: tape deck draws its elapsed time and running size off the 1 Hz poll the rest of
    #: the radio already uses — a second timer in the client would keep counting through
    #: a stop it had not heard about yet (SDR_RECORDING_PLAN.md §4).
    recording: dict[str, Any] | None = None


def _base(settings: Any) -> str:
    url = cast(str, settings.sdr_url or "")
    if not url:
        raise HTTPException(status_code=503, detail="No SDR on this box.")
    return url


async def _post(
    settings: Any, path: str, body: dict[str, Any], wait_s: float | None = None
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            base_url=_base(settings), timeout=wait_s or _TIMEOUT
        ) as client:
            resp = await client.post(path, json=body)
    except httpx.TimeoutException as slow:
        # NOT "it failed". MEASURED 2026-09-04: a `USBDEVFS_RESET` outran the ordinary
        # timeout and the owner got a 500 for an operation that had in fact HAPPENED —
        # the device left the bus. "The radio did not reset" would have been worse than
        # the traceback, because it is false. A timeout licenses "look again", nothing
        # more, and that is all this says.
        raise HTTPException(
            status_code=504,
            detail="The radio hasn't answered yet. Whatever was asked for may still be "
            "happening — check the radio list again in a moment rather than assuming it "
            "did not.",
        ) from slow
    if resp.status_code == 409:
        raise HTTPException(status_code=409, detail=_detail(resp, "The radio is busy."))
    if resp.status_code == 400:
        # The sidecar's 400s are sentences for an OPERATOR, not gateway faults: "a sweep
        # cannot go below 24 MHz", "the radio did not start: No matching devices found".
        # Wrapping them in a 502 buried the one thing the owner can act on behind
        # "sdr sidecar:" and a status that reads as the box being broken.
        raise HTTPException(status_code=400, detail=_detail(resp, "The radio refused that."))
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502, detail=f"sdr sidecar: {_detail(resp, resp.text[:300])}"
        )
    return cast(dict[str, Any], resp.json())


async def _usb_scan(request: Request, settings: Any) -> dict[str, Any]:
    """The supervisor's raw `/usb` payload, for the one caller that needs more than the
    serials — a reset needs the device node beside them."""
    client = request.app.state.supervisor_client
    try:
        resp = await client.get(
            "/usb", headers={"Authorization": f"Bearer {settings.supervisor_token}"}
        )
        resp.raise_for_status()
        return cast(dict[str, Any], resp.json())
    except (httpx.HTTPError, ValueError, AttributeError, KeyError):
        return {}


async def _attached(request: Request, settings: Any) -> list[str] | None:
    """What the scan can see, or None when it could not. See `sdr.resolve`."""
    return await attached_serials(request.app.state.supervisor_client, settings.supervisor_token)


async def _radio_for(
    request: Request, settings: Any, owner: Any, want: str, serial: str | None = None
) -> Choice:
    """Which radio `want` should open. The rule and the plumbing are shared with the
    debug console and jerv's tools, because a rule enforced at one of three doors is
    not enforced.

    `serial` is the owner pointing at a radio from a screen where the radio is the
    object. It changes the question from "which one" to "may that one" — see
    `roles.named` — and is honoured rather than treated as a hint."""
    return await for_purpose(
        request.app.state.supervisor_client,
        settings.supervisor_token,
        get_settings_store(request),
        ctx_for(owner),
        want,
        settings.sdr_url,
        serial,
    )


def _refuse(choice: Choice) -> None:
    """Turn an owner-fixable choice into a 409 naming the radio and the reason.

    A dedicated radio that is unplugged is not "the radio is busy" — it is one specific
    dongle, called what the owner called it, that has to go back in. The generic message
    would send them hunting for a session that does not exist."""
    detail = refusal(choice)
    if detail is not None:
        raise HTTPException(status_code=409, detail=detail)


def _detail(resp: httpx.Response, fallback: str) -> str:
    try:
        return cast(str, resp.json().get("detail") or fallback)
    except ValueError:
        return fallback


async def status_of(settings: Any, recording: dict[str, Any] | None = None) -> SdrStatusOut:
    """What the radio is doing, read from the sidecar's `/healthz`.

    Split out of the route so the owner debug console can be shown EXACTLY what the
    composer icon is showing, rather than a second answer to the same question. B7 moved
    that decision here to have one of them; a debug twin that re-derived it would put
    two back (CLAUDE.md #10 — the owner has no terminal, and a console that disagrees
    with their screen is worse than no console).

    `recording` is passed IN rather than read here, because the recorder lives on the
    app rather than in the sidecar and this function is deliberately reachable without
    one (the debug twin, and every test that has only a fake sidecar)."""
    if not settings.sdr_url:
        return SdrStatusOut(available=False, listening=None, recording=recording)
    try:
        async with httpx.AsyncClient(base_url=settings.sdr_url, timeout=5.0) as client:
            resp = await client.get("/healthz")
        health = cast(dict[str, Any], resp.json())
    except (httpx.HTTPError, ValueError):
        # The sidecar is configured but unreachable (starting, crashed). Idle is the
        # honest answer — the icon stays dark rather than lit over a dead radio.
        # A recording in progress is still reported: the recorder is the api's own state,
        # and hiding it would leave the tape deck running with nothing to stop.
        return SdrStatusOut(available=False, listening=None, recording=recording)
    live = health.get("sessions")
    one = health.get("listening")
    # An OLDER sidecar sends no `sessions`; it can hold only one thing, so `listening` IS
    # the list. Same fallback as `health.session_for`, for the seconds during an update
    # when the two containers are different builds.
    rows = live if isinstance(live, list) else ([one] if isinstance(one, dict) else [])
    # Each row is checked too, because `SdrStatusOut.sessions` is typed and a body this
    # route cannot model would otherwise 500 — darkening the composer icon over a radio
    # that is working. Dropping the row it could not read is the smaller loss.
    sessions = [s for s in rows if isinstance(s, dict)]
    return SdrStatusOut(
        available=True,
        # DECIDED HERE (B7). The sidecar used to rank the purposes and send its answer;
        # that is presentation policy in the radio process, and this route reshapes
        # `SessionInfo` anyway. Its `listening` is now what its name says — the tuner's
        # session — and reaches this line only through the old-build fallback above.
        listening=shown(sessions),
        sessions=sessions,
        recording=recording,
    )


@router.get("/status")
async def status(request: Request, settings: SettingsDep, _owner: OwnerDep) -> SdrStatusOut:
    """What the radio is doing. Answers `available: false` on a box with no radio
    rather than erroring, so the composer can simply never show the icon."""
    return await status_of(settings, recording_now(request))


def _tunable(frequency_mhz: float) -> None:
    """Refuse a frequency the radio would answer with a DIFFERENT one.

    `Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)` bounds the ENDS, and the reachable range
    has a hole in it: 14.4-24 MHz passes both bounds and comes back as `28.8 MHz − f`,
    because below 24 MHz the sidecar tunes with `-E direct2` and direct sampling folds
    the second Nyquist zone onto the first. Ask for 18.1 and hear 10.7 — with audio, a
    level meter and a caption, all of them confident (SDR_IQ_SPECTRUM_PLAN §8).

    A sentence and a 400, not a 422 with a validation blob: this is the surface an
    owner with no terminal has (CLAUDE.md #10), and the fact they need is which
    frequency they would actually have received."""
    refusal = out_of_range(frequency_mhz)
    if refusal:
        raise HTTPException(status_code=400, detail=refusal[0].upper() + refusal[1:])


@router.post("/listen")
async def listen(
    request: Request,
    settings: SettingsDep,
    _owner: OwnerDep,
    frequency_mhz: Annotated[float, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)],
    mode: Annotated[str, Query(pattern=f"^({'|'.join(MODES)})$")] = "wbfm",
    gain: Annotated[str | None, Query(max_length=16)] = None,
    serial: SerialQuery = None,
    bandwidth_hz: Annotated[int | None, Query(ge=MIN_BANDWIDTH_HZ, le=MAX_BANDWIDTH_HZ)] = None,
) -> dict[str, Any]:
    """Take a radio and start listening. 409 when it is already held.

    Naming none takes a GENERAL radio: one the owner reserved for a service is not one
    the tuner may borrow while that service happens to be idle. Naming one is the
    launcher asking for THAT radio, and it is refused by name rather than quietly
    served from another."""
    _tunable(frequency_mhz)
    chosen = await _radio_for(request, settings, _owner, GENERAL, serial)
    _refuse(chosen)
    return await _post(
        settings,
        "/listen/start",
        {
            "frequency_hz": int(round(frequency_mhz * 1_000_000)),
            "mode": mode,
            "gain": gain,
            "serial": chosen.serial,
            # Omitted rather than sent as null, so the sidecar's own default applies and
            # this route need not know what it is.
            **({"bandwidth_hz": bandwidth_hz} if bandwidth_hz is not None else {}),
        },
    )


@router.get("/packets")
async def packets(
    request: Request,
    settings: SettingsDep,
    owner: OwnerDep,
    maker: SessionMakerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """The heard log, newest first, plus whether anything is receiving right now.

    `logging` is what separates a quiet channel from a dead one, and the tab shows the
    two differently — a watch that silently died is worse than no watch
    (docs/mocks/aprs/README.md). Rows carry text transmitted by strangers; the client
    renders them as untrusted."""
    base = _base(settings)
    health = await _health(base)
    session = session_for(health, APRS_PURPOSE)
    rows = await AprsReader(maker).recent(ctx_for(owner), limit=limit)
    return {
        # `logging` answers "is something receiving"; `reachable` answers "do we know".
        # Collapsing the two would make an unreachable sidecar read as a switched-off
        # one — a dead receiver looking exactly like a quiet channel, which is the
        # confusion this whole surface exists to prevent.
        "reachable": health is not None,
        "logging": bool(session),
        "frequency_hz": session.get("frequency_hz") if session else None,
        # A third way this surface can lie, alongside `reachable` and `logging`: the radio
        # decodes, the drain runs, and every row fails to store. `_store` swallows its
        # errors so one bad frame cannot end the log, which means a broken INSERT — new
        # code against an un-migrated schema is the real case — stops the log with no
        # symptom at all. Non-zero here means packets are being HEARD and LOST.
        "store_failures": getattr(
            getattr(request.app.state, "aprs_logger", None), "store_failures", 0
        ),
        "packets": [
            {
                "heard_at": row["heard_at"].isoformat(),
                "frequency_hz": row["frequency_hz"],
                "source": row["source"],
                "destination": row["destination"],
                "path": row["path"],
                "info": row["info"],
            }
            for row in rows
        ],
    }


@router.get("/commands")
async def commands(
    owner: OwnerDep,
    maker: SessionMakerDep,
) -> dict[str, Any]:
    """The owner's radio commands and what has been tried against them.

    The APRS tab's read-only summary (docs/mocks/aprs/a-launcher-shape.html, the
    "Automations · radio" section, and c-single-dongle's "armed but deaf" block). Two
    facts the owner cannot get anywhere else on this screen:

    **What is armed**, so "armed while nothing is receiving" is visible as the pair it
    is. Arming a command and enabling the receiver are separate switches on purpose,
    and a task that says armed while the radio is deaf is the same lie a signal meter
    on a dead channel tells.

    **What has been TRIED.** A refusal is the row worth having — three attempts against
    `GATE` from an unknown station last Tuesday is a fact the owner must be able to find
    afterwards, and a push notification does not keep. It is read here rather than
    pushed only (APRS_CONTROL_PLAN.md P4: "every attempt is visible").

    Editing lives in Tasks; this is a view."""
    ctx = ctx_for(owner)
    async with scoped_session(maker, ctx) as session:
        armed = (
            (
                await session.execute(
                    text(
                        "SELECT id, name, enabled, command_word, command_callsign,"
                        " command_days, command_from, command_until, command_once,"
                        " command_failures,"
                        " command_last_at"
                        " FROM app.tasks WHERE schedule_kind = 'on_command'"
                        " ORDER BY command_word"
                    )
                )
            )
            .mappings()
            .all()
        )
        tried = (
            (
                await session.execute(
                    text(
                        "SELECT heard_at, source, word, accepted, reason"
                        " FROM app.command_attempts ORDER BY heard_at DESC LIMIT :n"
                    ),
                    {"n": ATTEMPTS_SHOWN},
                )
            )
            .mappings()
            .all()
        )
    return {
        "commands": [
            {
                "id": str(row["id"]),
                "name": row["name"],
                "enabled": row["enabled"],
                "word": row["command_word"],
                "callsign": row["command_callsign"],
                "days": list(row["command_days"] or []),
                "from": row["command_from"],
                "until": row["command_until"],
                "once": row["command_once"],
                "locked": int(row["command_failures"] or 0) >= MAX_FAILURES,
                "last_at": row["command_last_at"].isoformat() if row["command_last_at"] else None,
            }
            for row in armed
        ],
        "attempts": [
            {
                "heard_at": row["heard_at"].isoformat(),
                "source": row["source"],
                "word": row["word"],
                "accepted": row["accepted"],
                "reason": row["reason"],
            }
            for row in tried
        ],
    }


KINDS = ("Position", "Message", "Weather", "Object", "Other")
_WINDOW_IDS = "|".join(WINDOWS)


def _kinds(raw: str | None) -> list[str]:
    """The chip selection, as a whitelist rather than as text.

    Comma-separated because that is what a query string does naturally, and matched
    against the five known buckets so nothing from the URL reaches the query as a value
    the database has to be trusted to treat safely. An unknown chip is DROPPED rather
    than 422'd: a stale PWA asking for a kind we have since renamed should show the
    unfiltered roster, not an error page."""
    if not raw:
        return []
    return [k for k in (part.strip() for part in raw.split(",")) if k in KINDS]


def _provenance(raw: str | None) -> list[str]:
    """The gated/direct chips, on exactly the same terms as `_kinds`.

    Whitelisted against the three states `stations.PROVENANCE` defines — and here it is
    not only hygiene: these ids become SQL fragments rather than a bound parameter, so
    the key check IS the guard (`StationsReader.roster` repeats it for the same reason).
    An unknown one is dropped, so a stale PWA gets the unfiltered roster."""
    if not raw:
        return []
    return [p for p in (part.strip() for part in raw.split(",")) if p in PROVENANCE]


@router.get("/stations")
async def stations(
    owner: OwnerDep,
    maker: SessionMakerDep,
    window: Annotated[str, Query(pattern=f"^({_WINDOW_IDS})$")] = "1d",
    kinds: Annotated[str | None, Query(max_length=120)] = None,
    provenance: Annotated[str | None, Query(max_length=60)] = None,
    mine: Annotated[str | None, Query(max_length=16)] = None,
) -> dict[str, Any]:
    """Who has been heard, most recently heard first (`docs/mocks/aprs/e-stations.html`).

    Grouped on the TRUE sender rather than the AX.25 source, which is the whole point:
    measured on this box, `source` held 5 values while 15 stations were transmitting.

    The `kinds` chips narrow the ROSTER — stations that send that kind at all — not the
    packets. `kind_stations` therefore counts stations, because a chip reading 27 beside
    a list of three stations would be lying about what pressing it does.

    `provenance` narrows it the same way, on how the frames ARRIVED: `direct`, `gated`
    and `rf` are exclusive per packet but not per station, so a chip means "sent at least
    one frame that arrived this way" and a station heard both ways answers to both. Its
    counts (`provenance_stations`) therefore overlap and may sum past the total.

    `mine` pins the owner's own stations to the top BEFORE the list is capped. The client
    knows the callsign already (it is in Settings), so it travels as a parameter rather
    than costing a settings read on every poll — and it is a sort key on the owner's own
    request, not a permission."""
    return await StationsReader(maker).roster(
        ctx_for(owner),
        window=window,
        kinds=_kinds(kinds),
        provenance=_provenance(provenance),
        mine=mine,
    )


@router.get("/stations/{call}")
async def station(
    owner: OwnerDep,
    maker: SessionMakerDep,
    call: Annotated[str, Path(max_length=16)],
    window: Annotated[str, Query(pattern=f"^({_WINDOW_IDS})$")] = "1d",
    kinds: Annotated[str | None, Query(max_length=120)] = None,
) -> dict[str, Any]:
    """One station's traffic, newest first.

    404 when nothing has ever been heard from that callsign — including when the sweep
    has not classified it yet, which is why the roster reports `unclassified`."""
    wanted = call.strip().upper()
    # A callsign-shaped path or nothing. `Path(max_length=16)` alone lets a NUL through,
    # which Postgres refuses in a text column — the query raises and the owner gets a 500
    # from what is, at worst, a typo. Same guard the classifier files rows under, so a
    # station that made it into the roster can always be opened.
    if not looks_like_station(wanted):
        raise HTTPException(status_code=404, detail="nothing heard from that station")
    found = await StationsReader(maker).station(
        ctx_for(owner), wanted, window=window, kinds=_kinds(kinds)
    )
    if found is None:
        raise HTTPException(status_code=404, detail="nothing heard from that station")
    return found


class RadioOut(BaseModel):
    serial: str
    name: str
    description: str
    role: str
    attached: bool
    """Whether the scan can see it right now. A described radio that is unplugged still
    appears — that is how its service explains what it is waiting for."""


class RadiosOut(BaseModel):
    radios: list[RadioOut]
    conflicts: dict[str, list[str]]
    """Service -> serials, for services with more than one radio dedicated to them. The
    settings screen shows this on the cards, before anything tries to run."""
    scan_ok: bool
    """False when the USB scan could not be reached, so `attached` is unknown rather
    than false. The screen must not draw every radio as unplugged over a proxy hiccup."""


class RadioIn(BaseModel):
    name: Annotated[str, Field(max_length=200)] = ""
    description: Annotated[str, Field(max_length=600)] = ""
    # A service id, not prose. The store truncates too; this refuses rather than
    # silently shortening, because a role the caller did not ask for is a wrong answer
    # where a shortened description is only a shorter one.
    role: Annotated[str, Field(max_length=40)] = GENERAL


class ChannelOut(BaseModel):
    hz: int
    name: str
    note: str = ""


class SectionOut(BaseModel):
    """One band section, with the derived facts already worked out.

    The client never recomputes any of this. `hops`, `surveyable`, `bin_hz` and the
    image edges follow from the frequency and the hardware, and a screen that derived
    them itself would be a second implementation of the physics — free to disagree with
    the radio that actually runs. Everything here is either stored in the table or a
    property of it."""

    id: str
    band: str
    name: str
    start_hz: int
    stop_hz: int
    mode: str
    step_hz: int
    channel_hz: int
    note: str
    live: str
    continuous: bool
    sweep_seconds: int
    span_hz: int
    centre_hz: int
    hops: int
    duty: float
    """Roughly the fraction of each interval any one bin is actually observed. It is the
    honesty a live view turns on: past one hop a burst can fall between visits and leave
    no trace, and a picture that hid that would look identical to one that could not
    miss anything. Sent rather than derived for the reason the rest of this is."""
    surveyable: bool
    """False for every HF section: rtl_power hardcodes the ADC branch this hardware does
    not wire, so those bands can be SURVEYED nowhere below 24 MHz. It is not the same
    question as whether they can be watched live — that one is `live`."""
    direct_sampling: bool
    """True below 24 MHz, where the tuner is bypassed — and therefore where there is no
    gain control at all, because the tuner is powered down."""
    sample_rate_hz: int
    """What the radio digitises to draw this section in one hop, and 0 on the tiers
    rtl_power still serves. The picture is this WIDE, not as wide as the section: the
    frame is the passband."""
    fft_bins: int
    """Bins in one live row — derived from the rate so `bin_hz` divides exactly, and
    deliberately not a fixed 4096."""
    bin_hz: int | float
    """`sample_rate_hz / fft_bins`. A float would mean the pairing does not divide,
    which `validate()` refuses — it is typed honestly rather than rounded into
    agreement, because the PWA compares it exactly."""
    image_start_hz: int
    """The low edge of the band that folds onto this one, reversed, and 0 where none
    does. Everything on the direct path arrives summed with `28.8 MHz − f`; no software
    separates the two contributions, so the only honest thing is to say which band is in
    there. Replaces `mirrored`, which was False for all ten HF rows while all ten in
    fact carry an image."""
    image_stop_hz: int
    """The high edge of that image, mapped from this section's own START — the fold
    reverses, so the two edges cross over."""
    channels: list[ChannelOut]
    channel_plan: bool
    """Whether `channels` is the whole legal set here rather than a few landmarks.

    It is what lets the tuner's ± count in CHANNELS: on CB there are exactly forty
    places a signal may be and counting beats arithmetic, while airband's channels are
    allocated per facility and a ± that walked only between the listed ones would refuse
    most of the band."""


class BandsOut(BaseModel):
    region: str
    """Which band plan these rows encode. Channel spacings and sub-band boundaries are
    regional, so a table read against the wrong plan mis-tunes rather than erroring."""
    sections: list[SectionOut]
    tuner_min_hz: int
    tuner_max_hz: int
    direct_max_hz: int


def _section_out(section: bands.Section) -> SectionOut:
    return SectionOut(
        id=section.id,
        band=section.band,
        name=section.name,
        start_hz=section.start_hz,
        stop_hz=section.stop_hz,
        mode=section.mode,
        step_hz=section.step_hz,
        channel_hz=section.channel_hz,
        note=section.note,
        live=section.live,
        continuous=section.continuous,
        sweep_seconds=section.sweep_seconds,
        span_hz=section.span_hz,
        centre_hz=section.centre_hz,
        hops=section.hops,
        duty=section.duty,
        surveyable=section.surveyable,
        direct_sampling=section.direct_sampling,
        sample_rate_hz=section.sample_rate_hz,
        fft_bins=section.fft_bins,
        bin_hz=section.live_bin_hz,
        image_start_hz=section.image_start_hz,
        image_stop_hz=section.image_stop_hz,
        channels=[ChannelOut(hz=c.hz, name=c.name, note=c.note) for c in section.channels],
        channel_plan=section.channel_plan,
    )


@router.get("/bands")
async def band_sections(_owner: OwnerDep) -> BandsOut:
    """The band table: what is worth listening to, and how to hear each of it.

    Static, and deliberately not gated on a radio being present. The picker is how the
    owner learns what the box CAN do, which matters most on a box where the answer is
    currently "nothing is plugged in" — a list that vanishes with the hardware teaches
    nobody anything.

    Owner-only like the rest of this router: it is not secret, but every other route
    here is, and a lone public one is the kind of asymmetry nobody revisits."""
    return BandsOut(
        region=bands.REGION,
        sections=[_section_out(s) for s in bands.SECTIONS],
        tuner_min_hz=int(MIN_MHZ * 1_000_000),
        tuner_max_hz=int(MAX_MHZ * 1_000_000),
        direct_max_hz=bands.DIRECT_SAMPLING_MAX_HZ,
    )


@router.get("/radios")
async def radios(request: Request, settings: SettingsDep, owner: OwnerDep) -> RadiosOut:
    """Every radio: described, attached, or both.

    The union rather than either alone. A dongle plugged in but never described has to
    appear or a new one is invisible; a dongle described but unplugged has to appear or
    the service waiting for it cannot say what it is waiting for."""
    seen = await _attached(request, settings)
    attached = seen or []
    stored = await get_settings_store(request).sdr_radios(ctx_for(owner))
    known = {**{s: Radio(serial=s) for s in attached}, **stored}
    return RadiosOut(
        radios=[
            RadioOut(
                serial=radio.serial,
                name=radio.name,
                description=radio.description,
                role=radio.role,
                attached=radio.serial in attached,
            )
            for radio in sorted(known.values(), key=lambda r: r.serial)
        ],
        conflicts=conflicts(stored),
        # Whether the scan ANSWERED, not whether it found anything. Deriving this from
        # the device count told an owner who had simply unplugged both dongles that the
        # scan was unreachable, and sent them debugging a proxy that was fine.
        scan_ok=seen is not None,
    )


@router.put("/radios/{serial}")
async def describe_radio(
    request: Request,
    settings: SettingsDep,
    owner: OwnerDep,
    body: RadioIn,
    serial: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
) -> RadiosOut:
    """Name a radio, say what it is plugged into, and set what it is for.

    The serial pattern is narrow because this value becomes `-d serial=...` on an
    `rtl_fm` argv. It is not shell-interpolated (the sidecar uses a fixed argv, no
    shell), so this is a second lock rather than the only one — but a settings key that
    reaches a subprocess argument should not accept arbitrary text either way."""
    await get_settings_store(request).set_sdr_radio(
        ctx_for(owner),
        serial,
        name=body.name,
        description=body.description,
        role=body.role,
    )
    return await radios(request, settings, owner)


@router.post("/radios/{serial}/reset")
async def reset_radio(
    request: Request,
    settings: SettingsDep,
    _owner: OwnerDep,
    serial: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
) -> dict[str, Any]:
    """Re-enumerate one dongle — the software equivalent of unplugging it.

    **This exists because the owner has no terminal** (CLAUDE.md #10). An RTL-SDR left
    with transfers pending can stay on the bus and stop answering descriptor reads, and
    then every lookup by serial fails while sysfs still lists the device. Nothing else
    clears it: not a container restart, not a rebuild, not an update. Before this the
    only answer was "go and unplug it", which is not an answer when the box is somewhere
    else — so the terminal dependency is designed out rather than documented.

    The node is resolved HERE, from the supervisor's sysfs scan, and never taken from
    the caller: the sidecar opens whatever node it is handed, and "only the api can
    reach it" is a property of today's routing rather than of that code. Resolving by
    serial also means a caller cannot aim a reset at a device that is not an SDR."""
    request.state.debug_detail = f"sdr reset {serial}"
    nodes = nodes_in(await _usb_scan(request, settings))
    node = nodes.get(serial)
    if node is None:
        # Both readings land here: a serial nothing reports, and a scan that could not
        # be read at all. A reset needs a node either way, and guessing one is the last
        # thing to do with an ioctl that re-enumerates hardware.
        raise HTTPException(
            status_code=404,
            detail=f"No radio {serial} in the USB scan, so there is no device to reset.",
        )
    return await _post(
        settings, "/reset", {"serial": serial, "device_node": node}, wait_s=RESET_TIMEOUT_S
    )


@router.delete("/radios/{serial}")
async def forget_radio(
    request: Request,
    settings: SettingsDep,
    owner: OwnerDep,
    serial: Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")],
) -> RadiosOut:
    """Forget a radio's description — for a dongle that is gone for good.

    Distinct from setting it back to general use: a radio still on the desk and a radio
    sold last month are different states, and only one should keep a name in the list."""
    await get_settings_store(request).forget_sdr_radio(ctx_for(owner), serial)
    return await radios(request, settings, owner)


@router.post("/aprs")
async def aprs_logging(
    request: Request,
    settings: SettingsDep,
    _owner: OwnerDep,
    enabled: Annotated[bool, Query()],
    frequency_mhz: Annotated[float, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)] = APRS_DEFAULT_MHZ,
    serial: SerialQuery = None,
) -> dict[str, Any]:
    """Turn APRS logging on or off. 409 when something else holds the radio.

    The PWA's switch (`docs/mocks/aprs/c-single-dongle.html`, shape A). Idempotent in
    both directions, and turning it OFF stops the APRS session by id — never a
    listening session the owner started, which on a one-tuner box would silence the
    radio they were actually using."""
    if enabled:
        _tunable(frequency_mhz)
    base = _base(settings)
    health = await _health(base)
    if health is None:
        # An unreachable sidecar is not "off". Answering `{"logging": false}` here would
        # flip the switch to off in front of the owner while logging, if it is running,
        # carries on — the switch lying in the direction that looks harmless.
        raise HTTPException(status_code=502, detail="the radio isn't reachable")
    session = session_for(health, APRS_PURPOSE)
    logging_now = bool(session)

    if not enabled:
        if not logging_now:
            return {"logging": False, "changed": False}
        stopped = await _post(settings, "/listen/stop", {"session_id": session.get("session_id")})
        if not stopped.get("stopped"):
            # The sidecar answers 200 `{"stopped": false}` when the id no longer matches
            # — the session changed between the health read and the stop. Whether that
            # matters depends on what is there NOW: if it ended on its own the owner has
            # what they asked for, and if a new session took the tuner, reporting "off"
            # is how a timed window leaves it held all day.
            after = await _health(base)
            if session_for(after, APRS_PURPOSE):
                raise HTTPException(status_code=409, detail="the radio changed under us")
            return {"logging": False, "changed": False}
        return {"logging": False, "changed": True}

    if logging_now:
        return {"logging": True, "changed": False, "frequency_hz": session.get("frequency_hz")}
    # Which radio, before taking one. A dedicated dongle that is unplugged makes this a
    # 409 naming it, rather than APRS quietly moving to whatever else is plugged in —
    # the whole point of the setting, and invisible without this check.
    chosen = await _radio_for(request, settings, _owner, APRS_PURPOSE, serial)
    _refuse(chosen)
    body = await _post(
        settings,
        "/listen/start",
        {
            "frequency_hz": int(round(frequency_mhz * 1_000_000)),
            # 1200-baud AFSK is narrowband FM; nothing else can carry it.
            "mode": "fm",
            "gain": None,
            "purpose": APRS_PURPOSE,
            "serial": chosen.serial,
        },
    )
    if body.get("purpose") != APRS_PURPOSE:
        # A sidecar too old to understand `purpose` IGNORES it and returns 200 with a
        # plain LISTENING session. Without this the switch says logging is on while
        # nothing decodes and a tuner sits held on 144.39.
        raise HTTPException(
            status_code=502,
            detail="the radio software is too old to log APRS — nothing was changed",
        )
    return {"logging": True, "changed": True, "frequency_hz": body.get("frequency_hz")}


async def _health(base: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as client:
            resp = await client.get("/healthz")
        return cast(dict[str, Any], resp.json())
    except (httpx.HTTPError, ValueError):
        return None


@router.post("/tune")
async def tune(
    settings: SettingsDep,
    _owner: OwnerDep,
    frequency_mhz: Annotated[float, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)],
    mode: Annotated[str | None, Query(pattern=f"^({'|'.join(MODES)})$")] = None,
    session_id: Annotated[str | None, Query(max_length=32)] = None,
    bandwidth_hz: Annotated[int | None, Query(ge=MIN_BANDWIDTH_HZ, le=MAX_BANDWIDTH_HZ)] = None,
) -> dict[str, Any]:
    """Retune the live session. The session id survives, so the icon does not blink.

    A bandwidth with no frequency change is how the control sends a new filter width:
    the session keeps its width across a retune, so passing the current frequency back
    with a new width changes only the filter."""
    _tunable(frequency_mhz)
    body: dict[str, Any] = {"frequency_hz": int(round(frequency_mhz * 1_000_000))}
    if mode is not None:
        body["mode"] = mode
    if session_id is not None:
        body["session_id"] = session_id
    if bandwidth_hz is not None:
        body["bandwidth_hz"] = bandwidth_hz
    return await _post(settings, "/listen/tune", body)


#: Bounds for the picture width, as `MIN_BANDWIDTH_HZ` is for the filter: loose enough
#: for every ladder the sidecar offers (6 kHz to 360 kHz today), tight enough that
#: nonsense is a 422 here rather than a round trip. Which exact widths are real stays the
#: sidecar's to say — it owns the ladders and what its chain can actually supply.
MIN_VIEW_SPAN_HZ = 1_000
MAX_VIEW_SPAN_HZ = 2_000_000


@router.post("/view")
async def view_span(
    settings: SettingsDep,
    _owner: OwnerDep,
    span_hz: Annotated[int, Query(ge=MIN_VIEW_SPAN_HZ, le=MAX_VIEW_SPAN_HZ)],
    session_id: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """Set how wide the tuning picture is drawn. **Not a retune.**

    Its own route rather than a `/tune` parameter because it is not a retune: `/tune`
    rebuilds the demodulator, which is right for a filter change and wrong for a zoom —
    it would click the audio every time the owner changed magnification."""
    body: dict[str, Any] = {"span_hz": span_hz}
    if session_id is not None:
        body["session_id"] = session_id
    return await _post(settings, "/listen/view", body)


@router.post("/stop")
async def stop(
    settings: SettingsDep,
    _owner: OwnerDep,
    session_id: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """Release the radio. This is what makes the omnibox icon disappear."""
    return await _post(settings, "/listen/stop", {"session_id": session_id})


SPECTRUM_PURPOSE = "spectrum"


def _capture_body(capture: tuple[int, int, int] | None) -> dict[str, int]:
    """The capture as wire fields, or nothing at all.

    Absent rather than null when no I/Q capture covers the range, so a sidecar that
    predates F6 sees exactly the body it saw before and sweeps with rtl_power. The
    engine choice travels as the presence of these fields — there is no flag to keep in
    step on both sides.

    `hops` is 1 for a single capture and more for a stitched sweep (F11). It is sent
    always rather than only when it is interesting, because a sidecar reading it as
    absent would sweep 20 MHz at one tuning and draw the wrong band confidently."""
    if capture is None:
        return {}
    rate_hz, bins, hops = capture
    return {"rate_hz": rate_hz, "bins": bins, "hops": hops}


def _span(
    section: str | None,
    start_mhz: float | None,
    stop_mhz: float | None,
) -> tuple[int, int, int | float, tuple[int, int, int] | None]:
    """The range a live spectrum should cover, and the bin width that draws it.

    A section, or explicit edges: a section carries settings someone chose for that
    band while reading a band plan, so naming one is the ordinary way in, and the
    explicit edges are the expert mode the owner asked for.

    **The bin width is not the caller's to pick.** The parameter that used to carry an
    override is gone with the ladder it fed (SDR_IQ_SPECTRUM_PLAN §2.3, F7); what
    replaces it is whichever engine actually draws the picture. A one-hop capture has a
    rate and an N chosen TOGETHER so `rate / N` divides exactly
    (`bands.LIVE_CAPTURES`) — but only the I/Q engine transforms it, and that engine is
    F6. Until then every purpose=spectrum session is `rtl_power`, so every tier gets
    rtl_power's ladder and `SPECTRUM_ENGINE_IS_IQ` is the one line F6 flips.

    **Both paths ask the same question of the same row.** A hand-typed 144.100-144.300
    IS the 2 m SSB button, so it is resolved to that section (`bands.by_edges`) and
    they cannot disagree — five rows would otherwise, because a curated row may
    deliberately name a wider rate than the smallest one that covers it (`mw` takes
    2.048 MS/s to satisfy `R/2 <= fc`; the derived answer is 1.6). A range that is NOT
    a section's edges is nobody's curated row and gets the derived answer."""
    if section is not None:
        found = bands.by_id(section)
        if found is None:
            raise HTTPException(status_code=404, detail=f"No band section named {section!r}.")
        start_hz, stop_hz = found.start_hz, found.stop_hz
    else:
        if start_mhz is None or stop_mhz is None:
            raise HTTPException(
                status_code=400,
                detail="A waterfall needs a band section, or a start and stop frequency.",
            )
        start_hz = int(round(start_mhz * 1_000_000))
        stop_hz = int(round(stop_mhz * 1_000_000))
        found = bands.by_edges(start_hz, stop_hz)
    refusal = viewable(start_hz / 1_000_000, stop_hz / 1_000_000)
    if refusal:
        # The sentence, not a validation blob: this is the surface an owner with no
        # terminal has (CLAUDE.md #10), and "this is more than one capture down there"
        # is the fact they most need said in words.
        raise HTTPException(status_code=400, detail=refusal[0].upper() + refusal[1:])
    # The width is OURS, and the frame is exactly `rate / N` wide. The CAPTURE comes
    # back with it rather than being re-derived by the caller, because the width and the
    # engine that produces it have to be ONE decision.
    capture = bands.capture_for(start_hz, stop_hz)
    if capture is not None:
        rate_hz, fft_bins = capture
        return start_hz, stop_hz, bands.bin_width_hz(rate_hz, fft_bins), (rate_hz, fft_bins, 1)
    # F11: too wide for one capture is not the same as too wide for this engine.
    # The retune works on a live stream (F0), so a wide span is several captures
    # stitched — finer bins than rtl_power gave AND without its one-second clamp.
    hopped = bands.hop_plan(start_hz, stop_hz)
    if hopped is not None:
        rate_hz, fft_bins, _hops = hopped
        return start_hz, stop_hz, bands.bin_width_hz(rate_hz, fft_bins), hopped
    # THERE IS NO SECOND ENGINE ANY MORE (B1). `viewable` above already refused every
    # range with no plan, in a sentence naming the widest the waterfall stitches, so
    # reaching here is a disagreement between that refusal and this chooser rather than
    # a range to serve some other way — and serving it some other way is exactly what
    # made a `Frame.db` mean two different things depending on which engine drew it.
    raise HTTPException(
        status_code=400,
        detail=(
            f"No capture plan covers {start_hz / 1e6:g}-{stop_hz / 1e6:g} MHz. Pick a band section."
        ),
    )


@router.post("/spectrum")
async def spectrum_start(
    request: Request,
    settings: SettingsDep,
    _owner: OwnerDep,
    section: Annotated[str | None, Query(max_length=48)] = None,
    start_mhz: Annotated[float | None, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)] = None,
    stop_mhz: Annotated[float | None, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)] = None,
    gain: Annotated[str | None, Query(max_length=16)] = None,
    serial: SerialQuery = None,
) -> dict[str, Any]:
    """Take a radio and start drawing the band. 409 when every radio is held.

    Naming none takes a GENERAL radio, like the tuner: one the owner reserved for a
    service is not one a waterfall may borrow because that service is momentarily idle.
    Naming one is the launcher asking for THAT radio."""
    start_hz, stop_hz, chosen_bin, capture = _span(section, start_mhz, stop_mhz)
    chosen = await _radio_for(request, settings, _owner, GENERAL, serial)
    _refuse(chosen)
    body: dict[str, Any] = {
        "purpose": SPECTRUM_PURPOSE,
        "start_hz": start_hz,
        "stop_hz": stop_hz,
        "bin_hz": chosen_bin,
        "gain": gain,
        "serial": chosen.serial,
    }
    body.update(_capture_body(capture))
    # The band's channel raster, for the sidecar's per-row peak finding: it decides
    # which adjacent bins are ONE signal, and it is a band-plan fact rather than
    # anything the capture could imply — 200 kHz on the FM dial, 15 kHz on 2 m. Resolved
    # the same way `_span` resolves the range, so a hand-typed pair of edges that IS a
    # curated section gets that section's answer rather than a derived one.
    body["channel_hz"] = _channel_hz(section, start_hz, stop_hz)
    return await _post(settings, "/listen/start", body)


def _channel_hz(section: str | None, start_hz: int, stop_hz: int) -> int:
    """One channel's width on the band being drawn, or 0 when no section owns it."""
    found = bands.by_id(section) if section is not None else bands.by_edges(start_hz, stop_hz)
    return int(getattr(found, "channel_hz", 0) or 0)


@router.post("/spectrum/tune")
async def spectrum_tune(
    settings: SettingsDep,
    _owner: OwnerDep,
    section: Annotated[str | None, Query(max_length=48)] = None,
    start_mhz: Annotated[float | None, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)] = None,
    stop_mhz: Annotated[float | None, Query(ge=TUNABLE_MIN_MHZ, le=MAX_MHZ)] = None,
    session_id: Annotated[str | None, Query(max_length=32)] = None,
) -> dict[str, Any]:
    """Move the live spectrum to another band, on the radio it already holds.

    Not stop-and-start: releasing the radio between two bands is a window in which
    anything else may take it, and the owner would find their waterfall gone because
    they changed band. The session id survives, so the picture does not blink."""
    start_hz, stop_hz, chosen_bin, capture = _span(section, start_mhz, stop_mhz)
    body: dict[str, Any] = {
        "start_hz": start_hz,
        "stop_hz": stop_hz,
        "bin_hz": chosen_bin,
    }
    body.update(_capture_body(capture))
    # The new band's channel raster, exactly as `spectrum_start` sends it. Missing here
    # was a real fault rather than a tidiness one: picking a band from the sheet RETUNES
    # rather than restarts, so the sidecar kept a raster of 0 and only touching bins were
    # one signal — which split a 200 kHz FM carrier at any one-bin dip and reported one
    # station two or three times. Reported by the owner, seen on the FM dial.
    body["channel_hz"] = _channel_hz(section, start_hz, stop_hz)
    if session_id is not None:
        body["session_id"] = session_id
    return await _post(settings, "/listen/tune", body)


@router.get("/spectrum")
async def spectrum(
    request: Request,
    settings: SettingsDep,
    _owner: OwnerDep,
    view: Annotated[str | None, Query(pattern="^(band|channel|all)$")] = None,
    serial: Annotated[str | None, Query(max_length=64)] = None,
    backfill: Annotated[int, Query(ge=1, le=240)] = 1,
) -> StreamingResponse:
    """The waterfall's rows, as server-sent events.

    SSE rather than a WebSocket, which is what an earlier sketch of this assumed. The
    traffic is one-directional — rows out, nothing in — and a socket would have brought
    its own handshake auth and its own CSWSH gate (`api/live.py`) for no message it
    needs to carry. Retuning is a POST, which is where it belongs: the picture and the
    control are separate concerns and separate failures. As SSE it inherits the owner
    session, the same proxy hop, and EventSource's own reconnect for free.

    Each row is relayed verbatim, because each row already says which band it covers
    (`listen.Frame`). This route understands nothing about the picture, which is what
    lets a retune land without a message on this stream at all.

    `view` picks WHICH picture, on a session that draws two off one capture: the band it
    is sitting in, the channel it is demodulating, or `all` for both interleaved (every
    row says which it is). Omitted keeps whatever that session's rows meant before there
    was a choice, so a client that never sends it sees no change.

    `serial` picks WHICH RADIO, and on a two-dongle box a client that omits it is asking
    for trouble: the sidecar's `drawing()` prefers a spectrum session, so a surface
    wanting the tuner's own channel strip while the other dongle sweeps was attached to
    the sweep and waited forever for rows that session does not draw.

    `backfill` is how many already-drawn rows to send before the live ones. A waterfall
    that has been running for minutes is otherwise redrawn from nothing at the row rate
    — two a second on a hopped band — so the picture an owner comes back to is one strip
    at the bottom of an empty box."""
    base = _base(settings)
    asked = {
        "view": view,
        "serial": serial,
        "backfill": str(backfill) if backfill > 1 else None,
    }
    kept = {key: value for key, value in asked.items() if value}
    query = f"?{urlencode(kept)}" if kept else ""

    async def pump():
        client = httpx.AsyncClient(base_url=base, timeout=None)
        try:
            async with client.stream("GET", f"/listen/spectrum{query}") as upstream:
                if upstream.status_code != 200:
                    body = await upstream.aread()
                    yield _event({"error": _detail_of(body, "The radio is busy.")})
                    return
                async for line in upstream.aiter_lines():
                    if await request.is_disconnected():
                        break
                    if line.strip():
                        # A keepalive is relayed as-is: it holds this socket open too,
                        # and the client already ignores rows without a `db`.
                        yield f"data: {line}\n\n"
        except httpx.HTTPError as exc:
            yield _event({"error": str(exc)[:200]})
        finally:
            await client.aclose()

    return StreamingResponse(
        pump(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


def _detail_of(body: bytes, fallback: str) -> str:
    """The sidecar's own sentence out of an error body, or a fallback.

    Needed beside `_detail` because a streamed response's body is read separately from
    its status — the refusal arrives as bytes here rather than as an `httpx.Response`
    the caller already holds."""
    try:
        parsed = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return fallback
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    return str(detail) if detail else fallback


@router.get("/audio")
async def audio(request: Request, settings: SettingsDep, _owner: OwnerDep) -> StreamingResponse:
    """Proxy the sidecar's live Opus to the browser.

    A proxy rather than a redirect because the `radio` network is internal: the PWA
    cannot reach the sidecar directly, and it should not be able to. Streaming it
    through here also means the audio inherits the owner session — an unauthenticated
    URL for the box's live radio is not something to hand out."""
    base = _base(settings)

    async def pump():
        client = httpx.AsyncClient(base_url=base, timeout=None)
        try:
            async with client.stream("GET", "/listen/audio") as upstream:
                if upstream.status_code != 200:
                    return
                async for chunk in upstream.aiter_bytes():
                    if await request.is_disconnected():
                        break
                    yield chunk
        finally:
            await client.aclose()

    return StreamingResponse(pump(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@router.get("/captions")
async def captions(request: Request, settings: SettingsDep, _owner: OwnerDep) -> StreamingResponse:
    """Live captions for whatever the radio is playing, as server-sent events.

    Whisper is not a streaming model — it transcribes a finished clip — so the sidecar
    cuts the live audio into segments on the quiet gaps between transmissions, and each
    one is transcribed and emitted as it lands. Words carry the per-token confidence the
    client already asks for, so the caption tints on the same rose-amber-green scale as
    the transcript viewer.

    **Reading and transcribing run apart.** The reader drains the sidecar as fast as it
    sends; whatever is waiting when whisper comes free is transcribed as ONE merged clip
    (`_Backlog`). Done in step instead, each whisper call stalls the reader, the
    sidecar's queue fills behind it, and the captioner settles permanently a backlog
    behind the live edge — which the client cannot correct for, because a caption that
    arrives after its audio was heard can only be shown late.

    **The model is deliberately NOT freed between segments.** Measured on this box
    (26 consecutive calls, model resident throughout): a transcription costs ~9.8 s
    whatever the clip holds. That is INFERENCE, not model loading — an earlier reading
    of the same flat number blamed load/unload, and the residency rule was written on
    that mistake. The rule survives being right for a different reason: unloading would
    add the load back on top of the 9.8 s, and the capture route pays exactly that
    because it unloads when it finishes. The cost is flat because whisper.cpp pads every
    clip to a 30 s window, which is also what makes merging a backlog free (`_Backlog`).
    It is why captions are an explicit toggle rather than always-on, too, since a
    resident whisper shares the GPU with the chat model.

    Segments arrive already squelched: the sidecar drops any whose loudest moment never
    crossed the floor, because whisper answers an empty band with fluent invented
    sentences rather than silence."""
    base = _base(settings)
    if not settings.whisper_url:
        raise HTTPException(
            status_code=503,
            detail="no whisper gateway on this box, so live captions cannot run",
        )
    whisper = WhisperCppClient(
        settings.whisper_url, settings.whisper_model, timeout=settings.whisper_timeout
    )

    async def pump():
        client = httpx.AsyncClient(base_url=base, timeout=None)
        backlog = _Backlog()
        arrived = asyncio.Event()
        ended = asyncio.Event()

        async def read(upstream: httpx.Response) -> None:
            """Drain the sidecar as fast as it sends, whatever whisper is doing."""
            try:
                async for started, wav in _segments(upstream):
                    if wav is None:
                        continue  # a keep-alive; the loop below sends its own
                    backlog.add(started, wav)
                    arrived.set()
            finally:
                ended.set()
                arrived.set()

        try:
            async with client.stream("GET", "/listen/segments") as upstream:
                if upstream.status_code != 200:
                    yield _event({"error": "the radio is not listening"})
                    return
                reader = asyncio.create_task(read(upstream))
                try:
                    while not await request.is_disconnected():
                        # Cleared BEFORE looking, so a segment that lands between the
                        # look and the wait still wakes us rather than being slept on.
                        arrived.clear()
                        batch = backlog.take()
                        if batch is None:
                            if ended.is_set():
                                break
                            try:
                                await asyncio.wait_for(arrived.wait(), CAPTION_IDLE_S)
                            except TimeoutError:
                                yield ": keepalive\n\n"
                            continue
                        started, wav = batch
                        try:
                            result = await whisper.transcribe(
                                wav, filename="segment.wav", media_type="audio/wav"
                            )
                        except (httpx.HTTPError, ValueError) as exc:
                            # One bad segment must not end the caption stream; the next
                            # transmission is a fresh chance and the owner keeps listening.
                            yield _event({"error": str(exc)[:200], "started_at": started})
                            continue
                        text = result.text.strip()
                        if not text:
                            continue
                        yield _event(
                            {
                                "started_at": started,
                                "text": text,
                                "words": [
                                    {"text": w.text, "confidence": round(w.confidence, 4)}
                                    for w in result.words
                                ],
                            }
                        )
                finally:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reader
        finally:
            await client.aclose()

    return StreamingResponse(
        pump(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


def _event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _segments(upstream: httpx.Response):
    """Split the sidecar's newline-framed stream into (started_at, wav) pairs.

    The frame is a JSON header line then exactly `bytes` of WAV. Framing rather than a
    request per segment because the gap between requests always lands mid-sentence."""
    buffer = b""
    async for block in upstream.aiter_bytes():
        buffer += block
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            try:
                head = json.loads(buffer[:newline] or b"{}")
            except json.JSONDecodeError:
                buffer = buffer[newline + 1 :]
                continue
            if head.get("keepalive"):
                buffer = buffer[newline + 1 :]
                yield 0.0, None
                continue
            size = int(head.get("bytes", 0))
            if size <= 0:
                # Not a segment frame — a blank line, or a header that lost its size.
                # Yielding it would hand whisper zero bytes of audio to describe.
                buffer = buffer[newline + 1 :]
                continue
            if len(buffer) < newline + 1 + size:
                break  # the WAV has not all arrived yet
            wav = buffer[newline + 1 : newline + 1 + size]
            buffer = buffer[newline + 1 + size :]
            yield float(head.get("started_at", 0.0)), wav


def _clip_seconds(wav: bytes) -> float:
    """How much audio a WAV clip holds, without reading its samples."""
    try:
        with wave.open(io.BytesIO(wav), "rb") as src:
            return src.getnframes() / (src.getframerate() or 1)
    except (wave.Error, EOFError, OSError):
        return 0.0


def _merge(clips: list[bytes]) -> bytes:
    """Join consecutive WAV clips into one.

    The clips are contiguous slices of the same live capture, so concatenating their
    frames reproduces the audio exactly as it was on the air — there is no crossfade or
    resample to get wrong."""
    frames: list[bytes] = []
    rate = 0
    for clip in clips:
        try:
            with wave.open(io.BytesIO(clip), "rb") as src:
                frames.append(src.readframes(src.getnframes()))
                rate = src.getframerate() or rate
        except (wave.Error, EOFError, OSError):
            continue  # a truncated clip is dropped, not allowed to poison the batch
    if not frames or not rate:
        return clips[0] if clips else b""
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(rate)
        dst.writeframes(b"".join(frames))
    return out.getvalue()


class _Backlog:
    """Segments waiting for whisper, so that READING never waits on TRANSCRIBING.

    Read in step with transcription — the shape this route had first — the reader stalls
    for the whole of every whisper call, the sidecar's queue fills behind it, and the
    captioner ends up working through audio that was on the air a minute ago. Because it
    never catches up, that lag is permanent: captions arrive long after the listener has
    heard the words, which is the one failure the client cannot correct for.

    What waits here is transcribed TOGETHER rather than one clip at a time. Whisper's
    cost on this box is flat in clip length (~10.7 s for 4 s of audio and for 11 s
    alike), so a merged clip costs what a single one does and loses no words — where
    taking only the newest would silently drop whole sentences. The cap is what keeps a
    merge from reaching back further than a live caption sensibly can.
    """

    def __init__(self, max_seconds: float = CAPTION_BACKLOG_S) -> None:
        self._max = max_seconds
        self._held: list[tuple[float, bytes, float]] = []

    def add(self, started: float, wav: bytes) -> None:
        self._held.append((started, wav, _clip_seconds(wav)))
        # Give up the OLDEST past the cap. Dropping the newest instead would leave the
        # captioner reading history while the live edge went by unseen.
        while len(self._held) > 1 and sum(c[2] for c in self._held) > self._max:
            self._held.pop(0)

    def take(self) -> tuple[float, bytes] | None:
        """Everything waiting, as one clip stamped with the first segment's start."""
        if not self._held:
            return None
        held, self._held = self._held, []
        if len(held) == 1:
            return held[0][0], held[0][1]
        return held[0][0], _merge([wav for _, wav, _ in held])


# --- Recordings ---------------------------------------------------------------------
#
# docs/plans/SDR_RECORDING_PLAN.md R1/R2. Everything above this line is process state in
# the sidecar; everything below is a row and a blob, and therefore the only part of this
# file with an RLS scope and a firewall to keep.

#: The lease purpose the tuner holds. Recording subscribes to THAT session's audio —
#: `/listen/audio` serves the listening session specifically, so an APRS lease on another
#: dongle is neither what gets recorded nor what makes Record available.
LISTEN_PURPOSE = "listen"

#: A recording id as it may appear in a path. `app.sdr_recordings.id` is a uuid, and an
#: id that is not one reaches Postgres as a cast error — a 500 for what is really a
#: malformed request. Bounded here so it is a 422 before any query runs.
_UUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
RecordingId = Annotated[str, Path(pattern=_UUID_RE)]

#: The shortest clip a trim may leave. An MP3 frame at the sidecar's 16 kHz is 72 ms, so
#: anything under this is asking for a handful of frames — and a cut that lands on a
#: frame boundary could otherwise round a very short selection down to nothing at all.
MIN_TRIM_S = 0.2


def get_recordings_repo(maker: SessionMakerDep) -> RecordingsRepo:
    return RecordingsRepo(maker)


RecordingsDep = Annotated[RecordingsRepo, Depends(get_recordings_repo)]


def get_recorder(request: Request) -> SdrRecorder:
    """The box's one recorder, made on first use and kept on the app.

    One instance is the enforcement of "at most one recording at a time" — a per-request
    recorder would let two Record presses open two streams and write two rows over one
    content-addressed blob, so that deleting either would take the other's audio.
    """
    state = request.app.state
    recorder = getattr(state, "sdr_recorder", None)
    if recorder is None:
        recorder = SdrRecorder(state.blob_store, RecordingsRepo(state.session_maker))
        state.sdr_recorder = recorder
    return cast(SdrRecorder, recorder)


RecorderDep = Annotated[SdrRecorder, Depends(get_recorder)]


def recording_now(request: Request) -> dict[str, Any] | None:
    """What the recorder is doing, for `status_of` — tolerant of there being none yet.

    Deliberately does NOT create a recorder: `GET /sdr/status` is polled once a second by
    every open tab, and "is anything recording" on a box that has never recorded is None
    without touching the app at all.
    """
    recorder = getattr(request.app.state, "sdr_recorder", None)
    return cast("dict[str, Any] | None", recorder.state()) if recorder is not None else None


def _recording_out(row: dict[str, Any]) -> dict[str, Any]:
    """A row as the PWA sees it — everything except where the bytes live.

    `blob_sha256` is dropped rather than merely unused: a client that had it would
    eventually ask for a blob BY it, and that request cannot be scoped to the row the
    caller was allowed to read. The audio route resolves the sha itself, from the row.
    """
    return {k: v for k, v in row.items() if k != "blob_sha256"}


def _refused_recording(refused: RecorderRefused) -> HTTPException:
    """The sidecar's refusal, mapped the way `_post` maps every other one.

    409 is the owner-fixable case and keeps the sidecar's own sentence ("nothing is
    listening"): telling them the box is broken when the fix is to press Listen is the
    failure this mapping exists to avoid.
    """
    if refused.status in (400, 409):
        return HTTPException(status_code=refused.status, detail=refused.detail)
    return HTTPException(status_code=502, detail=f"sdr sidecar: {refused.detail}")


@router.post("/record")
async def record(
    settings: SettingsDep,
    owner: OwnerDep,
    recorder: RecorderDep,
    on: Annotated[bool, Query()],
) -> dict[str, Any]:
    """Start or stop recording the live session. Idempotent both ways, like `/sdr/aprs`.

    Recording is one more subscriber on the audio the browser is already playing, so it
    neither interrupts listening nor needs the radio to be re-tuned. What it stores is
    the settings in force when Record was pressed — a retune does not restart the
    pipeline, so a clip may span a frequency change, and the row keeps where it began.

    Turning it on with nothing listening is a **409 with a sentence** rather than a
    silent no-op: there is no audio to record, and the owner's next move is to press
    Listen (CLAUDE.md #10 — the sentence is the whole interface they have).
    """
    if not on:
        # No health check on the way out: stopping must work when the sidecar has already
        # gone, which is precisely when there is a half-finished blob worth keeping.
        saved = await recorder.stop()
        return {"recording": None, "saved": _recording_out(saved) if saved else None}

    base = _base(settings)
    health = await _health(base)
    if health is None:
        raise HTTPException(status_code=502, detail="sdr sidecar: the radio isn't reachable")
    session = session_for(health, LISTEN_PURPOSE)
    if not session:
        raise HTTPException(
            status_code=409,
            detail="Nothing is listening, so there is no audio to record. "
            "Start the radio first, then press Record.",
        )
    try:
        state = await recorder.start(
            ctx_for(owner),
            base_url=base,
            frequency_hz=int(session.get("frequency_hz") or 0),
            mode=str(session.get("mode") or ""),
            # The sidecar reports 0 for "this session has no channel filter"; None is the
            # honest column value for that, and keeps the library from printing "0 Hz
            # wide" under a recording.
            bandwidth_hz=int(session.get("bandwidth_hz") or 0) or None,
            gain=session.get("gain"),
            serial=session.get("serial"),
        )
    except RecorderRefused as refused:
        raise _refused_recording(refused) from refused
    except httpx.TimeoutException as slow:
        raise HTTPException(
            status_code=504, detail="The radio hasn't answered yet. Nothing is recording."
        ) from slow
    except httpx.HTTPError as broken:
        raise HTTPException(status_code=502, detail=f"sdr sidecar: {broken}") from broken
    return {"recording": state}


@router.get("/recordings")
async def recordings(
    owner: OwnerDep,
    repo: RecordingsDep,
    limit: Annotated[int, Query(ge=1, le=RECENT_MAX)] = RECENT_DEFAULT,
) -> dict[str, Any]:
    """The library, newest first, with the disk line beside it.

    `usage` travels with the list because the two are read together and the meter is the
    argument for trimming — a library that reports its size only on a second request is
    a library whose size the owner never sees.
    """
    ctx = ctx_for(owner)
    return {
        "recordings": [_recording_out(row) for row in await repo.recent(ctx, limit=limit)],
        "usage": await repo.usage(ctx),
    }


@router.get("/recordings/{recording_id}")
async def recording(
    recording_id: RecordingId,
    owner: OwnerDep,
    repo: RecordingsDep,
) -> dict[str, Any]:
    """One recording, with the waveform the trim sheet draws.

    Separate from the list on purpose. `peaks` is 400 floats, which is worth its bytes
    for the ONE clip a sheet is open on and would dwarf a hundred-row library — so the
    list omits it and the sheet asks for it here, once, when it opens.

    Without this the sheet has handles over an empty picture, which is the shape's whole
    argument missing: a trim is placeable because the silence at each end is visible.
    """
    row = await repo.get(ctx_for(owner), recording_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such recording.")
    return _recording_out(row)


@router.get("/recordings/{recording_id}/audio")
async def recording_audio(
    recording_id: RecordingId,
    owner: OwnerDep,
    repo: RecordingsDep,
    blobs: BlobStoreDep,
) -> FileResponse:
    """The clip itself, as a file Starlette can serve byte ranges out of.

    `FileResponse` is what makes Preview and scrubbing work at all: it answers Range
    requests natively, so the trim sheet can seek without downloading the clip and
    without a line of range code here.

    **The sha comes from the row, never from the URL.** Every blob on this box lives in
    one content-addressed store — attachments, notes, images — so a route that served
    `path_for(sha)` from a path segment would hand out any of them to anyone who could
    guess a digest, going around the firewall rather than through it. Resolving it from a
    row the caller could read under their own scope is what keeps this owner-only.
    """
    row = await repo.get(ctx_for(owner), recording_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such recording.")
    sha = cast(str, row["blob_sha256"])
    if not await blobs.exists(sha):
        # The row outlived its audio — a restore of the database without the blob volume.
        # A 404 with a sentence, rather than the 500 a missing file would become inside
        # the response.
        raise HTTPException(status_code=404, detail="That recording's audio is missing.")
    return FileResponse(blobs.path_for(sha), media_type="audio/mpeg")


class TrimIn(BaseModel):
    """Where to cut, in seconds from the start of the clip as it stands now."""

    start_s: Annotated[float, Field(ge=0)]
    end_s: Annotated[float, Field(gt=0)]


@router.post("/recordings/{recording_id}/trim")
async def trim_recording(
    recording_id: RecordingId,
    body: TrimIn,
    owner: OwnerDep,
    repo: RecordingsDep,
    blobs: BlobStoreDep,
) -> dict[str, Any]:
    """Cut a recording down to what is worth keeping, and **discard the original**.

    `ffmpeg -c copy` copies the MP3 frames rather than re-encoding: lossless and instant,
    at the price of landing on a frame boundary within 72 ms of the handle. So the server
    answers with what it ACTUALLY cut, measured from the result, rather than echoing what
    was asked for — the client asks in seconds and the truth comes back from here.

    Deleting the old blob is the point of the feature (the plan's §5): a trim that kept
    the original would add a blob and free nothing. `captured_s` is deliberately left
    where it was, because `duration_s < captured_s` is what makes a row "trimmed" and
    prices what trimming has given back.

    **The original goes only once the replacement has been PROVEN to contain audio.**
    `ffmpeg -c copy` exits 0 for a seek past the last frame and writes a header with no
    frames under it, so "the cut ran" is not "there is a clip". `cut_clip` measures its
    own output before this route stores any of it; an unmeasurable cut is refused here,
    with the row and its blob untouched and nothing left on disk to collect.
    """
    ctx = ctx_for(owner)
    row = await repo.get(ctx, recording_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such recording.")
    duration_s = float(row["duration_s"] or 0.0)
    _check_trim(body, duration_s)

    old_sha = cast(str, row["blob_sha256"])
    cut = await cut_clip(blobs.path_for(old_sha), body.start_s, body.end_s)
    if not cut.data:
        # The recording is untouched: nothing has been repointed and no blob deleted.
        raise HTTPException(
            status_code=500,
            detail="The trim did not run, so the recording is unchanged. Try again.",
        )
    if cut.duration_s is None:
        # The cut ran and produced a file with no audio in it — the selection landed past
        # the last frame. A 400 rather than a 500 because it IS the owner's selection that
        # is wrong, and the sheet is where they can fix it (CLAUDE.md #10). Refused BEFORE
        # anything is stored, so the recording still plays and no blob is orphaned.
        raise HTTPException(
            status_code=400,
            detail="That selection came back with no audio in it, so the recording is "
            "unchanged. Move the start of the trim back and try again.",
        )
    # Never `body.end_s - body.start_s`. A length taken from what was ASKED for is a row
    # over-stating its own audio, and the next trim then places its handles — and its
    # bounds check — against seconds the file does not have.
    kept_s = cut.duration_s
    new_sha = await blobs.put(cut.data)
    updated = await repo.retrim(
        ctx,
        recording_id,
        duration_s=kept_s,
        blob_sha256=new_sha,
        bytes_=len(cut.data),
        peaks=cut.peaks,
    )
    if updated is None:
        # Deleted from under us between the read and the write. The new blob would
        # otherwise sit on disk for ever with nothing pointing at it.
        if not await repo.blob_in_use(ctx, new_sha):
            await blobs.delete(new_sha)
        raise HTTPException(status_code=404, detail="No such recording.")
    # `new_sha == old_sha` is REACHABLE, not paranoia: trimming a clip to its full extent
    # produces identical bytes and therefore the identical digest, and deleting "the old
    # blob" there would delete the audio this row was just repointed at.
    if new_sha != old_sha and not await repo.blob_in_use(ctx, old_sha):
        await blobs.delete(old_sha)
    return {
        "recording": _recording_out(updated),
        # What was really cut. The start lands on the frame at or before what was asked
        # for, and the end follows from the measured length of the result.
        "cut": {"start_s": body.start_s, "end_s": body.start_s + kept_s},
        "usage": await repo.usage(ctx),
    }


def _check_trim(body: TrimIn, duration_s: float) -> None:
    """Refuse a cut that cannot mean anything, with a sentence rather than a 422 blob.

    The numbers arrive from a sheet with two draggable handles, so every refusal here is
    something the owner can see and correct on that sheet — which is why each one says
    what is wrong instead of naming a field.

    These bounds are a courtesy, NOT the safety net. The sheet is one of several callers
    (the owner debug API is another), and `duration_s` is only ever as true as the last
    measurement — so what actually protects the audio is that a cut must be proven to
    play before the original is deleted, in `trim_recording`. This just means the common
    mistake gets a sentence instead of a decode."""
    if body.end_s <= body.start_s:
        raise HTTPException(
            status_code=400, detail="The end of the trim has to come after the start."
        )
    if body.end_s - body.start_s < MIN_TRIM_S:
        raise HTTPException(
            status_code=400,
            detail=f"A trim has to keep at least {MIN_TRIM_S:g} seconds of audio.",
        )
    if duration_s <= 0:
        raise HTTPException(status_code=400, detail="That recording has no audio to trim.")
    # `duration_s - MIN_TRIM_S`, not `duration_s`: a start inside the last fraction of a
    # second cannot leave a clip, and the copy-cut's answer to it is a header with no
    # frames under it — the shape that used to pass validation and then delete the audio.
    if body.start_s > duration_s - MIN_TRIM_S:
        raise HTTPException(
            status_code=400,
            detail=f"That recording is only {duration_s:.1f} seconds long, so the trim "
            "would start after the end of it.",
        )
    if body.end_s > duration_s + MIN_TRIM_S:
        raise HTTPException(
            status_code=400,
            detail=f"That recording is only {duration_s:.1f} seconds long.",
        )


@router.delete("/recordings/{recording_id}")
async def delete_recording(
    recording_id: RecordingId,
    owner: OwnerDep,
    repo: RecordingsDep,
    blobs: BlobStoreDep,
) -> dict[str, Any]:
    """Forget a recording: the row, and then the audio it held.

    The row goes first, under the caller's scope, and its blob is freed only if no other
    row still points at it — content-addressed storage means two identical clips are one
    file, and unlinking on the first delete would silently empty the second.

    Returns the new `usage` so the disk meter moves with the list rather than a poll
    later.
    """
    ctx = ctx_for(owner)
    sha = await repo.remove(ctx, recording_id)
    if sha is None:
        raise HTTPException(status_code=404, detail="No such recording.")
    if not await repo.blob_in_use(ctx, sha):
        await blobs.delete(sha)
    return {"deleted": True, "usage": await repo.usage(ctx)}
