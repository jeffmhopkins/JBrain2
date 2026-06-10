"""Worker entrypoint.

Phase 0 placeholder: proves the container wiring (image, env, DB reachability)
that the Phase 2 job queue will inherit. Heartbeats keep the service honest in
`docker compose ps` and the Ops screen.
"""

import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from jbrain.config import get_settings

log = structlog.get_logger()

HEARTBEAT_SECONDS = 60


async def run() -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        while True:
            try:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                log.info("worker.heartbeat", database="ok")
            except Exception as exc:  # noqa: BLE001 - heartbeat must survive DB blips
                log.warning("worker.heartbeat", database="unavailable", error=str(exc))
            await asyncio.sleep(HEARTBEAT_SECONDS)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
