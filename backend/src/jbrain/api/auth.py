from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jbrain.api.deps import AuthRepoDep, PrincipalDep, SettingsDep
from jbrain.api.session_cookie import delete_session_cookies, session_token, set_session_cookie
from jbrain.auth import service
from jbrain.auth.service import InvalidCredentials

router = APIRouter(prefix="/auth")


class LoginRequest(BaseModel):
    # Unauthenticated: bounded so a caller can't make the api normalize and hash megabytes.
    owner_key: str = Field(max_length=1024)
    device_label: str = ""


@router.post("/session", status_code=204)
async def create_session(
    body: LoginRequest,
    response: Response,
    repo: AuthRepoDep,
    settings: SettingsDep,
) -> None:
    try:
        token = await service.login(repo, body.owner_key, body.device_label)
    except InvalidCredentials:
        raise HTTPException(status_code=401, detail="invalid key") from None
    set_session_cookie(response, settings, token, samesite="lax", max_age=60 * 60 * 24 * 365)


@router.get("/me")
async def me(principal: PrincipalDep) -> dict[str, str]:
    return {"principal_id": principal.id, "kind": principal.kind, "label": principal.label}


@router.delete("/session", status_code=204)
async def delete_session(
    request: Request,
    response: Response,
    repo: AuthRepoDep,
    settings: SettingsDep,
) -> None:
    await service.logout(repo, session_token(request, settings))
    # Both names: a browser mid-migration may hold either, and a stale one left behind
    # would quietly sign it back in.
    delete_session_cookies(response, settings)
