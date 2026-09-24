"""`endpoint_panel` — the settings one panel may read and no panel may write.

Two properties, and each one is a bedroom wall if it is wrong:

- **A panel sees its own row and no other.** These are unauthenticated-to-each-other devices in
  two children's rooms. A panel that could read its sibling's row would learn what that unit is
  called and what it looks like, which is small; the reason it matters is the same reason
  `0210` gave for status — the policy is the mechanism, and "the handler only asks for one row"
  is a code-review convention that survives exactly until someone edits the handler.
- **No panel may write at all.** This is what makes it a DEFAULT rather than a setting. The four
  taps and a hold that change the body stay a live toggle costing nothing to try, and the form
  the panel comes back as is still the one the owner chose. A writable row would turn every
  accidental gesture into a permanent change nobody made on purpose.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from jbrain.db.session import SessionContext, scoped_session
from jbrain.devices import service as device_service
from jbrain.devices.repo import SqlDeviceRepo
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _as_panel(subject_id: str) -> SessionContext:
    """A panel's own session: `device_key`, pinned to its subject — what `ctx_for` builds from
    the credential in NVS."""
    return SessionContext(
        principal_id=f"key-of-{subject_id}", principal_kind="device_key", subject_id=subject_id
    )


async def _panel(maker: async_sessionmaker, name: str) -> str:
    made = await device_service.provision_or_reflash(
        SqlDeviceRepo(maker), OWNER, name, device_role="jpet"
    )
    return made.device.id


async def _set(maker: async_sessionmaker, sid: str, *, pet: str, form: str) -> None:
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                "INSERT INTO app.endpoint_panel (subject_id, pet_name, form)"
                " VALUES (CAST(:sid AS uuid), :pet, :form)"
                " ON CONFLICT (subject_id) DO UPDATE"
                " SET pet_name = EXCLUDED.pet_name, form = EXCLUDED.form"
            ),
            {"sid": sid, "pet": pet, "form": form},
        )
        await s.commit()


async def test_a_panel_reads_its_own_row_and_not_its_siblings(maker: async_sessionmaker) -> None:
    mine = await _panel(maker, "panel rls-mine")
    theirs = await _panel(maker, "panel rls-theirs")
    await _set(maker, mine, pet="Pip", form="robot")
    await _set(maker, theirs, pet="Nim", form="ostrich")

    async with scoped_session(maker, _as_panel(mine)) as s:
        rows = (
            await s.execute(text("SELECT subject_id::text, pet_name FROM app.endpoint_panel"))
        ).all()

    # Not "it found its own row" — it found ONE row, and asking for everything gave it only that.
    assert [(r[0], r[1]) for r in rows] == [(mine, "Pip")]


async def test_a_panel_cannot_write_its_own_row(maker: async_sessionmaker) -> None:
    """The gesture is a live toggle, not a silent overwrite of the owner's default."""
    mine = await _panel(maker, "panel rls-nowrite")
    await _set(maker, mine, pet="Pip", form="ostrich")

    async with scoped_session(maker, _as_panel(mine)) as s:
        await s.execute(
            text(
                "UPDATE app.endpoint_panel SET form = 'robot' WHERE subject_id = CAST(:sid AS uuid)"
            ),
            {"sid": mine},
        )
        await s.commit()

    async with scoped_session(maker, OWNER) as s:
        form = (
            await s.execute(
                text("SELECT form FROM app.endpoint_panel WHERE subject_id = CAST(:sid AS uuid)"),
                {"sid": mine},
            )
        ).scalar_one()
    assert form == "ostrich", "a panel may not promote its own gesture into the owner's default"


async def test_a_panel_cannot_insert_a_row_for_anybody(maker: async_sessionmaker) -> None:
    """Including its own. A row that appeared because a device asked for one would be a setting
    the owner never chose, and the owner is the only one who chooses here."""
    mine = await _panel(maker, "panel rls-noinsert")

    async with scoped_session(maker, _as_panel(mine)) as s:
        with pytest.raises(Exception) as caught:
            await s.execute(
                text(
                    "INSERT INTO app.endpoint_panel (subject_id, pet_name, form)"
                    " VALUES (CAST(:sid AS uuid), 'Sneaky', 'robot')"
                ),
                {"sid": mine},
            )
            await s.commit()
    assert "policy" in str(caught.value).lower() or "permission" in str(caught.value).lower()


async def test_the_settings_survive_a_reflash(maker: async_sessionmaker) -> None:
    """THE REASON THE ROW KEYS ON THE SUBJECT. Re-flashing is what the owner does when something
    is wrong; losing a child's pet to the step meant to fix her panel would be its own small
    betrayal, and before the flash rotated rather than re-provisioned that is exactly what would
    have happened."""
    sid = await _panel(maker, "panel rls-survives")
    await _set(maker, sid, pet="Pip", form="robot")

    again = await device_service.provision_or_reflash(
        SqlDeviceRepo(maker), OWNER, "panel rls-survives", device_role="jpet"
    )
    assert again.device.id == sid

    async with scoped_session(maker, OWNER) as s:
        row = (
            await s.execute(
                text(
                    "SELECT pet_name, form FROM app.endpoint_panel"
                    " WHERE subject_id = CAST(:sid AS uuid)"
                ),
                {"sid": sid},
            )
        ).first()
    assert row is not None and (row[0], row[1]) == ("Pip", "robot")
