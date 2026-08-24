"""jerv's 1f916.ai citizenship panel (docs/plans/F1916_CITIZENSHIP_PLAN.md, W1).

Register and rotate NEVER pass through the agent loop (threat model §2.2): they are
owner-only API routes behind a PWA Settings panel, the Tavily-key precedent. The
register handler consumes the platform's one-time secret from the HTTP response
itself, stores it in owner-only app.settings, and answers with booleans + the
public handle only — the secret appears in no response body, no log line, and no
tool output, ever.

Registration binds an Ed25519 identity key generated here ON-BOX (the platform will
never generate one — "a key the server made is a key the server held"); the private
half goes straight into the settings store and is never model-reachable. It is the
platform's only recovery-adjacent mechanism: a signed public disavowal if the
bearer secret is ever stolen and rotated away.

The platform's hard-won register advice is followed literally: persist the secret
FIRST, read the stored copy back, then prove it live with `GET /api/me` while the
response is still in hand (two citizens died in week one from dropping the
response). Registering twice mints a second citizen, so a box that already holds a
secret refuses to register again — rotate instead.
"""

import base64
import re

import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from jbrain.api.deps import PrincipalDep
from jbrain.api.notes import ctx_for
from jbrain.api.settings import SettingsStoreDep
from jbrain.db.session import SessionContext
from jbrain.settings_store import SqlSettingsStore
from jbrain.web.f1916 import F1916Client, F1916Error, find_secret

log = structlog.get_logger()

router = APIRouter()

# Belt-and-braces on every human-readable detail string: no response leaves this
# router carrying anything shaped like a citizen secret, whatever upstream echoed.
_SECRET_RE = re.compile(r"1f916_sk_[A-Za-z0-9_\-]+")
# The platform publishes no handle rules at the door; this is our own sanity bound so
# a typo'd paste fails here with a clear message instead of on the wire.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{1,38}$")


def _scrub(text: str) -> str:
    return _SECRET_RE.sub("1f916_sk_[redacted]", text)


def _client(request: Request) -> F1916Client:
    return request.app.state.f1916_client


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class F1916StatusOut(BaseModel):
    # Booleans + the PUBLIC handle only — the secret and the signing key are never in
    # any response shape (the Tavily key_set precedent).
    enabled: bool
    registered: bool
    handle: str
    signing_key_set: bool


class F1916Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None


class F1916RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Both published forever on the forum — the owner types them at the panel
    # (plan §4 owner decision points). `model` is self-declared testimony.
    handle: str = Field(min_length=2, max_length=39)
    model: str = Field(min_length=1, max_length=120)


class F1916ActionOut(BaseModel):
    ok: bool
    detail: str
    status: F1916StatusOut


async def _status(store: SqlSettingsStore, ctx: SessionContext) -> F1916StatusOut:
    return F1916StatusOut(
        enabled=await store.f1916_enabled(ctx),
        registered=bool(await store.f1916_secret_key(ctx)),
        handle=await store.f1916_handle(ctx),
        signing_key_set=bool(await store.f1916_signing_key(ctx)),
    )


@router.get("/settings/1f916")
async def read_f1916_settings(principal: PrincipalDep, store: SettingsStoreDep) -> F1916StatusOut:
    return await _status(store, ctx_for(principal))


@router.put("/settings/1f916")
async def update_f1916_settings(
    body: F1916Patch, principal: PrincipalDep, store: SettingsStoreDep
) -> F1916StatusOut:
    ctx = ctx_for(principal)
    if body.enabled is not None:
        await store.set_f1916_enabled(ctx, body.enabled)
    return await _status(store, ctx)


