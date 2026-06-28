"""External-LLM sessions: the owner CRUD + the token-gated public proxy.

The service contract (mint binds a secret, the on/off toggle and revoke fail auth
closed, usage accrues) runs against the fake repo; the proxy's security gates
(bad/disabled token → 401, coder not loaded → 503) and a metered happy path run
against the real app with the upstream shim faked.
"""

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import external_llm
from jbrain.auth import service as auth_service
from jbrain.config import Settings
from jbrain.main import create_app
from tests.unit.fakes import FakeAuthRepo

_DB = "postgresql+asyncpg://nobody@localhost:1/none"
_COOKIE = "jbrain_session"


# --- service contract (fake repo) ---


@pytest.mark.asyncio
async def test_mint_then_authenticate_and_meter() -> None:
    repo = FakeAuthRepo()
    key, record = await auth_service.mint_external_llm(repo, "Sarah's box", ttl_hours=None)
    assert key and record.label == "Sarah's box" and record.enabled
    assert record.expires_at is None  # None ttl = long-lived

    principal = await auth_service.authenticate_external_llm(repo, key)
    assert principal is not None and principal.kind == "external_llm"

    await auth_service.record_external_usage(repo, record.id, 120, 45)
    await auth_service.record_external_usage(repo, record.id, 30, 5)
    listed = {s.id: s for s in await repo.list_external_llm()}[record.id]
    assert (listed.in_tokens, listed.out_tokens, listed.requests) == (150, 50, 2)


@pytest.mark.asyncio
async def test_off_toggle_and_revoke_fail_auth_closed() -> None:
    repo = FakeAuthRepo()
    key, record = await auth_service.mint_external_llm(repo, "x", ttl_hours=None)
    assert await auth_service.authenticate_external_llm(repo, key) is not None

    # Toggle OFF → auth fails; toggle back ON → auth works again.
    assert await repo.set_external_llm_enabled(record.id, False) is True
    assert await auth_service.authenticate_external_llm(repo, key) is None
    assert await repo.set_external_llm_enabled(record.id, True) is True
    assert await auth_service.authenticate_external_llm(repo, key) is not None

    # Revoke → permanently dead.
    assert await repo.revoke_external_llm(record.id) is True
    assert await auth_service.authenticate_external_llm(repo, key) is None
    assert record.id not in {s.id for s in await repo.list_external_llm()}


@pytest.mark.asyncio
async def test_wrong_kind_key_never_authenticates_as_external() -> None:
    repo = FakeAuthRepo()
    owner_key = await auth_service.rotate_owner_key(repo)
    assert await auth_service.authenticate_external_llm(repo, owner_key) is None
    assert await auth_service.authenticate_external_llm(repo, "") is None


# --- the usage parser (pure) ---


def test_usage_from_chunks_parses_json_and_sse() -> None:
    whole = b'{"type":"message","usage":{"input_tokens":11,"output_tokens":22}}'
    assert external_llm._usage_from_chunks([whole]) == (11, 22)

    sse = (
        b"event: message_start\n"
        b'data: {"message":{"usage":{"input_tokens":7,"output_tokens":1}}}\n\n'
        b"event: message_delta\n"
        b'data: {"usage":{"output_tokens":40}}\n\n'
    )
    # input from message_start, output is the running max from message_delta.
    assert external_llm._usage_from_chunks([sse]) == (7, 40)
    assert external_llm._usage_from_chunks([b"not json"]) == (0, 0)


def test_usage_from_chunks_parses_openai_shape() -> None:
    # OpenAI names the fields differently and carries the full usage on a final chunk.
    whole = b'{"choices":[],"usage":{"prompt_tokens":33,"completion_tokens":99}}'
    assert external_llm._usage_from_chunks([whole]) == (33, 99)

    sse = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":34}}\n\n'
        b"data: [DONE]\n\n"
    )
    assert external_llm._usage_from_chunks([sse]) == (12, 34)


