"""Chat-turn attachments API with fakes and a real (tmp-dir) blob store: upload /
download / delete, the session-scope domain rule, and the vision capability endpoint.
"""

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jbrain.agent.attachments import AttachmentInfo, domain_for_session
from jbrain.agent.session import AgentSessionInfo, read_context
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.main import create_app
from jbrain.storage import FsBlobStore
from tests.unit.fakes import FakeAuthRepo, FakeSettingsStore


@dataclass
class FakeAgentSessions:
    """Just enough of AgentSessionRepo for the attachments endpoints: create and get."""

    sessions: dict[str, AgentSessionInfo] = field(default_factory=dict)

    def add(self, scopes: tuple[str, ...]) -> str:
        sid = str(uuid.uuid4())
        self.sessions[sid] = AgentSessionInfo(
            id=sid,
            title="",
            status="active",
            domain_scopes=scopes,
            subject_ids=(),
            created_at=datetime.now(UTC),
            last_active_at=datetime.now(UTC),
        )
        return sid

    async def get(self, ctx: SessionContext, session_id: str) -> AgentSessionInfo | None:
        return self.sessions.get(session_id)


@dataclass
class FakeTurnAttachments:
    sessions: FakeAgentSessions
    rows: dict[str, AttachmentInfo] = field(default_factory=dict)
    # Each add records the (session_id, domain_code, ctx scopes) so tests can assert
    # the firewall scope chosen and that the write ran under the narrowed context.
    added: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)

    async def session_read_context(
        self, owner_ctx: SessionContext, session_id: str
    ) -> SessionContext | None:
        info = self.sessions.sessions.get(session_id)
        if info is None:
            return None
        return read_context(owner_ctx.principal_id, info.domain_scopes)

    async def add(
        self,
        ctx: SessionContext,
        session_id: str,
        *,
        sha256: str,
        filename: str,
        media_type: str,
        size_bytes: int,
        domain_code: str,
    ) -> AttachmentInfo:
        info = AttachmentInfo(
            id=str(uuid.uuid4()),
            filename=filename,
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=sha256,
            domain_code=domain_code,
        )
        self.rows[info.id] = info
        self.added.append((session_id, domain_code, tuple(ctx.domain_scopes)))
        return info

    async def get(self, ctx: SessionContext, attachment_id: str) -> AttachmentInfo | None:
        return self.rows.get(attachment_id)

    async def remove(self, ctx: SessionContext, attachment_id: str) -> str | None:
        return attachment_id if self.rows.pop(attachment_id, None) is not None else None


@pytest.fixture
def client(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, FakeAgentSessions, FakeTurnAttachments, FakeSettingsStore]]:
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        blob_dir=str(tmp_path),
    )
    app = create_app(settings)
    sessions = FakeAgentSessions()
    repo = FakeTurnAttachments(sessions)
    store = FakeSettingsStore()
    auth_repo = FakeAuthRepo()
    with TestClient(app) as c:
        app.state.auth_repo = auth_repo
        app.state.agent_sessions = sessions
        app.state.turn_attachments = repo
        app.state.settings_store = store
        app.state.blob_store = FsBlobStore(tmp_path)
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            c.post("/api/auth/session", json={"owner_key": key, "device_label": "t"}).status_code
            == 204
        )
        yield c, sessions, repo, store


def test_upload_download_delete_roundtrip(
    client: tuple[TestClient, FakeAgentSessions, FakeTurnAttachments, FakeSettingsStore],
) -> None:
    c, sessions, _repo, _ = client
    sid = sessions.add(("health",))
    up = c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("scan.png", b"\x89PNG fake", "image/png")},
    )
    assert up.status_code == 201
    att = up.json()
    assert att["filename"] == "scan.png"
    assert att["media_type"] == "image/png"

    down = c.get(f"/api/chat-attachments/{att['id']}")
    assert down.status_code == 200
    assert down.content == b"\x89PNG fake"
    assert down.headers["content-type"].startswith("image/png")

    assert c.delete(f"/api/chat-attachments/{att['id']}").status_code == 204
    assert c.get(f"/api/chat-attachments/{att['id']}").status_code == 404
    assert c.delete(f"/api/chat-attachments/{att['id']}").status_code == 404


def test_upload_to_missing_session_404(
    client: tuple[TestClient, FakeAgentSessions, FakeTurnAttachments, FakeSettingsStore],
) -> None:
    c, _, _, _ = client
    resp = c.post(
        f"/api/sessions/{uuid.uuid4()}/attachments",
        files={"file": ("x.txt", b"x", "text/plain")},
    )
    assert resp.status_code == 404


def test_single_domain_session_stamps_that_domain(
    client: tuple[TestClient, FakeAgentSessions, FakeTurnAttachments, FakeSettingsStore],
) -> None:
    c, sessions, repo, _ = client
    sid = sessions.add(("finance",))
    c.post(
        f"/api/sessions/{sid}/attachments",
        files={"file": ("r.pdf", b"%PDF", "application/pdf")},
    )
    session_id, domain_code, ctx_scopes = repo.added[-1]
    assert session_id == sid
    # The file inherits the session's single firewall, and the write ran under it.
    assert domain_code == "finance"
    assert ctx_scopes == ("finance",)


def test_multi_and_empty_scope_sessions_stamp_general(
    client: tuple[TestClient, FakeAgentSessions, FakeTurnAttachments, FakeSettingsStore],
) -> None:
    c, sessions, repo, _ = client
    multi = sessions.add(("health", "finance"))
    empty = sessions.add(())
    c.post(f"/api/sessions/{multi}/attachments", files={"file": ("a.txt", b"a", "text/plain")})
    c.post(f"/api/sessions/{empty}/attachments", files={"file": ("b.txt", b"b", "text/plain")})
    assert repo.added[-2][1] == "general"  # multi-domain → general
    assert repo.added[-1][1] == "general"  # empty (Jerv/Teacher) → general


def test_chat_attachments_require_auth(tmp_path: Path) -> None:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get(f"/api/chat-attachments/{uuid.uuid4()}").status_code == 401
        assert anon.get("/api/chat/capabilities").status_code == 401


def test_capabilities_default_model_supports_vision(
    client: tuple[TestClient, FakeAgentSessions, FakeTurnAttachments, FakeSettingsStore],
) -> None:
    c, _, _, _ = client
    # Default agent.turn is xai:grok-4.3 — a vision-capable model.
    resp = c.get("/api/chat/capabilities")
    assert resp.status_code == 200
    assert resp.json() == {"supports_vision": True}


def test_capabilities_reflects_text_only_override(
    client: tuple[TestClient, FakeAgentSessions, FakeTurnAttachments, FakeSettingsStore],
) -> None:
    c, _, _, store = client
    # A stored override to a text-only local model flips the flag off.
    store.values["llm_task_overrides"] = {"agent.turn": {"spec": "local:text-only-model"}}
    assert c.get("/api/chat/capabilities").json() == {"supports_vision": False}


def test_domain_for_session_helper() -> None:
    assert domain_for_session(("health",)) == "health"
    assert domain_for_session(()) == "general"
    assert domain_for_session(("a", "b")) == "general"
