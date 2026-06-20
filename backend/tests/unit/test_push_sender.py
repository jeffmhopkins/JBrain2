"""The content-free poke sender (JBrain360 M6b). HTTP is faked with an httpx
MockTransport — CI never calls FCM. The headline gate is **no PII in the payload**:
a poke is a bare data-only wake-up, never a name/place/coordinate."""

import json

import httpx

from jbrain.push import NullNotifier, fcm_message
from jbrain.push.sender import FcmNotifier


def test_fcm_message_is_content_free() -> None:
    msg = fcm_message("device-token-123")
    # Data-only: there is NO `notification` block, so the OS renders nothing itself.
    assert "notification" not in msg["message"]
    assert msg["message"]["token"] == "device-token-123"
    # The data carries only the type hint — no subject, place, or coordinate.
    assert msg["message"]["data"] == {"type": "location_event"}
    # Belt-and-suspenders: nothing PII-shaped anywhere in the serialized body.
    blob = json.dumps(msg).lower()
    for leak in ("lat", "lon", "subject", "place", "name", "address", "battery"):
        assert leak not in blob


async def test_null_notifier_is_a_noop() -> None:
    await NullNotifier().poke(["a", "b"])  # no raise, no network


async def test_fcm_notifier_posts_a_content_free_message_per_token() -> None:
    sent: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"name": "projects/p/messages/1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        notifier = FcmNotifier(client, project_id="proj-1", access_token=_token("bearer-xyz"))
        await notifier.poke(["tok-a", "tok-b"])

    assert len(sent) == 2
    assert str(sent[0].url) == "https://fcm.googleapis.com/v1/projects/proj-1/messages:send"
    assert sent[0].headers["Authorization"] == "Bearer bearer-xyz"
    bodies = [json.loads(r.content) for r in sent]
    assert {b["message"]["token"] for b in bodies} == {"tok-a", "tok-b"}
    for b in bodies:
        assert b["message"]["data"] == {"type": "location_event"}
        assert "notification" not in b["message"]


async def test_fcm_notifier_survives_a_bad_token() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom")  # the first token errors
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        notifier = FcmNotifier(client, "p", _token("t"))
        await notifier.poke(["bad", "good"])  # must not raise

    assert calls["n"] == 2  # the second token was still attempted


def _token(value: str):  # noqa: ANN202 - tiny async closure helper
    async def provide() -> str:
        return value

    return provide
