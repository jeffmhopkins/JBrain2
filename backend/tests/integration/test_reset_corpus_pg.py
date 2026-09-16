"""The reset migrations against real Postgres: the wipe takes the corpus, spares the keepers.

Every test here runs against EACH reset migration (`_RESETS`) — 0202, and 0205 repeating it
at the owner's second request. A repeat is a copy of a list of table names, which is the
shape that rots quietly: a table renamed or a constraint added between one reset and the
next breaks the later file and nothing says so.

CI cannot catch a mistake in these migrations. It runs them against an EMPTY schema, where
a wrong statement order never trips a foreign key and a statement that deletes too much
deletes nothing. The only place a reset is ever exercised with rows in the tables is the
owner's box, once, irreversibly — so it is exercised here instead.

What this pins is the reason 0202 uses DELETE where the plan said
`TRUNCATE ... CASCADE`: TRUNCATE is STRUCTURAL, so it would have emptied every table
holding a foreign key into the wipe set regardless of what that key declares. The owner's
shopping list is exactly such a table (`list_items.source_note_id`, ON DELETE SET NULL) —
the column exists so a list outlives the note that spawned it, and a TRUNCATE would have
destroyed five of his list items on a box where the plan promised to keep them.
"""

import importlib.util
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType

import pytest
import sqlalchemy
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.db.session import scoped_session
from jbrain.notes.repo import SqlNotesRepo
from tests.conftest import docker_available
from tests.integration.test_rls import (  # noqa: F401
    OWNER,
    _admin_url,
    _Cluster,
    _pg_cluster,
    database_url,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: EVERY reset migration, so a repeat gets the same proof the first one got rather than
#: riding on the fact that it was copied. The owner has now asked for this twice, and a
#: copy is exactly the shape that rots: `_WIPE` names tables by string, so a table renamed
#: or a constraint added between one reset and the next breaks the later file silently.
_RESETS = ("0202_reset_note_corpus.py", "0205_reset_note_corpus_again.py")


def _load(filename: str) -> ModuleType:
    path = BACKEND_ROOT / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{filename[:4]}_pg", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=_RESETS)
def migration(request: pytest.FixtureRequest) -> ModuleType:
    return _load(request.param)


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def run_upgrade(
    _pg_cluster: _Cluster,  # noqa: F811
    database_url: str,  # noqa: F811
    migration: ModuleType,
) -> Iterator[object]:
    """Invoke `upgrade()` the way alembic does — a real connection bound to the `op`
    proxy — rather than reimplementing its SQL here, which would test the test.

    Connects as the SUPERUSER, like the deploy's `migrate` service: these tables FORCE
    row-level security, so a migration running as `jbrain_app` with no principal set
    would match zero rows and report success having deleted nothing."""
    dbname = database_url.rsplit("/", 1)[-1]
    engine = sqlalchemy.create_engine(_admin_url(_pg_cluster, dbname, driver="psycopg"))

    def _run() -> None:
        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()

    yield _run
    engine.dispose()


async def _principal(maker: async_sessionmaker) -> str:
    """Mint the owner. The template database seeds no principal, and `lists` and
    `agent_sessions` both FK one — so a test that skipped this would be asserting against
    rows it never managed to insert."""
    await service.rotate_owner_key(SqlAuthRepo(maker))
    async with scoped_session(maker, OWNER) as s:
        pid = (
            await s.execute(
                text("SELECT id FROM app.principals WHERE kind = 'owner' AND revoked_at IS NULL")
            )
        ).scalar_one()
    return str(pid)


async def _count(maker: async_sessionmaker, sql: str, **params: object) -> int:
    async with scoped_session(maker, OWNER) as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def test_the_wipe_spares_a_list_item_and_blanks_its_note(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """The defect `TRUNCATE ... CASCADE` would have shipped.

    A list item carrying `source_note_id` is the shape the plan promises to keep and
    TRUNCATE would have taken. It must survive the wipe with the column NULLed — which is
    what its own ON DELETE SET NULL says should happen, and what DELETE honours."""
    principal = await _principal(maker)
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"wipe-{uuid.uuid4()}", domain="general", destination=None, body="milk"
    )
    list_id, item_id = uuid.uuid4(), uuid.uuid4()
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.lists (id, principal_id, domain_code, title)"
                " VALUES (:id, :p, 'general', 'Groceries')"
            ),
            {"id": list_id, "p": principal},
        )
        await s.execute(
            text(
                "INSERT INTO app.list_items (id, list_id, body, source_note_id)"
                " VALUES (:id, :list, 'milk', :note)"
            ),
            {"id": item_id, "list": list_id, "note": note.id},
        )

    run_upgrade()  # type: ignore[operator]

    assert await _count(maker, "SELECT count(*) FROM app.notes WHERE id = :n", n=note.id) == 0
    # The item survives, and so does the list it belongs to.
    assert await _count(maker, "SELECT count(*) FROM app.list_items WHERE id = :i", i=item_id) == 1
    assert await _count(maker, "SELECT count(*) FROM app.lists WHERE id = :i", i=list_id) == 1
    orphaned = await _count(
        maker,
        "SELECT count(*) FROM app.list_items WHERE id = :i AND source_note_id IS NULL",
        i=item_id,
    )
    assert orphaned == 1, "the note link is blanked, not the row deleted"


