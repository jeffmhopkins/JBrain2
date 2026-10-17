"""The owner tunnel's phones: enrolling one by QR on the box's screen (§3.5), the bound
session an enrolled phone mints, and the owner's list of phones with rename and revoke
(docs/plans/OWNER_TUNNEL_PLAN.md §3.4)."""

import hmac
import ipaddress
import uuid
from datetime import datetime
from typing import Annotated, cast

import segno
from cryptography import x509
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from jbrain.api import client_ip
from jbrain.api import endpoint as endpoint_api
from jbrain.api.deps import AuthRepoDep, OwnerDep, SettingsDep
from jbrain.api.internal_auth import require_wall
from jbrain.api.session_cookie import set_session_cookie
from jbrain.api.ws_reauth import kick
from jbrain.auth import service
from jbrain.auth.service import BOUND_SESSION_IDLE, PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.owner_tunnel import enrollment, keys
from jbrain.owner_tunnel.devices import AlreadyEnrolled, OwnerDeviceInfo, OwnerDeviceRepo
from jbrain.owner_tunnel.enrollment import EnrollmentWindow
from jbrain.owner_tunnel.guard import STATE_KEY, kick_device
from jbrain.settings_store import OWNER_ENROLL_SUBNET_KEY, SqlSettingsStore
from jbrain.storage import FsOwnerKeyFiles

router = APIRouter(prefix="/owner-devices")
# The wall's view of the enrollment window, for the QR on the box's own screen.
internal_router = APIRouter(prefix="/owner-enroll", dependencies=[Depends(require_wall)])

LABEL_MAX_BYTES = 80
# The enrollment route runs before any principal exists; reading the owner's subnet setting
# needs the owner's RLS identity, and nothing else is read under it.
_SYSTEM_OWNER = SessionContext(principal_kind="owner")


def get_owner_device_repo(request: Request) -> OwnerDeviceRepo:
    return cast(OwnerDeviceRepo, request.app.state.owner_device_repo)


OwnerDeviceRepoDep = Annotated[OwnerDeviceRepo, Depends(get_owner_device_repo)]


def get_enrollment_window(request: Request) -> EnrollmentWindow:
    return cast(EnrollmentWindow, request.app.state.owner_enrollment)


EnrollmentWindowDep = Annotated[EnrollmentWindow, Depends(get_enrollment_window)]


def _store(request: Request) -> SqlSettingsStore:
    return cast(SqlSettingsStore, request.app.state.settings_store)


def _key_files(settings: Settings) -> FsOwnerKeyFiles:
    return FsOwnerKeyFiles(settings.owner_keys_dir, settings.owner_site_dir)


def _label(raw: str) -> str:
    label = raw.strip()
    if not label:
        raise HTTPException(status_code=422, detail="label is blank")
    # The column's limit is in bytes, so a label of wide characters can pass the field's.
    if len(label.encode()) > LABEL_MAX_BYTES:
        raise HTTPException(status_code=422, detail="label is too long")
    return label


def _ctx(owner: PrincipalInfo) -> SessionContext:
    return SessionContext(principal_id=owner.id, principal_kind="owner")


class DeviceOut(BaseModel):
    id: str
    label: str
    enrolled_at: datetime
    last_seen_at: datetime | None
    last_ingress: str | None
    revoked: bool

    @classmethod
    def of(cls, d: OwnerDeviceInfo) -> "DeviceOut":
        return cls(
            id=d.id,
            label=d.label,
            enrolled_at=d.enrolled_at,
            last_seen_at=d.last_seen_at,
            last_ingress=d.last_ingress,
            revoked=d.revoked_at is not None,
        )


class RenameIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)


@router.post("/session", status_code=204)
async def mint_session(
    request: Request, response: Response, repo: AuthRepoDep, settings: SettingsDep
) -> None:
    """The app's sign-in: an owner session bound to the phone this request came from.
    Only reachable through the owner site, where the guard has already matched the phone's
    verified key to an enrolled, unrevoked device."""
    device = cast(OwnerDeviceInfo | None, request.scope.get("state", {}).get(STATE_KEY))
    if device is None:
        raise HTTPException(status_code=403, detail="only from an enrolled phone")
    token = await service.mint_device_owner_session(repo, device.id, f"phone: {device.label}")
    if token is None:
        raise HTTPException(status_code=409, detail="no owner")
    set_session_cookie(
        response,
        settings,
        token,
        samesite="lax",
        max_age=int(BOUND_SESSION_IDLE.total_seconds()),
    )


