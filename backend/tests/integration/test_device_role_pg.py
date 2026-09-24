"""`subjects.device_role` against real Postgres — the column that replaced a label prefix.

Panels and phones are the same `Subject(kind='device')` substrate, and for a long time the only
thing telling them apart was the label `/flash` wrote. Three queries matched `label LIKE 'panel%'`
and it broke the way a convention breaks: a third unit took the unnamed default, `room endpoint
panel` matched the roster predicate, and a box on the owner's desk joined two children's
addressing and stopped their voice messages. Meanwhile the Location screen's phone list — which
filtered on nothing but `kind = 'device'` — had been listing every panel ever flashed.

What is pinned here is what the column has to make true, and each one failed before it existed.
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

# The context `_panel_names` runs the roster under. Narrow on purpose: reading it under a panel's
# own context returns exactly one row — itself — which is why panel-to-panel messaging answered
# 409 from the first commit.
ADDRESSING = SessionContext(auth_context="login")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_scope_separates_phones_from_panels(maker: async_sessionmaker) -> None:
    """The listing a screen asks for is the listing it gets.

    THE BUG THIS ENDS: one unfiltered `WHERE s.kind = 'device'` served the Location screen, so
    every panel ever flashed appeared there — one row per flash, each rendering a status line
    (last fix, battery, speed) that a panel structurally never produces.
    """
    devices = SqlDeviceRepo(maker)
    phone = await device_service.provision_device(devices, OWNER, "scope phone")
    pet = await device_service.provision_device(
        devices, OWNER, "panel scope-pet", device_role="jpet"
    )
    desk = await device_service.provision_device(
        devices, OWNER, "panel scope-desk", device_role="display"
    )

    phones = {d.id for d in await devices.list(OWNER, scope="phones")}
    endpoints = {d.id for d in await devices.list(OWNER, scope="endpoints")}
    everything = {d.id for d in await devices.list(OWNER)}

    assert phone.device.id in phones
    assert {pet.device.id, desk.device.id} & phones == set()
    assert {pet.device.id, desk.device.id} <= endpoints
    assert phone.device.id not in endpoints
    # The owner's admin API still sees every device there is — narrowing that too would have
    # traded one blind spot for another.
    assert everything == phones | endpoints

    listed = {d.id: d.device_role for d in await devices.list(OWNER)}
    assert listed[phone.device.id] is None
    assert listed[pet.device.id] == "jpet"
    assert listed[desk.device.id] == "display"


async def test_a_display_is_not_in_the_twins_roster(maker: async_sessionmaker) -> None:
    """The whole point, in one assertion.

    `send(to="panel")` needs exactly one sibling. A third unit that called itself nothing in
    particular used to make three candidates and the feature stopped working. A display is now
    invisible to that query however it is named — including when it is named like a pet.
    """
    devices = SqlDeviceRepo(maker)
    await device_service.provision_device(devices, OWNER, "panel roster-a", device_role="jpet")
    await device_service.provision_device(devices, OWNER, "panel roster-b", device_role="jpet")
    # Named exactly the way the unnamed default used to name it — the label that caused the
    # breakage. Under a prefix convention this is a third pet; under the column it is not.
    await device_service.provision_device(
        devices, OWNER, "room endpoint panel", device_role="display"
    )

    async with scoped_session(maker, ADDRESSING) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT p.label FROM app.principals p"
                    " JOIN app.subjects s ON s.id = p.subject_id"
                    " WHERE p.kind = 'device_key' AND p.revoked_at IS NULL"
                    "   AND s.device_role = 'jpet'"
                )
            )
        ).all()
    labels = {str(r[0]) for r in rows}
    assert {"panel roster-a", "panel roster-b"} <= labels
    # THE ASSERTION THE WHOLE COLUMN IS FOR. Under the old prefix convention this exact label
    # was a pet, and a box on a desk carrying it made `send(to="panel")` see three candidates
    # where it needs one. (Subset comparisons throughout: the database is module-scoped, so
    # rows from the other tests here are present and are not this test's business.)
    assert "room endpoint panel" not in labels


async def test_the_addressing_context_can_read_subjects(maker: async_sessionmaker) -> None:
    """`subjects_select_login` — without it the roster reads zero rows and nobody has a sibling.

    Worth its own test because the failure is silent and total: the join returns nothing, every
    panel looks like the only panel, and that presents as "messages between the twins stopped"
    rather than as a permissions error.
    """
    devices = SqlDeviceRepo(maker)
    pet = await device_service.provision_device(
        devices, OWNER, "panel read-check", device_role="jpet"
    )

    async with scoped_session(maker, ADDRESSING) as session:
        role = (
            await session.execute(
                text("SELECT device_role FROM app.subjects WHERE id = CAST(:sid AS uuid)"),
                {"sid": pet.device.id},
            )
        ).scalar_one_or_none()
    assert role == "jpet"


async def test_the_addressing_context_still_cannot_write(maker: async_sessionmaker) -> None:
    """READ ONLY, and deliberately. The new policy is `FOR SELECT`; widening `subjects_access`
    instead would have handed the login context UPDATE and DELETE as well, since one `FOR ALL`
    policy's `USING` governs all three."""
    devices = SqlDeviceRepo(maker)
    pet = await device_service.provision_device(
        devices, OWNER, "panel write-check", device_role="jpet"
    )

    async with scoped_session(maker, ADDRESSING) as session:
        await session.execute(
            text("UPDATE app.subjects SET device_role = 'display' WHERE id = CAST(:sid AS uuid)"),
            {"sid": pet.device.id},
        )
        await session.commit()

    # RLS filters the row out of the UPDATE rather than raising: the write finds nothing to do.
    async with scoped_session(maker, OWNER) as session:
        role = (
            await session.execute(
                text("SELECT device_role FROM app.subjects WHERE id = CAST(:sid AS uuid)"),
                {"sid": pet.device.id},
            )
        ).scalar_one()
    assert role == "jpet"


