"""The `fcm_token` registry (JBrain360 M6a).

A device registers its FCM token under its own subject (RLS pins it); a full owner /
system reads all for routing. Routing reads only ACTIVE principals' tokens, so a
revoked device drops out of every poke even though its row lingers until the
principal is purged.
"""

from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.db.session import SessionContext, scoped_session


class FcmTokenRepo(Protocol):
    async def register(
        self, ctx: SessionContext, *, principal_id: str, subject_id: str, token: str
    ) -> None: ...

    async def delete(self, ctx: SessionContext, *, token: str) -> None: ...

    async def tokens_for_subjects(
        self, ctx: SessionContext, subject_ids: list[str]
    ) -> list[str]: ...


class SqlFcmTokenRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def register(
        self, ctx: SessionContext, *, principal_id: str, subject_id: str, token: str
    ) -> None:
        """Upsert this device's FCM token (RLS pins it to the device's own subject).
        A token is globally unique, so a re-registration — or a token that migrated
        to this device — re-points to the current principal."""
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "INSERT INTO app.fcm_token (principal_id, subject_id, token)"
                    " VALUES (cast(:p AS uuid), cast(:s AS uuid), :t)"
                    " ON CONFLICT (token) DO UPDATE SET"
                    "   principal_id = excluded.principal_id,"
                    "   subject_id = excluded.subject_id,"
                    "   updated_at = now()"
                ),
                {"p": principal_id, "s": subject_id, "t": token},
            )

    async def delete(self, ctx: SessionContext, *, token: str) -> None:
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(text("DELETE FROM app.fcm_token WHERE token = :t"), {"t": token})

    async def tokens_for_subjects(self, ctx: SessionContext, subject_ids: list[str]) -> list[str]:
        """The FCM tokens of ACTIVE devices for the given subjects (owner/system
        read, for routing). A revoked principal's token is excluded — the
        revoke-kills-token guarantee — and the result is de-duplicated."""
        if not subject_ids:
            return []
        async with scoped_session(self._maker, ctx) as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT DISTINCT ft.token"
                        " FROM app.fcm_token ft"
                        " JOIN app.principals p ON p.id = ft.principal_id"
                        " WHERE ft.subject_id = ANY(cast(:sids AS uuid[]))"
                        "   AND p.revoked_at IS NULL"
                    ),
                    {"sids": subject_ids},
                )
            ).all()
        return [r.token for r in rows]
