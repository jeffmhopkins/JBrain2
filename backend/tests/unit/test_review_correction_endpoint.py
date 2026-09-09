"""POST /api/review/{id}/correction — the review card's "correct it" flow. Owner-gated,
mints an owner_correction note (the #7 channel) so the fix force-supersedes what it
corrects instead of colliding with it. Mirrors the wiki-corrections gate/provenance test."""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jbrain.api import analysis as analysis_api
from jbrain.api.deps import owner_only
from jbrain.auth import service as auth_service
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.main import create_app
from jbrain.notes.service import NoteInfo, UnknownDomain
from tests.unit.fakes import FakeAuthRepo


class FakeAnalysisRepo:
    """Only the gate the endpoint consults. `correctable` decides what the (real) repo
    would have read off the target card's payload; the payload semantics themselves are
    pinned end-to-end in tests/integration/test_review_correction_gate_pg.py."""

    def __init__(self) -> None:
        self.correctable = True
        self.asked: list[str] = []

    async def review_correctable(self, ctx, item_id: str) -> bool:
        self.asked.append(item_id)
        return self.correctable


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
    # The same gate the wiki correction path uses: a capability token can never mint
    # the privileged owner_correction write.
    with pytest.raises(Exception) as exc:
        await owner_only(PrincipalInfo(id="t", kind="capability_token", label="tok"))
    assert "403" in str(exc.value) or "owner" in str(exc.value)


@pytest.fixture
def api(monkeypatch) -> Iterator[tuple[TestClient, FakeNotesRepo, FakeAnalysisRepo]]:
    repo = FakeNotesRepo()
    analysis = FakeAnalysisRepo()

    async def _no_emit(*args, **kwargs):  # the event-emit hits the DB; stub it for the unit test
        return None

    monkeypatch.setattr(analysis_api.wf_events, "emit_event", _no_emit)
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.auth_repo = FakeAuthRepo()
        app.state.notes_repo = repo
        app.state.analysis_repo = analysis
        app.state.session_maker = object()  # unused once emit is stubbed
        key = asyncio.run(auth_service.rotate_owner_key(app.state.auth_repo))
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )
        yield client, repo, analysis


def test_correction_mints_an_owner_correction_note(
    api: tuple[TestClient, FakeNotesRepo, FakeAnalysisRepo],
) -> None:
    client, repo, _analysis = api
    resp = client.post(
        "/api/review/item-42/correction",
        json={"body": "The value for address should be 6070 Chapman Street.", "domain": "finance"},
    )
    assert resp.status_code == 201
    assert resp.json()["note_id"] == "note-1"
    call = repo.calls[-1]
    # The whole point: an owner correction, not a plain human note — so it
    # force-supersedes + pins instead of colliding.
    assert call["provenance"] == "owner_correction"
    assert call["source_ref"] == "review:item-42"
    assert call["client_id"].startswith("correction-")
    assert call["domain"] == "finance"


def test_an_uncorrectable_card_is_refused_before_any_note_is_minted(
    api: tuple[TestClient, FakeNotesRepo, FakeAnalysisRepo],
) -> None:
    # `correctable: false` is a SERVER gate, not just the footer-suppressing render hint:
    # this endpoint is reachable by anything that is not the shipped UI, and on the EMR
    # firewall card a correction would pin the held value back into the very domain the
    # guard kept it out of. 409 like the router's other target-state refusal
    # ("review item is not open") — the caller is the owner and duly authorized, so this
    # is not a 403.
    client, repo, analysis = api
    analysis.correctable = False
    resp = client.post(
        "/api/review/fw-1/correction",
        json={"body": "The address for that encounter is 12 Elm Street.", "domain": "health"},
    )
    assert resp.status_code == 409
    assert "not correctable" in resp.json()["detail"]
    assert analysis.asked == ["fw-1"]  # the gate consulted the card the request named
    assert repo.calls == []  # and nothing was minted


def test_unknown_domain_is_400(api: tuple[TestClient, FakeNotesRepo, FakeAnalysisRepo]) -> None:
    client, repo, _analysis = api
    repo.raise_unknown_domain = True
    resp = client.post("/api/review/i1/correction", json={"body": "x", "domain": "bogus"})
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
                "/api/review/i1/correction", json={"body": "x", "domain": "general"}
            ).status_code
            == 401
        )
