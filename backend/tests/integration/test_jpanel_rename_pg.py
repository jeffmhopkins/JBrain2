"""Naming a panel from the PWA, against real Postgres — the route that removes a cable.

A panel's name lives on the BOX. `/flash` writes `panel <name>` onto the device key it mints,
and everything the twins see — the thread in the PWA, the `X-Jpanel-From` a sibling's pop-up
reads out — comes from that label. A unit enrolled without a name therefore announces itself as
"the other one" forever, and until this route the only correction was to re-flash it over USB:
a terminal by another name, which CLAUDE.md #10 exists to stop.

THE CASE THIS FILE IS REALLY FOR IS THE GROUP RENAME. Every `/flash` mints a fresh key and
nothing retires the old one, so one panel is routinely several unrevoked principals carrying one
label — fifteen of them on the live box at one point. `_panel_names` collapses that with
`DISTINCT ON (label)`, which makes the LABEL the identity; renaming only the newest key would
leave the rest under the old name and grow a second, unreachable panel in the roster. That is
SQL against real policies, so it is asserted here rather than reasoned about (CLAUDE.md rule 3).
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from jbrain.api.jpanel import _wav
from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import SessionContext, scoped_session
from jbrain.main import create_app
from jbrain.storage import FsBlobStore
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]

# `bootstrap` is what the CLI creates principals under — the one context allowed to insert them
# without already being the owner, which is exactly what a test fixture is.
_BOOTSTRAP = SessionContext(auth_context="bootstrap")


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # EVERY CASE HERE COUNTS PANELS, and the container is shared across this file — so a
    # leftover `panel Rae` from the case above changes what "exactly one sibling" means in the
    # case below. Cleared rather than worked around: an assertion about a roster is only worth
    # making against a roster the test built.
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.jpanel_message"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        await s.commit()
    yield sessions
    await engine.dispose()


async def _panel(maker: async_sessionmaker[AsyncSession], label: str, age_days: int) -> str:
    """One device key, as `/flash` leaves it. `age_days` orders the group: `_panel_names` keeps
    the NEWEST key per label, so which one the PWA is looking at is decided by this."""
    pid = str(uuid.uuid4())
    async with scoped_session(maker, _BOOTSTRAP) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.principals (id, kind, key_hash, label, created_at)
                VALUES (CAST(:id AS uuid), 'device_key', :hash, :label,
                        now() - make_interval(days => :age))
                """
            ),
            {"id": pid, "hash": f"hash-{pid}", "label": label, "age": age_days},
        )
        await s.commit()
    return pid


def _sibling(client: TestClient, key: str, panel_key: str) -> str:
    """What `GET /waiting` tells THAT PANEL its twin is called.

    AS THE PANEL, NOT AS THE OWNER HOLDING A PANEL KEY. The owner cookie from the login also
    satisfies this route and it authenticates FIRST, so a bearer header alone is answered for
    the owner — every assertion here would have been about the wrong principal, which is the
    same trap `_provision_panel` clears its cookies for in the unit tests."""
    client.cookies.clear()
    answer = client.get("/api/jpanel/waiting", headers={"Authorization": f"Bearer {panel_key}"})
    assert answer.status_code == 200, answer.text
    client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
    return str(answer.json()["sibling"])


def _names(client: TestClient) -> dict[str, str]:
    threads = client.get("/api/jpanel/messages").json()["panels"]
    return {t["device_id"]: t["name"] for t in threads}


