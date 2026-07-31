"""The jcode SearXNG search bridge (api.jcode_llm web_search / web_fetch): shared-token
auth, pre-rendered text for the sandbox's shell helpers, and the fail modes. Runs the
router on a bare app with a fake searxng + fetcher (no network).

docs/plans/JCODE_GROK_INTERNET_PLAN.md
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jcode_llm
from jbrain.web.fetch import FetchResult, WebFetchError
from jbrain.web.search import SearchHit, WebSearchError

_AUTH = {"Authorization": "Bearer sk-test"}


class _FakeSearxng:
    def __init__(self, hits: list[SearchHit] | Exception) -> None:
        self._hits = hits
        self.calls: list[tuple[str, int]] = []

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        self.calls.append((query, limit))
        if isinstance(self._hits, Exception):
            raise self._hits
        return self._hits


class _FakeFetcher:
    def __init__(self, result: FetchResult | Exception) -> None:
        self._result = result
        self.calls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _app(
    *,
    token: str = "sk-test",
    searxng: object | None = "unset",
    fetcher: object | None = "unset",
    searxng_url: str = "http://searxng:8080",
) -> FastAPI:
    app = FastAPI()
    app.include_router(jcode_llm.router, prefix="/api")
    app.state.settings = SimpleNamespace(
        jcode_gateway_token=token,
        searxng_url=searxng_url,
    )
    if searxng != "unset":
        app.state.searxng = searxng
    if fetcher != "unset":
        app.state.web_fetcher = fetcher
    return app


def test_web_search_renders_hits_as_shell_text() -> None:
    hits = [
        SearchHit(title="Grok CLI", url="https://x.ai/cli", snippet="the official CLI"),
        SearchHit(title="SearXNG", url="https://searxng.org", snippet="metasearch"),
    ]
    fake = _FakeSearxng(hits)
    client = TestClient(_app(searxng=fake))
    resp = client.post("/api/jcode/llm/v1/web_search", headers=_AUTH, json={"query": "grok"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "Web results:" in resp.text
    assert "https://x.ai/cli" in resp.text and "metasearch" in resp.text
    assert fake.calls == [("grok", 6)]  # default limit


def test_web_search_clamps_limit_to_the_ceiling() -> None:
    fake = _FakeSearxng([])
    client = TestClient(_app(searxng=fake))
    client.post("/api/jcode/llm/v1/web_search", headers=_AUTH, json={"query": "q", "limit": 99})
    assert fake.calls == [("q", 10)]


def test_web_search_empty_results_is_a_plain_message() -> None:
    client = TestClient(_app(searxng=_FakeSearxng([])))
    resp = client.post("/api/jcode/llm/v1/web_search", headers=_AUTH, json={"query": "nada"})
    assert resp.status_code == 200
    assert "No web results" in resp.text


def test_web_search_requires_a_query() -> None:
    client = TestClient(_app(searxng=_FakeSearxng([])))
    resp = client.post("/api/jcode/llm/v1/web_search", headers=_AUTH, json={"query": "  "})
    assert resp.status_code == 400


def test_web_search_502_when_service_unavailable() -> None:
    client = TestClient(_app(searxng=_FakeSearxng(WebSearchError("down"))))
    resp = client.post("/api/jcode/llm/v1/web_search", headers=_AUTH, json={"query": "q"})
    assert resp.status_code == 502


def test_web_search_503_when_searxng_not_configured() -> None:
    # No client on app.state (searxng service absent) → the bridge is off.
    client = TestClient(_app(searxng=None, searxng_url=""))
    resp = client.post("/api/jcode/llm/v1/web_search", headers=_AUTH, json={"query": "q"})
    assert resp.status_code == 503


def test_web_search_rejects_a_bad_token() -> None:
    client = TestClient(_app(searxng=_FakeSearxng([])))
    resp = client.post(
        "/api/jcode/llm/v1/web_search",
        headers={"Authorization": "Bearer nope"},
        json={"query": "q"},
    )
    assert resp.status_code == 401


def test_web_fetch_renders_page_with_links() -> None:
    result = FetchResult(
        title="Example",
        url="https://example.com/final",
        text="the readable body",
        links=("https://example.com/a", "https://example.com/b"),
    )
    fake = _FakeFetcher(result)
    client = TestClient(_app(fetcher=fake))
    resp = client.post(
        "/api/jcode/llm/v1/web_fetch", headers=_AUTH, json={"url": "https://example.com"}
    )
    assert resp.status_code == 200
    assert "# Example" in resp.text and "the readable body" in resp.text
    assert "https://example.com/a" in resp.text
    assert fake.calls == ["https://example.com"]


def test_web_fetch_requires_a_url() -> None:
    client = TestClient(_app(fetcher=_FakeFetcher(WebFetchError("x"))))
    resp = client.post("/api/jcode/llm/v1/web_fetch", headers=_AUTH, json={})
    assert resp.status_code == 400


def test_web_fetch_502_on_fetch_error() -> None:
    client = TestClient(_app(fetcher=_FakeFetcher(WebFetchError("blocked"))))
    resp = client.post(
        "/api/jcode/llm/v1/web_fetch", headers=_AUTH, json={"url": "https://x.example"}
    )
    assert resp.status_code == 502


def test_web_fetch_503_when_fetcher_absent() -> None:
    client = TestClient(_app(fetcher=None))
    resp = client.post(
        "/api/jcode/llm/v1/web_fetch", headers=_AUTH, json={"url": "https://x.example"}
    )
    assert resp.status_code == 503
