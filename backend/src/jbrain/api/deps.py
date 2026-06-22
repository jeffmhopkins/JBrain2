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


async def member_only(principal: PrincipalDep) -> PrincipalInfo:
    """A member dashboard session: a device-kind cookie minted by /session/mint.

    403s anything that is not a device key (the owner uses the /locations surface;
    a capability token has no dashboard). The device principal carries its subject,
    which scopes every member read to its own + its family group via RLS."""
    if principal.kind != "device_key":
        raise HTTPException(status_code=403, detail="member access required")
    return principal


# A member-gated principal: the route 403s anything but a device-key cookie.
MemberDep = Annotated[PrincipalInfo, Depends(member_only)]


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


def _bearer(authorization: str) -> str | None:
    """The token from an `Authorization: Bearer` header, or None if malformed."""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


async def current_debug_principal(
    request: Request, repo: AuthRepoDep, settings: SettingsDep
) -> PrincipalInfo:
    """Authenticate the owner debug console by a capability-token bearer key.

    Two gates, both fail-closed: the feature must be enabled (a 404 hides the
    surface entirely when it is off — no oracle that the route even exists), and
    the key must resolve to a live, unexpired, unrevoked capability_token. The
    lookup is physically distinct from the owner-cookie and device paths, so a
    debug token can never reach owner/member/data routes and vice-versa."""
    if not settings.debug_access_enabled:
        raise HTTPException(status_code=404, detail="not found")
    key = _bearer(request.headers.get("Authorization", ""))
    principal = await service.authenticate_capability(repo, key) if key else None
    if principal is None:
        raise HTTPException(
            status_code=401,
            detail="invalid debug token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return principal


# A debug-console principal: gates the /api/debug/* surface behind a live,
# revocable, time-boxed capability token (and the JBRAIN_DEBUG_ACCESS_ENABLED flag).
DebugDep = Annotated[PrincipalInfo, Depends(current_debug_principal)]
