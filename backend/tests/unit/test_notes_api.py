"""Notes API surface with a fake repo and a real (tmp-dir) blob store."""

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.db.session import SessionContext
from jbrain.main import create_app
from jbrain.notes.service import AttachmentInfo, NoteInfo, UnknownDomain
from jbrain.storage import FsBlobStore
from tests.unit.fakes import FakeAuthRepo

KNOWN_DOMAINS = {"general", "health", "finance", "location"}


@dataclass
class FakeJobQueue:
    enqueued: list[tuple[str, dict]] = field(default_factory=list)

    async def enqueue(self, ctx: SessionContext, kind: str, payload: dict) -> str:
        self.enqueued.append((kind, payload))
        return str(uuid.uuid4())


@dataclass
class FakeNotesRepo:
    notes: list[NoteInfo] = field(default_factory=list)

    async def create_note(
        self,
        ctx: SessionContext,
        *,
        client_id: str,
        domain: str,
        destination: str | None,
        body: str,
    ) -> tuple[NoteInfo, bool]:
        if domain not in KNOWN_DOMAINS:
            raise UnknownDomain(domain)
        for n in self.notes:
            if n.client_id == client_id:
                return n, False
        note = NoteInfo(
            id=str(uuid.uuid4()),
            client_id=client_id,
            domain=domain,
            destination=destination,
            body=body,
            created_at=datetime.now(UTC) + timedelta(seconds=len(self.notes)),
        )
        self.notes.append(note)
        return note, True

    async def list_notes(
        self, ctx: SessionContext, *, limit: int, before: datetime | None
    ) -> list[NoteInfo]:
        rows = sorted(self.notes, key=lambda n: n.created_at, reverse=True)
        if before is not None:
            rows = [n for n in rows if n.created_at < before]
        return rows[:limit]

    async def add_attachment(
        self,
        ctx: SessionContext,
        *,
        note_id: str,
        sha256: str,
        filename: str,
        media_type: str,
        size_bytes: int,
    ) -> AttachmentInfo | None:
        for n in self.notes:
            if n.id == note_id:
                info = AttachmentInfo(
                    id=str(uuid.uuid4()),
                    filename=filename,
                    media_type=media_type,
                    size_bytes=size_bytes,
                    sha256=sha256,
                )
                n.attachments.append(info)
                return info
        return None

    async def get_attachment(
        self, ctx: SessionContext, attachment_id: str
    ) -> AttachmentInfo | None:
        for n in self.notes:
            for a in n.attachments:
                if a.id == attachment_id:
                    return a
        return None


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, FakeNotesRepo, FakeJobQueue]]:
    settings = Settings(
        secure_cookies=False,
        database_url="postgresql+asyncpg://nobody@localhost:1/none",
        blob_dir=str(tmp_path),
    )
    app = create_app(settings)
    repo = FakeNotesRepo()
    auth_repo = FakeAuthRepo()
    jobs = FakeJobQueue()
    with TestClient(app) as test_client:
        app.state.auth_repo = auth_repo
        app.state.notes_repo = repo
        app.state.blob_store = FsBlobStore(tmp_path)
        app.state.job_queue = jobs
        key = asyncio.run(auth_service.rotate_owner_key(auth_repo))
        assert (
            test_client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield test_client, repo, jobs


def test_notes_require_auth(tmp_path: Path) -> None:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert anon.get("/api/notes").status_code == 401
        assert anon.post("/api/notes", json={"client_id": "x", "body": "y"}).status_code == 401


def test_create_note_is_idempotent_on_client_id(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _repo, _jobs = client
    payload = {"client_id": "abc", "domain": "general", "body": "hello"}
    first = c.post("/api/notes", json=payload)
    second = c.post("/api/notes", json=payload)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_create_note_rejects_unknown_domain(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    resp = c.post("/api/notes", json={"client_id": "x", "domain": "nope", "body": "y"})
    assert resp.status_code == 400


def test_list_notes_pagination(client: tuple[TestClient, FakeNotesRepo, FakeJobQueue]) -> None:
    c, _, _ = client
    for i in range(5):
        c.post("/api/notes", json={"client_id": f"n{i}", "body": f"note {i}"})
    page = c.get("/api/notes", params={"limit": 3}).json()
    assert len(page["notes"]) == 3
    assert page["next_cursor"] is not None
    rest = c.get("/api/notes", params={"limit": 3, "before": page["next_cursor"]}).json()
    assert len(rest["notes"]) == 2
    assert rest["next_cursor"] is None
    bodies = [n["body"] for n in page["notes"] + rest["notes"]]
    assert bodies == [f"note {i}" for i in (4, 3, 2, 1, 0)]


def test_attachment_upload_and_download_roundtrip(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    note = c.post("/api/notes", json={"client_id": "a1", "body": "with file"}).json()
    up = c.post(
        f"/api/notes/{note['id']}/attachments",
        files={"file": ("lab.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert up.status_code == 201
    att = up.json()
    assert att["filename"] == "lab.pdf"

    down = c.get(f"/api/attachments/{att['id']}")
    assert down.status_code == 200
    assert down.content == b"%PDF-fake"
    assert down.headers["content-type"].startswith("application/pdf")

    listed = c.get("/api/notes").json()["notes"][0]
    assert listed["attachments"][0]["filename"] == "lab.pdf"


def test_attachment_to_missing_note_404(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    resp = c.post(
        f"/api/notes/{uuid.uuid4()}/attachments", files={"file": ("x.txt", b"x", "text/plain")}
    )
    assert resp.status_code == 404


def test_download_missing_attachment_404(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    assert c.get(f"/api/attachments/{uuid.uuid4()}").status_code == 404


def test_note_responses_expose_ingest_state(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    created = c.post("/api/notes", json={"client_id": "s1", "body": "state"}).json()
    assert created["ingest_state"] == "pending"
    listed = c.get("/api/notes").json()["notes"][0]
    assert listed["ingest_state"] == "pending"


def test_create_note_enqueues_ingestion_once(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, jobs = client
    payload = {"client_id": "q1", "body": "index me"}
    note = c.post("/api/notes", json=payload).json()
    c.post("/api/notes", json=payload)  # idempotent retry must not re-enqueue
    assert jobs.enqueued == [("ingest_note", {"note_id": note["id"]})]
    # Payload carries the id only — never note content.
    assert "index me" not in str(jobs.enqueued)


def test_attachment_upload_enqueues_reingestion(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, jobs = client
    note = c.post("/api/notes", json={"client_id": "q2", "body": "n"}).json()
    jobs.enqueued.clear()
    up = c.post(
        f"/api/notes/{note['id']}/attachments",
        files={"file": ("a.txt", b"text", "text/plain")},
    )
    assert up.status_code == 201
    assert jobs.enqueued == [("ingest_note", {"note_id": note["id"]})]


def test_failed_attachment_upload_does_not_enqueue(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, jobs = client
    jobs.enqueued.clear()
    resp = c.post(
        f"/api/notes/{uuid.uuid4()}/attachments", files={"file": ("x.txt", b"x", "text/plain")}
    )
    assert resp.status_code == 404
    assert jobs.enqueued == []
