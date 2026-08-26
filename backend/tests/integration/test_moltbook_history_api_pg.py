"""jmolt's history browser HTTP API against real Postgres (docs/plans/JMOLT_PLAN.md).

Drives the actual FastAPI app: owner login, then the read-only history surface the PWA's
jmolt screen uses to walk jmolt's nights, transcripts, action ledger, and scratchpad
without a debug token. Proves the owner-gated routes reach real RLS-scoped rows written
under jmolt's own night context, that an unauthenticated caller is refused (the security
path), and that a non-uuid session id degrades to an empty transcript rather than a 500.
"""

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.main import create_app
from jbrain.models.jmolt import JmoltScratchRepo
from jbrain.models.jmolt_outbox import ActionLedgerRepo
from tests.conftest import docker_available
from tests.integration.test_rls import database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _jmolt_ctx(pid: str) -> SessionContext:
    # jmolt's nightly write context — the sole context the M19 RLS split lets write jmolt's
    # tables (auth_ctx='jmolt'), owner-scoped and firewalled to the jmolt domain.
    return SessionContext(
        principal_id=pid,
        principal_kind="owner",
        domain_scopes=("jmolt",),
        auth_context="jmolt",
        owner_scoped=True,
    )


async def _owner_pid_and_key(maker: async_sessionmaker[AsyncSession]) -> tuple[str, str]:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as s:
        pid = (await s.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))).scalar()
    return str(pid), key


async def _seed(maker: async_sessionmaker[AsyncSession], pid: str) -> str:
    """One night's worth of history — a completed session + its run + a turn, a notebook
    file, and a logged action — all written under jmolt's own night context (owner pid).
    The module-scoped database is pristine, so nothing needs clearing first."""
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        sid = (
            await s.execute(
                text(
                    "INSERT INTO app.agent_sessions (id, principal_id, agent, title, domain_scopes)"
                    " VALUES (gen_random_uuid(), :pid, 'jmolt', 'night', ARRAY['jmolt'])"
                    " RETURNING id"
                ),
                {"pid": pid},
            )
        ).scalar()
        await s.execute(
            text(
                "INSERT INTO app.runs (id, session_id, kind, status, stop_reason, step_count,"
                " cost_tokens, prompt_version) VALUES (gen_random_uuid(), :sid, 'agent', 'done',"
                " 'end_turn', 9, 46125, 'v1')"
            ),
            {"sid": sid},
        )
        await s.execute(
            text(
                "INSERT INTO app.agent_turns (id, session_id, role, content, reasoning)"
                " VALUES (gen_random_uuid(), :sid, 'assistant', 'lurked the general submolt',"
                " 'it is mostly noise so far')"
            ),
            {"sid": sid},
        )
        # A second sitting under the same night session — the nights endpoint aggregates
        # runs per session (sum steps/cost, count sittings).
        await s.execute(
            text(
                "INSERT INTO app.runs (id, session_id, kind, status, stop_reason, step_count,"
                " cost_tokens, prompt_version) VALUES (gen_random_uuid(), :sid, 'agent', 'done',"
                " 'end_turn', 5, 20000, 'v1')"
            ),
            {"sid": sid},
        )
        await s.execute(
            text(
                "INSERT INTO app.agent_turns (id, session_id, role, content, reasoning)"
                " VALUES (gen_random_uuid(), :sid, 'assistant', 'replied to two agents',"
                " 'the philosophy submolt is more my speed')"
            ),
            {"sid": sid},
        )
        await JmoltScratchRepo().write(s, pid, "intro.md", "who I am: a naturalist among agents")
        await ActionLedgerRepo().record(
            s, pid, action="web_fetch", target="https://example/agents", reacted_to="a memory post"
        )
    return str(sid)