async def test_a_reflash_retires_the_keys_it_replaced(maker: async_sessionmaker) -> None:
    """What the flash comment always claimed, finally true.

    `/flash` has said since it was written that a re-flash issues a new identity and "the old key
    should stop working at that moment". Nothing made it, so thirteen flashes of one panel left
    thirteen live keys under one name — thirteen identical rows in the owner's device list, with
    the two he actually needed to revoke buried among them, and every one of them still
    authenticating.
    """
    devices = SqlDeviceRepo(maker)
    first = await device_service.provision_device(
        devices, OWNER, "panel reflash", device_role="jpet"
    )
    second = await device_service.provision_device(
        devices, OWNER, "panel reflash", device_role="jpet"
    )

    again = await device_service.provision_or_reflash(
        devices, OWNER, "panel reflash", device_role="jpet"
    )
    assert again.device.id == first.device.id, "a re-flash re-credentials the panel it already is"

    live = {d.id for d in await devices.list(OWNER, scope="endpoints") if not d.revoked}
    assert first.device.id in live
    # The stray subject a pre-0212 flash left behind is retired, not left authenticating.
    assert second.device.id not in live

    # Idempotent: flashing again lands on the same subject rather than growing a third.
    third = await device_service.provision_or_reflash(
        devices, OWNER, "panel reflash", device_role="jpet"
    )
    assert third.device.id == first.device.id


async def test_retirement_does_not_reach_across_roles(maker: async_sessionmaker) -> None:
    """A display named `Jeff` must not retire a pet named `Jeff`.

    The narrowness is the point: identity across a re-flash is the NAME, because the physical
    unit carries nothing else the box can recognise. Matching on name alone would let the owner's
    desk box silently kill a panel on a child's wall.
    """
    devices = SqlDeviceRepo(maker)
    pet = await device_service.provision_device(
        devices, OWNER, "panel crossrole", device_role="jpet"
    )
    desk = await device_service.provision_device(
        devices, OWNER, "panel crossrole", device_role="display"
    )

    again = await device_service.provision_or_reflash(
        devices, OWNER, "panel crossrole", device_role="display"
    )
    assert again.device.id == desk.device.id, "the display re-credentials itself, not the pet"

    live = {d.id for d in await devices.list(OWNER, scope="endpoints") if not d.revoked}
    assert {pet.device.id, desk.device.id} <= live
