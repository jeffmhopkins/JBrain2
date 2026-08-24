"""The 1f916.ai typed client (docs/plans/F1916_CITIZENSHIP_PLAN.md W1) — pinned host,
whitelisted routes, bearer injected server-side from the live provider, redirects
refused, bodies bounded. HTTP is faked via MockTransport (no live network)."""

import json

import httpx
import pytest

from jbrain.web.f1916 import F1916Client, F1916Creds, F1916Error, find_secret

SECRET = "1f916_sk_live_abc123"


def _provider(secret: str = SECRET, enabled: bool = True):  # type: ignore[no-untyped-def]
    async def creds() -> F1916Creds:
        return F1916Creds(enabled=enabled, handle="jerv", secret=secret)

    return creds


def _client(handler, secret: str = SECRET) -> F1916Client:  # type: ignore[no-untyped-def]
    return F1916Client(
        "https://1f916.ai", creds=_provider(secret), transport=httpx.MockTransport(handler)
    )


async def test_keyless_reads_carry_no_credential() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request
        return httpx.Response(200, json={"now_utc": "2026-08-24", "posts": []})

    client = _client(handler)
    await client.front(limit=5)
    await client.search("hello")
    await client.post(7)
    await client.citizen("@smith")
    await client.changes(since=123)
    await client.events(kind="moderation")
    # Even with a secret registered, public reads never send the bearer — nothing to
    # leak on a route that doesn't need it.
    for req in seen.values():
        assert "authorization" not in {k.lower() for k in req.headers}
    assert seen["/api/search"].url.params["q"] == "hello"
    assert "/api/citizen/smith" in seen  # the leading @ is shed before the path


async def test_me_and_pulse_inject_the_bearer_from_the_live_provider() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        calls.append(request.url.path)
        return httpx.Response(200, json={"replies": []})

    client = _client(handler)
    await client.me()
    await client.pulse()
    assert calls == ["/api/me", "/api/pulse"]


async def test_me_without_a_registered_secret_refuses_before_the_wire() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request may leave without a secret")

    with pytest.raises(F1916Error, match="no 1f916 citizen"):
        await _client(handler, secret="").me()


async def test_a_redirect_is_refused_never_followed() -> None:
    # A 3xx from the pinned host is out of contract: following it could carry the
    # bearer to another origin, so the client refuses instead.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    with pytest.raises(F1916Error) as exc:
        await _client(handler).pulse()
    assert exc.value.status == 302


async def test_an_oversized_body_is_refused_not_buffered() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2_000_000)

    with pytest.raises(F1916Error, match="oversized"):
        await _client(handler).front()


async def test_the_platforms_error_prose_rides_back_with_its_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "No credentials: register first."})

    with pytest.raises(F1916Error, match="No credentials") as exc:
        await _client(handler).me()
    assert exc.value.status == 401


async def test_register_posts_the_key_bind_fields_and_returns_the_full_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST" and request.url.path == "/api/register"
        body = json.loads(request.content)
        assert body == {
            "handle": "jerv",
            "model": "gpt-oss-120b",
            "public_key": "pubkey",
            "signature": "sig",
        }
        return httpx.Response(200, json={"citizen": {"secret_key": "1f916_sk_new"}})

    out = await _client(handler).register(
        handle="jerv", model="gpt-oss-120b", public_key="pubkey", signature="sig"
    )
    assert find_secret(out) == "1f916_sk_new"


async def test_rotate_authenticates_with_the_current_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/rotate"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        return httpx.Response(200, json={"new_secret": "1f916_sk_rotated"})

    assert find_secret(await _client(handler).rotate()) == "1f916_sk_rotated"


def test_find_secret_scans_by_shape_not_field_name() -> None:
    # The platform documents the prefix, not the field — a rename upstream must not
    # orphan a just-minted citizen.
    assert find_secret({"deep": {"list": [{"whatever": "1f916_sk_x"}]}}) == "1f916_sk_x"
    assert find_secret({"note": "keep your 1f916 secret safe"}) == ""
    assert find_secret([1, None, "1f916_sk_y"]) == "1f916_sk_y"


async def test_non_json_and_non_object_bodies_are_typed_errors() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>cloudflare</html>")

    with pytest.raises(F1916Error, match="non-JSON"):
        await _client(html).front()

    def array(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    with pytest.raises(F1916Error, match="unexpected"):
        await _client(array).front()


async def test_network_failure_is_a_typed_unreachable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(F1916Error, match="unreachable"):
        await _client(handler).front()
