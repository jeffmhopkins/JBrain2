"""The PUBLIC jlaunch results-share surface: token resolve, hardening headers, rate limit,
and the artifact download (no DB — the share lookups are monkeypatched to canned rows)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import jlaunch_share
from jbrain.external import jlaunch_shares
from jbrain.locations.ratelimit import TokenBucket
from jbrain.models.jlaunch import JlaunchRunRow


def _run(sha: str = "sha1", name: str = "es_1e12_artifacts.tar.gz") -> JlaunchRunRow:
    return JlaunchRunRow(
        id="job1",
        spec_name="erdos_straus_1e12",
        title="Erdos-Straus census to 10^12",
        repo="https://github.com/jeffmhopkins/Erd-s-Straus-attack",
        state="succeeded",
        phase="headline",
        verify_ok=True,
        headline=["hard primes: 42"],
        machine={"cpu": "Fake", "cores": 8},
        phase_times=[{"phase": "run", "seconds": 5}],
        error="",
        artifact_name=name,
        artifact_sha256=sha,
        artifact_bytes=13,
        created_at="2026-08-03T00:00:00+00:00",
        last_active_at="2026-08-03T06:00:00+00:00",
    )


class _Blobs:
    def __init__(self, path: Path) -> None:
        self._path = path

    def path_for(self, _sha: str) -> Path:
        return self._path


def _app(*, limiter: TokenBucket, blobs: object = None) -> FastAPI:
    app = FastAPI()
    app.include_router(jlaunch_share.router, prefix="/api")
    app.state.session_maker = object()  # opaque; the share funcs are monkeypatched
    app.state.blob_store = blobs
    app.state.jlaunch_share_rate_limiter = limiter
    return app


def test_unknown_token_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(_maker, _token):
        return None

    monkeypatch.setattr(jlaunch_shares, "resolve_share", _none)
    client = TestClient(_app(limiter=TokenBucket(capacity=30, refill_per_sec=1.0)))
    assert client.get("/api/jlaunch-share/nope").status_code == 404


def test_view_returns_public_results_and_hardening(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(_maker, _token):
        return "link-1"

    async def _fetch(_maker, _link):
        return _run()

    monkeypatch.setattr(jlaunch_shares, "resolve_share", _resolve)
    monkeypatch.setattr(jlaunch_shares, "fetch_shared_run", _fetch)
    client = TestClient(_app(limiter=TokenBucket(capacity=30, refill_per_sec=1.0)))
    resp = client.get("/api/jlaunch-share/tok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"].startswith("Erdos-Straus")
    assert body["verify_ok"] is True
    assert body["headline"] == ["hard primes: 42"]
    assert body["has_artifact"] is True
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert "noindex" in resp.headers["X-Robots-Tag"]


def test_rate_limit_429(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(_maker, _token):
        return "link-1"

    async def _fetch(_maker, _link):
        return _run()

    monkeypatch.setattr(jlaunch_shares, "resolve_share", _resolve)
    monkeypatch.setattr(jlaunch_shares, "fetch_shared_run", _fetch)
    client = TestClient(_app(limiter=TokenBucket(capacity=1, refill_per_sec=0.0)))
    assert client.get("/api/jlaunch-share/tok").status_code == 200
    assert client.get("/api/jlaunch-share/tok").status_code == 429


def test_download_streams_artifact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blob = tmp_path / "blob"
    blob.write_bytes(b"ARTIFACT-BYTES")

    async def _resolve(_maker, _token):
        return "link-1"

    async def _fetch(_maker, _link):
        return _run(sha="sha1")

    monkeypatch.setattr(jlaunch_shares, "resolve_share", _resolve)
    monkeypatch.setattr(jlaunch_shares, "fetch_shared_run", _fetch)
    app = _app(limiter=TokenBucket(capacity=30, refill_per_sec=1.0), blobs=_Blobs(blob))
    client = TestClient(app)
    resp = client.get("/api/jlaunch-share/tok/artifact")
    assert resp.status_code == 200
    assert resp.content == b"ARTIFACT-BYTES"
    assert "es_1e12_artifacts.tar.gz" in resp.headers.get("content-disposition", "")


def test_download_404_when_no_artifact(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(_maker, _token):
        return "link-1"

    async def _fetch(_maker, _link):
        return _run(sha="")  # no collected artifact

    monkeypatch.setattr(jlaunch_shares, "resolve_share", _resolve)
    monkeypatch.setattr(jlaunch_shares, "fetch_shared_run", _fetch)
    client = TestClient(_app(limiter=TokenBucket(capacity=30, refill_per_sec=1.0)))
    assert client.get("/api/jlaunch-share/tok/artifact").status_code == 404
