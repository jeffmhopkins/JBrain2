"""POST /api/wiki/{id}/corrections — the owner correction-create path: owner-gated, mints an
owner_correction note with the right provenance/anchor, and validates the revision/domain."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jbrain.api import wiki as wiki_api
from jbrain.api.deps import owner_only
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.notes.service import NoteInfo, UnknownDomain
from tests.unit.fakes import FakeAuthRepo


class FakeNotesRepo:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.created = True
        self.raise_unknown_domain = False

    async def create_note(self, ctx, **kw) -> tuple[NoteInfo, bool]:
        if self.raise_unknown_domain:
            raise UnknownDomain("nope")
        self.calls.append(kw)
        note = NoteInfo(
            id="note-1",
            client_id=kw["client_id"],
            domain=kw["domain"],
            destination=None,
            body=kw["body"],
            created_at=None,  # type: ignore[arg-type]
            ingest_state="pending",
            provenance=kw.get("provenance", "human"),
        )
        return note, self.created


async def test_owner_only_rejects_a_non_owner_token() -> None:
    # The gate that protects the privileged owner_correction write.
    with pytest.raises(Exception) as exc:
        await owner_only(PrincipalInfo(id="t", kind="capability_token", label="tok"))
    assert "403" in str(exc.value) or "owner" in str(exc.value)


@pytest.fixture
def api(monkeypatch) -> Iterator[tuple[TestClient, FakeNotesRepo]]:
    repo = FakeNotesRepo()

    async def _no_emit(*args, **kwargs):  # the event-emit hits the DB; stub it for the unit test
        return None

    monkeypatch.setattr(wiki_api.wf_events, "emit_event", _no_emit)
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        app.state.notes_repo = repo
        app.state.session_maker = object()  # unused once emit is stubbed and revision_id omitted
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield client, repo


def test_file_correction_mints_an_owner_correction_note(
    api: tuple[TestClient, FakeNotesRepo],
) -> None:
    client, repo = api
    resp = client.post(
        "/api/wiki/priya-nair/corrections",
        json={"body": "She actually works at Globex.", "domain": "general"},
    )
    assert resp.status_code == 201
    assert resp.json()["note_id"] == "note-1"
    call = repo.calls[-1]
    assert call["provenance"] == "owner_correction"
    assert call["source_ref"] == "wiki:priya-nair"
    assert call["client_id"].startswith("correction-")
    assert call["wiki_revision_id"] is None


def test_bad_revision_id_is_400(api: tuple[TestClient, FakeNotesRepo]) -> None:
    client, _ = api
    resp = client.post(
        "/api/wiki/a1/corrections",
        json={"body": "x", "domain": "general", "revision_id": "not-a-uuid"},
    )
    assert resp.status_code == 400


def test_unknown_domain_is_400(api: tuple[TestClient, FakeNotesRepo]) -> None:
    client, repo = api
    repo.raise_unknown_domain = True
    resp = client.post("/api/wiki/a1/corrections", json={"body": "x", "domain": "bogus"})
    assert resp.status_code == 400


def test_requires_auth() -> None:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as anon:
        app.state.auth_repo = FakeAuthRepo()
        assert (
            anon.post(
                "/api/wiki/a1/corrections", json={"body": "x", "domain": "general"}
            ).status_code
            == 401
        )