async def test_the_wipe_takes_a_note_thread_and_leaves_ordinary_chat(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """ "Conversation history" is the owner's CHAT, which stays. A note's ingestion thread
    is not chat — it belongs to the note being deleted, and its `agent_sessions` row can
    only be identified while `note_conversations` still exists to name it. That ordering
    is the one thing in 0202 that cannot be recovered by re-running it."""
    principal = await _principal(maker)
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"wipe-{uuid.uuid4()}", domain="general", destination=None, body="guppy"
    )
    chat_id, thread_id = uuid.uuid4(), uuid.uuid4()
    async with scoped_session(maker, OWNER) as s:
        for sid in (chat_id, thread_id):
            await s.execute(
                text(
                    "INSERT INTO app.agent_sessions (id, principal_id, domain_scopes)"
                    " VALUES (:id, :p, ARRAY['general'])"
                ),
                {"id": sid, "p": principal},
            )
        await s.execute(
            text(
                "INSERT INTO app.note_conversations (session_id, note_id, state, note_body_sha)"
                " VALUES (:s, :n, 'settled', 'sha')"
            ),
            {"s": thread_id, "n": note.id},
        )

    run_upgrade()  # type: ignore[operator]

    assert (
        await _count(maker, "SELECT count(*) FROM app.agent_sessions WHERE id = :i", i=chat_id) == 1
    )
    gone = await _count(maker, "SELECT count(*) FROM app.agent_sessions WHERE id = :i", i=thread_id)
    assert gone == 0, "the note thread's session goes with the note"
    assert await _count(maker, "SELECT count(*) FROM app.note_conversations") == 0


async def test_the_wipe_runs_clean_on_an_already_empty_corpus(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """Idempotent, and safe in the order it is written. Running it twice must not raise:
    the deploy can retry `migrate`, and a statement ordered after its own parent would
    only surface here — CI runs this migration against an empty schema, where no foreign
    key is ever populated enough to complain."""
    run_upgrade()  # type: ignore[operator]
    run_upgrade()  # type: ignore[operator]
    assert await _count(maker, "SELECT count(*) FROM app.notes") == 0
    assert await _count(maker, "SELECT count(*) FROM app.facts") == 0


def test_the_downgrade_refuses_rather_than_lying(migration: ModuleType) -> None:
    """A `downgrade()` that silently did nothing would read, to someone under pressure,
    as a rollback that worked. The rows are gone; the backup is the way back."""
    with pytest.raises(RuntimeError, match="no downgrade"):
        migration.downgrade()


async def test_the_wipe_deletes_a_dated_fact_and_its_temporal_token(
    maker: async_sessionmaker, run_upgrade: object
) -> None:
    """The case that aborted the first draft, and the one this file existed for and did
    not cover.

    `facts.temporal_token_id` is NO ACTION, so the token is the PARENT even though it
    reads like a detail hanging off the fact. Deleting tokens first raises
    `facts_temporal_token_id_fkey` and takes the whole migration — and with it every
    `Ops -> Update` on the release carrying it. `temporal_token_id` is written on the
    normal path for any DATED fact, so this is the common case, not an edge."""
    repo = SqlNotesRepo(maker)
    note, _ = await repo.create_note(
        OWNER, client_id=f"wipe-{uuid.uuid4()}", domain="general", destination=None, body="dated"
    )
    entity_id, token_id, fact_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.entities (id, kind, canonical_name, domain_code)"
                " VALUES (:id, 'Person', 'Sam', 'general')"
            ),
            {"id": entity_id},
        )
        await s.execute(
            text(
                "INSERT INTO app.temporal_tokens (id, note_id, surface_phrase, kind,"
                " resolved_start, temporal_precision, capture_anchor, domain_code)"
                " VALUES (:id, :n, 'last Tuesday', 'point', now(), 'day', now(), 'general')"
            ),
            {"id": token_id, "n": note.id},
        )
        await s.execute(
            text(
                "INSERT INTO app.facts"
                " (id, note_id, entity_id, kind, predicate, statement, assertion,"
                " reported_at, temporal_precision, extractor, prompt_version,"
                " temporal_token_id, domain_code)"
                " VALUES (:id, :n, :e, 'event', 'attended', 'Sam attended.', 'asserted',"
                " now(), 'day', 'test', 'test-v1', :t, 'general')"
            ),
            {"id": fact_id, "n": note.id, "e": entity_id, "t": token_id},
        )

    run_upgrade()  # type: ignore[operator]

    assert await _count(maker, "SELECT count(*) FROM app.facts") == 0
    assert await _count(maker, "SELECT count(*) FROM app.temporal_tokens") == 0
    assert await _count(maker, "SELECT count(*) FROM app.entities") == 0


