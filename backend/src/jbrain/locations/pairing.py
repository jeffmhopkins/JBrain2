"""Pairing-code minting + redemption (JBrain360 M2c).

`mint_code` is owner-only (runs under the owner ctx; the pairing_code RLS enforces).
`redeem` calls the SECURITY DEFINER `app.redeem_pairing_code`, which atomically
validates the code and mints the device subject + `device_key` principal — so it
needs no scope; a bare session suffices. The plaintext key is generated here and
returned exactly once; only its hash reaches the DB.
"""

import base64
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jbrain.auth import keys
from jbrain.db.session import SessionContext, scoped_session

CODE_TTL = timedelta(minutes=15)
# Redemption invokes a SECURITY DEFINER function that does its own privileged
# inserts, so the caller needs no scope.
_REDEEM_CTX = SessionContext()


@dataclass(frozen=True)
class RedeemedDevice:
    subject_id: str
    principal_id: str  # the MQTT username — the M0 broker ACL binds it to this id
    label: str
    monitoring: int
    key: str  # the device key = MQTT password, returned exactly once


def generate_pairing_code() -> str:
    """A high-entropy (~160-bit), one-time, URL/QR-safe code."""
    return secrets.token_urlsafe(20)


# The embeddable pairing payload version — bump if the shape changes so an old app
# can reject a newer payload it can't parse.
PAIRING_PAYLOAD_VERSION = 1


def build_pairing_payload(server_base: str, code: str) -> str:
    """A single self-contained string the owner shares and the app pastes/scans: it
    embeds the server URL alongside the one-time code, so the app learns where to
    redeem (and operate) from the code itself — no server URL baked into the build.
    base64url(JSON), so it stays one opaque QR-safe token and is extensible (add
    fields under new keys without breaking older readers)."""
    raw = json.dumps(
        {"v": PAIRING_PAYLOAD_VERSION, "u": server_base.rstrip("/"), "c": code},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def build_owntracks_config(
    device: RedeemedDevice, *, broker_host: str, broker_port: int
) -> dict[str, Any]:
    """The OwnTracks `_type:configuration` the fork injects (owntracks:///config).

    `username`/`clientId` = the device principal id, because the M0 broker ACL binds
    the MQTT username to it; the device key is the password. `remoteConfiguration`
    is enabled (default false upstream) so the server can switch monitoring mode.
    """
    pid = device.principal_id
    return {
        "_type": "configuration",
        "mode": 0,  # 0 = MQTT
        "host": broker_host,
        "port": broker_port,
        "tls": True,
        "username": pid,
        "password": device.key,
        "deviceId": "phone",
        "clientId": pid,
        "pubTopicBase": f"owntracks/{pid}/phone",
        "sub": True,
        "subTopic": "owntracks/+/+",
        "monitoring": device.monitoring,
        "cmd": True,
        "remoteConfiguration": True,
    }


class PairingRepo(Protocol):
    async def mint_code(
        self,
        ctx: SessionContext,
        *,
        label: str,
        monitoring: int,
        subject_id: str | None = None,
        ttl: timedelta = ...,
    ) -> tuple[str, datetime]: ...

    async def redeem(self, code: str) -> RedeemedDevice | None: ...


class SqlPairingRepo:
    def __init__(self, maker: async_sessionmaker[AsyncSession]):
        self._maker = maker

    async def mint_code(
        self,
        ctx: SessionContext,
        *,
        label: str,
        monitoring: int,
        subject_id: str | None = None,
        ttl: timedelta = CODE_TTL,
    ) -> tuple[str, datetime]:
        """Owner-only: create a one-time code. With `subject_id` it targets an
        EXISTING device (re-pair) — redemption rotates that device's key in place;
        without it the code provisions a fresh device. Returns (code, expiry)."""
        code = generate_pairing_code()
        expires_at = datetime.now(UTC) + ttl
        async with scoped_session(self._maker, ctx) as session:
            await session.execute(
                text(
                    "INSERT INTO app.pairing_code (code, label, monitoring, subject_id, expires_at)"
                    " VALUES (:c, :l, :m, :s, :e)"
                ),
                {"c": code, "l": label, "m": monitoring, "s": subject_id, "e": expires_at},
            )
        return code, expires_at

    async def redeem(self, code: str) -> RedeemedDevice | None:
        """Atomically redeem a code into a fresh device; None if invalid/expired/used."""
        if not code:
            return None
        key = keys.generate_owner_key()
        async with scoped_session(self._maker, _REDEEM_CTX) as session:
            row = (
                await session.execute(
                    text(
                        "SELECT subject_id::text AS sid, principal_id::text AS pid,"
                        "   label, monitoring"
                        " FROM app.redeem_pairing_code(:c, :kh)"
                    ),
                    {"c": code, "kh": keys.hash_key(key)},
                )
            ).first()
        if row is None:
            return None
        return RedeemedDevice(
            subject_id=row.sid,
            principal_id=row.pid,
            label=row.label,
            monitoring=row.monitoring,
            key=key,
        )
