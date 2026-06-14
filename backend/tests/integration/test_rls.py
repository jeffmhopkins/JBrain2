"""Domain-firewall and auth-table RLS proofs against real Postgres.

These tests are the enforcement evidence for CLAUDE.md rule 3: they connect
as the unprivileged app role and demonstrate that scope GUCs — not
application politeness — decide row visibility.
"""

import argparse
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available, pgvector_container

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

APP_PASSWORD = "app_test_pw"


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    with pgvector_container() as pg:
        admin_url = pg.get_connection_url(driver="psycopg")
        import sqlalchemy

        admin = sqlalchemy.create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f"CREATE ROLE jbrain_app LOGIN PASSWORD '{APP_PASSWORD}'"))
        async_url = pg.get_connection_url(driver="asyncpg")
        cfg = Config("alembic.ini")
        cfg.cmd_opts = argparse.Namespace(x=[f"database_url={async_url}"])
        command.upgrade(cfg, "head")

        host, port = pg.get_container_host_ip(), pg.get_exposed_port(5432)
        yield f"postgresql+asyncpg://jbrain_app:{APP_PASSWORD}@{host}:{port}/{pg.dbname}"
        admin.dispose()


@pytest.fixture
async def app_engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    # Per-test engine with NullPool: asyncpg connections are bound to the
    # event loop, and pytest-asyncio gives each test its own loop.
    engine = create_async_engine(database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


def maker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


UNSCOPED = SessionContext()
OWNER = SessionContext(principal_id=str(uuid.uuid4()), principal_kind="owner")


async def test_full_auth_flow_through_real_rls(app_engine: AsyncEngine) -> None:
    repo = SqlAuthRepo(maker(app_engine))
    key = await service.rotate_owner_key(repo)

    token = await service.login(repo, key, "integration")
    principal = await service.authenticate(repo, token)
    assert principal is not None and principal.kind == "owner"

    with pytest.raises(service.InvalidCredentials):
        await service.login(repo, "jb1-WRONG", "integration")

    await service.logout(repo, token)
    assert await service.authenticate(repo, token) is None


async def test_unscoped_session_cannot_read_credentials(app_engine: AsyncEngine) -> None:
    repo = SqlAuthRepo(maker(app_engine))
    await service.rotate_owner_key(repo)

    async with scoped_session(maker(app_engine), UNSCOPED) as session:
        principals = (await session.execute(text("SELECT count(*) FROM app.principals"))).scalar()
        sessions = (
            await session.execute(text("SELECT count(*) FROM app.device_sessions"))
        ).scalar()
    assert principals == 0
    assert sessions == 0


async def test_unscoped_session_cannot_insert_principal(app_engine: AsyncEngine) -> None:
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError):
        async with scoped_session(maker(app_engine), UNSCOPED) as session:
            await session.execute(
                text(
                    "INSERT INTO app.principals (id, kind, key_hash)"
                    " VALUES (gen_random_uuid(), 'owner', 'forged')"
                )
            )


async def test_owner_sees_all_subjects_others_only_their_own(app_engine: AsyncEngine) -> None:
    dad, mom = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker(app_engine), OWNER) as session:
        for sid, name in ((dad, "Dad"), (mom, "Mom")):
            await session.execute(
                text(
                    "INSERT INTO app.subjects (id, display_name, kind)"
                    " VALUES (:id, :name, 'person')"
                ),
                {"id": sid, "name": name},
            )

    async with scoped_session(maker(app_engine), OWNER) as session:
        assert (await session.execute(text("SELECT count(*) FROM app.subjects"))).scalar() == 2

    dads_token = SessionContext(principal_kind="capability_token", subject_id=dad)
    async with scoped_session(maker(app_engine), dads_token) as session:
        names = (await session.execute(text("SELECT display_name FROM app.subjects"))).scalars()
        assert list(names) == ["Dad"]

    async with scoped_session(maker(app_engine), UNSCOPED) as session:
        assert (await session.execute(text("SELECT count(*) FROM app.subjects"))).scalar() == 0


