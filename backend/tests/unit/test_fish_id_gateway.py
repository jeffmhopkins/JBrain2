"""The fishial admin client: best-effort status() with a loaded flag, and a surfaced
error on free(). All via httpx.MockTransport (no network, no GPU)."""

import httpx
import pytest

from jbrain.fish_id.gateway import (
    FishIdGatewayClient,
    FishIdGatewayError,
    _parse_loaded,
)


def _client(handler: object) -> FishIdGatewayClient:
    return FishIdGatewayClient(
        "http://fish-id:8200",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_status_reports_reachable_and_loaded() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/health"
        return httpx.Response(200, json={"loaded": True})

    status = await _client(handle).status()
    assert status.reachable and status.loaded is True


async def test_status_unreachable_on_http_error() -> None:
    status = await _client(lambda r: httpx.Response(503)).status()
    assert not status.reachable
    assert status.loaded is None


async def test_status_unreachable_when_connection_refused() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert not (await _client(boom).status()).reachable


async def test_status_reachable_but_no_loaded_field() -> None:
    status = await _client(lambda r: httpx.Response(200, json={})).status()
    assert status.reachable and status.loaded is None


async def test_free_posts_to_free() -> None:
    seen: list[tuple[str, str]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    await _client(handle).free()
    assert seen == [("POST", "/free")]


async def test_free_raises_on_gateway_failure() -> None:
    with pytest.raises(FishIdGatewayError):
        await _client(lambda r: httpx.Response(500)).free()


def test_parse_loaded_tolerates_bad_shapes() -> None:
    assert _parse_loaded(["not", "a", "dict"]) is None
    assert _parse_loaded({"loaded": "yes"}) is None
    assert _parse_loaded({"loaded": False}) is False
