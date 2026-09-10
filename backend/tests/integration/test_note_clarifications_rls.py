"""Migration 0193 against real Postgres: RLS isolation for `app.note_clarifications`
(CLAUDE.md rule 3 — every new table gets one).

The posture is the NOTE's, not the owner-only posture of `graph_rebuild_runs` or
`archivist_memory`: a clarification is the owner's own words about a note in a domain,
and the same sentence is already firewalled by domain in `app.chunks`. So a narrowed
owner — which is exactly what a note conversation runs as (constraint 2) — sees only
the clarifications of notes in its own scopes, and a capability token scoped to a
domain sees that domain's, consistently with the note and the chunks it can already
read.

The write side proves the D6 freeze where it actually holds: 0193 grants only
`UPDATE (domain_code)`, so nothing — not even a full-owner session — can rewrite what
the owner said.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, UNSCOPED, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

HEALTH_TOKEN = SessionContext(principal_kind="capability_token", domain_scopes=("health",))
GENERAL_TOKEN = SessionContext(principal_kind="capability_token", domain_scopes=("general",))
# A narrowed owner session (0015): owner identity, firewalled to its domain scopes —
# the shape a note conversation runs as.
OWNER_HEALTH = SessionContext(
    principal_id=str(uuid.uuid4()),
    principal_kind="owner",
    domain_scopes=("health",),
    owner_scoped=True,
)

COUNT = "SELECT count(*) FROM app.note_clarifications WHERE id = CAST(:id AS uuid)"


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed(maker: async_sessionmaker, domain: str) -> tuple[str, str]:
    """A note in `domain` plus one clarification on it; returns (note_id, block_id)."""
    note_id, block_id = str(uuid.uuid4()), str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.notes (id, client_id, domain_code, body)"
                " VALUES (CAST(:id AS uuid), :cid, :d, 'Ran 10k.')"
            ),
            {"id": note_id, "cid": f"clar-rls-{note_id}", "d": domain},
        )
        await s.execute(
            text(
                "INSERT INTO app.note_clarifications"
                " (id, note_id, question, answer, domain_code)"
                " VALUES (CAST(:id AS uuid), CAST(:n AS uuid), 'Which 10k?',"
                " 'The canal loop.', :d)"
            ),
            {"id": block_id, "n": note_id, "d": domain},
        )
    return note_id, block_id


async def visible(maker: async_sessionmaker, ctx: SessionContext, block_id: str) -> int:
    async with scoped_session(maker, ctx) as s:
        return int((await s.execute(text(COUNT), {"id": block_id})).scalar_one())


async def count_for_note(maker: async_sessionmaker, note_id: str) -> int:
    async with scoped_session(maker, OWNER) as s:
        return int(
            (
                await s.execute(
                    text(
                        "SELECT count(*) FROM app.note_clarifications"
                        " WHERE note_id = CAST(:n AS uuid)"
                    ),
                    {"n": note_id},
                )
            ).scalar_one()
        )


async def test_a_block_is_firewalled_by_its_note_s_domain(maker: async_sessionmaker) -> None:
    _, block_id = await seed(maker, "health")
    assert await visible(maker, OWNER, block_id) == 1
    assert await visible(maker, OWNER_HEALTH, block_id) == 1
    assert await visible(maker, HEALTH_TOKEN, block_id) == 1
    # The firewall: a general-scoped reader must not see a health note's answer, the
    # same way it cannot see that note or its chunks.
    assert await visible(maker, GENERAL_TOKEN, block_id) == 0
    assert await visible(maker, UNSCOPED, block_id) == 0


async def test_a_narrowed_owner_cannot_read_across_the_firewall(
    maker: async_sessionmaker,
) -> None:
    """The note conversation is an owner-scoped session (constraint 2). Owner identity
    is not a domain pass: a finance note's clarification stays out of a health scope."""
    _, block_id = await seed(maker, "finance")
    assert await visible(maker, OWNER, block_id) == 1
    assert await visible(maker, OWNER_HEALTH, block_id) == 0


async def test_a_scoped_writer_cannot_append_outside_its_scope(
    maker: async_sessionmaker,
) -> None:
    note_id, _ = await seed(maker, "health")
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, GENERAL_TOKEN) as s:
            await s.execute(
                text(
                    "INSERT INTO app.note_clarifications (note_id, question, answer,"
                    " domain_code) VALUES (CAST(:n AS uuid), 'q', 'a', 'health')"
                ),
                {"n": note_id},
            )


