"""A robot's leg calibration, kept on its `app.growbot_robots` row (migration 0261).

Upstream's creature reads it from the phone's localStorage (`gb_walk_cal`); here the box holds
it per robot and the creature page copies it in, so every phone that wears the body drives it
the same way. NULL means the PWA never set it, and a phone keeps what it has.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session


class BodyRepo(Protocol):
    async def get_cal(self, ctx: SessionContext, code: str) -> tuple[bool, dict[str, Any] | None]:
        """(whether the robot exists, its calibration or None when never set)."""
        ...

    async def set_cal(self, ctx: SessionContext, code: str, cal: dict[str, Any]) -> bool:
        """Store `code`'s calibration; False when there is no such robot."""
        ...


class SqlBodyRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def get_cal(self, ctx: SessionContext, code: str) -> tuple[bool, dict[str, Any] | None]:
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text("SELECT cal FROM app.growbot_robots WHERE code = :c"), {"c": code}
                )
            ).first()
        if row is None:
            return False, None
        return True, dict(row.cal) if isinstance(row.cal, dict) else None

    async def set_cal(self, ctx: SessionContext, code: str, cal: dict[str, Any]) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            done = await session.execute(
                text(
                    "UPDATE app.growbot_robots SET cal = CAST(:cal AS jsonb)"
                    " WHERE code = :c RETURNING code"
                ),
                {"c": code, "cal": json.dumps(cal)},
            )
            return done.first() is not None
