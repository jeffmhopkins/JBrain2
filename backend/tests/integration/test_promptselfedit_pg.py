"""The `prompt_self_edit` nightly action (Loop 4, Wave 3) against real Postgres: a
cluster of owner-REJECTED proposals from one source drives a `prompt-edit` Proposal
against that source's prompt — propose-only, owner-gated, budget-gated, cooldown-
deduped. The drafting LLM is faked. Proves the durable owner-origin signal, the
threshold, the cooldown, the kill-switch, and that an unmapped source is ignored.
"""

import json
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.promptselfedit import PromptSelfEditAction
from jbrain.agent.proposals import ProposalRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter
from jbrain.queue import PermanentJobError
from jbrain.settings_store import SELF_IMPROVEMENT_KILL_SWITCH_KEY, SqlSettingsStore
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A valid draft of the (real) skill.distill prompt — the action reads the real body and
# diffs against this. No URLs, so the lint passes; version bumped so the spec builds.
_DRAFT = {
    "proposed_body": "Distill only clearly reusable runs. Prefer fewer, higher-value playbooks.",
    "proposed_version": "skill-distill-v2",
    "rationale": "Be more selective so the owner sees fewer low-value skill proposals.",
    "new_eval_fixture": "A one-off run with no reusable shape yields no skill.",
}


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
            await conn.execute(
                text("TRUNCATE app.proposals, app.settings RESTART IDENTITY CASCADE")
            )
    finally:
        await admin.dispose()


async def _owner_principal(maker: async_sessionmaker) -> str:
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid)


async def _rejected(maker: async_sessionmaker, pid: str, source: str, *, n: int) -> None:
    """Stage `n` proposals from `source`, each with a rejected leaf — the owner-origin
    'I keep rejecting these' signal."""
    kind = "skill-promotion" if source == "skill_distill" else "correction"
    async with scoped_session(maker, OWNER) as session:
        for i in range(n):
            prop_id = (
                await session.execute(
                    text(
                        "INSERT INTO app.proposals (principal_id, kind, domain_code, title,"
                        " provenance) VALUES (:pid, :k, 'general', :t, cast(:prov AS jsonb))"
                        " RETURNING id"
                    ),
                    {
                        "pid": pid,
                        "k": kind,
                        "t": f"{source} {i}",
                        "prov": json.dumps({"source": source}),
                    },
                )
            ).scalar()
            await session.execute(
                text(
                    "INSERT INTO app.proposal_nodes (proposal_id, type, label, status)"
                    " VALUES (:p, 'leaf', 'x', 'rejected')"
                ),
                {"p": str(prop_id)},
            )


def _action(maker: async_sessionmaker) -> PromptSelfEditAction:
    fake = FakeLlmClient(responses=[json.dumps(_DRAFT)])
    router = LlmRouter({"xai": fake}, {}, tiers={"high": ("xai", "m")})
    return PromptSelfEditAction(
        maker, router=router, settings=SqlSettingsStore(maker), proposals=ProposalRepo(maker)
    )


async def _prompt_edits(maker: async_sessionmaker) -> list[tuple[str, dict]]:
    async with scoped_session(maker, OWNER) as session:
        props = (
            await session.execute(
                text("SELECT id, title FROM app.proposals WHERE kind = 'prompt-edit'")
            )
        ).all()
        out = []
        for p in props:
            node = (
                await session.execute(
                    text("SELECT preview FROM app.proposal_nodes WHERE proposal_id = :p"),
                    {"p": str(p.id)},
                )
            ).one()
            out.append((p.title, dict(node.preview)))
        return out


async def test_a_rejection_cluster_drafts_a_prompt_edit(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=3)  # at the threshold
    await _action(maker).run({})

    edits = await _prompt_edits(maker)
    assert len(edits) == 1
    _title, preview = edits[0]
    assert preview["target_name"] == "skill.distill"  # the source's prompt, via the bar
    assert preview["proposed_version"] == "skill-distill-v2"
    assert preview["unified_diff"].startswith("--- a/")


async def test_below_threshold_does_nothing(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=2)  # under the threshold of 3
    await _action(maker).run({})
    assert await _prompt_edits(maker) == []


async def test_cooldown_prevents_re_proposing(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=4)
    await _action(maker).run({})
    await _action(maker).run({})  # immediate second run is within the cooldown window
    assert len(await _prompt_edits(maker)) == 1


async def test_an_unmapped_source_is_ignored(maker: async_sessionmaker) -> None:
    """Rejections of proposals from a source NOT in the map (e.g. a manual or untrusted
    origin) never trigger a self-edit — only the owner-origin mapped sources count."""
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "some_other_source", n=5)
    await _action(maker).run({})
    assert await _prompt_edits(maker) == []


async def test_the_kill_switch_refuses(maker: async_sessionmaker) -> None:
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=3)
    await SqlSettingsStore(maker).upsert(OWNER, SELF_IMPROVEMENT_KILL_SWITCH_KEY, True)
    with pytest.raises(PermanentJobError):
        await _action(maker).run({})
    assert await _prompt_edits(maker) == []
