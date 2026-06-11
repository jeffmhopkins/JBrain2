"""In-memory AuthRepo for unit-testing auth flows without Postgres."""

import uuid
from dataclasses import dataclass, field

from jbrain.auth.service import PrincipalInfo


@dataclass
class FakePrincipal:
    id: str
    kind: str
    key_hash: str
    label: str
    revoked: bool = False


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
                return PrincipalInfo(id=p.id, kind=p.kind, label=p.label)
        return None

    async def create_session(self, principal_id: str, token_hash: str, label: str) -> None:
        self.sessions.append(FakeSession(principal_id, token_hash, label))

    async def find_principal_by_session_token_hash(self, token_hash: str) -> PrincipalInfo | None:
        for s in self.sessions:
            if s.token_hash == token_hash and not s.revoked:
                for p in self.principals:
                    if p.id == s.principal_id and not p.revoked:
                        return PrincipalInfo(id=p.id, kind=p.kind, label=p.label)
        return None

    async def revoke_session(self, token_hash: str) -> None:
        for s in self.sessions:
            if s.token_hash == token_hash:
                s.revoked = True

    async def revoke_principals_of_kind(self, kind: str) -> None:
        for p in self.principals:
            if p.kind == kind:
                p.revoked = True

    async def create_principal(self, kind: str, key_hash: str, label: str) -> None:
        self.principals.append(FakePrincipal(str(uuid.uuid4()), kind, key_hash, label))


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
