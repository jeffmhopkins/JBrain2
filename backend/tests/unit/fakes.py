"""In-memory AuthRepo for unit-testing auth flows without Postgres."""

import contextlib
import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jbrain.auth.service import CapabilityToken, ExternalSession, PrincipalInfo
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
    suspended_at: datetime | None = None
    jcode_session_id: str = ""
    redeemed_at: datetime | None = None
    ext_in_tokens: int = 0
    ext_out_tokens: int = 0
    ext_requests: int = 0


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
        now = datetime.now(UTC)
        for s in self.sessions:
            if s.token_hash == token_hash and not s.revoked:
                for p in self.principals:
                    live = p.expires_at is None or p.expires_at > now
                    if p.id == s.principal_id and not p.revoked and live:
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
            if (
                p.key_hash == key_hash
                and p.kind == "capability_token"
                and not p.revoked
                and p.suspended_at is None
                and live
            ):
                p.last_used_at = now
                return _info(p)
        return None

    async def has_active_capability(self) -> bool:
        now = datetime.now(UTC)
        return any(
            p.kind == "capability_token"
            and not p.revoked
            and p.suspended_at is None
            and (p.expires_at is None or p.expires_at > now)
            for p in self.principals
        )

    async def list_capabilities(self) -> list[CapabilityToken]:
        return [_capability(p) for p in self.principals if p.kind == "capability_token"]

    async def revoke_capability(self, capability_id: str) -> bool:
        for p in self.principals:
            if p.id == capability_id and p.kind == "capability_token" and not p.revoked:
                p.revoked = True
                return True
        return False

    async def suspend_capability(self, capability_id: str) -> bool:
        for p in self.principals:
            if (
                p.id == capability_id
                and p.kind == "capability_token"
                and not p.revoked
                and p.suspended_at is None
            ):
                p.suspended_at = datetime.now(UTC)
                return True
        return False

    async def resume_capability(self, capability_id: str) -> bool:
        for p in self.principals:
            if (
                p.id == capability_id
                and p.kind == "capability_token"
                and not p.revoked
                and p.suspended_at is not None
            ):
                p.suspended_at = None
                return True
        return False

    async def create_jcode_share(
        self, key_hash: str, label: str, session_id: str, expires_at: datetime
    ) -> CapabilityToken:
        p = FakePrincipal(
            str(uuid.uuid4()),
            "jcode_share_link",
            key_hash,
            label,
            expires_at=expires_at,
            jcode_session_id=session_id,
        )
        self.principals.append(p)
        return _capability(p)

    async def find_active_jcode_share_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        now = datetime.now(UTC)
        for p in self.principals:
            live = p.expires_at is None or p.expires_at > now
            if p.key_hash == key_hash and p.kind == "jcode_share_link" and not p.revoked and live:
                p.last_used_at = now
                return _info(p)
        return None

    async def consume_jcode_share(self, key_hash: str) -> PrincipalInfo | None:
        now = datetime.now(UTC)
        for p in self.principals:
            live = p.expires_at is None or p.expires_at > now
            if (
                p.key_hash == key_hash
                and p.kind == "jcode_share_link"
                and not p.revoked
                and p.redeemed_at is None
                and live
            ):
                p.redeemed_at = now  # single-use: claimed, never claimable again
                return _info(p)
        return None

    async def list_jcode_shares(self, session_id: str) -> list[CapabilityToken]:
        return [
            _capability(p)
            for p in self.principals
            if p.kind == "jcode_share_link" and p.jcode_session_id == session_id and not p.revoked
        ]

    async def revoke_jcode_share(self, share_id: str, session_id: str) -> bool:
        for p in self.principals:
            if (
                p.id == share_id
                and p.kind == "jcode_share_link"
                and p.jcode_session_id == session_id
                and not p.revoked
            ):
                p.revoked = True
                return True
        return False

    async def create_external_llm(
        self, key_hash: str, label: str, expires_at: datetime | None
    ) -> ExternalSession:
        p = FakePrincipal(str(uuid.uuid4()), "external_llm", key_hash, label, expires_at=expires_at)
        self.principals.append(p)
        return _external(p)

    async def find_active_external_llm_by_key_hash(self, key_hash: str) -> PrincipalInfo | None:
        now = datetime.now(UTC)
        for p in self.principals:
            live = p.expires_at is None or p.expires_at > now
            if (
                p.key_hash == key_hash
                and p.kind == "external_llm"
                and not p.revoked
                and p.suspended_at is None
                and live
            ):
                p.last_used_at = now
                return _info(p)
        return None

    async def list_external_llm(self) -> list[ExternalSession]:
        return [_external(p) for p in self.principals if p.kind == "external_llm" and not p.revoked]

    async def set_external_llm_enabled(self, session_id: str, enabled: bool) -> bool:
        for p in self.principals:
            if p.id == session_id and p.kind == "external_llm" and not p.revoked:
                p.suspended_at = None if enabled else datetime.now(UTC)
                return True
        return False

    async def revoke_external_llm(self, session_id: str) -> bool:
        for p in self.principals:
            if p.id == session_id and p.kind == "external_llm" and not p.revoked:
                p.revoked = True
                return True
        return False

    async def add_external_usage(self, session_id: str, in_tokens: int, out_tokens: int) -> None:
        for p in self.principals:
            if p.id == session_id and p.kind == "external_llm":
                p.ext_in_tokens += in_tokens
                p.ext_out_tokens += out_tokens
                p.ext_requests += 1
                return


