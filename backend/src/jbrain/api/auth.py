from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from jbrain.api.deps import AuthRepoDep, PrincipalDep, SettingsDep
from jbrain.auth import service
from jbrain.auth.service import InvalidCredentials

router = APIRouter(prefix="/auth")


class LoginRequest(BaseModel):
    owner_key: str
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
    response.set_cookie(
        settings.session_cookie,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=60 * 60 * 24 * 365,
    )


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
    await service.logout(repo, request.cookies.get(settings.session_cookie, ""))
    response.delete_cookie(settings.session_cookie)
