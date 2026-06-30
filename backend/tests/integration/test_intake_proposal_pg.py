"""The editable intake-link Proposal → mint flow (W4, real Postgres).

A staged intake-link Proposal is owner-editable (constrained fields; subject/domain are
NOT editable), and approving it mints the link show-once with the edited config, marking
the Proposal enacted. Subject/domain are re-validated at mint."""

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.proposals import NodeSpec, ProposalRepo, ProposalSpec
from jbrain.api import intake
from jbrain.api.deps import current_principal
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.auth.service import PrincipalInfo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.intake.repo import SqlIntakeRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _owner(maker: async_sessionmaker) -> tuple[str, SessionContext]:
    await auth_service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid), SessionContext(principal_id=str(pid), principal_kind="owner")


async def _subject(maker: async_sessionmaker, ctx: SessionContext) -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:i, 'S', 'person')"),
            {"i": sid},
        )
    return sid


def _config(subject_id: str) -> dict:
    return {
        "subject_id": subject_id,
        "domain": "general",
        "fields_brief": "a phone number",
        "persona_brief": "",
        "opening_blurb": "original blurb",
        "label": "",
        "max_runs": 2,
        "max_opens": 8,
        "bind_on_first": False,
        "ttl_hours": 24.0,
        "capture_enterer_name": True,
        "disclose_owner_identity": False,
    }


async def _stage_intake_link(
    maker: async_sessionmaker, owner_pid: str, ctx: SessionContext, config: dict
) -> tuple[str, str]:
    """Stage an intake-link proposal (the make_intake_link tool's effect). Returns
    (proposal_id, node_id)."""
    proposals = ProposalRepo(maker)
    spec = ProposalSpec(
        kind="intake-link",
        domain=config["domain"],
        subject_id=config["subject_id"],
        title="phone",
        nodes=[NodeSpec(id=str(uuid.uuid4()), type="leaf", op="mint_intake_link", preview=config)],
        provenance={"source": "chat"},
    )
    prop_id = await proposals.stage(ctx, principal_id=owner_pid, spec=spec)
    _, nodes = await proposals.load(ctx, prop_id)
    return prop_id, nodes[0].id


def _app(maker: async_sessionmaker, owner_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(intake.router, prefix="/api")
    app.state.auth_repo = SqlAuthRepo(maker)
    app.state.intake_repo = SqlIntakeRepo(maker)
    app.state.agent_proposals = ProposalRepo(maker)
    app.state.settings = Settings(secure_cookies=False)
    app.dependency_overrides[current_principal] = lambda: PrincipalInfo(
        id=owner_id, kind="owner", label="owner"
    )
    return app


async def test_edit_then_mint_from_proposal(maker: async_sessionmaker) -> None:
    owner_pid, ctx = await _owner(maker)
    subject_id = await _subject(maker, ctx)
    prop_id, node_id = await _stage_intake_link(maker, owner_pid, ctx, _config(subject_id))
    app = _app(maker, owner_pid)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        # Edit the constrained config; a subject_id in the body is ignored (not editable).
        patched = await client.patch(
            f"/api/intake/proposals/nodes/{node_id}/config",
            json={"opening_blurb": "edited blurb", "max_runs": 5, "subject_id": str(uuid.uuid4())},
        )
        assert patched.status_code == 204

        # Approve → mint: returns the secret once and a link carrying the EDITED config.
        minted = await client.post(f"/api/intake/links/from-proposal/{prop_id}")
        assert minted.status_code == 201
        body = minted.json()
        assert body["secret"]
        link = (await client.get(f"/api/intake/links/{body['id']}")).json()
        assert link["opening_blurb"] == "edited blurb"  # the edit took
        assert link["max_runs"] == 5
        assert link["subject_id"] == subject_id  # subject was NOT changed by the patch
        assert link["domain_code"] == "general"

    # The proposal is enacted (no longer mintable a second time).
    async with scoped_session(maker, ctx) as session:
        assert (
            await session.execute(
                text("SELECT status FROM app.proposals WHERE id = :i"), {"i": prop_id}
            )
        ).scalar() == "enacted"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        again = await client.post(f"/api/intake/links/from-proposal/{prop_id}")
        assert again.status_code == 409


async def test_patch_is_rejected_after_the_proposal_is_decided(maker: async_sessionmaker) -> None:
    owner_pid, ctx = await _owner(maker)
    subject_id = await _subject(maker, ctx)
    prop_id, node_id = await _stage_intake_link(maker, owner_pid, ctx, _config(subject_id))
    # Move the proposal out of 'staged' — the editable window is closed.
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text("UPDATE app.proposals SET status = 'approved' WHERE id = :i"), {"i": prop_id}
        )
    app = _app(maker, owner_pid)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.patch(
            f"/api/intake/proposals/nodes/{node_id}/config", json={"opening_blurb": "too late"}
        )
        assert resp.status_code == 404


async def test_mint_revalidates_subject_at_mint(maker: async_sessionmaker) -> None:
    owner_pid, ctx = await _owner(maker)
    # A config whose subject_id does not exist — staging doesn't enforce the FK, but mint does.
    bogus = _config(str(uuid.uuid4()))
    prop_id, _ = await _stage_intake_link(maker, owner_pid, ctx, bogus)
    app = _app(maker, owner_pid)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(f"/api/intake/links/from-proposal/{prop_id}")
        assert resp.status_code == 400  # FK re-validation at mint rejects it
