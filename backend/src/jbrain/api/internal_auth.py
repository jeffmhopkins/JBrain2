"""Per-caller authentication for the `/internal/*` routes.

`/internal` is never routed off-box by Caddy, but the api joins every docker network a
sandbox or headless browser sits on, so "internal network only" admitted a jcode session,
a fetched page's JavaScript in the reader, anything (docs/archive/SECURITY_REVIEW_FIXES_PLAN.md
F7). Each legitimate caller now holds its own token, scoped to its own routes: the
LAN-facing wall opens only `/internal/pet*` and must never hold a key to MQTT, and
mosquitto opens only `/internal/mqtt-*`.

The tokens live in the `internal_secrets` volume (storage.FsInternalSecrets): the api mints
them on start-up and the callers mount the volume read-only, so the secret arrives with the
same update as this check. The worker's `/internal/llm/regen-gateway-config` keeps its own
HMAC bearer (api/llm_internal.py).

A refusal never logs what was presented: for mosquitto the token rides in User-Agent, a
header request loggers habitually record.
"""

import hmac
from typing import cast

from fastapi import HTTPException, Request

from jbrain.storage import InternalSecrets

WALL = "wall"
MQTT = "mqtt"
CALLERS = (WALL, MQTT)


def _expected(request: Request, caller: str) -> bytes | None:
    return cast(InternalSecrets, request.app.state.internal_secrets).read(caller)


def _check(presented: str, expected: bytes | None) -> None:
    # Starlette decodes header values as latin-1, so encoding back is lossless and the
    # compare runs on bytes (a non-ASCII str would make compare_digest raise). No token
    # yet (the file has not been minted) fails closed: an empty presented value must
    # never match an empty expected one.
    if not expected or not hmac.compare_digest(presented.encode("latin-1"), expected):
        raise HTTPException(status_code=401, detail="unauthorized")


async def require_wall(request: Request) -> None:
    """The wall display (deploy/wall/serve.py) sends `Authorization: Bearer <wall.token>`."""
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    _check(presented.strip() if scheme.lower() == "bearer" else "", _expected(request, WALL))


async def require_mqtt(request: Request) -> None:
    """mosquitto-go-auth's HTTP backend can set no header but User-Agent
    (`auth_opt_http_user_agent`), so the whole User-Agent value is the token."""
    _check(request.headers.get("user-agent", ""), _expected(request, MQTT))
