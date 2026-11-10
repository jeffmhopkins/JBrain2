"""The robots' device keys and the phone tickets, both owner-only (migrations 0254, 0255).

Secrets are stored as `hash_token` digests only. A ticket is a 256-bit random value and so
is a device key, so a plain SHA-256 is the right hash (`jbrain.auth.keys`), and looking a
ticket up BY its hash leaks nothing through timing: an attacker can steer the digest's bytes
no better than by guessing the ticket.

A revoked robot keeps its row (its name, its history) with a NULL key hash, which no key can
match; a reflash mints a new key and clears the revoke.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.auth.keys import generate_capability_key, generate_session_token, hash_token
from jbrain.db.session import SessionContext, scoped_session

TICKET_TTL = timedelta(seconds=120)
# The pairing code the chip prints at boot and the relay path carries (`/d/<code>`).
CODE_RE = re.compile(r"^gb-[A-Za-z0-9]{1,32}$")
# Compared against when no robot has the code, so an unknown code costs the same compare.
_NO_ROBOT = hash_token("")
# Used tickets and long-expired ones are kept a day for the debug trail, then dropped on the
# next mint, so the table holds at most a day of opens.
_KEEP_SPENT = timedelta(days=1)


# What the owner may call a robot (the column's CHECK is the same 40).
NAME_MAX = 40


@dataclass(frozen=True)
class RobotInfo:
    code: str
    created_at: datetime
    rotated_at: datetime | None
    name: str = ""
    revoked_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True)
class MintedTicket:
    ticket: str
    expires_at: datetime


class GrowbotRepo(Protocol):
    async def mint_ticket(self, ctx: SessionContext) -> MintedTicket: ...

    async def redeem_ticket(self, ctx: SessionContext, ticket: str) -> bool:
        """Spend `ticket` if it is unused and unexpired. True exactly once per ticket."""
        ...

    async def put_robot(self, ctx: SessionContext, code: str, name: str | None = None) -> str:
        """Mint `code`'s device key, replacing any earlier one and clearing a revoke. Returns
        the key, once. `name` None keeps the robot's name."""
        ...

    async def list_robots(self, ctx: SessionContext) -> list[RobotInfo]: ...

    async def revoke_robot(self, ctx: SessionContext, code: str) -> RobotInfo | None:
        """Null `code`'s key so no key matches it; None when there is no such robot."""
        ...

    async def delete_robot(self, ctx: SessionContext, code: str) -> bool: ...

    async def verify_robot(self, ctx: SessionContext, code: str, key: str) -> bool:
        """Whether `key` is `code`'s live key; a match stamps the robot's last_seen_at."""
        ...


_COLUMNS = "code, created_at, rotated_at, name, revoked_at, last_seen_at"


def _info(r: Any) -> RobotInfo:
    return RobotInfo(r.code, r.created_at, r.rotated_at, r.name, r.revoked_at, r.last_seen_at)


class SqlGrowbotRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def mint_ticket(self, ctx: SessionContext) -> MintedTicket:
        ticket = generate_session_token()
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "DELETE FROM app.growbot_tickets"
                    " WHERE expires_at < now() - make_interval(secs => :keep)"
                ),
                {"keep": _KEEP_SPENT.total_seconds()},
            )
            expires_at = (
                await session.execute(
                    text(
                        "INSERT INTO app.growbot_tickets (ticket_hash, expires_at)"
                        " VALUES (:h, now() + make_interval(secs => :ttl))"
                        " RETURNING expires_at"
                    ),
                    {"h": hash_token(ticket), "ttl": TICKET_TTL.total_seconds()},
                )
            ).scalar_one()
        return MintedTicket(ticket=ticket, expires_at=expires_at)

    async def redeem_ticket(self, ctx: SessionContext, ticket: str) -> bool:
        if not ticket:
            return False
        # One statement: two redeems racing on the same ticket cannot both see it unused.
        async with scoped_session(self._maker, ctx) as session:
            spent = await session.execute(
                text(
                    "UPDATE app.growbot_tickets SET used_at = now()"
                    " WHERE ticket_hash = :h AND used_at IS NULL AND expires_at > now()"
                    " RETURNING ticket_hash"
                ),
                {"h": hash_token(ticket)},
            )
            return spent.first() is not None

    async def put_robot(self, ctx: SessionContext, code: str, name: str | None = None) -> str:
        key = generate_capability_key()
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "INSERT INTO app.growbot_robots (code, key_hash, name)"
                    " VALUES (:c, :h, coalesce(:n, ''))"
                    " ON CONFLICT (code) DO UPDATE"
                    " SET key_hash = EXCLUDED.key_hash, rotated_at = now(), revoked_at = NULL,"
                    " name = coalesce(:n, app.growbot_robots.name)"
                ),
                {"c": code, "h": hash_token(key), "n": name},
            )
        return key

    async def list_robots(self, ctx: SessionContext) -> list[RobotInfo]:
        async with scoped_session(self._maker, ctx) as session:
            rows = await session.execute(
                text(f"SELECT {_COLUMNS} FROM app.growbot_robots ORDER BY code")
            )
            return [_info(r) for r in rows]

    async def revoke_robot(self, ctx: SessionContext, code: str) -> RobotInfo | None:
        async with scoped_session(self._maker, ctx) as session:
            row = (
                await session.execute(
                    text(
                        "UPDATE app.growbot_robots"
                        " SET key_hash = NULL, revoked_at = coalesce(revoked_at, now())"
                        f" WHERE code = :c RETURNING {_COLUMNS}"
                    ),
                    {"c": code},
                )
            ).first()
            return _info(row) if row is not None else None

    async def delete_robot(self, ctx: SessionContext, code: str) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            gone = await session.execute(
                text("DELETE FROM app.growbot_robots WHERE code = :c RETURNING code"),
                {"c": code},
            )
            return gone.first() is not None

    async def verify_robot(self, ctx: SessionContext, code: str, key: str) -> bool:
        async with scoped_session(self._maker, ctx) as session:
            stored = (
                await session.execute(
                    text("SELECT key_hash FROM app.growbot_robots WHERE code = :c"),
                    {"c": code},
                )
            ).scalar_one_or_none()
            # A revoked robot (NULL hash) costs the same compare as an unknown one and never
            # matches.
            matched = hmac.compare_digest(hash_token(key), stored or _NO_ROBOT)
            ok = stored is not None and bool(key) and matched
            if ok:
                # Keyed on the hash it matched, so a rotate or revoke racing this hello is
                # never stamped as the robot having reached the box.
                await session.execute(
                    text(
                        "UPDATE app.growbot_robots SET last_seen_at = now()"
                        " WHERE code = :c AND key_hash = :h"
                    ),
                    {"c": code, "h": stored},
                )
        return ok