@router.get("")
async def list_devices(owner: OwnerDep, devices: OwnerDeviceRepoDep) -> list[DeviceOut]:
    return [DeviceOut.of(d) for d in await devices.list_all(_ctx(owner))]


@router.patch("/{device_id}", status_code=204)
async def rename_device(
    device_id: str, body: RenameIn, owner: OwnerDep, devices: OwnerDeviceRepoDep
) -> None:
    if not await devices.rename(_ctx(owner), device_id, _label(body.label)):
        raise HTTPException(status_code=404, detail="no such device")


def _canonical(device_id: str) -> str:
    """The id as the guard keys its watchers: Postgres matched an uppercase or braced UUID in
    the path, but the kick must name the row's own spelling or it reaches no one."""
    try:
        return str(uuid.UUID(device_id))
    except ValueError:
        return device_id


@router.post("/{device_id}/revoke", status_code=204)
async def revoke_device(
    device_id: str, request: Request, owner: OwnerDep, devices: OwnerDeviceRepoDep
) -> None:
    if not await devices.revoke(_ctx(owner), device_id):
        raise HTTPException(status_code=404, detail="no such device")
    # Everything the phone has open ends now rather than at the next tick: its owner-site
    # requests and streams (kick_device), and sockets holding its sessions (kick).
    kick_device(request.app, _canonical(device_id))
    kick(request.app, owner.id)


# --- enrollment (§3.5) -------------------------------------------------------------------


class ArmOut(BaseModel):
    expires_at: datetime


@router.post("/enroll/arm")
async def arm_enrollment(_owner: OwnerDep, window: EnrollmentWindowDep) -> ArmOut:
    """Open the window for the box's screen to show a QR. Reveals nothing, so it answers on
    any site."""
    return ArmOut(expires_at=window.arm())


async def _home_subnet(request: Request) -> str:
    value = await _store(request).get(_SYSTEM_OWNER, OWNER_ENROLL_SUBNET_KEY, "")
    return value if isinstance(value, str) else ""


async def _from_home_network(request: Request) -> bool:
    """The LAN site, not by way of the tunnel, and inside the home subnet when one is set.
    Never "a private address": the docker networks are private too, and sandboxes run
    arbitrary code on them. Defence in depth only; the QR is the proof."""
    if client_ip.ingress(request) != "lan" or client_ip.via_tunnel(request):
        return False
    subnet = await _home_subnet(request)
    if not subnet:
        return True
    address = client_ip.client_ip(request)
    try:
        return address is not None and ipaddress.ip_address(address) in ipaddress.ip_network(subnet)
    except ValueError:
        return False


class EnrollIn(BaseModel):
    csr: str = Field(max_length=8192)
    label: str = Field(min_length=1, max_length=LABEL_MAX_BYTES)
    # base64url of HMAC-SHA256(code, "app->box" label + csr)
    mac: str = Field(max_length=64)


class EnrollOut(BaseModel):
    device_id: str
    cert: str
    owner_ca: str
    enroll_ca: str
    words: list[str]
    mac: str


@router.post("/enroll")
async def enroll(
    request: Request,
    body: EnrollIn,
    window: EnrollmentWindowDep,
    devices: OwnerDeviceRepoDep,
    settings: SettingsDep,
) -> EnrollOut:
    """A phone that scanned the box's QR sends its certificate request, bound to the QR's code
    by a MAC; the box signs it and answers with a MAC of its own over everything it returns.
    The code is spent by the first request that proves it, whatever happens after."""
    if not await _from_home_network(request):
        raise HTTPException(status_code=403, detail="enrollment is open only on the home network")
    label = _label(body.label)
    csr_pem = body.csr.encode()
    try:
        csr = keys.parse_device_csr(csr_pem)
        tag = enrollment.b64url_decode(body.mac)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="malformed request") from exc
    code = window.claim(lambda c: hmac.compare_digest(enrollment.app_mac(c, csr_pem), tag))
    if code is None:
        raise HTTPException(status_code=403, detail="no open enrollment matches")
    try:
        issued = keys.issue_device(_key_files(settings), csr)
    except keys.KeysNotReady as exc:
        raise HTTPException(status_code=503, detail="the box's keys are not made yet") from exc
    try:
        device = await devices.create(label, issued.key_hash, issued.public_key_der)
    except AlreadyEnrolled as exc:
        raise HTTPException(status_code=409, detail="this phone's key is already enrolled") from exc
    words = enrollment.fingerprint(issued.key_hash)
    window.record(enrollment.Enrolled(device.id, words))
    mac = enrollment.box_mac(code, tag, issued.cert_pem, issued.owner_ca_pem, issued.enroll_ca_pem)
    return EnrollOut(
        device_id=device.id,
        cert=issued.cert_pem.decode(),
        owner_ca=issued.owner_ca_pem.decode(),
        enroll_ca=issued.enroll_ca_pem.decode(),
        words=list(words),
        mac=enrollment.b64url(mac),
    )


