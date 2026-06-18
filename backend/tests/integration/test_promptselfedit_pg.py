"""The `prompt_self_edit` nightly action (Loop 4, Wave 3) against real Postgres: a
cluster of owner-REJECTED proposals from one source drives a `prompt-edit` Proposal
against that source's prompt — propose-only, owner-gated, budget-gated, high-water-
deduped. The drafting LLM is faked and the discovery root is a synthetic tree (so the
test is decoupled from the real prompt bodies). Proves the durable owner-origin signal,
the threshold, the high-water re-fire guard, the bar, budget/kill refusal, the draft-
failure spend charge, the unmapped-source exclusion, and RLS on the new query path.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.promptselfedit import PromptSelfEditAction, _rejection_count
from jbrain.agent.proposals import ProposalRepo
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.llm.fake import FakeLlmClient
from jbrain.llm.router import LlmRouter
from jbrain.queue import SYSTEM_CTX, PermanentJobError
from jbrain.settings_store import (
    SELF_IMPROVEMENT_BUDGET_KEY,
    SELF_IMPROVEMENT_KILL_SWITCH_KEY,
    SqlSettingsStore,
)
from tests.conftest import docker_available
from tests.integration.test_rls import APP_PASSWORD, OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# A valid draft of the synthetic skill.distill prompt — no URLs/markers, version bumped.
_DRAFT = {
    "proposed_body": "Distill only clearly reusable runs into short playbooks.",
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


@pytest.fixture
def editable_root(tmp_path: Path) -> Path:
    """A synthetic package root with a self-editable `skill.distill` (no safety markers,
    so the draft below isn't refused by the safety guard) — decouples the test from the
    real prompt body."""
    (tmp_path / "prompts").mkdir()
    front = "name: skill.distill\nversion: skill-distill-v1\nstrength: high\nself_editable: true"
    (tmp_path / "prompts" / "s.prompt").write_text(
        f"---\n{front}\n---\nDistill runs into short reusable playbooks.\n", encoding="utf-8"
    )
    return tmp_path


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


def _action(
    maker: async_sessionmaker, root: Path, *, response: str | None = None
) -> PromptSelfEditAction:
    fake = FakeLlmClient(responses=[response if response is not None else json.dumps(_DRAFT)])
    router = LlmRouter({"xai": fake}, {}, tiers={"high": ("xai", "m")})
    return PromptSelfEditAction(
        maker,
        router=router,
        settings=SqlSettingsStore(maker),
        proposals=ProposalRepo(maker),
        root=root,
    )


async def _prompt_edits(maker: async_sessionmaker) -> list[dict]:
    async with scoped_session(maker, OWNER) as session:
        props = (
            await session.execute(text("SELECT id FROM app.proposals WHERE kind = 'prompt-edit'"))
        ).all()
        out = []
        for p in props:
            node = (
                await session.execute(
                    text("SELECT preview FROM app.proposal_nodes WHERE proposal_id = :p"),
                    {"p": str(p.id)},
                )
            ).one()
            out.append(dict(node.preview))
        return out


async def test_a_rejection_cluster_drafts_a_prompt_edit(
    maker: async_sessionmaker, editable_root: Path
) -> None:
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=3)  # at the threshold
    await _action(maker, editable_root).run({})

    edits = await _prompt_edits(maker)
    assert len(edits) == 1
    assert edits[0]["target_name"] == "skill.distill"  # the source's prompt, via the bar
    assert edits[0]["proposed_version"] == "skill-distill-v2"
    assert edits[0]["unified_diff"].startswith("--- a/")


async def test_below_threshold_does_nothing(maker: async_sessionmaker, editable_root: Path) -> None:
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=2)  # under the threshold of 3
    await _action(maker, editable_root).run({})
    assert await _prompt_edits(maker) == []


async def test_the_high_water_mark_prevents_a_stale_cluster_refiring(
    maker: async_sessionmaker, editable_root: Path
) -> None:
    """Finding-2 fix: after a draft, the SAME rejections are below the high-water mark,
    so a second run does not re-propose — only genuinely new rejections would."""
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=4)
    await _action(maker, editable_root).run({})
    await _action(maker, editable_root).run({})  # nothing new since the mark
    assert len(await _prompt_edits(maker)) == 1


async def test_a_source_whose_prompt_is_not_editable_is_skipped(
    maker: async_sessionmaker, tmp_path: Path
) -> None:
    """The bar before spend: with a root where skill.distill is NOT self-editable, the
    cluster is never even counted — no draft, no spend."""
    (tmp_path / "prompts").mkdir()  # empty: no editable targets
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=5)
    await _action(maker, tmp_path).run({})
    assert await _prompt_edits(maker) == []
    assert (
        await SqlSettingsStore(maker).self_improvement_spent_today(
            SYSTEM_CTX, day=datetime.now(UTC).strftime("%Y-%m-%d")
        )
        == 0
    )


async def test_an_untrusted_origin_source_is_ignored(
    maker: async_sessionmaker, editable_root: Path
) -> None:
    """A rejected proposal whose provenance source is the chat origin (not one of the
    owner-origin nightly sources) never triggers a self-edit (#10)."""
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "chat", n=5)
    await _action(maker, editable_root).run({})
    assert await _prompt_edits(maker) == []


async def test_a_failed_draft_charges_spend_but_stages_nothing(
    maker: async_sessionmaker, editable_root: Path
) -> None:
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=3)
    await _action(maker, editable_root, response="not json at all").run({})
    assert await _prompt_edits(maker) == []
    spent = await SqlSettingsStore(maker).self_improvement_spent_today(
        SYSTEM_CTX, day=datetime.now(UTC).strftime("%Y-%m-%d")
    )
    assert spent > 0  # the failed drafting call still charged the budget (#10)


async def test_the_kill_switch_refuses(maker: async_sessionmaker, editable_root: Path) -> None:
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=3)
    await SqlSettingsStore(maker).upsert(OWNER, SELF_IMPROVEMENT_KILL_SWITCH_KEY, True)
    with pytest.raises(PermanentJobError):
        await _action(maker, editable_root).run({})
    assert await _prompt_edits(maker) == []


async def test_an_exhausted_budget_refuses(maker: async_sessionmaker, editable_root: Path) -> None:
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=3)
    # A budget below the per-run estimate -> the gate refuses before any draft.
    await SqlSettingsStore(maker).upsert(OWNER, SELF_IMPROVEMENT_BUDGET_KEY, 1)
    with pytest.raises(PermanentJobError):
        await _action(maker, editable_root).run({})
    assert await _prompt_edits(maker) == []


async def test_rejection_count_is_owner_only_rls(
    maker: async_sessionmaker, editable_root: Path
) -> None:
    """RLS on the new query path (#8): the rejection signal is owner-only — a non-owner
    session sees zero, so it can neither read nor manufacture a cluster."""
    pid = await _owner_principal(maker)
    await _rejected(maker, pid, "skill_distill", n=3)
    floor = datetime.fromtimestamp(0, UTC)
    async with scoped_session(maker, OWNER) as session:
        assert await _rejection_count(session, source="skill_distill", after=floor) == 3
    non_owner = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker, non_owner) as session:
        assert await _rejection_count(session, source="skill_distill", after=floor) == 0