async def test_the_wipe_order_satisfies_every_blocking_constraint(
    maker: async_sessionmaker, migration: ModuleType
) -> None:
    """Derive the rule from the schema instead of re-reading the list.

    A seeded test only catches the constraint someone thought to seed — which is exactly
    how `facts -> temporal_tokens` survived the first draft. This asks Postgres for every
    NO ACTION / RESTRICT foreign key INSIDE the wipe set (the ones that actually block a
    DELETE; CASCADE and SET NULL resolve themselves) and asserts `_WIPE` is a valid order
    for all of them. It fails on a constraint added years from now by someone who never
    reads this file."""
    wipe = list(migration._WIPE)
    position = {t: i for i, t in enumerate(wipe)}
    async with scoped_session(maker, OWNER) as s:
        pairs = (
            await s.execute(
                text(
                    "SELECT DISTINCT src.relname, tgt.relname"
                    " FROM pg_constraint c"
                    " JOIN pg_class src ON src.oid = c.conrelid"
                    " JOIN pg_class tgt ON tgt.oid = c.confrelid"
                    " WHERE c.contype = 'f' AND c.confdeltype IN ('a', 'r')"
                    "   AND src.relname = ANY(:w) AND tgt.relname = ANY(:w)"
                    "   AND src.relname <> tgt.relname"
                ),
                {"w": wipe},
            )
        ).all()

    assert pairs, "no blocking constraints found — the query or the table names are wrong"
    wrong = [(c, p) for c, p in pairs if position[c] > position[p]]
    assert not wrong, "\n".join(
        f"{child} must be deleted BEFORE {parent} (its FK is NO ACTION), but _WIPE has"
        f" {parent} at {position[parent]} and {child} at {position[child]}"
        for child, parent in wrong
    )


async def test_no_kept_table_can_block_the_wipe(
    maker: async_sessionmaker, migration: ModuleType
) -> None:
    """The other half of the ordering rule, and the one nothing checked.

    Ordering inside the set only matters if the set can be emptied at all. A table the
    wipe KEEPS that holds a NO ACTION / RESTRICT key into it cannot be ordered around —
    its row blocks the parent's delete outright, aborts the migration, and takes every
    `Ops -> Update` on the release with it. Today all three keepers that reach in declare
    a resolution (`list_items.source_note_id` SET NULL, `agent_episode_refs` and
    `place_share` CASCADE), which is why 0202 ran. Nothing made that true on purpose, and
    a new feature that FKs `notes` and forgets `ondelete` would make it false silently —
    on the box, never in CI, where these tables are empty and no key is ever tested."""
    wipe = list(migration._WIPE)
    async with scoped_session(maker, OWNER) as s:
        blockers = (
            await s.execute(
                text(
                    "SELECT DISTINCT src.relname, att.attname, tgt.relname"
                    " FROM pg_constraint c"
                    " JOIN pg_class src ON src.oid = c.conrelid"
                    " JOIN pg_class tgt ON tgt.oid = c.confrelid"
                    " JOIN pg_namespace n ON n.oid = src.relnamespace"
                    " JOIN unnest(c.conkey) AS k(attnum) ON true"
                    " JOIN pg_attribute att ON att.attrelid = src.oid AND att.attnum = k.attnum"
                    " WHERE c.contype = 'f' AND c.confdeltype IN ('a', 'r')"
                    "   AND n.nspname = 'app'"
                    "   AND tgt.relname = ANY(:w) AND src.relname <> ALL(:w)"
                ),
                {"w": wipe},
            )
        ).all()

    assert not blockers, "\n".join(
        f"app.{child}.{col} is a NO ACTION / RESTRICT key into app.{parent}, which the"
        f" wipe deletes — so a single {child} row aborts the whole migration. Either give"
        f" that column an ON DELETE action, or add {child} to _WIPE."
        for child, col, parent in blockers
    )


def test_the_repeat_wipes_exactly_what_the_first_one_did() -> None:
    """0205 is a deliberate copy of 0202, and both are frozen. This can only fail if
    someone edits a shipped migration — which is the bug, not the signal. It exists so
    that the copy is checkable rather than merely asserted in a docstring."""
    first, repeat = (_load(name) for name in _RESETS)
    assert repeat._WIPE == first._WIPE
