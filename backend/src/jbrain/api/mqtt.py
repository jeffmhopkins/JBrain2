"""Internal MQTT auth/ACL endpoints for mosquitto-go-auth's HTTP backend.

The broker calls these over the docker `internal` network — `/internal/mqtt-auth`
on every MQTT connect, `/internal/mqtt-acl` on every publish/subscribe. They are
NOT public: Caddy fronts only `/api`, while `/internal` is reachable solely from
the broker on the internal network. go-auth runs in `status` response mode, so a
**200 means allow and anything else denies**; all credential/ACL logic lives here
(the plugin is a dumb forwarder — plan T2/B2).

Two identities authenticate:
- a **device** (M0): the MQTT password is its device key, resolved via the shipped
  `service.authenticate_device`; it must claim its own principal id as the username
  (so the stateless ACL can trust `username`), and is confined to its own
  `owntracks/<username>/#` namespace.
- the **ingest consumer** (M1): a server-side subscriber authenticated by the
  configured `mqtt_ingest_secret` (a service secret, not a device key), granted
  read-only `owntracks/#` so it can stream every device's fixes into the ingest
  core. Disabled when the secret is empty (fail-closed).
"""

import hmac
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from jbrain.auth import service
from jbrain.auth.service import AuthRepo
from jbrain.config import Settings
from jbrain.mqtt.authz import authorize_ingest_subscribe, authorize_topic

router = APIRouter()

_ALLOW = 200
_DENY = 403  # any non-2xx denies in go-auth `status` mode


def get_auth_repo(request: Request) -> AuthRepo:
    return cast(AuthRepo, request.app.state.auth_repo)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


AuthRepoDep = Annotated[AuthRepo, Depends(get_auth_repo)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class AuthCheck(BaseModel):
    username: str = ""
    password: str = ""
    clientid: str = ""


class AclCheck(BaseModel):
    username: str = ""
    clientid: str = ""
    topic: str = ""
    acc: int = 0


def _is_ingest_identity(settings: Settings, username: str) -> bool:
    """The configured ingest service identity (only when a secret is set)."""
    return bool(settings.mqtt_ingest_secret) and username == settings.mqtt_ingest_username


@router.post("/mqtt-auth")
async def mqtt_auth(body: AuthCheck, repo: AuthRepoDep, settings: SettingsDep) -> Response:
    """Authenticate a connecting client. 200 allow / 403 deny (fail-closed)."""
    if _is_ingest_identity(settings, body.username) and hmac.compare_digest(
        body.password, settings.mqtt_ingest_secret
    ):
        return Response(status_code=_ALLOW)
    # A device: valid active device key AND claiming its own principal id (else a
    # valid key could be flown under a forged identity the ACL would then trust).
    principal = await service.authenticate_device(repo, body.password)
    if principal is not None and body.username == principal.id:
        return Response(status_code=_ALLOW)
    return Response(status_code=_DENY)


@router.post("/mqtt-acl")
async def mqtt_acl(body: AclCheck, settings: SettingsDep) -> Response:
    """Authorize a publish/subscribe. `username` is trusted (bound at auth)."""
    if _is_ingest_identity(settings, body.username):
        ok = authorize_ingest_subscribe(body.topic, body.acc)
    else:
        # M0 floor: a device may touch only its own namespace (view-scope, M2).
        ok = authorize_topic(body.username, body.topic)
    return Response(status_code=_ALLOW if ok else _DENY)