# --- routes (real app, faked upstream) ---


class _FakeGateway:
    def __init__(self, resident: set[str]) -> None:
        self._resident = resident

    async def running(self) -> set[str]:
        return self._resident


@pytest.fixture
def app_repo() -> Iterator[tuple[FastAPI, FakeAuthRepo]]:
    app = create_app(
        Settings(
            secure_cookies=False,
            database_url=_DB,
            session_cookie=_COOKIE,
            jcode_model="qwen3-coder-next",
            jcode_shim_url="http://shim:4000",
            jcode_gateway_token="sk-test",
            public_base_url="https://box.example",
        )
    )
    repo = FakeAuthRepo()
    with TestClient(app):
        app.state.auth_repo = repo
        app.state.local_gateway = _FakeGateway({"qwen3-coder-next"})
        yield app, repo


def _owner(app: FastAPI, repo: FakeAuthRepo) -> TestClient:
    client = TestClient(app)
    key = asyncio.run(auth_service.rotate_owner_key(repo))
    assert (
        client.post("/api/auth/session", json={"owner_key": key, "device_label": "t"})
    ).status_code == 204
    return client


def test_owner_mint_list_toggle_revoke(app_repo: tuple[FastAPI, FakeAuthRepo]) -> None:
    app, repo = app_repo
    owner = _owner(app, repo)
    minted = owner.post("/api/jcode/external", json={"label": "Remote"}).json()
    assert minted["token"] and minted["url"] == f"https://box.example/api/ext/llm/{minted['id']}"
    sid = minted["id"]

    listed = owner.get("/api/jcode/external").json()
    assert [s["id"] for s in listed] == [sid] and listed[0]["enabled"] is True
    assert "token" not in listed[0]

    assert (
        owner.post(f"/api/jcode/external/{sid}/enabled", json={"enabled": False}).status_code == 200
    )
    assert owner.get("/api/jcode/external").json()[0]["enabled"] is False
    assert owner.delete(f"/api/jcode/external/{sid}").status_code == 204
    assert owner.get("/api/jcode/external").json() == []


def test_management_is_owner_only(app_repo: tuple[FastAPI, FakeAuthRepo]) -> None:
    app, _ = app_repo
    anon = TestClient(app)
    assert anon.post("/api/jcode/external", json={}).status_code == 401
    assert anon.get("/api/jcode/external").status_code == 401


def test_proxy_rejects_bad_or_disabled_token(app_repo: tuple[FastAPI, FakeAuthRepo]) -> None:
    app, repo = app_repo
    owner = _owner(app, repo)
    sid = owner.post("/api/jcode/external", json={}).json()["id"]
    caller = TestClient(app)

    # No / wrong token → 401.
    body = {"model": "ignored", "messages": []}
    assert caller.post(f"/api/ext/llm/{sid}/v1/messages", json=body).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert caller.post(f"/api/ext/llm/{sid}/v1/messages", json=body, headers=bad).status_code == 401


def test_proxy_503_when_coder_not_loaded(app_repo: tuple[FastAPI, FakeAuthRepo]) -> None:
    app, repo = app_repo
    owner = _owner(app, repo)
    minted = owner.post("/api/jcode/external", json={}).json()
    app.state.local_gateway = _FakeGateway(set())  # nothing resident
    caller = TestClient(app)
    auth = {"Authorization": f"Bearer {minted['token']}"}
    r = caller.post(f"/api/ext/llm/{minted['id']}/v1/messages", json={"messages": []}, headers=auth)
    assert r.status_code == 503


