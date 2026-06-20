"""The ComfyUI admin client: best-effort status() with real VRAM parsing, and a
surfaced error on free(). All via httpx.MockTransport (no network, no GPU)."""

import httpx
import pytest

from jbrain.image_gen.gateway import (
    ComfyUiGatewayClient,
    ComfyUiGatewayError,
    _parse_vram,
)

GB = 1024**3


def _client(handler: object) -> ComfyUiGatewayClient:
    return ComfyUiGatewayClient(
        "http://comfyui:8188",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


async def test_status_reports_reachable_and_real_vram() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/system_stats"
        return httpx.Response(
            200,
            json={"devices": [{"name": "cuda:0", "vram_total": 128 * GB, "vram_free": 96 * GB}]},
        )

    status = await _client(handle).status()
    assert status.reachable
    assert status.vram_total_gb == 128.0 and status.vram_free_gb == 96.0


async def test_status_sums_multiple_devices() -> None:
    payload = {
        "devices": [
            {"vram_total": 8 * GB, "vram_free": 2 * GB},
            {"vram_total": 8 * GB, "vram_free": 6 * GB},
        ]
    }
    status = await _client(lambda r: httpx.Response(200, json=payload)).status()
    assert status.vram_total_gb == 16.0 and status.vram_free_gb == 8.0


async def test_status_unreachable_on_http_error() -> None:
    status = await _client(lambda r: httpx.Response(503)).status()
    assert not status.reachable
    assert status.vram_total_gb is None and status.vram_free_gb is None


async def test_status_unreachable_when_connection_refused() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert not (await _client(boom).status()).reachable


async def test_status_reachable_but_no_vram_fields() -> None:
    # An older build that omits device VRAM: reachable, but the meter stays blank.
    status = await _client(lambda r: httpx.Response(200, json={"system": {}})).status()
    assert status.reachable
    assert status.vram_total_gb is None and status.vram_free_gb is None


async def test_free_posts_the_unload_flags() -> None:
    seen: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        import json

        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    await _client(handle).free()
    assert seen["path"] == "/free"
    assert seen["body"] == {"unload_models": True, "free_memory": True}


async def test_free_raises_on_gateway_failure() -> None:
    with pytest.raises(ComfyUiGatewayError):
        await _client(lambda r: httpx.Response(500)).free()


async def test_interrupt_posts_to_interrupt() -> None:
    seen: list[tuple[str, str]] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200)

    await _client(handle).interrupt()
    assert seen == [("POST", "/interrupt")]


async def test_interrupt_raises_on_gateway_failure() -> None:
    with pytest.raises(ComfyUiGatewayError):
        await _client(lambda r: httpx.Response(500)).interrupt()


def test_parse_vram_tolerates_bad_shapes() -> None:
    assert _parse_vram(["not", "a", "dict"]) == (None, None)
    assert _parse_vram({"devices": "nope"}) == (None, None)
    assert _parse_vram({"devices": [{"vram_total": "x", "vram_free": 1}]}) == (None, None)
