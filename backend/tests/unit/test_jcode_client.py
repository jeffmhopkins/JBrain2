"""The jcode control-server client: JSON calls, stop/restart, error mapping, and the
in-memory fake used by the route tests."""

from __future__ import annotations

import httpx
import pytest

from jbrain.jcode import FakeJcodeClient, JcodeClient, JcodeError


async def test_fake_create_list_get_delete() -> None:
    fake = FakeJcodeClient()
    s = await fake.create_session("github.com/me/r", "main", "")
    assert s["id"] == "sess1"
    assert s["work_branch"] == "jcode/sess1"
    assert [x["id"] for x in await fake.list_sessions()] == ["sess1"]
    assert (await fake.get_session("sess1"))["repo"] == "github.com/me/r"
    await fake.delete("sess1")
    with pytest.raises(JcodeError):
        await fake.get_session("sess1")


async def test_fake_stop_and_restart() -> None:
    fake = FakeJcodeClient()
    s = await fake.create_session("r", "main", "")
    assert (await fake.stop(s["id"]))["status"] == "stopped"
    assert (await fake.restart(s["id"]))["status"] == "ready"


async def test_client_stop_and_restart_post_to_the_control_server() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"id": "s1", "status": "stopped"})

    client = JcodeClient("http://jcode:9100", "tok", transport=httpx.MockTransport(handler))
    await client.stop("s1")
    await client.restart("s1")
    assert seen == ["POST /sessions/s1/stop", "POST /sessions/s1/restart"]


async def test_client_create_session_posts_typed_body() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = request.content
        return httpx.Response(201, json={"id": "s1", "repo": "r", "status": "ready"})

    client = JcodeClient("http://jcode:9100", "tok", transport=httpx.MockTransport(handler))
    result = await client.create_session("r", "main", "")
    assert result["id"] == "s1"
    assert seen["url"] == "http://jcode:9100/sessions"


async def test_client_maps_http_error_to_jcode_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = JcodeClient("http://jcode:9100", "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(JcodeError):
        await client.get_session("s1")


async def test_client_delete_swallows_404_already_gone() -> None:
    # The control server lost the session (idle-reaped, or a restart wiped its
    # in-memory index) but the launcher row persists. Delete must succeed so the
    # route can drop the durable row — a 404 here is the desired end state.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        return httpx.Response(404, json={"detail": "unknown session: s1"})

    client = JcodeClient("http://jcode:9100", "tok", transport=httpx.MockTransport(handler))
    await client.delete("s1")  # does not raise


async def test_client_delete_raises_on_other_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = JcodeClient("http://jcode:9100", "tok", transport=httpx.MockTransport(handler))
    with pytest.raises(JcodeError):
        await client.delete("s1")


async def test_unconfigured_client_raises() -> None:
    client = JcodeClient("", "tok")
    with pytest.raises(JcodeError):
        await client.list_sessions()