async def test_renaming_moves_every_key_the_old_label_had(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    # Her panel, flashed three times without a name, plus her brother's, named at flash time.
    old = await _panel(maker, "room endpoint panel", 30)
    older = await _panel(maker, "room endpoint panel", 60)
    live = await _panel(maker, "room endpoint panel", 1)
    ellie = await _panel(maker, "panel Ellie", 10)

    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        # Owner-gated: naming somebody's child's panel is not a thing a device key may do.
        assert (
            client.post(f"/api/jpanel/panels/{live}/name", json={"name": "Nora"}).status_code == 401
        )
        assert (
            client.post(
                "/api/auth/session", json={"owner_key": key, "device_label": "t"}
            ).status_code
            == 204
        )

        before = _names(client)
        assert before == {live: "the other one", ellie: "Ellie"}, (
            "the three unnamed keys should already collapse to one row — if they do not, the "
            "rename below is not the thing under test"
        )

        done = client.post(f"/api/jpanel/panels/{live}/name", json={"name": "Nora"})
        assert done.status_code == 200, done.text
        assert done.json()["name"] == "Nora"
        # THE NUMBER IS THE POINT: all three, not the one addressed.
        assert done.json()["keys"] == 3

        after = _names(client)
        assert after == {live: "Nora", ellie: "Ellie"}, (
            "a rename that left the superseded keys behind would show a second, unreachable "
            "panel still called 'the other one'"
        )

    async with scoped_session(maker, _BOOTSTRAP) as s:
        labels = {
            str(row[0]): str(row[1])
            for row in (
                await s.execute(
                    text("SELECT id::text, label FROM app.principals WHERE kind = 'device_key'")
                )
            ).all()
        }
    assert labels[old] == labels[older] == labels[live] == "panel Nora"
    assert labels[ellie] == "panel Ellie", "the sibling's label must not move"


async def test_a_panel_learns_its_twin_s_name_from_the_poll_it_already_makes(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The blue recording indicator said MESSAGE where it should say TO ELORA, and that was an
    honest gap rather than a placeholder: a panel is flashed with its OWN name, the box mints
    the other one's at the OTHER unit's flash, and nothing on the device could answer "who is
    my twin". `GET /waiting` carries it now — a poll that was already happening.

    Empty unless there is EXACTLY ONE other panel, which is the rule `send(to="panel")` already
    follows: with two siblings "the other one" is a question rather than a name, and a guess
    would put the wrong child on the glass."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        mine = client.post("/api/devices", json={"label": "panel Ellie"}).json()

        # Alone on the box: there is no sibling, and saying so is the correct answer.
        assert _sibling(client, key, mine["key"]) == ""

        client.post("/api/devices", json={"label": "panel Nora"})
        assert _sibling(client, key, mine["key"]) == "Nora"

        # A third unit, and the answer goes back to nothing rather than to a guess.
        client.post("/api/devices", json={"label": "panel Rae"})
        assert _sibling(client, key, mine["key"]) == ""


async def test_renaming_the_twin_changes_what_the_panel_is_told_to_say(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The two halves joined up: naming the unnamed panel from the PWA is what puts a name on
    the OTHER panel's recording indicator. Before the rename its twin is told nothing to say —
    "the other one" is what an unnamed panel is called, not a name to shout at a child."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        mine = client.post("/api/devices", json={"label": "panel Ellie"}).json()
        client.post("/api/devices", json={"label": "room endpoint panel"})
        assert _sibling(client, key, mine["key"]) == "the other one"

        # THE ID THE PWA WOULD USE, which is the panel's PRINCIPAL id off the thread list —
        # not the `id` the provisioning route echoes back, which is the device's. The rename
        # addresses principals because that is what the roster is made of.
        twin = next(pid for pid, name in _names(client).items() if name == "the other one")
        done = client.post(f"/api/jpanel/panels/{twin}/name", json={"name": "Nora"})
        assert done.status_code == 200, done.text
        assert _sibling(client, key, mine["key"]) == "Nora"


async def test_a_name_another_panel_already_has_is_refused(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The one failure this addressing model has, and it must not be reachable from a text box.
    `_panel_names` keeps one row per label, so two panels called Ellie become one row and one
    twin becomes unreachable — documented where it is caused, refused here."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    unnamed = await _panel(maker, "room endpoint panel", 1)
    await _panel(maker, "panel Ellie", 10)

    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        clash = client.post(f"/api/jpanel/panels/{unnamed}/name", json={"name": "Ellie"})
        assert clash.status_code == 409
        assert "unreachable" in clash.json()["detail"]
        # Case is not a loophole: "ellie" collapses onto "Ellie" just as surely.
        assert (
            client.post(f"/api/jpanel/panels/{unnamed}/name", json={"name": "ellie"}).status_code
            == 409
        )
        assert _names(client)[unnamed] == "the other one", "a refused rename changes nothing"


async def test_only_a_panel_can_be_renamed_through_this_route(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """The owner's phone is a `device_key` too. This route writes the `panel ` prefix that makes
    a principal ADDRESSABLE as a panel, so pointing it at an OwnTracks key would enrol that
    phone into the twins' post — which is why the lookup filters on the label rather than
    trusting the id in the path."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    phone = await _panel(maker, "owntracks phone", 5)

    app = create_app(Settings(secure_cookies=False, database_url=database_url))
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        assert (
            client.post(f"/api/jpanel/panels/{phone}/name", json={"name": "Nora"}).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/jpanel/panels/{uuid.uuid4()}/name", json={"name": "Nora"}
            ).status_code
            == 404
        )
        # And a path segment that is not a uuid at all: 404, not the 500 a raw cast gives.
        assert (
            client.post("/api/jpanel/panels/not-a-uuid/name", json={"name": "N"}).status_code == 404
        )

    async with scoped_session(maker, _BOOTSTRAP) as s:
        label = (
            await s.execute(
                text("SELECT label FROM app.principals WHERE id = CAST(:id AS uuid)"),
                {"id": phone},
            )
        ).scalar_one()
    assert label == "owntracks phone"


async def _plant(
    maker: async_sessionmaker[AsyncSession], blob_dir, panel_id: str, words: bytes
) -> tuple[str, bytes]:
    """A message from the owner to `panel_id`, with real audio in the store.

    Built directly rather than through `POST /messages`, because that route synthesises Dad's
    voice and there is no TTS in this environment — and what is under test here is the replay,
    not the synthesiser."""
    pcm = words * 400  # a second or so of something, so the round trip has bytes to compare
    sha = await FsBlobStore(blob_dir).put(_wav(pcm))
    mid = str(uuid.uuid4())
    async with scoped_session(maker, OWNER) as s:
        await s.execute(
            text(
                """
                INSERT INTO app.jpanel_message
                    (id, sender_kind, recipient_kind, recipient_device, blob_sha256,
                     transcript, composed, duration_ms)
                VALUES (CAST(:id AS uuid), 'owner', 'panel', :dev, :sha,
                        'good night', 'text', 1000)
                """
            ),
            {"id": mid, "dev": panel_id, "sha": sha},
        )
        await s.commit()
    return mid, pcm


async def test_a_panel_can_re_fetch_a_message_it_has_already_heard(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """THE ROUTE THAT MAKES "AGAIN" POSSIBLE WITHOUT KEEPING THE BYTES.

    Since 0.2.96 the panel streams a message through a four-second ring and the audio is gone
    as it plays — which is what lifts the length cap. The repeat icon therefore has to ask the
    box a second time, and it must be able to do so for a message that is already PLAYED,
    which `GET /next` by definition will not return.

    It must also not spend a delivery attempt. `deliveries` is the give-up rule — five tries
    and the box stops offering a message — so counting a replay would make listening to
    something twice a way to lose it."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, blob_dir=str(tmp_path))
    )
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        panel = client.post("/api/devices", json={"label": "panel Ellie"}).json()
        pid = next(iter(_names(client)))
        message_id, _ = await _plant(maker, tmp_path, pid, b"\x11\x22")

        head = {"Authorization": f"Bearer {panel['key']}"}
        client.cookies.clear()
        first = client.get("/api/jpanel/next", headers=head)
        assert first.status_code == 200, first.text
        assert first.headers["X-Jpanel-Id"] == message_id
        assert (
            client.post("/api/jpanel/played", json={"id": message_id}, headers=head).status_code
            == 204
        )

        # `/next` has nothing more — the message is played. That is the whole problem.
        assert client.get("/api/jpanel/next", headers=head).status_code == 204

        again = client.get(f"/api/jpanel/message/{message_id}/pcm", headers=head)
        assert again.status_code == 200, again.text
        assert again.content == first.content, "the same audio, byte for byte"

    # A replay is not a delivery attempt.
    async with scoped_session(maker, OWNER) as s:
        deliveries = (
            await s.execute(
                text("SELECT deliveries FROM app.jpanel_message WHERE id = CAST(:i AS uuid)"),
                {"i": message_id},
            )
        ).scalar_one()
    assert int(deliveries) == 1, "the one real delivery, not two"


async def test_a_panel_cannot_re_fetch_a_message_that_is_not_its_own(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    """The replay route takes a message id from a device on a child's wall, so the id must not
    be the thing that grants access. It is not: `jpanel_message_panel_read` opens a row only to
    the panel that sent it or was sent it, so a guessed id is a 404 from the policy rather than
    from a check in the handler (CLAUDE.md rule 3)."""
    key = await service.rotate_owner_key(SqlAuthRepo(maker))
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, blob_dir=str(tmp_path))
    )
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
        client.post("/api/devices", json={"label": "panel Ellie"})
        snooper = client.post("/api/devices", json={"label": "panel Nora"}).json()
        ellie_id = next(pid for pid, name in _names(client).items() if name == "Ellie")
        hers, _ = await _plant(maker, tmp_path, ellie_id, b"\x33\x44")

        client.cookies.clear()
        head = {"Authorization": f"Bearer {snooper['key']}"}
        assert client.get(f"/api/jpanel/message/{hers}/pcm", headers=head).status_code == 404
        # And a path segment that is not a uuid is a 404 too, not a 500.
        assert client.get("/api/jpanel/message/not-a-uuid/pcm", headers=head).status_code == 404