def _info(p: FakePrincipal) -> PrincipalInfo:
    return PrincipalInfo(
        id=p.id,
        kind=p.kind,
        label=p.label,
        subject_id=p.subject_id,
        jcode_session_id=p.jcode_session_id,
    )


def _capability(p: FakePrincipal) -> CapabilityToken:
    return CapabilityToken(
        id=p.id,
        label=p.label,
        created_at=p.created_at,
        expires_at=p.expires_at,
        last_used_at=p.last_used_at,
        revoked_at=p.created_at if p.revoked else None,
        suspended_at=p.suspended_at,
        redeemed_at=p.redeemed_at,
    )


def _external(p: FakePrincipal) -> ExternalSession:
    return ExternalSession(
        id=p.id,
        label=p.label,
        enabled=p.suspended_at is None,
        created_at=p.created_at,
        expires_at=p.expires_at,
        last_used_at=p.last_used_at,
        in_tokens=p.ext_in_tokens,
        out_tokens=p.ext_out_tokens,
        requests=p.ext_requests,
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

    async def tavily_enabled(self, ctx: object) -> bool:
        return self.values.get("tavily_enabled", True) is True

    async def tavily_api_key(self, ctx: object) -> str:
        raw = self.values.get("tavily_api_key", "")
        return raw if isinstance(raw, str) else ""

    async def set_tavily_enabled(self, ctx: object, enabled: bool) -> None:
        self.values["tavily_enabled"] = bool(enabled)

    async def set_tavily_api_key(self, ctx: object, api_key: str) -> None:
        self.values["tavily_api_key"] = api_key

    async def moltbook_api_key(self, ctx: object) -> str:
        raw = self.values.get("moltbook_api_key", "")
        return raw if isinstance(raw, str) else ""

    async def moltbook_handle(self, ctx: object) -> str:
        raw = self.values.get("moltbook_handle", "")
        return raw if isinstance(raw, str) else ""

    async def moltbook_autonomy(self, ctx: object) -> bool:
        return self.values.get("moltbook_autonomy", False) is True

    async def moltbook_killed(self, ctx: object) -> bool:
        return self.values.get("moltbook_killed", False) is True

    async def moltbook_engine(self, ctx: object) -> str:
        # Imported here, as `owner_timezone` does below: `fakes` is imported by nearly every
        # test module and a top-level import of the real store would make the fake depend on
        # it loading cleanly.
        from jbrain.settings_store import MOLTBOOK_ENGINE_DEFAULT, MOLTBOOK_ENGINES

        engine = self.values.get("jmolt_engine", MOLTBOOK_ENGINE_DEFAULT)
        return engine if engine in MOLTBOOK_ENGINES else MOLTBOOK_ENGINE_DEFAULT

    async def moltbook_disclosure(self, ctx: object) -> str:
        raw = self.values.get(
            "moltbook_disclosure",
            "Autonomous experiment; one hour a night; my human reads the logs.",
        )
        return raw if isinstance(raw, str) and raw.strip() else "Autonomous experiment."

    async def set_moltbook_api_key(self, ctx: object, api_key: str) -> None:
        self.values["moltbook_api_key"] = api_key

    async def set_moltbook_handle(self, ctx: object, handle: str) -> None:
        self.values["moltbook_handle"] = handle

    async def set_moltbook_autonomy(self, ctx: object, on: bool) -> None:
        self.values["moltbook_autonomy"] = bool(on)

    async def set_moltbook_killed(self, ctx: object, killed: bool) -> None:
        self.values["moltbook_killed"] = bool(killed)

    async def set_moltbook_disclosure(self, ctx: object, line: str) -> None:
        self.values["moltbook_disclosure"] = line

    async def moltbook_advisory_note(self, ctx: object) -> str:
        raw = self.values.get("moltbook_advisory_note", "")
        return raw if isinstance(raw, str) else ""

    async def set_moltbook_advisory_note(self, ctx: object, note: str) -> None:
        self.values["moltbook_advisory_note"] = note

    async def moltbook_night_deadline(self, ctx: object) -> str:
        raw = self.values.get("moltbook_night_deadline", "")
        return raw if isinstance(raw, str) else ""

    async def set_moltbook_night_deadline(self, ctx: object, iso: str) -> None:
        self.values["moltbook_night_deadline"] = iso

    async def moltbook_last_night(self, ctx: object) -> str:
        raw = self.values.get("moltbook_last_night", "")
        return raw if isinstance(raw, str) else ""

    async def set_moltbook_last_night(self, ctx: object, iso_date: str) -> None:
        self.values["moltbook_last_night"] = iso_date

    async def moltbook_drip_last_swept(self, ctx: object) -> str:
        raw = self.values.get("moltbook_drip_last_swept", "")
        return raw if isinstance(raw, str) else ""

    async def set_moltbook_drip_last_swept(self, ctx: object, iso: str) -> None:
        self.values["moltbook_drip_last_swept"] = iso

    async def moltbook_last_digest(self, ctx: object) -> str:
        raw = self.values.get("moltbook_last_digest", "")
        return raw if isinstance(raw, str) else ""

    async def set_moltbook_last_digest(self, ctx: object, iso_date: str) -> None:
        self.values["moltbook_last_digest"] = iso_date

    async def moltbook_night_enabled(self, ctx: object) -> bool:
        return self.values.get("moltbook_night_enabled", True) is True

    async def set_moltbook_night_enabled(self, ctx: object, on: bool) -> None:
        self.values["moltbook_night_enabled"] = bool(on)

    async def moltbook_night_hour(self, ctx: object) -> int:
        raw = self.values.get("moltbook_night_hour", 3)
        try:
            return max(0, min(23, int(str(raw))))
        except (TypeError, ValueError):
            return 3

    async def set_moltbook_night_hour(self, ctx: object, hour: int) -> None:
        self.values["moltbook_night_hour"] = max(0, min(23, int(hour)))

    async def code_mode_hold_names(self, ctx: object) -> frozenset[str]:
        raw = self.values.get("code_mode_hold_name", [])
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(x for x in raw if isinstance(x, str) and x)

    async def set_code_mode_hold_names(self, ctx: object, served_models: Sequence[str]) -> None:
        self.values["code_mode_hold_name"] = sorted({m for m in served_models if m})

    async def night_hold_names(self, ctx: object) -> frozenset[str]:
        raw = self.values.get("jmolt_night_hold_name", [])
        if not isinstance(raw, list):
            return frozenset()
        return frozenset(x for x in raw if isinstance(x, str) and x)

    async def set_night_hold_names(self, ctx: object, served_models: Sequence[str]) -> None:
        self.values["jmolt_night_hold_name"] = sorted({m for m in served_models if m})

    async def box_hold_names(self, ctx: object) -> frozenset[str]:
        return await self.code_mode_hold_names(ctx) | await self.night_hold_names(ctx)

    async def moltbook_account_state(self, ctx: object) -> str:
        raw = self.values.get("moltbook_account_state", "ok")
        return raw if isinstance(raw, str) and raw.strip() else "ok"

    async def set_moltbook_account_state(self, ctx: object, state: str) -> None:
        self.values["moltbook_account_state"] = state

    async def moltbook_integrity_last_pass(self, ctx: object) -> str:
        raw = self.values.get("moltbook_integrity_last_pass", "")
        return raw if isinstance(raw, str) else ""

    async def set_moltbook_integrity_last_pass(self, ctx: object, when: str) -> None:
        self.values["moltbook_integrity_last_pass"] = when

    async def moltbook_verify_fail_streak(self, ctx: object) -> int:
        raw = self.values.get("moltbook_verify_fail_streak", 0)
        return raw if isinstance(raw, int) else 0

    async def set_moltbook_verify_fail_streak(self, ctx: object, n: int) -> None:
        self.values["moltbook_verify_fail_streak"] = int(n)

    async def jcode_model(self, ctx: object) -> str:
        raw = self.values.get("jcode_model", "")
        return raw if isinstance(raw, str) else ""

    async def set_jcode_model(self, ctx: object, model_id: str) -> str:
        self.values["jcode_model"] = model_id
        return model_id

    async def jcode_planner_model(self, ctx: object) -> str:
        raw = self.values.get("jcode_planner_model", "")
        return raw if isinstance(raw, str) else ""

    async def set_jcode_planner_model(self, ctx: object, model_id: str) -> str:
        self.values["jcode_planner_model"] = model_id
        return model_id

    async def workflow_dispatch_mode(self, ctx: object) -> str:
        mode = self.values.get("workflow_dispatch_mode", "shadow")
        return mode if mode in ("shadow", "live", "off") else "shadow"

    async def owner_timezone(self, ctx: object) -> str | None:
        from jbrain.settings_store import is_valid_timezone

        tz = self.values.get("owner_timezone")
        return tz if isinstance(tz, str) and is_valid_timezone(tz) else None

    async def gmail_credentials(self, ctx: object) -> tuple[str, str, str]:
        return (
            str(self.values.get("gmail_client_id", "") or ""),
            str(self.values.get("gmail_client_secret", "") or ""),
            str(self.values.get("gmail_refresh_token", "") or ""),
        )

    async def set_gmail_credentials(
        self,
        ctx: object,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        if client_id is not None:
            self.values["gmail_client_id"] = client_id
        if client_secret is not None:
            self.values["gmail_client_secret"] = client_secret
        if refresh_token is not None:
            self.values["gmail_refresh_token"] = refresh_token

    async def reflexion_buffer_retry(self, ctx: object) -> bool:
        return self.values.get("reflexion_buffer_retry", False) is True

    async def brain_llm_stream(self, ctx: object) -> bool:
        return self.values.get("brain_llm_stream", False) is True

    async def brain_read_aloud(self, ctx: object) -> bool:
        return self.values.get("brain_read_aloud", False) is True

    async def brain_answer_voice(self, ctx: object) -> str:
        raw = self.values.get("brain_answer_voice", "kokoro-af_heart")
        return raw if isinstance(raw, str) and raw else "kokoro-af_heart"

    async def brain_read_aloud_engine(self, ctx: object) -> str:
        raw = self.values.get("brain_read_aloud_engine", "piper")
        return raw if raw in ("piper", "native") else "piper"

    async def brain_answer_speed(self, ctx: object) -> float:
        try:
            val = float(self.values.get("brain_answer_speed", 1.0))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 1.0
        return max(0.5, min(2.0, val))

    async def brain_answer_pitch(self, ctx: object) -> float:
        try:
            val = float(self.values.get("brain_answer_pitch", 0.0))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0
        return max(-12.0, min(12.0, val))

    async def brain_answer_chorus(self, ctx: object) -> bool:
        return self.values.get("brain_answer_chorus", False) is True

    async def brain_answer_robot(self, ctx: object) -> bool:
        return self.values.get("brain_answer_robot", False) is True

    async def local_llm_auto_update(self, ctx: object) -> bool:
        # Default ON; only an explicit false turns it off (mirrors the SQL store).
        return self.values.get("local_llm_auto_update", True) is not False

    async def local_llm_patch_restore_checkpoint(self, ctx: object) -> bool:
        # Default OFF; only an explicit true turns it on (mirrors the SQL store).
        return self.values.get("local_llm_patch_restore_checkpoint", False) is True

    async def pronunciation_lexicon(self, ctx: object) -> dict[str, str]:
        raw = self.values.get("pronunciation_lexicon", {})
        if not isinstance(raw, dict):
            return {}
        return {
            w.strip(): s.strip()
            for w, s in raw.items()
            if isinstance(w, str) and isinstance(s, str) and w.strip() and s.strip()
        }

    async def set_pronunciation_lexicon(
        self, ctx: object, lexicon: dict[str, str]
    ) -> dict[str, str]:
        # Store the SANITIZED map (mirrors the SQL store — it refuses to persist what it wouldn't
        # read back), so store.values reflects what a reader would see.
        self.values["pronunciation_lexicon"] = lexicon
        clean = await self.pronunciation_lexicon(ctx)
        self.values["pronunciation_lexicon"] = clean
        return clean

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

    async def llm_local_parallel_slots(self, ctx: object) -> dict[str, int]:
        raw = self.values.get("llm_local_parallel_slots", {})
        if not isinstance(raw, dict):
            return {}
        return {
            mid: n
            for mid, n in raw.items()
            if isinstance(mid, str) and isinstance(n, int) and not isinstance(n, bool) and n > 1
        }

    async def set_llm_local_parallel_slots(
        self, ctx: object, *, model_id: str, slots: int | None
    ) -> dict[str, int]:
        current = await self.llm_local_parallel_slots(ctx)
        if slots is None or slots <= 1:
            current.pop(model_id, None)
        else:
            current[model_id] = slots
        self.values["llm_local_parallel_slots"] = current
        return current

    async def llm_local_image_min_tokens(self, ctx: object) -> dict[str, int]:
        raw = self.values.get("llm_local_image_min_tokens", {})
        return dict(raw) if isinstance(raw, dict) else {}

    async def set_llm_local_image_min_tokens(
        self, ctx: object, *, model_id: str, tokens: int | None
    ) -> dict[str, int]:
        current = await self.llm_local_image_min_tokens(ctx)
        if tokens is None or tokens <= 0:
            current.pop(model_id, None)
        else:
            current[model_id] = tokens
        self.values["llm_local_image_min_tokens"] = current
        return current

    async def llm_local_extra_args(self, ctx: object) -> dict[str, list[str]]:
        raw = self.values.get("llm_local_extra_args", {})
        if not isinstance(raw, dict):
            return {}
        return {
            mid: list(args)
            for mid, args in raw.items()
            if isinstance(mid, str) and isinstance(args, list) and args
        }

    async def set_llm_local_extra_args(
        self, ctx: object, *, model_id: str, args: list[str] | None
    ) -> dict[str, list[str]]:
        current = await self.llm_local_extra_args(ctx)
        if not args:
            current.pop(model_id, None)
        else:
            current[model_id] = list(args)
        self.values["llm_local_extra_args"] = current
        return current

    async def llm_local_free_ram_fraction(self, ctx: object) -> float | None:
        raw = self.values.get("llm_local_free_ram_fraction")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        return float(raw) if 0.0 < raw < 1.0 else None

    async def set_llm_local_free_ram_fraction(
        self, ctx: object, fraction: float | None
    ) -> float | None:
        if fraction is None or isinstance(fraction, bool) or not (0.0 < fraction < 1.0):
            self.values["llm_local_free_ram_fraction"] = None
            return None
        self.values["llm_local_free_ram_fraction"] = float(fraction)
        return float(fraction)

    async def llm_local_auto_restore(self, ctx: object) -> bool:
        return self.values.get("llm_local_auto_restore", True) is not False

    async def set_llm_local_auto_restore(self, ctx: object, enabled: bool) -> bool:
        clean = bool(enabled)
        self.values["llm_local_auto_restore"] = clean
        return clean

    async def llm_local_unavailable(self, ctx: object) -> list[str]:
        raw = self.values.get("llm_local_unavailable", [])
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for mid in raw:
            if isinstance(mid, str) and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    async def set_llm_local_unavailable(self, ctx: object, ids: list[str]) -> list[str]:
        seen: set[str] = set()
        clean: list[str] = []
        for mid in ids:
            if isinstance(mid, str) and mid not in seen:
                seen.add(mid)
                clean.append(mid)
        self.values["llm_local_unavailable"] = clean
        return clean

    async def llm_local_provision_requested(self, ctx: object) -> list[str]:
        raw = self.values.get("llm_local_provision_requested", [])
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for mid in raw:
            if isinstance(mid, str) and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    async def set_llm_local_provision_requested(self, ctx: object, ids: list[str]) -> list[str]:
        seen: set[str] = set()
        clean: list[str] = []
        for mid in ids:
            if isinstance(mid, str) and mid not in seen:
                seen.add(mid)
                clean.append(mid)
        self.values["llm_local_provision_requested"] = clean
        return clean

    async def llm_local_remove_requested(self, ctx: object) -> list[str]:
        raw = self.values.get("llm_local_remove_requested", [])
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for mid in raw:
            if isinstance(mid, str) and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out

    async def set_llm_local_remove_requested(self, ctx: object, ids: list[str]) -> list[str]:
        seen: set[str] = set()
        clean: list[str] = []
        for mid in ids:
            if isinstance(mid, str) and mid not in seen:
                seen.add(mid)
                clean.append(mid)
        self.values["llm_local_remove_requested"] = clean
        return clean


class FakeLocalGateway:
    """In-memory stand-in for the llama-swap admin client (LocalGatewayClient)."""

    def __init__(
        self,
        running: set[str] | None = None,
        *,
        fail_unload: bool = False,
        fail_load: bool = False,
        fail_logs: bool = False,
        logs_text: str = "",
        fail_props: bool = False,
        props_payload: dict[str, object] | None = None,
    ) -> None:
        self._running = set(running or ())
        self.fail_unload = fail_unload
        self.fail_load = fail_load
        self.fail_logs = fail_logs
        self.logs_text = logs_text
        self.fail_props = fail_props
        self.props_payload = props_payload or {}
        self.unloaded: list[str] = []
        self.loaded: list[str] = []
        # The prompt size the fake's warm "measured" — what after_warm receives.
        self.warm_prompt_tokens: int = 28_000
        # The persona system prompt each load() was asked to prime the warm-up with
        # (None when unset), so a test can assert the manual Load primes the cache.
        self.warmed_system: list[str | None] = []
        # The tool schemas each load() was asked to prime alongside the persona (None when
        # unset), so a test can assert the prime carries tools, not just the persona.
        self.warmed_tools: list[list[dict[str, object]] | None] = []
        # What /running would report as each model's state. Only set by tests that care;
        # an unset model reads as "" (unknown), matching the real client for a build that
        # reports no state.
        self.states: dict[str, str] = {}

    def state_of(self, served_model: str) -> str:
        return self.states.get(served_model, "")

    async def running(self) -> set[str]:
        return set(self._running)

    async def unload(self, served_model: str) -> None:
        from jbrain.llm.local_gateway import LocalGatewayError

        if self.fail_unload:
            raise LocalGatewayError("simulated gateway failure")
        self.unloaded.append(served_model)
        self._running.discard(served_model)

    async def load(
        self,
        served_model: str,
        *,
        warm_system: str | None = None,
        warm_tools: list[dict[str, object]] | None = None,
        warm_reasoning_effort: str | None = None,
        before_warm=None,
        after_warm=None,
    ) -> None:
        from jbrain.llm.local_gateway import LocalGatewayError

        # Mirror the real gateway: the restore hook runs ahead of the warm, best-effort.
        if before_warm is not None:
            with contextlib.suppress(Exception):
                await before_warm()

        if self.fail_load:
            raise LocalGatewayError("simulated gateway failure")
        self.loaded.append(served_model)
        self.warmed_system.append(warm_system)
        self.warmed_tools.append(warm_tools)
        self._running.add(served_model)
        # And the save hook runs after a successful warm, with the warm's prompt size.
        if after_warm is not None:
            with contextlib.suppress(Exception):
                await after_warm(self.warm_prompt_tokens)

    async def tail_logs(self) -> str:
        from jbrain.llm.local_gateway import LocalGatewayError

        if self.fail_logs:
            raise LocalGatewayError("simulated gateway failure")
        return self.logs_text

    async def props(self, served_model: str) -> dict[str, object]:
        from jbrain.llm.local_gateway import LocalGatewayError

        if self.fail_props:
            raise LocalGatewayError("simulated gateway failure")
        return dict(self.props_payload)


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
