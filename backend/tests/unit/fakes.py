"""In-memory AuthRepo for unit-testing auth flows without Postgres."""

import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jbrain.auth.service import CapabilityToken, PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.devices.repo import DeviceInfo
from jbrain.locations.pairing import CODE_TTL, RedeemedDevice


@dataclass
class FakePrincipal:
    id: str
    kind: str
    key_hash: str
    label: str
    revoked: bool = False
    subject_id: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    last_used_at: datetime | None = None


@dataclass
class FakeSession:
    principal_id: str
    token_hash: str
    label: str
    revoked: bool = False


@dataclass
class FakeAuthRepo:
    principals: list[FakePrincipal] = field(default_factory=list)
    sessions: list[FakeSession] = field(default_factory=list)

    async def find_active_principal_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        for p in self.principals:
            if p.key_hash == key_hash and not p.revoked:
                return _info(p)
        return None

    async def find_active_device_principal_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        for p in self.principals:
            if p.key_hash == key_hash and p.kind == "device_key" and not p.revoked:
                return _info(p)
        return None

    async def find_active_device_principal_by_id(self, principal_id: str) -> PrincipalInfo | None:
        for p in self.principals:
            if p.id == principal_id and p.kind == "device_key" and not p.revoked:
                return _info(p)
        return None

    async def create_session(self, principal_id: str, token_hash: str, label: str) -> None:
        self.sessions.append(FakeSession(principal_id, token_hash, label))

    async def find_principal_by_session_token_hash(self, token_hash: str) -> PrincipalInfo | None:
        for s in self.sessions:
            if s.token_hash == token_hash and not s.revoked:
                for p in self.principals:
                    if p.id == s.principal_id and not p.revoked:
                        return _info(p)
        return None

    async def revoke_session(self, token_hash: str) -> None:
        for s in self.sessions:
            if s.token_hash == token_hash:
                s.revoked = True

    async def revoke_principals_of_kind(self, kind: str) -> None:
        for p in self.principals:
            if p.kind == kind:
                p.revoked = True

    async def create_principal(
        self, kind: str, key_hash: str, label: str, subject_id: str | None = None
    ) -> None:
        self.principals.append(
            FakePrincipal(str(uuid.uuid4()), kind, key_hash, label, subject_id=subject_id or "")
        )

    async def create_capability(
        self, key_hash: str, label: str, expires_at: datetime | None
    ) -> CapabilityToken:
        p = FakePrincipal(
            str(uuid.uuid4()), "capability_token", key_hash, label, expires_at=expires_at
        )
        self.principals.append(p)
        return _capability(p)

    async def find_active_capability_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        now = datetime.now(UTC)
        for p in self.principals:
            live = p.expires_at is None or p.expires_at > now
            if p.key_hash == key_hash and p.kind == "capability_token" and not p.revoked and live:
                p.last_used_at = now
                return _info(p)
        return None

    async def list_capabilities(self) -> list[CapabilityToken]:
        return [_capability(p) for p in self.principals if p.kind == "capability_token"]

    async def revoke_capability(self, capability_id: str) -> bool:
        for p in self.principals:
            if p.id == capability_id and p.kind == "capability_token" and not p.revoked:
                p.revoked = True
                return True
        return False


def _info(p: FakePrincipal) -> PrincipalInfo:
    return PrincipalInfo(id=p.id, kind=p.kind, label=p.label, subject_id=p.subject_id)


def _capability(p: FakePrincipal) -> CapabilityToken:
    return CapabilityToken(
        id=p.id,
        label=p.label,
        created_at=p.created_at,
        expires_at=p.expires_at,
        last_used_at=p.last_used_at,
        revoked_at=p.created_at if p.revoked else None,
    )


@dataclass
class FakeViewScopeRepo:
    """In-memory view-scope: the (viewer_subject, target_subject) pairs allowed to see."""

    allowed: set[tuple[str, str]] = field(default_factory=set)

    async def may_view(self, viewer_subject_id: str, target_subject_id: str) -> bool:
        return bool(viewer_subject_id) and (viewer_subject_id, target_subject_id) in self.allowed


@dataclass
class FakePairingRepo:
    """In-memory pairing repo: records mints and redeems configured codes."""

    minted: list[tuple[str, int]] = field(default_factory=list)  # (label, monitoring)
    targets: list[str | None] = field(default_factory=list)  # per-mint re-pair subject_id
    redeemable: dict[str, RedeemedDevice] = field(default_factory=dict)  # code -> device

    async def mint_code(
        self,
        ctx: SessionContext,
        *,
        label: str,
        monitoring: int,
        subject_id: str | None = None,
        ttl: timedelta = CODE_TTL,
    ) -> tuple[str, datetime]:
        self.minted.append((label, monitoring))
        self.targets.append(subject_id)
        return "fake-code", datetime.now(UTC) + ttl

    async def redeem(self, code: str) -> RedeemedDevice | None:
        return self.redeemable.get(code)


