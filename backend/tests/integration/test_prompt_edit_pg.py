"""The Loop-4 prompt-edit Proposal against real Postgres (docs/LOOP4_PROMPT_TOOL_EDIT_PLAN.md,
Wave 1): a `prompt-edit` proposal stages, approves, and enacts through the SHIPPED
ProposalRepo + build_leaf_executor — and the load-bearing #6 invariant holds: enact
is RECORD-ONLY. No note is created, and no on-disk prompt/tool file is mutated (its
content is byte-identical before and after). The diff lives only in the preview.
"""

import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.connectortools import build_leaf_executor
from jbrain.agent.proposals import ProposalRepo
from jbrain.agent.selfedit import build_prompt_edit_spec, self_editable_targets
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


class _Jobs:
    """A note executor enqueues `ingest_note`; record-only must enqueue nothing."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict]] = []

    async def enqueue(self, ctx: object, kind: str, payload: dict) -> str:
        self.enqueued.append((kind, payload))
        return "job-1"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _isolate(database_url: str) -> AsyncIterator[None]:  # noqa: F811
    yield
    admin = create_async_engine(
        database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test"), poolclass=NullPool
    )
    try:
        async with admin.begin() as conn:
            await conn.execute(text("TRUNCATE app.proposals, app.notes RESTART IDENTITY CASCADE"))
    finally:
        await admin.dispose()


@pytest.fixture
def editable_tree(tmp_path: Path) -> Path:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "title.prompt").write_text(
        "---\nname: session.title\nversion: v1\nstrength: low\nself_editable: true\n---\n"
        "Title the chat in a few words.\n",
        encoding="utf-8",
    )
    return tmp_path


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_prompt_edit_enact_is_record_only(
    maker: async_sessionmaker, editable_tree: Path
) -> None:
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    jobs = _Jobs()
    target_path = editable_tree / "prompts" / "title.prompt"
    before = _digest(target_path)

    spec = build_prompt_edit_spec(
        "session.title",
        proposed_body="Title the chat in at most five words, no punctuation.",
        proposed_version="v2",
        rationale="cap the length and drop punctuation",
        new_eval_fixture="a fixture asserting <=5 word titles",
        root=editable_tree,
    )
    prop_id = await repo.stage(OWNER, principal_id=pid, spec=spec)

    # Enact through the SHIPPED executor (the same one the API builds), with stub
    # collaborators — a prompt-edit leaf must touch none of them.
    executor = build_leaf_executor(
        notes=None,  # type: ignore[arg-type]
        connectors=None,  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        analysis=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
    )
    await repo.decide(OWNER, spec.nodes[0].id, approve=True)
    plan = await repo.enact(OWNER, prop_id, executor)

    # The leaf ran and is recorded enacted...
    assert plan.enactable == (spec.nodes[0].id,) and plan.held == ()
    proposal, nodes = await repo.load(OWNER, prop_id)
    assert proposal.status == "enacted"
    assert nodes[0].status == "enacted"
    # ...but NOTHING was applied: no ingestion enqueued (record-only, not a note),
    # no note row created, and the on-disk prompt is byte-identical (#6).
    assert jobs.enqueued == []
    async with scoped_session(maker, OWNER) as session:
        note_count = (await session.execute(text("SELECT count(*) FROM app.notes"))).scalar()
    assert note_count == 0
    assert _digest(target_path) == before
    # The diff survived as data in the preview — the deliverable the owner applies.
    assert nodes[0].preview["target_name"] == "session.title"
    assert nodes[0].preview["unified_diff"].startswith("--- a/")


async def test_crafted_body_preview_still_creates_no_note(maker: async_sessionmaker) -> None:
    """The axis-2 threat made concrete: a prompt-edit leaf whose preview carries a
    `body` key (the field the agent-note executor reads) must STILL create no note —
    proving dispatch keys on `op`, not preview, and never falls through to the note
    executor. Build the spec by hand to inject the hostile `body`."""
    from jbrain.agent.proposals import NodeSpec, ProposalSpec

    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    jobs = _Jobs()
    node = NodeSpec(
        id=str(uuid.uuid4()),
        type="leaf",
        op="prompt_edit_record",
        label="hostile",
        preview={"target_name": "x", "unified_diff": "--- a/x", "body": "smuggle me into a note"},
    )
    spec = ProposalSpec(kind="prompt-edit", domain="general", title="hostile", nodes=[node])
    prop_id = await repo.stage(OWNER, principal_id=pid, spec=spec)

    executor = build_leaf_executor(
        notes=None,  # type: ignore[arg-type]
        connectors=None,  # type: ignore[arg-type]
        jobs=jobs,  # type: ignore[arg-type]
        analysis=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
    )
    await repo.decide(OWNER, node.id, approve=True)
    await repo.enact(OWNER, prop_id, executor)

    assert jobs.enqueued == []
    async with scoped_session(maker, OWNER) as session:
        note_count = (await session.execute(text("SELECT count(*) FROM app.notes"))).scalar()
    assert note_count == 0


async def test_a_non_owner_principal_cannot_stage_a_prompt_edit(
    maker: async_sessionmaker, editable_tree: Path
) -> None:
    """RLS isolation on the new staging path (#8/F7): a non-owner principal cannot
    stage a behavior (prompt-edit) proposal — the proposals WITH CHECK is is_owner()
    AND has_domain_scope, so a non-owner session is rejected at the DB, the firewall,
    not a handler check."""
    pid = await _owner_principal(maker)
    repo = ProposalRepo(maker)
    spec = build_prompt_edit_spec(
        "session.title",
        proposed_body="Title the chat in at most five words.",
        proposed_version="v2",
        rationale="cap length",
        new_eval_fixture="<=5 words",
        root=editable_tree,
    )
    non_owner = SessionContext(
        principal_id=pid, principal_kind="capability_token", domain_scopes=("general",)
    )
    with pytest.raises(ProgrammingError):
        await repo.stage(non_owner, principal_id=pid, spec=spec)


async def test_self_editable_targets_finds_the_marked_prompt(editable_tree: Path) -> None:
    found = self_editable_targets(editable_tree)
    assert set(found) == {"session.title"}
    assert found["session.title"].version == "v1"
