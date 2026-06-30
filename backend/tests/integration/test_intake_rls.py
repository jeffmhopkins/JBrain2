"""RLS proofs for the guided-intake principal and tables (W1, security-critical).

The load-bearing properties (GUIDED_INTAKE_PLAN.md §5):
  * the `intake_link` principal fails BOTH `is_owner()` and `is_full_owner()`;
  * the OWNER-BYPASS AUDIT — it is denied by every `USING(app.is_owner())` table
    (and cannot be stored in `app.agent_sessions`), so "empty scope" leaks nothing;
  * it reads ZERO of the owner's domain content (empty read scope, #8);
  * recipients are isolated from each other by the per-session `principal_id` pin;
  * the owner sees everything as a FULL owner, but a domain-NARROWED owner does not
    (proving the policies use `is_full_owner()`, never the `is_owner()` shortcut).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.agent.session import read_context
from jbrain.auth import keys
from jbrain.auth import service as auth_service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, intake_context, scoped_session
from jbrain.intake.repo import SqlIntakeRepo
from jbrain.intake.service import IntakeLinkConfig, mint_intake_link
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
    """A real owner principal + its full-owner context (for FK references + RLS)."""
    await auth_service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, SessionContext(principal_kind="owner")) as session:
        pid = (
            await session.execute(text("SELECT id FROM app.principals WHERE kind = 'owner'"))
        ).scalar()
    return str(pid), SessionContext(principal_id=str(pid), principal_kind="owner")


async def _subject(maker: async_sessionmaker, ctx: SessionContext, name: str) -> str:
    sid = str(uuid.uuid4())
    async with scoped_session(maker, ctx) as session:
        await session.execute(
            text("INSERT INTO app.subjects (id, display_name, kind) VALUES (:i, :n, 'person')"),
            {"i": sid, "n": name},
        )
    return sid


def _config(subject_id: str, **over: object) -> IntakeLinkConfig:
    base: dict = dict(
        subject_id=subject_id,
        domain_code="general",
        label="intake",
        persona_brief="",
        fields_brief="collect a phone number",
        opening_blurb="hi",
        max_runs=5,
        max_opens=5,
        bind_on_first=False,
        ttl_hours=24.0,
    )
    base.update(over)
    return IntakeLinkConfig(**base)  # type: ignore[arg-type]


async def _link_and_session(
    maker: async_sessionmaker, owner_ctx: SessionContext, subject_id: str, **over: object
) -> tuple[str, str, str]:
    """Mint a link and claim one session; return (link_id, principal_id, session_id)."""
    repo = SqlIntakeRepo(maker)
    secret, record = await mint_intake_link(repo, owner_ctx, _config(subject_id, **over))
    claim = await repo.claim(
        secret_hash=keys.hash_token(secret),
        principal_key_hash=keys.hash_token("k" + secret),
        label="x",
    )
    assert claim is not None
    return record.id, claim.principal_id, claim.session_id


async def test_intake_principal_fails_owner_checks(maker: async_sessionmaker) -> None:
    _, owner_ctx = await _owner(maker)
    sid = await _subject(maker, owner_ctx, "Dana")
    _, pid, _ = await _link_and_session(maker, owner_ctx, sid)

    async with scoped_session(maker, intake_context(pid)) as session:
        is_owner, is_full = (
            await session.execute(text("SELECT app.is_owner(), app.is_full_owner()"))
        ).one()
    assert is_owner is False and is_full is False


async def test_owner_bypass_audit(maker: async_sessionmaker) -> None:
    """The §5 must-fix: the intake principal is denied by EVERY is_owner() table, sees
    no owner row, and cannot be stored in agent_sessions."""
    owner_pid, owner_ctx = await _owner(maker)
    sid = await _subject(maker, owner_ctx, "Dana")
    _, pid, _ = await _link_and_session(maker, owner_ctx, sid)

    # Plant OWNER rows so the audit proves isolation, not emptiness: an owner-only
    # agent_session AND a real domain note in the SAME domain the link is attributed to
    # (general) — the latter proves empty read scope holds across the domain firewall, not
    # just for is_owner() tables.
    async with scoped_session(maker, owner_ctx) as session:
        await session.execute(
            text(
                "INSERT INTO app.agent_sessions (id, principal_id, domain_scopes)"
                " VALUES (:i, :p, '{}')"
            ),
            {"i": str(uuid.uuid4()), "p": owner_pid},
        )
        await session.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (:i, :c, 'general', 'a private owner note')"
            ),
            {"i": str(uuid.uuid4()), "c": "audit-" + uuid.uuid4().hex},
        )
        # Every table whose READ (the SELECT/ALL USING clause) keys on is_owner() — not
        # is_full_owner (a different function), and not a is_owner()-on-WRITE table that
        # is intentionally world-readable (e.g. the actions catalog, USING(true)).
        # Self-referential identity tables are audited separately below.
        owner_tables = [
            r[0]
            for r in (
                await session.execute(
                    text(
                        "SELECT DISTINCT tablename FROM pg_policies"
                        " WHERE schemaname = 'app'"
                        "   AND cmd IN ('SELECT', 'ALL')"
                        "   AND coalesce(qual, '') LIKE '%is_owner%'"
                        "   AND tablename NOT IN ('principals', 'device_sessions')"
                    )
                )
            ).all()
        ]
    assert "agent_sessions" in owner_tables  # the audit set is non-empty and on target

    async with scoped_session(maker, intake_context(pid)) as session:
        for table in owner_tables:
            count = (
                await session.execute(text(f"SELECT count(*) FROM app.{table}"))  # noqa: S608
            ).scalar()
            assert count == 0, f"intake principal saw {count} rows of owner-only app.{table}"
        # Empty read scope (#8): zero domain content (a planted general-domain owner
        # note) even though the link is attributed to the general domain.
        assert (await session.execute(text("SELECT count(*) FROM app.notes"))).scalar() == 0
        # It sees its OWN principal row (every principal does) but NOT the owner's.
        assert (
            await session.execute(text("SELECT count(*) FROM app.principals WHERE kind = 'owner'"))
        ).scalar() == 0
        assert (
            await session.execute(
                text("SELECT count(*) FROM app.principals WHERE id = :p"), {"p": pid}
            )
        ).scalar() == 1

    # Write-denial too, not just read-denial: the intake principal's INSERT is rejected by
    # the WITH CHECK of owner-only tables (a valid-but-unauthorized row, so the failure is
    # the RLS policy, not a NOT NULL/FK). agent_sessions is the §5-named table; tasks is a
    # second, independent is_owner() table proving the denial isn't agent_sessions-specific.
    for stmt, params in (
        (
            "INSERT INTO app.agent_sessions (id, principal_id, domain_scopes)"
            " VALUES (:i, :p, '{}')",
            {"i": str(uuid.uuid4()), "p": pid},
        ),
        (
            "INSERT INTO app.tasks (principal_id, prompt) VALUES (:p, 'x')",
            {"p": pid},
        ),
    ):
        with pytest.raises(ProgrammingError):
            async with scoped_session(maker, intake_context(pid)) as session:
                await session.execute(text(stmt), params)


async def test_recipients_isolated_by_principal_pin(maker: async_sessionmaker) -> None:
    owner_pid, owner_ctx = await _owner(maker)
    sid = await _subject(maker, owner_ctx, "Dana")
    link_id, pid1, sess1 = await _link_and_session(maker, owner_ctx, sid)
    # A distinct session (distinct principal) — two strangers, isolated from each other.
    _, pid2, sess2 = await _link_and_session(maker, owner_ctx, sid)

    # Recipient 1 sees only its own session row; recipient 2's is invisible to it.
    async with scoped_session(maker, intake_context(pid1)) as session:
        ids = [
            str(r[0])
            for r in (await session.execute(text("SELECT id FROM app.intake_sessions"))).all()
        ]
    assert ids == [sess1]
    async with scoped_session(maker, intake_context(pid2)) as session:
        ids = [
            str(r[0])
            for r in (await session.execute(text("SELECT id FROM app.intake_sessions"))).all()
        ]
    assert ids == [sess2]

    # Neither recipient can read the link itself (no is_full_owner, no auth context).
    async with scoped_session(maker, intake_context(pid1)) as session:
        assert (await session.execute(text("SELECT count(*) FROM app.intake_links"))).scalar() == 0


async def test_submission_pin_and_capture_write(maker: async_sessionmaker) -> None:
    owner_pid, owner_ctx = await _owner(maker)
    sid = await _subject(maker, owner_ctx, "Dana")
    link_id, pid1, sess1 = await _link_and_session(maker, owner_ctx, sid)
    _, pid2, _ = await _link_and_session(maker, owner_ctx, sid)

    # Recipient 1 captures its own submission (capture-only write, pinned to itself).
    sub_id = str(uuid.uuid4())
    async with scoped_session(maker, intake_context(pid1)) as session:
        await session.execute(
            text(
                "INSERT INTO app.intake_submissions"
                " (id, link_id, session_id, principal_id, draft)"
                " VALUES (:i, :l, :s, :p, '{}')"
            ),
            {"i": sub_id, "l": link_id, "s": sess1, "p": pid1},
        )

    # Recipient 2 cannot see it; the owner can.
    async with scoped_session(maker, intake_context(pid2)) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM app.intake_submissions"))
        ).scalar() == 0
    async with scoped_session(maker, owner_ctx) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM app.intake_submissions"))
        ).scalar() == 1

    # Recipient 2 cannot forge a submission carrying recipient 1's principal (WITH CHECK).
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, intake_context(pid2)) as session:
            await session.execute(
                text(
                    "INSERT INTO app.intake_submissions"
                    " (id, link_id, session_id, principal_id, draft)"
                    " VALUES (:i, :l, :s, :p, '{}')"
                ),
                {"i": str(uuid.uuid4()), "l": link_id, "s": sess1, "p": pid1},
            )


async def test_full_owner_sees_links_but_narrowed_owner_does_not(
    maker: async_sessionmaker,
) -> None:
    """Proves the intake tables use is_full_owner(), never the is_owner() shortcut: a
    domain-narrowed owner agent session (owner_scoped) is denied, closing §5's trap."""
    owner_pid, owner_ctx = await _owner(maker)
    sid = await _subject(maker, owner_ctx, "Dana")
    link_id, _, _ = await _link_and_session(maker, owner_ctx, sid)

    # Scope to THIS link's id — the module-scoped database accumulates rows across tests.
    seen = "SELECT count(*) FROM app.intake_links WHERE id = :i"
    async with scoped_session(maker, owner_ctx) as session:
        assert (await session.execute(text(seen), {"i": link_id})).scalar() == 1

    # A narrowed owner (read_context: owner_scoped=True) fails is_full_owner() → denied.
    narrowed = read_context(owner_pid, ("general",))
    async with scoped_session(maker, narrowed) as session:
        assert (await session.execute(text(seen), {"i": link_id})).scalar() == 0
