"""View-scope-aware poke routing (JBrain360 M6b).

When a subject crosses a geofence, the members who may SEE that subject get a
content-free poke. The audience is `app.visible_subjects(X)` minus X itself — by
the symmetry of family-group view-scope, the subjects X can see are exactly the
subjects who can see X. Their ACTIVE device tokens (revoked devices already
filtered, de-duplicated) get one poke each.
"""

import structlog
from sqlalchemy import text

from jbrain.db.session import scoped_session
from jbrain.push.repo import FcmTokenRepo
from jbrain.push.sender import PushNotifier
from jbrain.queue import SYSTEM_CTX

log = structlog.get_logger()


class PushRouter:
    def __init__(self, maker, tokens: FcmTokenRepo):  # noqa: ANN001 - async_sessionmaker
        self._maker = maker
        self._tokens = tokens

    async def poke_viewers_of(self, notifier: PushNotifier, subject_id: str) -> None:
        """Poke the members who may see `subject_id` (its family group, excluding
        itself). Best-effort: a routing/send error is logged, never raised — a poke
        must never break the ingest path that triggered it."""
        try:
            async with scoped_session(self._maker, SYSTEM_CTX) as session:
                viewers = (
                    (
                        await session.execute(
                            text(
                                "SELECT subject_id::text FROM app.visible_subjects(:v)"
                                " WHERE subject_id <> cast(:v AS uuid)"
                            ),
                            {"v": subject_id},
                        )
                    )
                    .scalars()
                    .all()
                )
            if not viewers:
                return
            tokens = await self._tokens.tokens_for_subjects(SYSTEM_CTX, list(viewers))
            if tokens:
                await notifier.poke(tokens)
        except Exception as exc:  # noqa: BLE001 - a poke must not break ingest
            log.warning("push.route_failed", subject_id=subject_id, error=repr(exc))
