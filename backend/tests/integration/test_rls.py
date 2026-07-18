"""Domain-firewall and auth-table RLS proofs against real Postgres.

These tests are the enforcement evidence for CLAUDE.md rule 3: they connect
as the unprivileged app role and demonstrate that scope GUCs — not
application politeness — decide row visibility.
"""

import argparse
import json
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import asdict, dataclass

import pytest
import sqlalchemy
from alembic import command
from alembic.config import Config
from filelock import FileLock
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
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
# Migrate once into this template; every module then clones a fresh database
# from it (a near-instant file copy) instead of replaying the full 44-migration
# chain — that per-module replay was the bulk of the old backend CI time.
TEMPLATE_DB = "jbrain_template"


@dataclass(frozen=True)
class _Cluster:
    """Coordinates of the shared Postgres, serialisable as JSON so xdist worker
    processes can share one container (the live object cannot cross processes)."""

    host: str
    port: int
    admin_db: str
    user: str
    password: str
    container_id: str


# Holds the provisioning worker's container object for the session so it is not
# garbage-collected; teardown happens by id via the docker CLI, from whichever
# worker is last out — so the object itself is never needed again.
_PROVISIONED: list = []


def _admin_url(c: _Cluster, dbname: str, *, driver: str) -> str:
    return f"postgresql+{driver}://{c.user}:{c.password}@{c.host}:{c.port}/{dbname}"


def _drop_connections(conn: sqlalchemy.Connection, dbname: str) -> None:
    """A database cannot be cloned-from or dropped while sessions are attached."""
    conn.execute(
        text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = :db AND pid <> pg_backend_pid()"
        ),
        {"db": dbname},
    )


def _clone_template(conn: sqlalchemy.Connection, dbname: str) -> None:
    """Clone the migrated template, retrying the "template in use" race.

    The template carries the `timescaledb` extension, whose background-worker
    scheduler reconnects to the database moments after we terminate it — so a
    `CREATE DATABASE ... TEMPLATE` can lose the race with `ObjectInUse`. Terminate
    the template's connections and retry; a couple of attempts reliably wins.
    """
    last: Exception | None = None
    for _ in range(12):
        _drop_connections(conn, TEMPLATE_DB)
        try:
            conn.execute(text(f'CREATE DATABASE "{dbname}" TEMPLATE "{TEMPLATE_DB}"'))
            return
        except OperationalError as exc:  # psycopg ObjectInUse
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"could not clone {TEMPLATE_DB} after retries") from last


def _provision() -> _Cluster:
    """Start the one container, create the app role, and migrate the template."""
    pg = pgvector_container()
    pg.start()
    _PROVISIONED.append(pg)

    container_id = pg.get_wrapped_container().id
    assert container_id is not None  # always set once the container is running

    cluster = _Cluster(
        host=pg.get_container_host_ip(),
        port=int(pg.get_exposed_port(5432)),
        admin_db=pg.dbname,
        user=pg.username,
        password=pg.password,
        container_id=container_id,
    )

    admin = sqlalchemy.create_engine(
        _admin_url(cluster, cluster.admin_db, driver="psycopg"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as conn:
        # Stop TimescaleDB's background-worker scheduler from attaching to the
        # template (and the clones): it would otherwise hold connections that
        # race `CREATE DATABASE ... TEMPLATE`. Tests need no background jobs.
        conn.execute(text("ALTER SYSTEM SET timescaledb.max_background_workers = 0"))
        conn.execute(text("SELECT pg_reload_conf()"))
        conn.execute(text(f"CREATE ROLE jbrain_app LOGIN PASSWORD '{APP_PASSWORD}'"))
        conn.execute(text(f'CREATE DATABASE "{TEMPLATE_DB}"'))
    admin.dispose()

    cfg = Config("alembic.ini")
    cfg.cmd_opts = argparse.Namespace(
        x=[f"database_url={_admin_url(cluster, TEMPLATE_DB, driver='asyncpg')}"]
    )
    command.upgrade(cfg, "head")

    # Guarantee the template is connection-free before any module clones it.
    admin = sqlalchemy.create_engine(
        _admin_url(cluster, cluster.admin_db, driver="psycopg"), isolation_level="AUTOCOMMIT"
    )
    with admin.connect() as conn:
        _drop_connections(conn, TEMPLATE_DB)
    admin.dispose()
    return cluster


def _remove_container(container_id: str) -> None:
    subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, check=False)


@pytest.fixture(scope="session")
def _pg_cluster(tmp_path_factory: pytest.TempPathFactory, worker_id: str) -> Iterator[_Cluster]:
    """The single Postgres container, shared across every xdist worker.

    Off xdist (worker_id == "master") it is provisioned and removed directly.
    Under xdist the first worker to win the lock provisions it and publishes its
    coordinates to the shared temp root; the rest reuse them. A refcount in that
    file lets the last worker out remove the container — by id, since the live
    container object exists only in the provisioning worker's process.
    """
    if worker_id == "master":
        cluster = _provision()
        try:
            yield cluster
        finally:
            _remove_container(cluster.container_id)
        return

    state = tmp_path_factory.getbasetemp().parent / "pg_cluster.json"
    with FileLock(f"{state}.lock"):
        if state.exists():
            data = json.loads(state.read_text())
            data["refs"] += 1
            state.write_text(json.dumps(data))
            cluster = _Cluster(**{k: v for k, v in data.items() if k != "refs"})
        else:
            cluster = _provision()
            state.write_text(json.dumps({**asdict(cluster), "refs": 1}))
    try:
        yield cluster
    finally:
        with FileLock(f"{state}.lock"):
            data = json.loads(state.read_text())
            data["refs"] -= 1
            last = data["refs"] <= 0
            state.write_text(json.dumps(data))
        if last:
            _remove_container(cluster.container_id)


@pytest.fixture(scope="module")
def database_url(_pg_cluster: _Cluster) -> Iterator[str]:
    """A pristine database for the module, cloned from the migrated template."""
    dbname = f"t_{uuid.uuid4().hex}"
    admin = sqlalchemy.create_engine(
        _admin_url(_pg_cluster, _pg_cluster.admin_db, driver="psycopg"),
        isolation_level="AUTOCOMMIT",
    )
    with admin.connect() as conn:
        _clone_template(conn, dbname)
    yield (
        f"postgresql+asyncpg://jbrain_app:{APP_PASSWORD}"
        f"@{_pg_cluster.host}:{_pg_cluster.port}/{dbname}"
    )
    with admin.connect() as conn:
        _drop_connections(conn, dbname)
        conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
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
    # `external` (0136) is the corpus-only domain for the ingested-video library — a firewall
    # peer of the four owner-knowledge domains, isolated from them.
    assert codes == {"general", "health", "finance", "location", "external"}


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
