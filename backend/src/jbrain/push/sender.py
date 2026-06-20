"""The content-free poke sender (JBrain360 M6b).

The whole privacy point of M6: the FCM payload carries NO PII — no subject, no
place, no coordinates. It is a bare data-only wake-up; the app, on receiving it,
fetches the real notification over its own authenticated channel and renders it
locally. So Google's infrastructure never sees who went where.

`FcmNotifier` is the real HTTP v1 sender, constructed only when a project +
credentials are configured; otherwise `NullNotifier` makes every poke a no-op (the
stock deploy, dev, and CI never reach FCM).
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx
import structlog

log = structlog.get_logger()

# The ONLY thing a poke carries. PII-free by construction; the no-PII gate asserts
# nothing else is ever added here.
POKE_DATA: dict[str, str] = {"type": "location_event"}


class PushNotifier(Protocol):
    async def poke(self, tokens: list[str]) -> None: ...


class NullNotifier:
    """No FCM project configured → pokes are no-ops (stock deploy / dev / CI)."""

    async def poke(self, tokens: list[str]) -> None:
        return None


def fcm_message(token: str) -> dict[str, Any]:
    """The FCM HTTP v1 body for one content-free poke. Data-only (NO `notification`
    block, so the OS renders nothing on its own) and the data carries no PII —
    just the type hint the app fetches against."""
    return {"message": {"token": token, "data": dict(POKE_DATA)}}


class FcmNotifier:
    """Sends a content-free data message per token via FCM HTTP v1. The OAuth access
    token comes from an injected provider (a service-account flow wired at deploy);
    a per-token send failure is logged, never raised — a dead token must not stall
    the rest of the fan-out."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        project_id: str,
        access_token: Callable[[], Awaitable[str]],
    ):
        self._client = client
        self._project_id = project_id
        self._access_token = access_token

    async def poke(self, tokens: list[str]) -> None:
        if not tokens:
            return
        url = f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"
        bearer = await self._access_token()
        headers = {"Authorization": f"Bearer {bearer}"}
        for token in tokens:
            try:
                await self._client.post(url, json=fcm_message(token), headers=headers)
            except Exception as exc:  # noqa: BLE001 - one bad token must not stop the rest
                log.warning("push.fcm_send_failed", error=repr(exc))
