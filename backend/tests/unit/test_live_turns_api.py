"""GET /api/ops/turns — the roster behind the vitals detail surface.

The list comes from the runs TABLE, not the in-process live-turn registry: that
registry holds only parent /chat turns, so a deep-research fan would collapse to one
row and a workflow run would never appear. These pin that, the call stamp's optional
shape, and the owner-only gate.
"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from jbrain.agent.runlog import LiveTurnRow
from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

NOW = datetime(2026, 8, 16, 7, 41, 56, tzinfo=UTC)


def row(**over: object) -> LiveTurnRow:
    base: dict[str, object] = {
        "id": "run_parent",
        "kind": "agent",
        "status": "running",
        "name": "agent",
        "started_at": NOW,
        "elapsed_ms": 252_000,
        "step_count": 9,
        "cost_tokens": 38_200,
        "progress_note": "synthesising 6 sources",
        "parent_run_id": None,
        "session_id": "sess_4b1e",
        "domain_code": None,
        "ran_as": "scoped",
        "prompt_version": "fullbrain@v14",
        "trigger_pipeline": None,
        "call_stamp": {
            "provider": "anthropic",
            "model": "claude-opus-4-6",
            "reasoning_effort": "high",
            "context_window": 200_000,
            "vision": True,
            "persona": "jerv",
            "tools": ["notes.search", "web.fetch"],
            "user_message": "Dig into heat pump sizing.",
            "user_message_truncated": False,
        },
    }
    base.update(over)
    return LiveTurnRow(**base)  # type: ignore[arg-type]


class FakeRunReader:
    def __init__(self) -> None:
        self.rows: list[LiveTurnRow] = []

    async def list_live(self, ctx: object, *, limit: int = 50) -> list[LiveTurnRow]:
        return self.rows


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def reader() -> FakeRunReader:
    return FakeRunReader()


@pytest.fixture
def client(repo: FakeAuthRepo, reader: FakeRunReader) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.run_reader = reader
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_turns_require_owner(client: TestClient) -> None:
    assert client.get("/api/ops/turns").status_code == 401


def test_lists_a_turn_with_its_call(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    reader.rows = [row()]
    login(client, repo)

    body = client.get("/api/ops/turns").json()

    assert len(body["turns"]) == 1
    turn = body["turns"][0]
    assert turn["kind"] == "agent"
    assert turn["progress_note"] == "synthesising 6 sources"
    assert turn["call"]["model"] == "claude-opus-4-6"
    assert turn["call"]["persona"] == "jerv"
    # The verbatim prompt is stored nowhere, so the payload must not imply otherwise.
    assert "prompt" not in turn["call"]


def test_nests_a_fan_by_parent(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """A deep-research fan is a parent plus its children — the whole reason this reads
    the table instead of the registry, which only knows the parent."""
    reader.rows = [
        row(),
        row(id="run_child_a", kind="subagent", parent_run_id="run_parent"),
        row(id="run_child_b", kind="subagent", parent_run_id="run_parent"),
    ]
    login(client, repo)

    turns = client.get("/api/ops/turns").json()["turns"]

    assert [t["id"] for t in turns] == ["run_parent", "run_child_a", "run_child_b"]
    assert [t["parent_run_id"] for t in turns[1:]] == ["run_parent", "run_parent"]


def test_renders_a_run_with_no_stamp(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """Runs predating migration 0166, and drivers that don't stamp, still list — the
    surface shows what it has rather than hiding the row."""
    reader.rows = [row(call_stamp=None, kind="pipeline", trigger_pipeline="nightly-reconcile")]
    login(client, repo)

    turn = client.get("/api/ops/turns").json()["turns"][0]

    assert turn["call"] is None
    assert turn["trigger_pipeline"] == "nightly-reconcile"


def test_keeps_a_partial_stamp(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    reader.rows = [row(call_stamp={"model": "gpt-oss-120b"})]
    login(client, repo)

    call = client.get("/api/ops/turns").json()["turns"][0]["call"]

    assert call["model"] == "gpt-oss-120b"
    assert call["provider"] is None


def test_distinguishes_a_wildcard_toolset_from_none(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader
) -> None:
    """null tools is the registry WILDCARD ("every in-scope tool"); [] is "no tools".
    Flattening them would tell the owner a jerv turn holds nothing."""
    reader.rows = [
        row(id="wild", call_stamp={"tools": None}),
        row(id="none", call_stamp={"tools": []}),
    ]
    login(client, repo)

    turns = client.get("/api/ops/turns").json()["turns"]

    assert turns[0]["call"]["tools"] is None
    assert turns[1]["call"]["tools"] == []


def test_carries_the_gauge_next_to_an_empty_roster(
    client: TestClient, repo: FakeAuthRepo, reader: FakeRunReader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPU busy covers the whole box — image generation included — so a high reading
    with nothing running is legitimate, and the surface needs both figures at once to
    say so instead of looking broken."""
    from jbrain.api import ops

    monkeypatch.setattr(ops, "read_gpu_busy_percent", lambda: 94.0)
    reader.rows = []
    login(client, repo)

    body = client.get("/api/ops/turns").json()

    assert body["turns"] == []
    assert body["gpu_busy_percent"] == 94.0
