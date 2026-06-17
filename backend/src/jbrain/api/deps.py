import base64
import binascii
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request

from jbrain.auth import service
from jbrain.auth.service import AuthRepo, PrincipalInfo
from jbrain.config import Settings


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_auth_repo(request: Request) -> AuthRepo:
    return cast(AuthRepo, request.app.state.auth_repo)


SettingsDep = Annotated[Settings, Depends(get_settings)]
AuthRepoDep = Annotated[AuthRepo, Depends(get_auth_repo)]


async def current_principal(
    request: Request, repo: AuthRepoDep, settings: SettingsDep
) -> PrincipalInfo:
    token = request.cookies.get(settings.session_cookie, "")
    principal = await service.authenticate(repo, token)
    if principal is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return principal


PrincipalDep = Annotated[PrincipalInfo, Depends(current_principal)]


async def owner_only(principal: PrincipalDep) -> PrincipalInfo:
    if principal.kind != "owner":
        raise HTTPException(status_code=403, detail="owner access required")
    return principal


# An owner-gated principal: the route 403s a non-owner (capability) token.
OwnerDep = Annotated[PrincipalInfo, Depends(owner_only)]


def _basic_password(authorization: str) -> str | None:
    """The password from an `Authorization: Basic` header, or None if malformed.

    OwnTracks sends the device key as the Basic password; the username is a device
    label only and is NEVER trusted for authz (it is not even returned here)."""
    scheme, _, payload = authorization.partition(" ")
    if scheme.lower() != "basic" or not payload:
        return None
    try:
        decoded = base64.b64decode(payload, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    _, sep, password = decoded.partition(":")
    return password if sep else None


async def current_device_principal(request: Request, repo: AuthRepoDep) -> PrincipalInfo:
    """Authenticate an OwnTracks device by HTTP Basic. Fail-closed 401.

    Physically distinct from `current_principal` (the owner cookie path): the two
    share no lookup, so a device key can never reach owner routes and an owner
    token can never reach the device ingest path (L4)."""
    password = _basic_password(request.headers.get("Authorization", ""))
    principal = await service.authenticate_device(repo, password) if password else None
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="invalid device key",
            headers={"WWW-Authenticate": 'Basic realm="owntracks"'},
        )
    return principal


DeviceDep = Annotated[PrincipalInfo, Depends(current_device_principal)]
