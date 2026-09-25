"""Migration 0206 against real Postgres: a panel reads its own knobs and nothing else.

THE POINT OF THIS FILE IS THE SECOND HALF. `app.endpoint_settings` exists rather than a key
in `app.settings` because that table is gated on `app.is_owner()` and holds the Gmail client
secret, the Moltbook bearer key and the global kill, while a panel authenticates as a
`device_key` specifically so a stolen one cannot reach them.

`0178_settings_deny_jmolt` makes the argument this test enforces: "no route does that" is a
code-review convention, not a mechanism. So the isolation is asserted in Postgres, under the
exact context a panel runs as — not inferred from the shape of the FastAPI handlers (CLAUDE.md
rule 3).
"""

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.auth import keys
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# Exactly what a flashed panel runs as: a device key, no domain scopes, not owner-scoped.
PANEL = SessionContext(principal_id="panel-1", principal_kind="device_key")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_a_panel_can_read_its_own_settings(maker: async_sessionmaker) -> None:
    """The whole feature rests on this: if a panel cannot SELECT, the knobs never apply."""
    async with scoped_session(maker, PANEL) as s:
        row = (
            await s.execute(
                text("SELECT volume, mic_gain_db, brightness FROM app.endpoint_settings")
            )
        ).first()
    assert row is not None, "a panel must be able to read the row it is meant to apply"


async def test_a_panel_cannot_write_its_own_volume(maker: async_sessionmaker) -> None:
    """A device on a child's wall able to raise the level in its own ear is the one thing the
    65 dB(A) reasoning exists to prevent.

    THE DENIAL IS SILENT, AND THAT IS WHAT THIS PINS. The panel policy is `FOR SELECT`, so the
    row is simply not visible to an UPDATE: Postgres matches nothing and reports success with
    zero rows rather than raising. An earlier version of this test expected an exception and
    passed a write that had in fact been denied — the assertion has to be about the effect,
    not about the error, or it proves nothing either way.
    """
    async with scoped_session(maker, OWNER) as s:
        await s.execute(text("UPDATE app.endpoint_settings SET volume = 55 WHERE id = 1"))
        await s.commit()

    async with scoped_session(maker, PANEL) as s:
        result = cast(
            CursorResult,
            await s.execute(text("UPDATE app.endpoint_settings SET volume = 100 WHERE id = 1")),
        )
        await s.commit()
    assert result.rowcount == 0, "a panel's write must touch no row"

    async with scoped_session(maker, OWNER) as s:
        volume = (
            await s.execute(text("SELECT volume FROM app.endpoint_settings WHERE id = 1"))
        ).scalar_one()
    assert volume == 55, "the owner's value must survive a panel trying to overwrite it"


async def test_a_panel_cannot_read_app_settings(maker: async_sessionmaker) -> None:
    """The reason this table exists. `app.settings` holds the Gmail client secret, the
    Moltbook bearer key, the autonomy switch and the global kill; a stolen panel key must
    come back with nothing."""
    async with scoped_session(maker, PANEL) as s:
        rows = (await s.execute(text("SELECT key FROM app.settings"))).scalars().all()
    assert list(rows) == [], "a device key must not see any owner setting"


async def test_the_owner_can_write_and_read_back(maker: async_sessionmaker) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text("UPDATE app.endpoint_settings SET volume = 61, brightness = 42 WHERE id = 1")
        )
        await s.commit()
    async with scoped_session(maker, PANEL) as s:
        row = (
            await s.execute(text("SELECT volume, brightness FROM app.endpoint_settings"))
        ).first()
    assert row is not None
    assert (row[0], row[1]) == (61, 42), "what the owner sets is what the panel reads"


# --- the poll that answers two questions --------------------------------------------------
#
# These drive the ROUTE rather than the table, and they live here rather than beside the other
# endpoint route tests because `GET /endpoint/settings` reads the database — the unit harness
# points `database_url` at a dead port on purpose, so the manifest tests pass there and these
# cannot.

# ONE KEY PER TEST, because the container's database outlives an individual test and
# `create_principal` is a unique insert on the key hash — two tests minting the same panel is a
# constraint violation that only appears when the file is run whole, which is how it appeared.
PANEL_KEY_SERVED = "k" * 43
PANEL_KEY_ABSENT = "m" * 43
FW_VERSION = "9.9.9"


def _firmware_tree(root: Path) -> Path:
    """The shape `_firmware_version` and `_dist` expect: a version and a dist/ beside it."""
    dist = root / "dist"
    dist.mkdir(parents=True)
    (root / "version.txt").write_text(FW_VERSION + "\n", encoding="utf-8")
    image = b"\x00" * 64
    (dist / "jbrain-endpoint.bin").write_bytes(image)
    (dist / "SHA256SUMS").write_text(
        f"{hashlib.sha256(image).hexdigest()}  jbrain-endpoint.bin\n", encoding="utf-8"
    )
    return root


async def test_the_settings_poll_also_says_what_firmware_is_served(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker,
    tmp_path: Path,
) -> None:
    """One round trip, two questions.

    Every fetch a panel makes is a fresh TLS handshake (`ota_fetch_settings` inits and cleans
    up its own client) and both panels report `int_largest` — the largest free internal DMA
    block — at 31 KB. So the served version rides the poll the panel already makes rather than
    costing a second handshake, which is what lets a three-second cadence notice an update
    without doubling the handshake rate.
    """
    await SqlAuthRepo(maker).create_principal(
        "device_key", keys.hash_key(PANEL_KEY_SERVED), "panel Elora"
    )
    app = create_app(
        Settings(
            secure_cookies=False,
            database_url=database_url,
            firmware_dir=str(_firmware_tree(tmp_path / "firmware")),
        )
    )
    with TestClient(app) as client:
        body = client.get(
            "/api/endpoint/settings", headers={"Authorization": f"Bearer {PANEL_KEY_SERVED}"}
        )
        assert body.status_code == 200, body.text
        got = body.json()
        assert got["fw_version"] == FW_VERSION
        # The knobs still arrive: the version is a rider, not a replacement. Asserted as
        # PRESENT AND IN RANGE rather than equal to the defaults, because this container's
        # database outlives a test and `test_the_owner_can_write_and_read_back` above leaves
        # brightness at 42 — a test that pinned 255 here would pass alone and fail in the file,
        # which is exactly how this one first failed.
        assert {"brightness", "dim_percent", "volume", "mic_gain_db"} <= set(got)
        assert 0 <= got["brightness"] <= 255
        assert 0 <= got["dim_percent"] <= 100


async def test_a_box_with_no_firmware_still_serves_the_knobs(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker,
    tmp_path: Path,
) -> None:
    """`GET /endpoint/firmware` answers 503 here; this route must not.

    A panel mid-deploy still needs to be told how bright to be, and an empty version means
    "nothing to compare against" rather than "you are out of date" — the firmware installs
    nothing on "", which is the flash-loop the manifest's 503 exists to prevent.
    """
    await SqlAuthRepo(maker).create_principal(
        "device_key", keys.hash_key(PANEL_KEY_ABSENT), "panel Nessa"
    )
    tree = _firmware_tree(tmp_path / "firmware")
    (tree / "version.txt").unlink()
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, firmware_dir=str(tree))
    )
    with TestClient(app) as client:
        resp = client.get(
            "/api/endpoint/settings", headers={"Authorization": f"Bearer {PANEL_KEY_ABSENT}"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["fw_version"] == ""
        # In range, not equal to a default: see the sibling test above.
        assert 0 <= resp.json()["brightness"] <= 255
