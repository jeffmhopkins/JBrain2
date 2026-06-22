"""The live location feed — in-process fan-out for the dashboard (JBrain360 M3b).

The "backend-proxied live feed": the API holds ONE privileged MQTT subscription
(the feeder, wired in `main`), parses each published OwnTracks fix into a `LiveFix`
keyed by the publisher's *subject*, and fans it out to connected dashboard sockets
through a `LiveBroadcaster`. Each socket then applies its own view-scope before
sending — so MQTT creds never reach the browser and the broker stays private (the
plan's B4 decision). This module is the data plumbing; the WS endpoint
(`api/locations`) owns auth + the per-connection scope filter + the audit.
"""

import asyncio
import contextlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

import aiomqtt
import structlog

from jbrain.auth.service import AuthRepo
from jbrain.config import Settings
from jbrain.locations.ingest import OwnTracksLocation, is_location_message
from jbrain.mqtt.consumer import principal_id_from_topic

log = structlog.get_logger()
SUBSCRIBE_FILTER = "owntracks/#"


@dataclass(frozen=True)
class LiveFix:
    """A live position for one subject, trimmed to what the map renders."""

    subject_id: str
    latitude: float
    longitude: float
    accuracy_m: float | None
    battery_pct: int | None
    velocity_mps: float | None
    captured_at: datetime


def live_fix_from_owntracks(subject_id: str, body: object) -> LiveFix | None:
    """Build a `LiveFix` from a `_type:location` body for an already-resolved subject.

    The subject is the *authenticated publisher's* subject (resolved from the topic,
    never the payload — L9); a non-location or schema-invalid body yields None."""
    if not subject_id or not is_location_message(body):
        return None
    try:
        loc = OwnTracksLocation.model_validate(body)
    except ValueError:
        return None
    return LiveFix(
        subject_id=subject_id,
        latitude=loc.lat,
        longitude=loc.lon,
        accuracy_m=loc.acc,
        battery_pct=loc.batt,
        velocity_mps=loc.vel / 3.6 if loc.vel is not None else None,  # km/h -> m/s
        captured_at=datetime.fromtimestamp(loc.tst, UTC),
    )


class LiveBroadcaster:
    """In-process fan-out of live fixes to connected sockets.

    Each subscriber gets a bounded queue; on overflow the oldest fix is dropped, so
    one slow socket can neither stall the others nor grow memory unboundedly (a
    dropped live fix is fine — the next one supersedes it)."""

    def __init__(self, maxsize: int = 256):
        self._subscribers: set[asyncio.Queue[LiveFix]] = set()
        self._maxsize = maxsize

    def subscribe(self) -> "asyncio.Queue[LiveFix]":
        q: asyncio.Queue[LiveFix] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue[LiveFix]") -> None:
        self._subscribers.discard(q)

    def publish(self, fix: LiveFix) -> None:
        for q in self._subscribers:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # drop the oldest to make room
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(fix)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


async def live_feeder(
    settings: Settings, auth_repo: AuthRepo, broadcaster: LiveBroadcaster
) -> None:  # pragma: no cover - operational loop, exercised at deploy not in CI
    """Hold one privileged MQTT subscription and publish each device's fix into the
    broadcaster. Authenticates as the ingest identity (same as the M1 consumer) and
    reconnects on broker errors; the subject is resolved from the topic owner."""
    while True:
        try:
            async with aiomqtt.Client(
                hostname=settings.mqtt_broker_host,
                port=settings.mqtt_broker_port,
                username=settings.mqtt_ingest_username,
                password=settings.mqtt_ingest_secret,
            ) as client:
                await client.subscribe(SUBSCRIBE_FILTER)
                log.info("locations.live_feeder_connected", host=settings.mqtt_broker_host)
                async for message in client.messages:
                    pid = principal_id_from_topic(str(message.topic))
                    if pid is None:
                        continue
                    principal = await auth_repo.find_active_device_principal_by_id(pid)
                    if principal is None:
                        continue
                    raw = message.payload
                    payload = raw if isinstance(raw, bytes | bytearray) else b""
                    try:
                        body = json.loads(bytes(payload))
                    except (ValueError, TypeError):
                        continue
                    fix = live_fix_from_owntracks(principal.subject_id, body)
                    if fix is not None:
                        broadcaster.publish(fix)
        except aiomqtt.MqttError as exc:
            log.warning("locations.live_feeder_disconnected", error=repr(exc))
            await asyncio.sleep(5)
