"""The on-demand report, end to end: raise the counter, the panel sees it, the ring reads back.

WHY THIS ROUND TRIP IS WORTH A CONTAINER. The three pieces live in three places — the counter
is a column, the settings route serves it to a panel authenticated by its own device key, and
the ring comes back out of a `jsonb` column written by a different route under a different
principal. Each half has passed on its own before while the whole failed: the appearance the
settings route served was read under a context with no subject pin, so every panel was told
the default for four days with every unit test green.

So this drives the real app against real Postgres, in the order the box actually does it:
the panel polls, the owner's debug token raises the counter, the panel polls again and sees a
different number, the panel reports, and the debug read shows the ring it posted.
"""

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

from jbrain.auth import service
from jbrain.auth.repo import SqlAuthRepo
from jbrain.config import Settings
from jbrain.db.session import scoped_session
from jbrain.main import create_app
from tests.conftest import docker_available
from tests.integration.test_rls import OWNER, database_url  # noqa: F401

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="requires a Docker daemon"),
]


@pytest.fixture
async def maker(database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:  # noqa: F811
    engine: AsyncEngine = create_async_engine(database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # The container is shared and this file counts panels and reads a global counter, so it
    # starts from a known fleet and a known number.
    async with scoped_session(sessions, OWNER) as s:
        await s.execute(text("DELETE FROM app.endpoint_status"))
        await s.execute(text("DELETE FROM app.principals WHERE kind = 'device_key'"))
        await s.execute(text("UPDATE app.endpoint_settings SET telemetry_seq = 0 WHERE id = 1"))
        await s.commit()
    yield sessions
    await engine.dispose()


async def _panel_and_debug(
    maker: async_sessionmaker[AsyncSession], client: TestClient
) -> tuple[dict[str, str], dict[str, str]]:
    """One registered panel and an owner debug token, as the two header dicts the routes want.

    The app must already be built with `debug_access_enabled` — without it the debug router is
    not mounted at all and every call is a 404 rather than the answer or the refusal it is
    asserting, which is exactly what this file did on its first CI run.
    """
    owner_key = await service.rotate_owner_key(SqlAuthRepo(maker))
    debug_key, _ = await service.mint_capability(SqlAuthRepo(maker), "claude", ttl_hours=1)
    client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
    panel = client.post("/api/devices", json={"label": "panel Elora", "device_role": "jpet"}).json()
    return {"Authorization": f"Bearer {panel['key']}"}, {"Authorization": f"Bearer {debug_key}"}


async def test_raising_the_counter_reaches_a_panel_and_the_ring_reads_back_named(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    owner_key = await service.rotate_owner_key(SqlAuthRepo(maker))
    debug_key, _ = await service.mint_capability(SqlAuthRepo(maker), "claude", ttl_hours=1)
    # `debug_access_enabled`, WITHOUT WHICH THE ROUTER IS NOT MOUNTED AT ALL and every
    # call here is a 404 rather than the refusal or the answer it is asserting. The
    # debug surface is opt-in per deployment (`main.py`), so a test that forgets the
    # flag is testing an app that has no debug API — which is exactly what this file
    # did on its first CI run.
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        client.post("/api/auth/session", json={"owner_key": owner_key, "device_label": "t"})
        panel = client.post(
            "/api/devices", json={"label": "panel Elora", "device_role": "jpet"}
        ).json()
        dbg = {"Authorization": f"Bearer {debug_key}"}
        pk = {"Authorization": f"Bearer {panel['key']}"}

        # WHAT THE PANEL SEES FIRST is the baseline it adopts in silence. Zero here is what
        # makes "never raised" indistinguishable from "nothing is being asked of you".
        client.cookies.clear()
        first = client.get("/api/endpoint/settings", headers=pk).json()
        assert first["telemetry_seq"] == 0

        raised = client.post("/api/debug/endpoint/report-now", headers=dbg)
        assert raised.status_code == 200, raised.text
        assert raised.json()["telemetry_seq"] == 1

        # THE NUMBER THE PANEL WOULD ACT ON. Different from the baseline is the whole
        # protocol — the panel holds no other state and the box keeps nothing to clear.
        client.cookies.clear()
        second = client.get("/api/endpoint/settings", headers=pk).json()
        assert second["telemetry_seq"] == 1, "the raise never reached the panel's own poll"

        # The panel obliges, in the five-field shape 0.3.10 and later actually send.
        client.cookies.clear()
        posted = client.post(
            "/api/endpoint/telemetry",
            headers=pk,
            json={
                "version": "0.3.14",
                "uptime_ms": 42_000,
                "heard": [
                    ["tell sister", 17, 0, 1200, "TfL SgSTk"],
                    ["tell dad", 41, 1, 1, "TfL DaD"],
                ],
            },
        )
        assert posted.status_code == 204, posted.text

        got = client.get("/api/debug/endpoint/heard", headers=dbg)
        assert got.status_code == 200, got.text
        body = got.json()
        assert body["telemetry_seq"] == 1, "the read should say which raise it reflects"
        assert len(body["panels"]) == 1
        ring = body["panels"][0]
        assert ring["label"] == "panel Elora"
        assert ring["version"] == "0.3.14"

        # NAMED, WHICH IS THE POINT OF THE ROUTE. The wire is a positional tuple and this is
        # the surface a person reads a diagnosis off.
        missed = ring["heard"][0]
        assert missed["phrase"] == "tell sister"
        assert missed["fired"] is False
        assert missed["raw"] == "TfL SgSTk", "the phoneme string is the reason this exists"
        # Past the old `uint8_t` ceiling, which saturated at 255 on a real panel.
        assert missed["count"] == 1200
        assert ring["heard"][1]["fired"] is True


async def test_the_box_can_say_why_a_panel_cannot_reach_it(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """ANSWERING "WHY DID THE PET NOT ANSWER HER?" WITHOUT READING THE ACCESS LOG BY HAND.

    On 2026-09-29 the owner pressed the pet on Lydian's panel, got the failure dash immediately,
    and asked why. The box's evidence was entirely NEGATIVE — no `POST /endpoint/converse` had
    arrived, no `GET /endpoint/settings` for the hour before, nothing at all for the last thirty
    minutes. All true, and all of it invisible except by paging back through two thousand log
    lines and noticing which requests had STOPPED, which is the hardest thing to find in a log
    because a request that never happened leaves no line.

    So the panel keeps the reasons across the outage and hands them over when it can speak again,
    and this route is where they land. Driven end to end against real Postgres because the value
    goes in as a `jsonb` column under a device key and comes back out under the owner's debug
    token — two principals, two routes, and every previous bug in this area was two halves
    disagreeing with nothing looking at both.
    """
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        pk, dbg = await _panel_and_debug(maker, client)

        # A panel reporting the evening of 2026-09-29: settings failing, the poll on the other
        # task fine, the conversation refused, and half an hour since it last reached the box.
        client.cookies.clear()
        posted = client.post(
            "/api/endpoint/telemetry",
            headers=pk,
            json={
                "version": "0.3.33",
                "uptime_ms": 2_513_000,
                "set_err": "connect",
                "set_fails": 3,
                "set_ago_s": 47,
                "talk_err": "connect",
                "talk_fails": 1,
                "talk_ago_s": 12,
                # A child's reply that did not go, and the dashes she watched appear. Both were
                # invisible to this box until 0.3.38 — the failing sends never arrived, so its
                # own record held only the ones that worked.
                "send_err": "upload-stall",
                "send_fails": 2,
                "send_ago_s": 90,
                "dashes": 4,
                "dash_err": "timeout",
                "dash_ago_s": 30,
                "box_quiet_s": 1860,
            },
        )
        assert posted.status_code == 204, posted.text

        got = client.get("/api/debug/endpoint/reach", headers=dbg)
        assert got.status_code == 200, got.text
        panel = got.json()["panels"][0]
        assert panel["label"] == "panel Elora"
        assert panel["box_quiet_s"] == 1860, (
            "the one number that says the panel is not talking to this box at all"
        )
        # Freshly posted, so the answer describes now rather than an hour ago — the distinction
        # `stale_s` exists to make, and the one that would have been the finding that evening.
        assert panel["stale_s"] < 60

        paths = {p["name"]: p for p in panel["paths"]}
        assert set(paths) == {"set", "poll", "talk", "send"}, "all four, named"
        assert paths["set"]["err"] == "connect" and paths["set"]["fails"] == 3
        assert paths["set"]["ago_s"] == 47
        assert paths["talk"]["err"] == "connect" and paths["talk"]["fails"] == 1
        # THE HEALTHY PATH IS LISTED TOO, and this is the assertion that carries the diagnosis:
        # one task reaching the box while another cannot is what rules out the network. A route
        # that omitted the working paths would make an absent row mean two different things.
        assert paths["poll"]["fails"] == 0 and paths["poll"]["err"] == ""
        # THE PATH WHERE A SILENT FAILURE COSTS THE MOST: the others fail and something is late,
        # this one fails and a message a four-year-old recorded for somebody is gone.
        assert paths["send"]["err"] == "upload-stall" and paths["send"]["fails"] == 2
        assert paths["send"]["ago_s"] == 90

        # AND THE DASH, which is counted beside the paths rather than inside them because one of
        # its two causes is not a path failure at all: `timeout` means nothing failed, the answer
        # just never came back in time — so there is no row above to read it off.
        assert panel["dashes"] == 4
        assert panel["dash_err"] == "timeout"
        assert panel["dash_ago_s"] == 30


async def test_the_box_can_tell_a_message_nobody_heard_from_one_that_played(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """THE TWO CASES THIS BOX CANNOT TELL APART ON ITS OWN, which is why the panel now measures it.

    The owner, 2026-09-29 on 0.3.32: *"I click the notification and then playback menu pulls up.
    But then [it] only stay[s] for about a half second before going back to the big blue
    notification and it doesn't play."* This box's record of that message was `GET /next` 200
    followed by `POST /played` 204 — served, digest verified by the panel, acknowledged. Which is
    also precisely what a message that played perfectly looks like.

    So the panel times its own ring, and the ratio is the finding: 32 bytes to the millisecond at
    16-bit mono 16 kHz. Asserted with both cases side by side, because a test that only pinned the
    broken one would pass against a route that always answered `heard: false`.
    """
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        pk, dbg = await _panel_and_debug(maker, client)

        # 98 KB is 3.06 s of audio, and the ring was quiet after 40 ms. Nobody heard this.
        client.cookies.clear()
        assert (
            client.post(
                "/api/endpoint/telemetry",
                headers=pk,
                json={
                    "version": "0.3.33",
                    "uptime_ms": 441_000,
                    "msg_bytes": 97_920,
                    "msg_ms": 40,
                    "msg_ok": 1,
                    "msg_bad": 0,
                },
            ).status_code
            == 204
        )
        msg = client.get("/api/debug/endpoint/reach", headers=dbg).json()["panels"][0][
            "last_message"
        ]
        assert msg["expected_ms"] == 3060, "98 KB of 16 kHz mono is three seconds of a voice"
        assert msg["heard"] is False, (
            "a ring that went quiet in 40 ms played nothing, and every server-side record of this "
            "message says it was delivered and heard"
        )

        # The same message, played. Slightly OVER the expected time, because the ring is observed
        # after it drains rather than as the last sample leaves.
        client.cookies.clear()
        assert (
            client.post(
                "/api/endpoint/telemetry",
                headers=pk,
                json={
                    "version": "0.3.33",
                    "uptime_ms": 500_000,
                    "msg_bytes": 97_920,
                    "msg_ms": 3_180,
                    "msg_ok": 2,
                    "msg_bad": 0,
                },
            ).status_code
            == 204
        )
        msg = client.get("/api/debug/endpoint/reach", headers=dbg).json()["panels"][0][
            "last_message"
        ]
        assert msg["heard"] is True, "a message that sounded for its full length was heard"
        assert msg["ok"] == 2


async def test_a_panel_that_just_reached_the_box_is_not_reported_as_never_having(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """ZERO IS THE HEALTHIEST ANSWER AND -1 IS THE WORST, so a reader that confuses them inverts
    the field.

    `box_quiet_s` is seconds since the panel last reached this box, with -1 for never. The first
    version of this route read it as `report.get("box_quiet_s", -1) or -1` — the obvious
    spelling, and wrong, because 0 is falsy: a panel that reached the box THIS INSTANT was
    reported as one that never had. That is exactly the confusion -1 was chosen to prevent,
    reintroduced in the reader rather than the writer, and it would have shown a perfectly
    healthy panel as the most broken thing on the page."""
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        pk, dbg = await _panel_and_debug(maker, client)
        client.cookies.clear()
        assert (
            client.post(
                "/api/endpoint/telemetry",
                headers=pk,
                json={"version": "0.3.33", "uptime_ms": 5_000, "box_quiet_s": 0},
            ).status_code
            == 204
        )
        panel = client.get("/api/debug/endpoint/reach", headers=dbg).json()["panels"][0]
        assert panel["box_quiet_s"] == 0, (
            "a panel that reached the box a moment ago is being reported as one that never has"
        )


async def test_a_panel_too_old_to_report_its_reach_is_not_reported_as_healthy(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """A FLEET UPGRADES ONE PANEL AT A TIME, so this route will be read against panels that
    predate every field on it. Saying "0 seconds since it last reached the box" about a panel
    that has told us nothing of the kind is the one answer worse than saying nothing."""
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        pk, dbg = await _panel_and_debug(maker, client)
        client.cookies.clear()
        assert (
            client.post(
                "/api/endpoint/telemetry",
                headers=pk,
                json={"version": "0.3.32", "uptime_ms": 1000},
            ).status_code
            == 204
        )

        panel = client.get("/api/debug/endpoint/reach", headers=dbg).json()["panels"][0]
        assert panel["box_quiet_s"] == -1, "never told is never, not just now"
        assert all(p["fails"] == 0 and p["err"] == "" for p in panel["paths"]), (
            "an older panel has no faults to report, which is not the same as having none"
        )


async def test_a_second_raise_is_a_different_number(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """MONOTONIC, NOT A TOGGLE. A panel posts on CHANGE, so a counter that ever repeated a
    value it had already served would be a request that silently did nothing — which is the
    failure mode this whole mechanism exists to avoid, since the caller is measuring and would
    read the previous ring as the new one."""
    debug_key, _ = await service.mint_capability(SqlAuthRepo(maker), "claude", ttl_hours=1)
    # `debug_access_enabled`, WITHOUT WHICH THE ROUTER IS NOT MOUNTED AT ALL and every
    # call here is a 404 rather than the refusal or the answer it is asserting. The
    # debug surface is opt-in per deployment (`main.py`), so a test that forgets the
    # flag is testing an app that has no debug API — which is exactly what this file
    # did on its first CI run.
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        dbg = {"Authorization": f"Bearer {debug_key}"}
        seen = [
            client.post("/api/debug/endpoint/report-now", headers=dbg).json()["telemetry_seq"]
            for _ in range(3)
        ]
    assert seen == [1, 2, 3], f"the counter must only ever go up; got {seen}"


async def test_the_debug_routes_are_not_reachable_without_the_token(
    database_url: str,  # noqa: F811
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """`report-now` WRITES, and both routes read every panel's telemetry. A capability token
    is on a physically distinct path from the owner cookie, so this gate is the only one."""
    # `debug_access_enabled`, WITHOUT WHICH THE ROUTER IS NOT MOUNTED AT ALL and every
    # call here is a 404 rather than the refusal or the answer it is asserting. The
    # debug surface is opt-in per deployment (`main.py`), so a test that forgets the
    # flag is testing an app that has no debug API — which is exactly what this file
    # did on its first CI run.
    app = create_app(
        Settings(secure_cookies=False, database_url=database_url, debug_access_enabled=True)
    )
    with TestClient(app) as client:
        assert client.get("/api/debug/endpoint/heard").status_code == 401
        assert client.post("/api/debug/endpoint/report-now").status_code == 401
        bad = {"Authorization": "Bearer nonsense"}
        assert client.get("/api/debug/endpoint/heard", headers=bad).status_code == 401
        assert client.post("/api/debug/endpoint/report-now", headers=bad).status_code == 401