async def test_domain_scope_firewall_pattern(app_engine: AsyncEngine, database_url: str) -> None:
    """The pattern every future domain-scoped table follows, proven end to end."""
    admin_url = database_url.replace(f"jbrain_app:{APP_PASSWORD}", "test:test")
    admin = create_async_engine(admin_url)
    async with admin.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE app.firewall_probe (
                    id serial PRIMARY KEY,
                    domain_code text NOT NULL,
                    body text NOT NULL
                )
                """
            )
        )
        await conn.execute(text("ALTER TABLE app.firewall_probe ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("ALTER TABLE app.firewall_probe FORCE ROW LEVEL SECURITY"))
        await conn.execute(
            text(
                "CREATE POLICY probe_access ON app.firewall_probe"
                " USING (app.has_domain_scope(domain_code))"
                " WITH CHECK (app.has_domain_scope(domain_code))"
            )
        )
        await conn.execute(text("GRANT SELECT, INSERT ON app.firewall_probe TO jbrain_app"))
        await conn.execute(text("GRANT USAGE ON SEQUENCE app.firewall_probe_id_seq TO jbrain_app"))
        await conn.execute(
            text(
                "INSERT INTO app.firewall_probe (domain_code, body) VALUES"
                " ('general', 'grocery thoughts'),"
                " ('health', 'blood pressure reading'),"
                " ('finance', 'account number')"
            )
        )
    await admin.dispose()

    general_only = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker(app_engine), general_only) as session:
        rows = list((await session.execute(text("SELECT body FROM app.firewall_probe"))).scalars())
        assert rows == ["grocery thoughts"]

    health_and_general = SessionContext(
        principal_kind="capability_token", domain_scopes=("general", "health")
    )
    async with scoped_session(maker(app_engine), health_and_general) as session:
        count = (await session.execute(text("SELECT count(*) FROM app.firewall_probe"))).scalar()
        assert count == 2

    async with scoped_session(maker(app_engine), UNSCOPED) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM app.firewall_probe"))
        ).scalar() == 0

    async with scoped_session(maker(app_engine), OWNER) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM app.firewall_probe"))
        ).scalar() == 3

    # Scoped writers cannot smuggle rows into other domains either.
    from sqlalchemy.exc import ProgrammingError

    with pytest.raises(ProgrammingError):
        async with scoped_session(maker(app_engine), general_only) as session:
            await session.execute(
                text(
                    "INSERT INTO app.firewall_probe (domain_code, body)"
                    " VALUES ('finance', 'sneaky')"
                )
            )


async def test_domains_are_readable_reference_data(app_engine: AsyncEngine) -> None:
    async with scoped_session(maker(app_engine), UNSCOPED) as session:
        codes = set((await session.execute(text("SELECT code FROM app.domains"))).scalars())
    assert codes == {"general", "health", "finance", "location"}


_CP_INSERT = (
    "INSERT INTO app.canonical_predicates (canonical_name, descriptor, value_shape, kind)"
    " VALUES (:name, 'd', 'scalar', 'attribute')"
)


async def test_canonical_predicates_are_global_reference_data(app_engine: AsyncEngine) -> None:
    # Owner seeds a predicate; a domain-scoped reader still sees it — the index is
    # global vocabulary (the app.domains pattern), not domain-partitioned.
    async with scoped_session(maker(app_engine), OWNER) as session:
        await session.execute(text(_CP_INSERT), {"name": "x.global"})
    scoped = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    async with scoped_session(maker(app_engine), scoped) as session:
        names = set(
            (
                await session.execute(text("SELECT canonical_name FROM app.canonical_predicates"))
            ).scalars()
        )
    assert "x.global" in names


async def test_canonical_predicates_writes_are_owner_only(app_engine: AsyncEngine) -> None:
    from sqlalchemy.exc import ProgrammingError

    scoped = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
    # A non-owner (capability token) cannot mint a predicate — the is_owner() gate.
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker(app_engine), scoped) as session:
            await session.execute(text(_CP_INSERT), {"name": "x.denied"})
    # The owner can — proving it's an owner gate, not a blanket write denial.
    async with scoped_session(maker(app_engine), OWNER) as session:
        await session.execute(text(_CP_INSERT), {"name": "x.allowed"})
    async with scoped_session(maker(app_engine), OWNER) as session:
        got = (
            await session.execute(
                text(
                    "SELECT count(*) FROM app.canonical_predicates"
                    " WHERE canonical_name = 'x.allowed'"
                )
            )
        ).scalar()
    assert got == 1


def test_cli_init_prints_owner_key_once(
    database_url: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from jbrain import cli

    monkeypatch.setenv("JBRAIN_DATABASE_URL", database_url)
    assert cli.main(["init"]) == 0
    out = capsys.readouterr().out
    assert "OWNER KEY" in out
    assert "jb1-" in out

    # reset rotates: a fresh key block prints and differs from the first.
    assert cli.main(["reset-owner-key"]) == 0
    second = capsys.readouterr().out
    assert "jb1-" in second
    assert second != out
