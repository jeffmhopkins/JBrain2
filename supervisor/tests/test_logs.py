"""Logs endpoints: tail defaults and cap, 404s, and SSE framing."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import AUTH, FakeGateway


def test_logs_returns_plaintext_lines(client: TestClient) -> None:
    response = client.get("/logs/api", headers=AUTH)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "line one\nline two\nline three"


def test_logs_default_tail_is_200(client: TestClient, gateway: FakeGateway) -> None:
    client.get("/logs/api", headers=AUTH)

    assert gateway.log_requests == [("api", 200)]


def test_logs_respects_tail_param(client: TestClient, gateway: FakeGateway) -> None:
    response = client.get("/logs/api", params={"tail": 2}, headers=AUTH)

    assert response.text == "line two\nline three"
    assert gateway.log_requests == [("api", 2)]


def test_logs_tail_capped_at_2000(client: TestClient, gateway: FakeGateway) -> None:
    response = client.get("/logs/api", params={"tail": 5000}, headers=AUTH)

    assert response.status_code == 200
    assert gateway.log_requests == [("api", 2000)]


def test_logs_unknown_service_is_404(client: TestClient) -> None:
    assert client.get("/logs/nope", headers=AUTH).status_code == 404


def test_stream_emits_sse_events(client: TestClient) -> None:
    with client.stream("GET", "/logs/api/stream", headers=AUTH) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert body == "data: line one\n\ndata: line two\n\ndata: line three\n\n"


def test_stream_unknown_service_is_404(client: TestClient) -> None:
    assert client.get("/logs/nope/stream", headers=AUTH).status_code == 404