class SubnetIO(BaseModel):
    # A CIDR such as 192.168.1.0/24; empty for no address rule.
    subnet: str = Field(max_length=64)


@router.get("/enroll/subnet")
async def get_enroll_subnet(request: Request, _owner: OwnerDep) -> SubnetIO:
    return SubnetIO(subnet=await _home_subnet(request))


@router.put("/enroll/subnet")
async def set_enroll_subnet(request: Request, body: SubnetIO, owner: OwnerDep) -> SubnetIO:
    """LAN only: from anywhere else, a stolen owner session could widen it to the internet."""
    if client_ip.ingress(request) != "lan" or client_ip.via_tunnel(request):
        raise HTTPException(status_code=403, detail="change this from the home network")
    value = body.subnet.strip()
    if value:
        try:
            value = str(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="not a subnet") from exc
    await _store(request).upsert(_ctx(owner), OWNER_ENROLL_SUBNET_KEY, value)
    return SubnetIO(subnet=value)


# --- the box's screen ----------------------------------------------------------------------


class ScreenOut(BaseModel):
    state: str
    expires_at: datetime | None = None
    # An SVG of the QR, only while the window is open and its code unused.
    qr_svg: str | None = None
    words: list[str] | None = None
    # Why the QR cannot be shown though the window is open, for the screen to say.
    problem: str | None = None


def _qr_svg(payload: str) -> str:
    return segno.make(payload, error="m").svg_inline(scale=8, border=2, dark="#000", light="#fff")


@internal_router.get("")
async def screen(request: Request, window: EnrollmentWindowDep, settings: SettingsDep) -> ScreenOut:
    snap = window.snapshot()
    words = list(snap.enrolled.words) if snap.enrolled else None
    if snap.code is None:
        return ScreenOut(state=snap.state, expires_at=snap.expires_at, words=words)
    lan_url = settings.lan_addr.strip().rstrip("/")
    lan_root = endpoint_api._lan_ca()
    if not lan_url or not lan_root:
        # The phone enrolls over the LAN site and validates it by the QR's root pin, so
        # without both there is nothing safe to put in the QR.
        return ScreenOut(
            state=snap.state, expires_at=snap.expires_at, problem="this box has no LAN site"
        )
    try:
        owner_pin = keys.spki_pin(keys.owner_ca_cert(_key_files(settings)))
    except keys.KeysNotReady:
        return ScreenOut(
            state=snap.state, expires_at=snap.expires_at, problem="the box's keys are not made yet"
        )
    lan_pin = keys.spki_pin(x509.load_pem_x509_certificate(lan_root.encode()))
    payload = enrollment.qr_payload(
        snap.code,
        owner_ca_pin=owner_pin,
        lan_url=lan_url,
        lan_root_pin=lan_pin,
        public_url=settings.public_base_url.strip().rstrip("/"),
    )
    return ScreenOut(state=snap.state, expires_at=snap.expires_at, qr_svg=_qr_svg(payload))


@internal_router.post("/arm")
async def arm_from_the_box(window: EnrollmentWindowDep) -> ArmOut:
    """A key on the box's own keyboard."""
    return ArmOut(expires_at=window.arm())


@internal_router.post("/close", status_code=204)
async def close_from_the_box(window: EnrollmentWindowDep) -> None:
    window.close()


@internal_router.post("/reject", status_code=204)
async def reject_from_the_box(
    request: Request, window: EnrollmentWindowDep, devices: OwnerDeviceRepoDep
) -> None:
    """The words did not match: revoke the phone just enrolled, on the spot."""
    snap = window.snapshot()
    if snap.enrolled is None:
        raise HTTPException(status_code=404, detail="no phone was just enrolled")
    await devices.revoke(_SYSTEM_OWNER, snap.enrolled.device_id)
    window.close()
    kick_device(request.app, snap.enrolled.device_id)
