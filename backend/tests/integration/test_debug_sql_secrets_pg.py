"""The debug SQL console can't read a credential out of app.settings by any spelling
(docs/archive/SECURITY_REVIEW_FIXES_PLAN.md S3.3, F12; migration 0242).

The old redaction keyed on column NAMES, so an alias or an expression over `value` read the
secret straight out. 0242 hides those rows from read-only transactions in Postgres itself;
these tests drive the real route against real RLS with the spellings that used to work, and
walk `SECRET_SETTING_KEYS` so a credential added in Python but not in the policy fails here.
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.api import debug
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext, scoped_session
from jbrain.settings_store import SECRET_SETTING_KEYS, SqlSettingsStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

_DEBUG_OWNER = SessionContext(principal_id="debug-console", principal_kind="owner")
_PRINCIPAL = PrincipalInfo(id="x", kind="capability_token", label="x")
_MARK = "zz-plaintext-credential-zz"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    store = SqlSettingsStore(m)
    for key in SECRET_SETTING_KEYS:
        await store.upsert(OWNER, key, f"{_MARK}-{key}")
    await store.upsert(OWNER, "moltbook_handle", "DaveFromSpace")
    yield m
    await engine.dispose()


async def _sql(maker: async_sessionmaker, sql: str) -> debug.SqlOut:
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_maker=maker)),
        state=SimpleNamespace(),
    )
    return await debug.run_sql(debug.SqlRequest(sql=sql), request, _PRINCIPAL)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM app.settings",
        "SELECT value AS v FROM app.settings WHERE key = 'tavily_api_key'",
        "SELECT substr(value::text, 1, 8) AS head, key AS k FROM app.settings",
        "SELECT s.value #>> '{}' AS x FROM app.settings s",
        "SELECT (SELECT string_agg(value::text, ',') FROM app.settings) AS everything",
        "WITH t AS (SELECT * FROM app.settings) SELECT key, value FROM t",
        "TABLE app.settings",
        "SELECT jsonb_agg(s) FROM app.settings s",
    ],
)
async def test_no_spelling_reads_a_settings_credential(maker: async_sessionmaker, sql: str) -> None:
    out = await _sql(maker, sql)
    assert _MARK not in repr(out.rows)


async def test_every_secret_key_is_hidden_and_ordinary_settings_are_not(
    maker: async_sessionmaker,
) -> None:
    out = await _sql(maker, "SELECT key, value FROM app.settings")
    keys = {row[0] for row in out.rows}
    assert not keys & SECRET_SETTING_KEYS, "a credential key the policy doesn't know"
    assert ["moltbook_handle", "DaveFromSpace"] in out.rows


async def test_the_app_still_reads_its_credentials(maker: async_sessionmaker) -> None:
    # The policy only bites read-only transactions; the app's own reads are ordinary ones.
    store = SqlSettingsStore(maker)
    for key in SECRET_SETTING_KEYS:
        assert await store.get(OWNER, key) == f"{_MARK}-{key}"


async def test_a_read_only_transaction_cannot_be_reopened(maker: async_sessionmaker) -> None:
    # The policy leans on this: once a query has run, Postgres won't go back to read-write,
    # so a statement that reached set_config some other way still couldn't lift the policy.
    with pytest.raises(DBAPIError):
        async with scoped_session(maker, _DEBUG_OWNER) as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            await session.execute(text("SELECT 1"))
            await session.execute(text("SELECT set_config('transaction_read_only', 'off', true)"))


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT query_to_xml('SELECT value FROM app.settings', true, false, '')",
        "SELECT set_config('app.principal_kind', 'owner', true)",
        "SELECT pg_read_file('/etc/passwd')",
    ],
)
async def test_refused_functions_never_reach_postgres(maker: async_sessionmaker, sql: str) -> None:
    with pytest.raises(HTTPException) as exc:
        await _sql(maker, sql)
    assert exc.value.status_code == 400 and "not allowed" in str(exc.value.detail)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT code, name FROM app.domains ORDER BY code",
        "SELECT d.code, count(s.key) FROM app.domains d"
        " LEFT JOIN app.settings s ON s.key = d.code GROUP BY d.code",
        "EXPLAIN SELECT * FROM app.domains",
        "SHOW search_path",
    ],
)
async def test_ordinary_diagnostic_reads_still_work(maker: async_sessionmaker, sql: str) -> None:
    out = await _sql(maker, sql)
    assert out.columns and out.row_count >= 1
