"""The MQTT ingest consumer (JBrain360 M1).

A server-side subscriber that turns published OwnTracks fixes into rows in the
shipped `location_fixes` hypertable. It subscribes to `owntracks/#`, and for each
`_type:location` message on a device's base topic it resolves the topic owner — the
device principal id, trusted because the broker ACL only lets a device publish
under its own id — to that device's subject, then feeds the SHARED ingest core
under `device_context` (so the subject-pin and geofence-at-ingest, L5a, hold
exactly as on the HTTP path). `handle_message` is the unit; `run` is the thin loop.
"""

import asyncio
import json
from typing import TYPE_CHECKING

import aiomqtt
import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.auth.service import AuthRepo
from jbrain.config import Settings
from jbrain.locations.ingest import LocationSink, ingest_location, is_location_message

if TYPE_CHECKING:
    from jbrain.push import PushNotifier

log = structlog.get_logger()

OWNTRACKS_ROOT = "owntracks"
SUBSCRIBE_FILTER = f"{OWNTRACKS_ROOT}/#"
RECONNECT_SECONDS = 5.0


def principal_id_from_topic(topic: str) -> str | None:
    """The device principal id from a base location topic `owntracks/<pid>/<device>`.

    None for anything that is not a 3-segment owntracks base topic — a subtopic
    (`.../cmd`, `.../event`, `.../waypoint`), a wildcard, or a foreign root — so only
    base-topic location reports are considered for ingest."""
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == OWNTRACKS_ROOT and parts[1] and parts[2]:
        return parts[1]
    return None


async def handle_message(
    auth_repo: AuthRepo,
    sink: LocationSink,
    maker: "async_sessionmaker[AsyncSession]",
    *,
    topic: str,
    payload: bytes,
    notifier: "PushNotifier | None" = None,
) -> bool:
    """Process one broker message; return True iff a new fix was stored.

    Deterministic drops (return False): a non-base / non-owntracks topic, an
    unknown/revoked publisher, undecodable JSON, a non-`location` body, or a
    schema-invalid location. Operational errors (e.g. the DB) propagate to `run`,
    which logs them per-message so one failure cannot wedge the loop."""
    pid = principal_id_from_topic(topic)
    if pid is None:
        return False
    principal = await auth_repo.find_active_device_principal_by_id(pid)
    if principal is None:
        log.warning("mqtt.ingest.unknown_principal", topic=topic)
        return False
    try:
        body = json.loads(payload)
    except (ValueError, TypeError):
        return False
    if not is_location_message(body):
        return False  # transition / waypoint / lwt / card: not ours to store
    try:
        return await ingest_location(
            sink,
            maker,
            principal_id=principal.id,
            subject_id=principal.subject_id,
            body=body,
            notifier=notifier,
        )
    except ValidationError:
        log.warning("mqtt.ingest.invalid_location", topic=topic)
        return False


async def run(
    settings: Settings,
    auth_repo: AuthRepo,
    sink: LocationSink,
    maker: "async_sessionmaker[AsyncSession]",
    notifier: "PushNotifier | None" = None,
) -> None:  # pragma: no cover - operational loop, exercised at deploy not in CI
    """Connect, subscribe `owntracks/#`, and dispatch each message; reconnect on
    broker errors. The consumer authenticates as the ingest service identity."""
    while True:
        try:
            async with aiomqtt.Client(
                hostname=settings.mqtt_broker_host,
                port=settings.mqtt_broker_port,
                username=settings.mqtt_ingest_username,
                password=settings.mqtt_ingest_secret,
            ) as client:
                await client.subscribe(SUBSCRIBE_FILTER)
                log.info("mqtt.ingest.connected", host=settings.mqtt_broker_host)
                async for message in client.messages:
                    raw = message.payload
                    payload = raw if isinstance(raw, bytes | bytearray) else b""
                    try:
                        await handle_message(
                            auth_repo,
                            sink,
                            maker,
                            topic=str(message.topic),
                            payload=bytes(payload),
                            notifier=notifier,
                        )
                    except Exception as exc:  # noqa: BLE001 - one bad message must not wedge the loop
                        log.warning("mqtt.ingest.message_error", error=repr(exc))
        except aiomqtt.MqttError as exc:
            log.warning("mqtt.ingest.disconnected", error=repr(exc))
            await asyncio.sleep(RECONNECT_SECONDS)