async def test_history_api_round_trip(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    pid, key = await _owner_pid_and_key(maker)
    sid = await _seed(maker, pid)
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        base = "/api/settings/moltbook"

        # Owner-gated: no session → 401 on every history surface.
        assert client.get(f"{base}/nights").status_code == 401
        assert client.get(f"{base}/files").status_code == 401

        assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204

        # --- nights: one night, its two sittings aggregated ---
        nights = client.get(f"{base}/nights").json()
        assert len(nights) == 1  # one session, though it has two sitting-runs
        night = nights[0]
        assert night["session_id"] == sid
        assert night["status"] == "done"
        assert night["sittings"] == 2
        assert night["steps"] == 14 and night["cost_tokens"] == 66125  # summed across sittings
        assert night["at"] is not None

        # --- transcript: content + reasoning across the night's sittings, in order ---
        turns = client.get(f"{base}/nights/{sid}/transcript").json()
        assert len(turns) == 2
        assert turns[0]["role"] == "assistant"
        assert "general submolt" in turns[0]["content"]
        assert "mostly noise" in turns[0]["reasoning"]
        assert "replied to two agents" in turns[1]["content"]
        # A non-uuid id is a clean empty transcript, never a 500 from casting to uuid.
        bad = client.get(f"{base}/nights/not-a-uuid/transcript")
        assert bad.status_code == 200 and bad.json() == []

        # --- actions ledger: what jmolt did ---
        actions = client.get(f"{base}/actions").json()
        assert len(actions) == 1
        assert actions[0]["action"] == "web_fetch"
        assert actions[0]["reacted_to"] == "a memory post"

        # --- scratchpad: list → read → history ---
        files = client.get(f"{base}/files").json()
        assert [f["filename"] for f in files] == ["intro.md"]
        assert files[0]["bytes"] > 0

        content = client.get(f"{base}/files/content", params={"filename": "intro.md"}).json()
        assert "naturalist among agents" in content["content"]
        # A missing file reads as null content, not an error.
        missing = client.get(f"{base}/files/content", params={"filename": "nope.md"}).json()
        assert missing["content"] is None

        history = client.get(f"{base}/files/history", params={"filename": "intro.md"}).json()
        assert len(history) >= 1
        assert history[0]["filename"] == "intro.md"
        assert "naturalist among agents" in history[0]["content"]


async def test_actions_filtering_paging_and_stats(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    # The action ledger endpoint filters server-side (family/kinds/since/cursor) and a
    # /stats aggregate gives honest per-kind counts — so a busy night's drip burst stays
    # legible in the PWA instead of a flat wall of identical publish rows.
    pid, key = await _owner_pid_and_key(maker)
    # The ledger's DELETE policy is non-jmolt-owner only, so clear under an admin context;
    # rows are then appended under jmolt's own night context.
    admin = SessionContext(principal_id=pid, principal_kind="owner", domain_scopes=("jmolt",))
    async with scoped_session(maker, admin) as s:
        await s.execute(text("DELETE FROM app.jmolt_action_ledger"))
    async with scoped_session(maker, _jmolt_ctx(pid)) as s:
        ledger = ActionLedgerRepo()
        await ledger.record(s, pid, action="stage_comment", target="Luna24", reacted_to="hi")
        await ledger.record(s, pid, action="stage_vote", target="t1")
        await ledger.record(s, pid, action="stage_follow", target="mundo")
        for _ in range(3):  # the drip burst: identical publish_comment rows on one thread
            await ledger.record(s, pid, action="publish_comment", target="post-9")
        await ledger.record(s, pid, action="publish_post", target="/memory")

    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        base = "/api/settings/moltbook"
        assert client.post("/api/auth/session", json={"owner_key": key}).status_code == 204

        # family=stage → only the drafted rows.
        stage = client.get(f"{base}/actions", params={"family": "stage"}).json()
        assert {a["action"] for a in stage} == {"stage_comment", "stage_vote", "stage_follow"}

        # kinds=comment → both stage_comment and the publish_comment burst (4).
        comments = client.get(f"{base}/actions", params={"kinds": "comment"}).json()
        assert len(comments) == 4 and all(a["action"].endswith("_comment") for a in comments)

        # family + kinds compose → just the 3 published comments.
        pub_c = client.get(
            f"{base}/actions", params={"family": "publish", "kinds": "comment"}
        ).json()
        assert len(pub_c) == 3

        # limit + cursor paging, newest-first by seq.
        page1 = client.get(f"{base}/actions", params={"limit": 2}).json()
        assert len(page1) == 2
        page2 = client.get(
            f"{base}/actions", params={"limit": 2, "cursor": page1[-1]["seq"]}
        ).json()
        assert len(page2) == 2 and page1[-1]["seq"] > page2[0]["seq"]  # page2 is older

        # /stats: honest per-(family, kind) counts, independent of any list filter.
        stats = client.get(f"{base}/actions/stats").json()
        by = {(x["family"], x["kind"]): x["count"] for x in stats}
        assert by[("publish", "comment")] == 3
        assert by[("stage", "comment")] == 1
        assert by[("stage", "follow")] == 1
