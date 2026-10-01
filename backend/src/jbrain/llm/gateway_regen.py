"""The worker's pre-load config re-stamp, delegated to the api over the internal network.

The worker loads models too, and a load must serve the SAVED window/slot/flag overrides — so
it has to re-stamp the active engine's llama-swap config first, exactly as the api's loads do.
It must not write that file itself: the worker processes untrusted content (mail, web pages,
uploads), and a writable models directory would let a compromised worker write a llama-swap
`cmd:` that the gateway executes, or swap a GGUF. So its models mount stays read-only and it
asks the api, which owns the write, serialises it with its own loads' re-stamps, and keeps
any failure where the settings screen reads it (`llm_settings.gateway_config_error`).

The bearer is DERIVED from the supervisor token both processes already hold, rather than a new
`.env` secret the owner would have to create from a shell (CLAUDE.md #10). It is a narrow
capability: knowing it lets a caller ask for a re-stamp from the saved settings, nothing else.
"""

import hashlib
import hmac

import httpx

# The api route this calls (`jbrain.api.llm_internal`), under the `/internal` prefix Caddy never
# routes off-box.
REGEN_PATH = "/internal/llm/regen-gateway-config"

# The re-stamp waits for llama-swap's reload to land (`llm_settings._GATEWAY_RELOAD_SETTLE_S`,
# 4 s) and may queue behind another one, so this is a few of those with margin. Bounded because
# a hung api must not hold a worker load forever; a timeout is a failed re-stamp, which the
# gateway client logs and loads through, exactly as it does for a local one.
REGEN_TIMEOUT_S = 30.0

_CONTEXT = b"jbrain:internal:llm-regen-gateway-config"


def regen_token(supervisor_token: str) -> str:
    """The bearer for the re-stamp route, or "" when there is no secret to derive it from — in
    which case the route refuses every caller (fail closed)."""
    if not supervisor_token:
        return ""
    return hmac.new(supervisor_token.encode(), _CONTEXT, hashlib.sha256).hexdigest()


async def request_regen(
    api_url: str,
    supervisor_token: str,
    *,
    timeout_s: float = REGEN_TIMEOUT_S,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Ask the api to re-stamp the active engine's config, returning once its reload settled.
    Raises on any failure; the caller (`LocalGatewayClient._config_regen`) logs and proceeds."""
    token = regen_token(supervisor_token)
    if not token:
        raise RuntimeError("no supervisor token to authenticate the re-stamp request with")
    async with httpx.AsyncClient(timeout=timeout_s, transport=transport) as client:
        resp = await client.post(
            f"{api_url.rstrip('/')}{REGEN_PATH}",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        # The re-stamp itself can fail in the api (an unwritable or unrenderable config) and
        # still answer 200 — the api records it for the settings screen. Raise so the worker's
        # log says so too, rather than reading as a clean re-stamp.
        try:
            body = resp.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise RuntimeError("the api answered the re-stamp with an unexpected body")
        error = body.get("error")
        if error:
            raise RuntimeError(f"the api could not re-stamp the gateway config: {error}")
