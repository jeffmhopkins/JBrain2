"""The Proposals API with fakes on app.state — owner-only list/get/decide/enact."""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from jbrain.agent.proposals import EnactmentPlan, NodeRow, ProposalRow, ProposalSummary
from jbrain.auth import service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo


class FakeProposals:
    def __init__(self) -> None:
        self.decided: list[tuple[str, bool]] = []
        self.enacted: list[str] = []
        self._summary = ProposalSummary("p1", "correction", "staged", "health", "PCP is Dr. Lin", 1)
        self._proposal = ProposalRow("p1", "correction", "staged", "health", "PCP is Dr. Lin", None)
        self._nodes = [
            NodeRow(
                "n1",
                None,
                "leaf",
                "add_note",
                "PCP is Dr. Lin",
                {"body": "PCP is Dr. Lin"},
                (),
                "pending",
            )
        ]

    async def list_open(self, ctx: object) -> list[ProposalSummary]:
        return [self._summary]

    async def load(self, ctx: object, proposal_id: str):  # type: ignore[no-untyped-def]
        if proposal_id != "p1":
            raise ValueError("no proposal with that id in scope")
        return self._proposal, self._nodes

    async def decide(self, ctx: object, node_id: str, *, approve: bool) -> None:
        self.decided.append((node_id, approve))

    async def enact(self, ctx: object, proposal_id: str, executor: object) -> EnactmentPlan:
        self.enacted.append(proposal_id)
        return EnactmentPlan(enactable=("n1",), held=())


@pytest.fixture
def repo() -> FakeAuthRepo:
    return FakeAuthRepo()


@pytest.fixture
def proposals() -> FakeProposals:
    return FakeProposals()


@pytest.fixture
def client(repo: FakeAuthRepo, proposals: FakeProposals) -> Iterator[TestClient]:
    settings = Settings(
        secure_cookies=False, database_url="postgresql+asyncpg://nobody@localhost:1/none"
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        app.state.auth_repo = repo
        app.state.agent_proposals = proposals
        yield test_client


def login(client: TestClient, repo: FakeAuthRepo) -> None:
    key = asyncio.run(service.rotate_owner_key(repo))
    assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204


def test_proposals_require_owner(client: TestClient) -> None:
    assert client.get("/api/proposals").status_code == 401


def test_list_and_get_proposal(client: TestClient, repo: FakeAuthRepo) -> None:
    login(client, repo)
    listed = client.get("/api/proposals")
    assert listed.status_code == 200
    assert listed.json()[0]["title"] == "PCP is Dr. Lin"

    tree = client.get("/api/proposals/p1")
    assert tree.status_code == 200
    assert tree.json()["nodes"][0]["op"] == "add_note"

    assert client.get("/api/proposals/ghost").status_code == 404


def test_decide_and_enact(client: TestClient, repo: FakeAuthRepo, proposals: FakeProposals) -> None:
    login(client, repo)
    d = client.post("/api/proposals/p1/nodes/n1/decision", json={"decision": "approve"})
    assert d.status_code == 204
    assert proposals.decided == [("n1", True)]

    e = client.post("/api/proposals/p1/enact")
    assert e.status_code == 200
    assert e.json() == {"enacted": ["n1"], "held": []}
    assert proposals.enacted == ["p1"]
