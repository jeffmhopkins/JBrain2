"""Internal MQTT auth/ACL endpoints for mosquitto-go-auth's HTTP backend (M0).

The broker calls these over the docker `internal` network — `/internal/mqtt-auth`
on every MQTT connect, `/internal/mqtt-acl` on every publish/subscribe. They are
NOT public: Caddy fronts only `/api`, while `/internal` is reachable solely from
the broker on the internal network. go-auth runs in `status` response mode, so a
**200 means allow and anything else denies**; all credential/ACL logic lives here
(the plugin is a dumb forwarder — plan T2/B2).

Auth reuses the shipped `device_key` path verbatim (`service.authenticate_device`
→ SHA-256-hex lookup, kind-filtered, `revoked_at IS NULL`), and additionally
**binds the connection to its principal**: the client must present its own
principal id as the MQTT username, so the stateless ACL check can trust
`username` thereafter. Authorization is the M0 own-namespace floor
(`jbrain.mqtt.authz`); view-scope widens it in M2.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from jbrain.auth import service
from jbrain.auth.service import AuthRepo
from jbrain.mqtt.authz import authorize_topic

router = APIRouter()

_ALLOW = 200
_DENY = 403  # any non-2xx denies in go-auth `status` mode


def get_auth_repo(request: Request) -> AuthRepo:
    return cast(AuthRepo, request.app.state.auth_repo)


AuthRepoDep = Annotated[AuthRepo, Depends(get_auth_repo)]


class AuthCheck(BaseModel):
    username: str = ""
    password: str = ""
    clientid: str = ""


class AclCheck(BaseModel):
    username: str = ""
    clientid: str = ""
    topic: str = ""
    acc: int = 0


@router.post("/mqtt-auth")
async def mqtt_auth(body: AuthCheck, repo: AuthRepoDep) -> Response:
    """Authenticate a connecting device. 200 allow / 403 deny (fail-closed).

    The MQTT password is the device key. Beyond a valid, active `device_key`
    principal, the client must claim its OWN principal id as the username —
    otherwise a valid key could be flown under a forged identity and the ACL,
    which trusts `username`, would scope it to someone else's namespace.
    """
    principal = await service.authenticate_device(repo, body.password)
    if principal is not None and body.username == principal.id:
        return Response(status_code=_ALLOW)
    return Response(status_code=_DENY)


@router.post("/mqtt-acl")
async def mqtt_acl(body: AclCheck) -> Response:
    """Authorize a publish/subscribe. M0: own OwnTracks namespace only.

    `username` is the device principal id, bound at auth and trusted here.
    """
    ok = authorize_topic(body.username, body.topic)
    return Response(status_code=_ALLOW if ok else _DENY)
