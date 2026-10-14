"""The ground players crossed, for a map tile, against real Postgres (plan §M8).

Bedrock never saves a chunk nothing changed in, so the map learns about ground a player
only crossed from the travel log's fog of war. `visited.tile_params` reads the fog inside
one tile, from every player, and packs it as the bitmap the sidecar draws from; a map
share reads it under its own RLS, which pins it to the link's world.
"""

from __future__ import annotations

import base64
import zlib
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import mc_share_context, scoped_session
from jbrain.minecraft import visited
from tests.conftest import docker_available
from tests.integration.test_mc_share_pg import _mint, _owner
from tests.integration.test_mc_timeline_pg import FOX, MIRA, T0, Seed
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    m = async_sessionmaker(engine, expire_on_commit=False)
    yield m
    async with scoped_session(m, OWNER) as s:
        for table in ("mc_share_links", "mc_player_explored"):
            await s.execute(text(f"DELETE FROM app.{table}"))
    await engine.dispose()


def _chunks(params: dict[str, str], span: int) -> set[tuple[int, int]]:
    """The offsets a `visited` bitmap marks, as the sidecar's `decode_visited` reads it."""
    blob = params["visited"]
    bits = zlib.decompress(base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4)))
    assert len(bits) == span * span // 8
    return {(i % span, i // span) for i in range(span * span) if bits[i >> 3] >> (i & 7) & 1}


async def _seed(maker: async_sessionmaker) -> None:
    seed = Seed()
    seed.chunk("castle", FOX, 0, 0, T0, "overworld")
    seed.chunk("castle", FOX, 15, 3, T0, "overworld")
    seed.chunk("castle", MIRA, 15, 3, T0, "overworld")  # two players, one chunk
    seed.chunk("castle", MIRA, 16, 0, T0, "overworld")  # the next tile east
    seed.chunk("castle", FOX, -1, -1, T0, "overworld")  # the tile north-west
    seed.chunk("castle", FOX, 2, 2, T0, "nether")
    seed.chunk("other", FOX, 1, 1, T0, "overworld")
    await seed.into(maker)


async def test_a_tile_gets_every_players_crossed_chunks_in_it_and_no_others(maker) -> None:
    await _seed(maker)
    got = await visited.tile_params(maker, "castle", "overworld", 0, 0, 0)
    assert _chunks(got, 16) == {(0, 0), (15, 3)}
    # Negative tiles: chunk -1 is the last of tile -1, not the first of tile 0.
    got = await visited.tile_params(maker, "castle", "overworld", 0, -1, -1)
    assert _chunks(got, 16) == {(15, 15)}
    assert _chunks(await visited.tile_params(maker, "castle", "nether", 0, 0, 0), 16) == {(2, 2)}
    # Zoomed out, one tile spans 256 chunks each way from (0, 0).
    got = await visited.tile_params(maker, "castle", "overworld", 4, 0, 0)
    assert _chunks(got, 256) == {(0, 0), (15, 3), (16, 0)}
    # Nobody went there: nothing to send.
    assert await visited.tile_params(maker, "castle", "overworld", 0, 5, 5) == {}
    assert await visited.tile_params(maker, "castle", "the_end", 0, 0, 0) == {}


async def test_a_share_reads_crossed_ground_only_in_its_own_world(maker) -> None:
    owner = await _owner(maker)
    await _seed(maker)
    _secret, link = await _mint(maker, owner)
    ctx = mc_share_context(link.id)
    got = await visited.tile_params(maker, "castle", "overworld", 0, 0, 0, ctx)
    assert _chunks(got, 16) == {(0, 0), (15, 3)}
    # Another world's fog is RLS-invisible to it, whatever folder it names.
    assert await visited.tile_params(maker, "other", "overworld", 0, 0, 0, ctx) == {}
    # An unpinned share context reads nothing at all.
    assert (
        await visited.tile_params(maker, "castle", "overworld", 0, 0, 0, mc_share_context()) == {}
    )