@router.post("/settings/1f916/register")
async def register_f1916_citizen(
    body: F1916RegisterIn, request: Request, principal: PrincipalDep, store: SettingsStoreDep
) -> F1916ActionOut:
    ctx = ctx_for(principal)
    if await store.f1916_secret_key(ctx):
        return F1916ActionOut(
            ok=False,
            detail=(
                "A citizen is already registered on this box — registering again would mint "
                "a second, unrelated citizen. Rotate the secret instead if it may be exposed."
            ),
            status=await _status(store, ctx),
        )
    handle = body.handle.strip()
    if not _HANDLE_RE.match(handle):
        return F1916ActionOut(
            ok=False,
            detail=(
                "Handle must be 2-39 characters of letters, digits, - or _, starting alphanumeric."
            ),
            status=await _status(store, ctx),
        )
    # The identity key is generated here, on-box, and only its public half ever leaves.
    private_key = Ed25519PrivateKey.generate()
    public_b64 = _b64url(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
    )
    statement = f"1f916.key-bind.v1:{handle}:{public_b64}"
    signature = _b64url(private_key.sign(statement.encode("utf-8")))
    client = _client(request)
    try:
        response = await client.register(
            handle=handle, model=body.model.strip(), public_key=public_b64, signature=signature
        )
    except F1916Error as exc:
        return F1916ActionOut(
            ok=False,
            detail=_scrub(f"1f916 refused the registration: {exc}"),
            status=await _status(store, ctx),
        )
    secret = find_secret(response)
    if not secret:
        # Registered-but-unstored is the platform's known lethal failure — say exactly
        # what happened; the handle may now be burned even though we hold no key.
        log.error("f1916.register_no_secret", handle=handle, keys=sorted(response.keys()))
        return F1916ActionOut(
            ok=False,
            detail=(
                "1f916 answered the registration but no citizen secret could be found in "
                "the response — nothing was stored. The handle may or may not be taken now; "
                "check the identity log (f1916 action=events) before retrying."
            ),
            status=await _status(store, ctx),
        )
    # Persist FIRST, before anything else touches the response (the door's advice).
    await store.set_f1916_citizen(
        ctx,
        handle=handle,
        secret=secret,
        signing_key=_b64url(
            private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        ),
        public_key=public_b64,
        registered_at=str(response.get("now_utc") or ""),
    )
    stored = await store.f1916_secret_key(ctx)
    if stored != secret:
        log.error("f1916.register_store_mismatch", handle=handle)
        return F1916ActionOut(
            ok=False,
            detail=(
                "The secret failed to read back from the settings store after saving — "
                "treat this citizen as unusable and alert the developer before retrying."
            ),
            status=await _status(store, ctx),
        )
    ok, verify_detail = await _verify_me(client)
    detail = f"Registered as @{handle} with the identity key bound. " + (
        "Live check passed: the stored secret reads back and /api/me answers."
        if ok
        else f"BUT the live check failed ({verify_detail}) — do not register again; "
        "use Test to re-check, and rotate if it keeps failing."
    )
    log.info("f1916.registered", handle=handle, verified=ok)
    return F1916ActionOut(ok=ok, detail=_scrub(detail), status=await _status(store, ctx))


@router.post("/settings/1f916/rotate")
async def rotate_f1916_secret(
    request: Request, principal: PrincipalDep, store: SettingsStoreDep
) -> F1916ActionOut:
    ctx = ctx_for(principal)
    if not await store.f1916_secret_key(ctx):
        return F1916ActionOut(
            ok=False,
            detail="No citizen is registered, so there is no secret to rotate.",
            status=await _status(store, ctx),
        )
    client = _client(request)
    try:
        response = await client.rotate()
    except F1916Error as exc:
        return F1916ActionOut(
            ok=False,
            detail=_scrub(f"1f916 refused the rotation: {exc}"),
            status=await _status(store, ctx),
        )
    secret = find_secret(response)
    if not secret:
        log.error("f1916.rotate_no_secret", keys=sorted(response.keys()))
        return F1916ActionOut(
            ok=False,
            detail=(
                "1f916 answered the rotation but no new secret could be found in the "
                "response. The OLD secret may already be dead — run Test, and if it fails "
                "the citizen is likely lost (rotation is the platform's only recovery)."
            ),
            status=await _status(store, ctx),
        )
    await store.set_f1916_secret(ctx, secret)
    ok, verify_detail = await _verify_me(client)
    detail = (
        "Secret rotated; the old one is dead. Live check passed."
        if ok
        else f"Secret rotated and stored, but the live check failed ({verify_detail})."
    )
    log.info("f1916.rotated", verified=ok)
    return F1916ActionOut(ok=ok, detail=_scrub(detail), status=await _status(store, ctx))


@router.post("/settings/1f916/test")
async def test_f1916_settings(
    request: Request, principal: PrincipalDep, store: SettingsStoreDep
) -> F1916ActionOut:
    """The live "Test" probe — `GET /api/me` with the stored secret, so the owner
    verifies citizenship from the PWA with no terminal (non-negotiable #10)."""
    ctx = ctx_for(principal)
    if not await store.f1916_secret_key(ctx):
        return F1916ActionOut(
            ok=False, detail="No citizen is registered yet.", status=await _status(store, ctx)
        )
    ok, detail = await _verify_me(_client(request))
    return F1916ActionOut(ok=ok, detail=_scrub(detail), status=await _status(store, ctx))


async def _verify_me(client: F1916Client) -> tuple[bool, str]:
    try:
        await client.me()
    except F1916Error as exc:
        if exc.status == 401:
            return False, "1f916 rejected the stored secret (401)"
        return False, str(exc)
    return True, "the citizen answers"
