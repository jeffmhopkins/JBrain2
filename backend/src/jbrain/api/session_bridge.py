"""The dashboard session bridge (JBrain360 M4a).

The forked app's WebView holds the device key in the Android Keystore and POSTs
it here over TLS; on success the browser gets the dashboard session cookie bound
to the device's subject + view-scope. Direct-key over TLS (owner decision) —
the same credential the device already presents on the MQTT / OwnTracks path,
verified against the shipped kind-filtered device lookup, so an owner or
capability key can never mint a member session here.

Distinct from `/auth/session` (the owner cookie): that exchanges the *owner* key
and sets a Lax cookie; this mints a *member* (device-kind) session and sets a
SameSite=Strict cookie (plan B8). Both land in the one session cookie, and the
kind-gated route dependencies keep member authority off owner routes.
"""

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from jbrain.api.deps import AuthRepoDep, SettingsDep
from jbrain.auth import service

router = APIRouter(prefix="/session")


class MintRequest(BaseModel):
    device_key: str


@router.post("/mint", status_code=204)
async def mint(
    body: MintRequest,
    response: Response,
    repo: AuthRepoDep,
    settings: SettingsDep,
) -> None:
    """Exchange the Keystore device key for the dashboard session cookie. 401 on an
    invalid / revoked / non-device key, writing no cookie."""
    token = await service.mint_dashboard_session(repo, body.device_key)
    if token is None:
        raise HTTPException(status_code=401, detail="invalid device key")
    response.set_cookie(
        settings.session_cookie,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="strict",
        max_age=60 * 60 * 24 * 365,
    )
