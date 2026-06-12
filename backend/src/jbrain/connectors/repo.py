"""The connector cache + audit log over RLS-scoped sessions (migration 0019). The
owner-only, domain-narrowed RLS is the firewall; this repo never re-checks scope."""

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session


class SqlConnectorCache:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def get(
        self, ctx: SessionContext, connector: str, input_hash: str, ttl_seconds: int
    ) -> dict[str, Any] | None:
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT result FROM app.connector_cache"
                        " WHERE connector = :c AND input_hash = :h"
                        "   AND fetched_at + (ttl_seconds * interval '1 second') > now()"
                    ),
                    {"c": connector, "h": input_hash},
                )
            ).scalar()
        return dict(row) if row is not None else None

    async def put(
        self,
        ctx: SessionContext,
        *,
        connector: str,
        input_hash: str,
        result: dict[str, Any],
        domain: str,
        ttl_seconds: int,
    ) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "INSERT INTO app.connector_cache"
                    " (connector, input_hash, result, domain_code, ttl_seconds)"
                    " VALUES (:c, :h, cast(:r AS jsonb), :d, :ttl)"
                    " ON CONFLICT (connector, input_hash) DO UPDATE"
                    "   SET result = excluded.result, fetched_at = now(),"
                    "       ttl_seconds = excluded.ttl_seconds"
                ),
                {
                    "c": connector,
                    "h": input_hash,
                    "r": json.dumps(result),
                    "d": domain,
                    "ttl": ttl_seconds,
                },
            )

    async def record(
        self,
        ctx: SessionContext,
        *,
        connector: str,
        input_hash: str,
        domain: str,
        principal_id: str,
    ) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "INSERT INTO app.connector_log"
                    " (connector, input_hash, domain_code, principal_id)"
                    " VALUES (:c, :h, :d, :p)"
                ),
                {"c": connector, "h": input_hash, "d": domain, "p": principal_id},
            )