@dataclass
class FakeDeviceRepo:
    """In-memory DeviceRepo for unit-testing device provisioning without Postgres."""

    devices: list[DeviceInfo] = field(default_factory=list)
    key_hashes: dict[str, str] = field(default_factory=dict)  # device id -> active key hash

    async def provision(self, ctx: SessionContext, *, label: str, key_hash: str) -> DeviceInfo:
        device = DeviceInfo(
            id=str(uuid.uuid4()), label=label, created_at=datetime.now(UTC), revoked=False
        )
        self.devices.append(device)
        self.key_hashes[device.id] = key_hash
        return device

    async def list(self, ctx: SessionContext) -> Sequence[DeviceInfo]:
        return list(self.devices)

    async def rotate(self, ctx: SessionContext, device_id: str, key_hash: str) -> bool:
        if not any(d.id == device_id for d in self.devices):
            return False
        self.key_hashes[device_id] = key_hash
        return True

    async def revoke(self, ctx: SessionContext, device_id: str) -> bool:
        for i, d in enumerate(self.devices):
            if d.id == device_id:
                self.devices[i] = dataclasses.replace(d, revoked=True)
                self.key_hashes.pop(device_id, None)
                return True
        return False

    async def rename(self, ctx: SessionContext, device_id: str, label: str) -> bool:
        for i, d in enumerate(self.devices):
            if d.id == device_id:
                self.devices[i] = dataclasses.replace(d, label=label)
                return True
        return False

    async def delete(self, ctx: SessionContext, device_id: str) -> bool:
        for i, d in enumerate(self.devices):
            if d.id == device_id:
                del self.devices[i]
                self.key_hashes.pop(device_id, None)
                return True
        return False


@dataclass
class FakeSettingsStore:
    """In-memory app.settings: the same default semantics as the SQL store."""

    values: dict[str, object] = field(default_factory=dict)

    async def get(self, ctx: object, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    async def upsert(self, ctx: object, key: str, value: object) -> None:
        self.values[key] = value

    async def image_analysis_mode(self, ctx: object) -> str:
        mode = self.values.get("image_analysis_mode", "full")
        return mode if mode in ("full", "ocr") else "full"

    async def workflow_dispatch_mode(self, ctx: object) -> str:
        mode = self.values.get("workflow_dispatch_mode", "shadow")
        return mode if mode in ("shadow", "live", "off") else "shadow"

    async def owner_timezone(self, ctx: object) -> str | None:
        from jbrain.settings_store import is_valid_timezone

        tz = self.values.get("owner_timezone")
        return tz if isinstance(tz, str) and is_valid_timezone(tz) else None

    async def reflexion_buffer_retry(self, ctx: object) -> bool:
        return self.values.get("reflexion_buffer_retry", False) is True

    async def skills_enabled(self, ctx: object) -> bool:
        return self.values.get("skills_enabled", False) is True

    async def llm_task_overrides(self, ctx: object) -> dict[str, dict[str, str]]:
        # Mirrors the SQL store's sanitizing read (drops malformed entries).
        raw = self.values.get("llm_task_overrides", {})
        if not isinstance(raw, dict):
            return {}
        clean: dict[str, dict[str, str]] = {}
        for task, entry in raw.items():
            if not isinstance(task, str) or not isinstance(entry, dict):
                continue
            sane: dict[str, str] = {}
            spec = entry.get("spec")
            if isinstance(spec, str) and spec:
                sane["spec"] = spec
            effort = entry.get("reasoning_effort")
            if effort in ("none", "low", "medium", "high"):
                sane["reasoning_effort"] = effort
            if sane:
                clean[task] = sane
        return clean

    async def llm_local_context_windows(self, ctx: object) -> dict[str, int]:
        raw = self.values.get("llm_local_context_windows", {})
        if not isinstance(raw, dict):
            return {}
        return {
            mid: win
            for mid, win in raw.items()
            if isinstance(mid, str)
            and isinstance(win, int)
            and not isinstance(win, bool)
            and win > 0
        }

    async def set_llm_local_context_window(
        self, ctx: object, *, model_id: str, window: int | None
    ) -> dict[str, int]:
        current = await self.llm_local_context_windows(ctx)
        if window is None:
            current.pop(model_id, None)
        else:
            current[model_id] = window
        self.values["llm_local_context_windows"] = current
        return current

    async def llm_local_staged(self, ctx: object) -> list[str]:
        raw = self.values.get("llm_local_staged", [])
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for mid in raw:
            if isinstance(mid, str) and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    async def set_llm_local_staged(self, ctx: object, ids: list[str]) -> list[str]:
        seen: set[str] = set()
        clean: list[str] = []
        for mid in ids:
            if isinstance(mid, str) and mid not in seen:
                seen.add(mid)
                clean.append(mid)
        self.values["llm_local_staged"] = clean
        return clean


class FakeLocalGateway:
    """In-memory stand-in for the llama-swap admin client (LocalGatewayClient)."""

    def __init__(
        self,
        running: set[str] | None = None,
        *,
        fail_unload: bool = False,
        fail_load: bool = False,
    ) -> None:
        self._running = set(running or ())
        self.fail_unload = fail_unload
        self.fail_load = fail_load
        self.unloaded: list[str] = []
        self.loaded: list[str] = []

    async def running(self) -> set[str]:
        return set(self._running)

    async def unload(self, served_model: str) -> None:
        from jbrain.llm.local_gateway import LocalGatewayError

        if self.fail_unload:
            raise LocalGatewayError("simulated gateway failure")
        self.unloaded.append(served_model)
        self._running.discard(served_model)

    async def load(self, served_model: str) -> None:
        from jbrain.llm.local_gateway import LocalGatewayError

        if self.fail_load:
            raise LocalGatewayError("simulated gateway failure")
        self.loaded.append(served_model)
        self._running.add(served_model)


class FakeComfyUiGateway:
    """In-memory stand-in for the ComfyUI management client (free-memory only)."""

    def __init__(self, *, fail_free: bool = False) -> None:
        self.fail_free = fail_free
        self.frees: list[tuple[bool, bool]] = []

    async def free(self, *, unload_models: bool = True, free_memory: bool = True) -> None:
        from jbrain.image_gen.gateway import ComfyUiGatewayError

        if self.fail_free:
            raise ComfyUiGatewayError("simulated gateway failure")
        self.frees.append((unload_models, free_memory))
