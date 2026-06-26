"""The jcode control-server client: SSE frame parsing, JSON calls, error mapping,
and the in-memory fake used by the route tests."""

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
    await fake.cancel("sess1")
    assert fake.cancelled == ["sess1"]
    await fake.delete("sess1")
    with pytest.raises(JcodeError):
        await fake.get_session("sess1")


async def test_client_stream_turn_yields_one_frame_per_event() -> None:
    body = b'data: {"type": "text", "text": "hi"}\n\ndata: {"type": "done"}\n\n'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = JcodeClient("http://jcode:9100", "tok", transport=httpx.MockTransport(handler))
    frames = [f async for f in client.stream_turn("s1", "do it")]
    assert frames == [
        b'data: {"type": "text", "text": "hi"}\n\n',
        b'data: {"type": "done"}\n\n',
    ]


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


async def test_unconfigured_client_raises() -> None:
    client = JcodeClient("", "tok")
    with pytest.raises(JcodeError):
        await client.list_sessions()
