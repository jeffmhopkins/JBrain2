"""Internal LLM routes the worker calls on the docker `internal` network.

Mounted under `/internal`, which Caddy never routes off-box, and additionally gated on a
bearer derived from the supervisor token (`jbrain.llm.gateway_regen.regen_token`) — so a
container on the internal network that does not hold that secret cannot trigger a re-stamp
(each one can cost the resident set a llama-swap reload)."""

import hmac
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from jbrain.api import llm_settings
from jbrain.api.deps import SettingsDep
from jbrain.llm.gateway_regen import regen_token
from jbrain.settings_store import SqlSettingsStore

router = APIRouter(prefix="/llm")


class RegenOut(BaseModel):
    # Why the gateway is serving stale flags after this re-stamp, or None when it is current —
    # the same value the settings screen shows.
    error: str | None


def _authorize(request: Request, supervisor_token: str) -> None:
    expected = regen_token(supervisor_token)
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        presented = ""
    # Fail closed with no secret configured: an empty expected token must not match an empty
    # presented one.
    if not expected or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status_code=403, detail="forbidden")


@router.post("/regen-gateway-config")
async def regen_gateway_config(request: Request, settings: SettingsDep) -> RegenOut:
    """Re-stamp the active engine's llama-swap config from the saved overrides, for a load the
    WORKER is about to run (it cannot write the config itself — jbrain.llm.gateway_regen).
    Returns only after llama-swap's reload has settled, so the caller's load cannot start
    inside it. Serialised with the api's own re-stamps by `llm_settings._REGEN_LOCK`.

    Takes no body: there is nothing a caller may choose — the re-stamp renders the saved
    settings, nothing else."""
    _authorize(request, settings.supervisor_token)
    if await request.body():
        raise HTTPException(status_code=422, detail="this route takes no body")
    store = cast(SqlSettingsStore, request.app.state.settings_store)
    await llm_settings.regen_gateway_config(settings, store)
    return RegenOut(error=llm_settings.gateway_config_error())
