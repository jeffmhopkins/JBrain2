"""Entrypoint for the MQTT ingest consumer: `python -m jbrain.mqtt_consumer`.

A separate long-running process (like the worker), run by the opt-in `mqtt`
compose service. Fails closed: with no `mqtt_ingest_secret` configured it logs and
exits rather than connecting unauthenticated.
"""

import asyncio

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import get_settings
from jbrain.locations import SqlLocationRepo
from jbrain.mqtt.consumer import run

log = structlog.get_logger()


async def main() -> None:  # pragma: no cover - process wiring, exercised at deploy
    settings = get_settings()
    if not settings.mqtt_ingest_secret:
        log.warning("mqtt.ingest.disabled", reason="mqtt_ingest_secret unset")
        return
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await run(settings, SqlAuthRepo(maker), SqlLocationRepo(maker), maker)
    finally:
        await engine.dispose()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