def test_proxy_pins_model_forwards_and_meters(
    app_repo: tuple[FastAPI, FakeAuthRepo], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, repo = app_repo
    owner = _owner(app, repo)
    minted = owner.post("/api/jcode/external", json={}).json()
    sent: dict[str, object] = {}

    # Fake the upstream shim: capture the forwarded body, return a usage-bearing JSON.
    class _FakeStream:
        def __init__(self, payload: object, headers: dict[str, str]) -> None:
            sent["payload"] = payload
            sent["headers"] = headers

        async def __aenter__(self) -> "_FakeStream":
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def aiter_raw(self):  # noqa: ANN202
            yield b'{"type":"message","usage":{"input_tokens":100,"output_tokens":250}}'

    class _FakeClient:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def stream(self, _method: str, _path: str, *, json: object, headers: dict[str, str]):  # noqa: ANN202
            return _FakeStream(json, headers)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(external_llm.httpx, "AsyncClient", _FakeClient)
    caller = TestClient(app)
    auth = {"Authorization": f"Bearer {minted['token']}"}
    r = caller.post(
        f"/api/ext/llm/{minted['id']}/v1/messages",
        json={
            "model": "whatever-the-caller-asked",
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers=auth,
    )
    assert r.status_code == 200
    # The model was PINNED to the on-box coder, not the caller's choice; shim auth attached.
    assert sent["payload"]["model"] == "qwen3-coder-next"  # type: ignore[index]
    assert sent["headers"]["Authorization"] == "Bearer sk-test"  # type: ignore[index]
    # Usage was metered onto the session.
    listed = owner.get("/api/jcode/external").json()[0]
    assert (listed["in_tokens"], listed["out_tokens"], listed["requests"]) == (100, 250, 1)


def test_openai_chat_completions_forwards_pins_and_meters(
    app_repo: tuple[FastAPI, FakeAuthRepo], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, repo = app_repo
    owner = _owner(app, repo)
    minted = owner.post("/api/jcode/external", json={}).json()
    sent: dict[str, object] = {}

    class _FakeStream:
        def __init__(self, path: str, payload: object) -> None:
            sent["path"] = path
            sent["payload"] = payload

        async def __aenter__(self) -> "_FakeStream":
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def aiter_raw(self):  # noqa: ANN202
            yield b'{"choices":[],"usage":{"prompt_tokens":40,"completion_tokens":60}}'

    class _FakeClient:
        def __init__(self, *a: object, **k: object) -> None:
            pass

        def stream(self, _method: str, path: str, *, json: object, headers: dict[str, str]):  # noqa: ANN202
            return _FakeStream(path, json)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(external_llm.httpx, "AsyncClient", _FakeClient)
    caller = TestClient(app)
    auth = {"Authorization": f"Bearer {minted['token']}"}
    r = caller.post(
        f"/api/ext/llm/{minted['id']}/v1/chat/completions",
        json={"model": "gpt-whatever", "messages": [{"role": "user", "content": "hi"}]},
        headers=auth,
    )
    assert r.status_code == 200
    # Forwarded to the OpenAI route on the shim, pinned to the on-box coder.
    assert sent["path"] == "/v1/chat/completions"
    assert sent["payload"]["model"] == "qwen3-coder-next"  # type: ignore[index]
    # OpenAI-shaped usage was metered onto the session.
    listed = owner.get("/api/jcode/external").json()[0]
    assert (listed["in_tokens"], listed["out_tokens"], listed["requests"]) == (40, 60, 1)


def test_openai_models_lists_pinned_coder_and_is_gated(
    app_repo: tuple[FastAPI, FakeAuthRepo],
) -> None:
    app, repo = app_repo
    owner = _owner(app, repo)
    minted = owner.post("/api/jcode/external", json={}).json()
    caller = TestClient(app)

    # No token → 401.
    assert caller.get(f"/api/ext/llm/{minted['id']}/v1/models").status_code == 401
    # With the bearer → advertises only the pinned coder.
    auth = {"Authorization": f"Bearer {minted['token']}"}
    body = caller.get(f"/api/ext/llm/{minted['id']}/v1/models", headers=auth).json()
    assert [m["id"] for m in body["data"]] == ["qwen3-coder-next"]
