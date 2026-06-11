"""Notes API surface with a fake repo and a real (tmp-dir) blob store."""

import asyncio
import dataclasses
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
from jbrain.notes.service import AttachmentInfo, NoteInfo, NoteUpdate, UnknownDomain
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
        created_at: datetime | None = None,
        tz_offset_minutes: int | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        accuracy_m: float | None = None,
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
            created_at=created_at or datetime.now(UTC) + timedelta(seconds=len(self.notes)),
            tz_offset_minutes=tz_offset_minutes,
            latitude=latitude,
            longitude=longitude,
            accuracy_m=accuracy_m,
        )
        self.notes.append(note)
        return note, True

    async def update_note(
        self, ctx: SessionContext, note_id: str, changes: NoteUpdate
    ) -> NoteInfo | None:
        if changes.domain is not None and changes.domain not in KNOWN_DOMAINS:
            raise UnknownDomain(changes.domain)
        for i, n in enumerate(self.notes):
            if n.id == note_id:
                updated = dataclasses.replace(
                    n,
                    body=changes.body if changes.body is not None else n.body,
                    domain=changes.domain if changes.domain is not None else n.domain,
                    destination=None
                    if changes.clear_destination
                    else changes.destination
                    if changes.destination is not None
                    else n.destination,
                    ingest_state="pending",
                )
                self.notes[i] = updated
                return updated
        return None

    async def delete_note(self, ctx: SessionContext, note_id: str) -> bool:
        before = len(self.notes)
        self.notes = [n for n in self.notes if n.id != note_id]
        return len(self.notes) < before

    async def set_hidden(self, ctx: SessionContext, note_id: str, hidden: bool) -> bool:
        for i, n in enumerate(self.notes):
            if n.id == note_id:
                self.notes[i] = dataclasses.replace(n, hidden=hidden)
                return True
        return False

    async def list_notes(
        self, ctx: SessionContext, *, limit: int, before: datetime | None
    ) -> list[NoteInfo]:
        rows = sorted(
            (n for n in self.notes if not n.hidden), key=lambda n: n.created_at, reverse=True
        )
        if before is not None:
            rows = [n for n in rows if n.created_at < before]
        return rows[:limit]

    async def get_note(self, ctx: SessionContext, note_id: str) -> NoteInfo | None:
        for n in self.notes:
            if n.id == note_id:
                return n
        return None

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

    async def remove_attachment(self, ctx: SessionContext, attachment_id: str) -> str | None:
        for n in self.notes:
            for a in n.attachments:
                if a.id == attachment_id:
                    n.attachments.remove(a)
                    return n.id
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


