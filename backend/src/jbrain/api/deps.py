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
