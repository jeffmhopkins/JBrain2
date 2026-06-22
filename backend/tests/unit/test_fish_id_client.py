"""The fishial identify client: multipart POST, ranked parsing, score clamp, top_k
truncation, and clean FishIdError on every failure. All via httpx.MockTransport
(no network, no GPU)."""

import httpx
import pytest

from jbrain.fish_id.client import (
    MAX_TOP_K,
    FishIdError,
    FishMatch,
    FishResult,
    HttpFishIdentifier,
    _coerce_matches,
)

_OK = {
    "candidates": [
        {"species": "Zebrasoma flavescens", "common_name": "Yellow tang", "score": 0.92},
        {"species": "Acanthurus coeruleus", "common_name": "Blue tang", "score": 0.05},
    ]
}


def _identifier(handler: object, **kw: object) -> HttpFishIdentifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return HttpFishIdentifier("http://fish-id:8200", client, **kw)  # type: ignore[arg-type]


async def test_identify_posts_image_and_parses_ranked() -> None:
    seen: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        seen["top_k"] = req.url.params.get("top_k")
        seen["has_image"] = b"abc123" in req.content  # multipart body carries the bytes
        return httpx.Response(200, json=_OK)

    result = await _identifier(handle).identify(b"abc123", top_k=5)
    assert seen["path"] == "/identify"
    assert seen["top_k"] == "5"
    assert seen["has_image"] is True
    assert result.top == FishMatch("Zebrasoma flavescens", "Yellow tang", 0.92)
    assert [m.species for m in result.matches] == ["Zebrasoma flavescens", "Acanthurus coeruleus"]


async def test_identify_truncates_to_top_k() -> None:
    payload = {"candidates": [{"species": f"sp{i}", "score": 0.5} for i in range(8)]}
    result = await _identifier(lambda r: httpx.Response(200, json=payload)).identify(b"x", top_k=3)
    assert len(result.matches) == 3


async def test_identify_clamps_top_k_to_max() -> None:
    seen: dict[str, object] = {}

    def handle(req: httpx.Request) -> httpx.Response:
        seen["top_k"] = req.url.params.get("top_k")
        return httpx.Response(200, json=_OK)

    await _identifier(handle).identify(b"x", top_k=999)
    assert seen["top_k"] == str(MAX_TOP_K)


async def test_identify_raises_on_http_error() -> None:
    with pytest.raises(FishIdError):
        await _identifier(lambda r: httpx.Response(503)).identify(b"x")


async def test_identify_raises_on_non_json() -> None:
    with pytest.raises(FishIdError):
        await _identifier(lambda r: httpx.Response(200, text="not json")).identify(b"x")


async def test_identify_raises_when_connection_refused() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(FishIdError):
        await _identifier(boom).identify(b"x")


def test_coerce_clamps_score_and_skips_bad_rows() -> None:
    payload = {
        "candidates": [
            {"species": "Good sp", "score": 1.7},  # clamped to 1.0
            {"species": "", "score": 0.5},  # blank species — skipped
            {"species": "No score"},  # missing score — skipped
            {"species": "Bool score", "score": True},  # bool is not a score — skipped
            "not a dict",  # skipped
            {"species": "Low", "score": -0.2},  # clamped to 0.0
        ]
    }
    matches = _coerce_matches(payload, MAX_TOP_K)
    assert [(m.species, m.score) for m in matches] == [("Good sp", 1.0), ("Low", 0.0)]


def test_coerce_rejects_non_dict_and_missing_list() -> None:
    with pytest.raises(FishIdError):
        _coerce_matches(["nope"], 5)
    with pytest.raises(FishIdError):
        _coerce_matches({"no_candidates": []}, 5)


def test_result_top_is_none_when_empty() -> None:
    assert FishResult(()).top is None