def test_create_note_persists_client_capture_time_and_offset(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    # Bug 2: the offline outbox sends its own capture instant + UTC offset so
    # the extraction anchor is the note's local time, not server flush time.
    c, repo, _ = client
    resp = c.post(
        "/api/notes",
        json={
            "client_id": "tz1",
            "body": "evening note",
            "created_at": "2026-06-10T17:11:00-07:00",
            "tz_offset_minutes": -420,
        },
    )
    assert resp.status_code == 201
    out = resp.json()
    assert out["tz_offset_minutes"] == -420
    assert out["created_at"].startswith("2026-06-10T17:11:00")
    assert repo.notes[0].tz_offset_minutes == -420


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


def test_get_note_by_id(client: tuple[TestClient, FakeNotesRepo, FakeJobQueue]) -> None:
    c, _, _ = client
    created = c.post("/api/notes", json={"client_id": "g1", "body": "fetch me"}).json()
    got = c.get(f"/api/notes/{created['id']}")
    assert got.status_code == 200
    assert got.json()["body"] == "fetch me"
    assert c.get(f"/api/notes/{uuid.uuid4()}").status_code == 404


def test_attachment_to_missing_note_404(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    resp = c.post(
        f"/api/notes/{uuid.uuid4()}/attachments", files={"file": ("x.txt", b"x", "text/plain")}
    )
    assert resp.status_code == 404


def test_remove_attachment_reingests_note(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _repo, jobs = client
    note = c.post("/api/notes", json={"client_id": "ra1", "body": "with file"}).json()
    att = c.post(
        f"/api/notes/{note['id']}/attachments",
        files={"file": ("x.txt", b"x", "text/plain")},
    ).json()
    jobs.enqueued.clear()

    assert c.delete(f"/api/attachments/{att['id']}").status_code == 204
    assert ("ingest_note", {"note_id": note["id"]}) in jobs.enqueued
    assert c.get(f"/api/attachments/{att['id']}").status_code == 404
    assert c.delete(f"/api/attachments/{uuid.uuid4()}").status_code == 404


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


def test_note_responses_expose_analyzed(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, repo, _ = client
    # Fresh notes can't have an analysis row yet: create + list say so.
    created = c.post("/api/notes", json={"client_id": "an1", "body": "analyze me"}).json()
    assert created["analyzed"] is False
    assert c.get("/api/notes").json()["notes"][0]["analyzed"] is False

    # Once the analyze_note job lands its note_analysis row, every read path
    # (list, single note, PATCH echo) carries analyzed=true.
    repo.notes[0] = dataclasses.replace(repo.notes[0], analyzed=True)
    assert c.get("/api/notes").json()["notes"][0]["analyzed"] is True
    assert c.get(f"/api/notes/{created['id']}").json()["analyzed"] is True
    patched = c.patch(f"/api/notes/{created['id']}", json={"body": "edited"}).json()
    assert patched["analyzed"] is True


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


def test_patch_note_updates_fields_and_reingests(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, jobs = client
    note = c.post(
        "/api/notes", json={"client_id": "p1", "body": "old", "destination": "Inbox"}
    ).json()
    jobs.enqueued.clear()
    resp = c.patch(f"/api/notes/{note['id']}", json={"body": "new", "domain": "health"})
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["body"] == "new"
    assert updated["domain"] == "health"
    assert updated["destination"] == "Inbox"  # untouched fields survive
    assert updated["ingest_state"] == "pending"  # edit invalidates the index
    assert jobs.enqueued == [("ingest_note", {"note_id": note["id"]})]


def test_patch_note_can_clear_destination(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    note = c.post(
        "/api/notes", json={"client_id": "p2", "body": "b", "destination": "Inbox"}
    ).json()
    cleared = c.patch(f"/api/notes/{note['id']}", json={"destination": None}).json()
    assert cleared["destination"] is None
    # Omitting the key leaves whatever is there.
    untouched = c.patch(f"/api/notes/{note['id']}", json={"body": "c"}).json()
    assert untouched["destination"] is None


def test_patch_note_unknown_domain_400(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    note = c.post("/api/notes", json={"client_id": "p3", "body": "b"}).json()
    assert c.patch(f"/api/notes/{note['id']}", json={"domain": "nope"}).status_code == 400


def test_patch_missing_note_404(client: tuple[TestClient, FakeNotesRepo, FakeJobQueue]) -> None:
    c, _, jobs = client
    jobs.enqueued.clear()
    assert c.patch(f"/api/notes/{uuid.uuid4()}", json={"body": "x"}).status_code == 404
    assert jobs.enqueued == []  # no re-ingest for a note we couldn't touch


def test_delete_note_204_then_404(client: tuple[TestClient, FakeNotesRepo, FakeJobQueue]) -> None:
    c, _, _ = client
    note = c.post("/api/notes", json={"client_id": "d1", "body": "bye"}).json()
    assert c.delete(f"/api/notes/{note['id']}").status_code == 204
    assert all(n["id"] != note["id"] for n in c.get("/api/notes").json()["notes"])
    assert c.delete(f"/api/notes/{note['id']}").status_code == 404


def test_hide_drops_note_from_stream_then_unhide_restores(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, jobs = client
    note = c.post("/api/notes", json={"client_id": "h1", "body": "keep but hide"}).json()
    assert note["hidden"] is False
    jobs.enqueued.clear()

    assert c.post(f"/api/notes/{note['id']}/hide").status_code == 204
    # Gone from the stream, but not re-ingested (visibility ≠ content change).
    assert all(n["id"] != note["id"] for n in c.get("/api/notes").json()["notes"])
    assert jobs.enqueued == []
    # Still directly fetchable — the note is searchable, just not streamed.
    assert c.get(f"/api/notes/{note['id']}").json()["hidden"] is True

    assert c.post(f"/api/notes/{note['id']}/unhide").status_code == 204
    assert any(n["id"] == note["id"] for n in c.get("/api/notes").json()["notes"])


def test_hide_unhide_missing_note_404(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    assert c.post(f"/api/notes/{uuid.uuid4()}/hide").status_code == 404
    assert c.post(f"/api/notes/{uuid.uuid4()}/unhide").status_code == 404


def test_create_note_stores_location_verbatim(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue],
) -> None:
    c, _, _ = client
    created = c.post(
        "/api/notes",
        json={
            "client_id": "loc1",
            "body": "here",
            "latitude": 47.6097,
            "longitude": -122.3331,
            "accuracy_m": 12.5,
        },
    ).json()
    assert (created["latitude"], created["longitude"], created["accuracy_m"]) == (
        47.6097,
        -122.3331,
        12.5,
    )
    # Location is optional and defaults to absent.
    bare = c.post("/api/notes", json={"client_id": "loc2", "body": "nowhere"}).json()
    assert bare["latitude"] is None and bare["longitude"] is None and bare["accuracy_m"] is None


@pytest.mark.parametrize(
    "patch",
    [
        {"latitude": 91},
        {"latitude": -90.5},
        {"longitude": 180.1},
        {"longitude": -181},
        {"accuracy_m": -1},
    ],
)
def test_create_note_rejects_out_of_range_location(
    client: tuple[TestClient, FakeNotesRepo, FakeJobQueue], patch: dict
) -> None:
    c, _, _ = client
    payload = {"client_id": "locbad", "body": "x", "latitude": 0, "longitude": 0, **patch}
    assert c.post("/api/notes", json=payload).status_code == 422


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