async def test_an_unscoped_session_sees_and_writes_nothing(maker: async_sessionmaker) -> None:
    note_id, block_id = await seed(maker, "general")
    assert await visible(maker, UNSCOPED, block_id) == 0
    with pytest.raises(ProgrammingError):
        async with scoped_session(maker, UNSCOPED) as s:
            await s.execute(
                text(
                    "INSERT INTO app.note_clarifications (note_id, question, answer,"
                    " domain_code) VALUES (CAST(:n AS uuid), 'q', 'a', 'general')"
                ),
                {"n": note_id},
            )


async def test_the_owner_s_words_cannot_be_rewritten(maker: async_sessionmaker) -> None:
    """D6's freeze, at the only level that holds it: 0193 grants `UPDATE (domain_code)`
    and nothing else, so a clarification is an immutable record of what was said even
    to a full-owner session."""
    _, block_id = await seed(maker, "general")
    for column in ("question", "answer"):
        with pytest.raises(ProgrammingError):
            async with scoped_session(maker, OWNER) as s:
                await s.execute(
                    text(
                        f"UPDATE app.note_clarifications SET {column} = 'rewritten'"
                        " WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": block_id},
                )


async def test_the_domain_carry_is_the_one_permitted_update(maker: async_sessionmaker) -> None:
    """`update_note` moves a note's domain and must carry its clarifications along
    (the 0002 attachment invariant) or they fall out of the note's own scope. The block
    follows the NOTE: moving it alone is what the domain-match trigger forbids."""
    note_id, block_id = await seed(maker, "general")
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.notes SET domain_code = 'health' WHERE id = CAST(:n AS uuid)"),
            {"n": note_id},
        )
        await s.execute(
            text(
                "UPDATE app.note_clarifications SET domain_code = 'health'"
                " WHERE id = CAST(:id AS uuid)"
            ),
            {"id": block_id},
        )
    assert await visible(maker, HEALTH_TOKEN, block_id) == 1
    assert await visible(maker, GENERAL_TOKEN, block_id) == 0


async def test_a_block_cannot_be_stamped_with_a_domain_its_note_does_not_have(
    maker: async_sessionmaker,
) -> None:
    """The gap the policy alone leaves. `USING`/`WITH CHECK` validate only the domain the
    WRITER supplies, and the FK to `app.notes` bypasses RLS as FK checks always do — so a
    general-scoped capability token could stamp `general` on a clarification of a HEALTH
    note, and the owner would then read that stranger's text inside the health note's
    body, where D7 turns it into a health chunk and health facts.

    0045 enforces the same subsection/parent domain match in Postgres, "not app code
    (non-negotiable #3)", for exactly this reason. The app path is safe on its own
    (`append_clarification` reads the note under RLS first); this is the backstop."""
    note_id, _ = await seed(maker, "health")
    with pytest.raises(DBAPIError, match="must equal its note"):
        async with scoped_session(maker, GENERAL_TOKEN) as s:
            await s.execute(
                text(
                    "INSERT INTO app.note_clarifications (note_id, question, answer,"
                    " domain_code) VALUES (CAST(:n AS uuid), 'q', 'a', 'general')"
                ),
                {"n": note_id},
            )
    assert await count_for_note(maker, note_id) == 1  # only the seeded one


async def test_the_domain_carry_cannot_walk_a_block_away_from_its_note(
    maker: async_sessionmaker,
) -> None:
    """The one granted UPDATE is the carry, and the trigger is what makes it a carry
    rather than a free move: the block's new domain has to be the note's."""
    _, block_id = await seed(maker, "general")
    with pytest.raises(DBAPIError, match="must equal its note"):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "UPDATE app.note_clarifications SET domain_code = 'health'"
                    " WHERE id = CAST(:id AS uuid)"
                ),
                {"id": block_id},
            )


async def test_a_blank_answer_is_refused(maker: async_sessionmaker) -> None:
    """An empty block would render as a timestamped 'A:' with nothing after it."""
    note_id, _ = await seed(maker, "general")
    with pytest.raises(IntegrityError):
        async with scoped_session(maker, OWNER) as s:
            await s.execute(
                text(
                    "INSERT INTO app.note_clarifications (note_id, question, answer,"
                    " domain_code) VALUES (CAST(:n AS uuid), 'q', '   ', 'general')"
                ),
                {"n": note_id},
            )
