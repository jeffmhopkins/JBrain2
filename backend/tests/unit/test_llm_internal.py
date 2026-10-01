"""The worker's pre-load config re-stamp, delegated to the api (jbrain.llm.gateway_regen,
jbrain.api.llm_internal): the bearer, the no-body rule, the process-level serialisation, and the
worker-side client."""

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jbrain.api import llm_settings
from jbrain.config import Settings
from jbrain.llm import gateway_regen
from jbrain.main import create_app
from tests.unit.fakes import FakeSettingsStore

_URL = gateway_regen.REGEN_PATH


def _settings(**kw: Any) -> Settings:
    kw.setdefault("secure_cookies", False)
    kw.setdefault("database_url", "postgresql+asyncpg://nobody@localhost:1/none")
    kw.setdefault("supervisor_token", "sek")
    return Settings(**kw)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    async def _fake(_settings: Any, _store: Any) -> None:
        seen.append("regen")

    monkeypatch.setattr(llm_settings, "_regen_gateway_config", _fake)
    monkeypatch.setattr(llm_settings, "_last_regen_error", None)
    return seen


def _client(**kw: Any) -> Iterator[TestClient]:
    app = create_app(_settings(**kw))
    with TestClient(app) as client:
        app.state.settings_store = FakeSettingsStore()
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from _client()


def _bearer(token: str = "sek") -> dict[str, str]:
    return {"Authorization": f"Bearer {gateway_regen.regen_token(token)}"}


def test_the_route_re_stamps_for_a_holder_of_the_derived_bearer(
    client: TestClient, calls: list[str]
) -> None:
    resp = client.post(_URL, headers=_bearer())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"error": None}
    assert calls == ["regen"]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer nope"},
        # The raw supervisor token is not the bearer: the route accepts only the derived one.
        {"Authorization": "Bearer sek"},
        {"Authorization": gateway_regen.regen_token("sek")},
        {"Authorization": f"Bearer {gateway_regen.regen_token('other')}"},
    ],
)
def test_the_route_refuses_without_the_bearer(
    client: TestClient, calls: list[str], headers: dict[str, str]
) -> None:
    assert client.post(_URL, headers=headers).status_code == 403
    assert calls == []


def test_the_route_fails_closed_with_no_secret(calls: list[str]) -> None:
    for client in _client(supervisor_token=""):
        assert client.post(_URL).status_code == 403
        assert client.post(_URL, headers={"Authorization": "Bearer "}).status_code == 403
    assert calls == []


def test_the_route_takes_no_body(client: TestClient, calls: list[str]) -> None:
    resp = client.post(_URL, headers=_bearer(), content=json.dumps({"windows": {"x": 1}}))
    assert resp.status_code == 422
    assert calls == []


def test_the_route_is_not_under_the_public_api_prefix(client: TestClient) -> None:
    assert client.post("/api/llm/regen-gateway-config", headers=_bearer()).status_code == 404


def test_the_route_reports_a_failed_re_stamp(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fail(_settings: Any, _store: Any) -> None:
        llm_settings._set_regen_error("read-only file system")

    monkeypatch.setattr(llm_settings, "_regen_gateway_config", _fail)
    resp = client.post(_URL, headers=_bearer())
    assert resp.json() == {"error": "read-only file system"}
    llm_settings._set_regen_error(None)


@pytest.mark.asyncio
async def test_re_stamps_never_interleave(monkeypatch: pytest.MonkeyPatch) -> None:
    """The api's own loads and the worker's requests share one lock: a second re-stamp starts
    only after the first (and its settle) finished."""
    log: list[str] = []

    async def _slow(_settings: Any, _store: Any) -> None:
        log.append("start")
        await asyncio.sleep(0.02)
        log.append("end")

    monkeypatch.setattr(llm_settings, "_regen_gateway_config", _slow)
    monkeypatch.setattr(llm_settings, "_REGEN_LOCK", asyncio.Lock())
    await asyncio.gather(
        *(llm_settings.regen_gateway_config(None, None) for _ in range(3))  # type: ignore[arg-type]
    )
    assert log == ["start", "end"] * 3


# --- the worker-side client ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_worker_client_posts_the_derived_bearer_and_no_body() -> None:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"error": None})

    await gateway_regen.request_regen(
        "http://api:8000/", "sek", transport=httpx.MockTransport(_handler)
    )
    (req,) = seen
    assert str(req.url) == "http://api:8000/internal/llm/regen-gateway-config"
    assert req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {gateway_regen.regen_token('sek')}"
    assert req.content == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(403, json={"detail": "forbidden"}),
        httpx.Response(500),
        httpx.Response(200, json={"error": "stale"}),
    ],
)
async def test_the_worker_client_raises_on_a_failed_re_stamp(response: httpx.Response) -> None:
    # The gateway client catches this, logs `config_regen_failed` and loads anyway — the same
    # handling a failed local re-stamp gets.
    with pytest.raises((httpx.HTTPError, RuntimeError)):
        await gateway_regen.request_regen(
            "http://api:8000", "sek", transport=httpx.MockTransport(lambda _r: response)
        )


@pytest.mark.asyncio
async def test_the_worker_client_needs_a_secret() -> None:
    with pytest.raises(RuntimeError):
        await gateway_regen.request_regen("http://api:8000", "")


def test_the_bearer_is_derived_not_the_raw_secret() -> None:
    assert gateway_regen.regen_token("") == ""
    token = gateway_regen.regen_token("sek")
    assert token and token != "sek" and token == gateway_regen.regen_token("sek")
