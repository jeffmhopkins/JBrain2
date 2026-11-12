"""The GrowBot call log (jbrain.growbot.calls, api/growbot_calls.py, and the model lane's
writes in api/growbot_llm.py): pictures kept as their size, the reply rebuilt from a stream
or a body, the stage timings, the sidecar's route behind `growbot.token`, and the debug reads.
A fake log and a faked gateway: no database, no model.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jbrain.api import growbot_calls, growbot_llm
from jbrain.api.deps import current_debug_principal
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.growbot.calls import (
    TEXT_MAX,
    CallRecord,
    LoggedCall,
    ReplyCollector,
    redact_images,
)
from jbrain.llm import prefill
from jbrain.storage import FsInternalSecrets
from tests.unit.test_growbot_llm import _COMPLETIONS, _IMAGE, _VL, _chat, _Gateway, _Residency

_PERSONA = "0b7c2f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
DEBUG = PrincipalInfo(id="cap1", kind="capability_token", label="token")


class _Log:
    def __init__(self, fail: bool = False) -> None:
        self.rows: list[tuple[SessionContext, CallRecord]] = []
        self.fail = fail
        self.asked: dict[str, Any] = {}

    async def record(self, ctx: SessionContext, rec: CallRecord) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.rows.append((ctx, rec))

    async def recent(
        self,
        ctx: SessionContext,
        *,
        since: datetime | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[LoggedCall]:
        self.asked = {"ctx": ctx, "since": since, "kind": kind, "limit": limit}
        rows = [r for _, r in self.rows if kind is None or r.kind == kind]
        now = datetime.now(UTC)
        return [LoggedCall(i, now, r) for i, r in enumerate(rows[:limit])]


@pytest.fixture(autouse=True)
def _fresh_ratio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prefill, "_ratio", {})


def _app(
    tmp_path: Path, log: _Log, gateway: _Gateway | None = None, residency: object = None
) -> tuple[FastAPI, dict[str, str]]:
    app = FastAPI()
    app.include_router(growbot_llm.router, prefix="/internal")
    app.include_router(growbot_calls.internal_router, prefix="/internal")
    app.include_router(growbot_calls.debug_router, prefix="/api")
    app.state.settings = SimpleNamespace(
        local_llm_enabled=True, local_models=["qwen3-vl-30b"], local_llm_url="http://gw/v1"
    )
    app.state.residency = residency or _Residency()
    app.state.growbot_llm_client_factory = gateway or _Gateway()
    app.state.growbot_calls = log
    secrets = FsInternalSecrets(tmp_path)
    secrets.ensure("growbot")
    app.state.growbot_secrets = secrets
    app.dependency_overrides[current_debug_principal] = lambda: DEBUG
    token = (secrets.read("growbot") or b"").decode()
    return app, {"Authorization": f"Bearer {token}"}


def _wait(log: _Log, n: int = 1) -> list[CallRecord]:
    deadline = time.monotonic() + 2
    while len(log.rows) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    return [rec for _, rec in log.rows]


def _sse(*objs: dict[str, Any]) -> tuple[bytes, ...]:
    return (*(f"data: {json.dumps(o)}\n\n".encode() for o in objs), b"data: [DONE]\n\n")


# --- the record -----------------------------------------------------------------------------


def test_pictures_are_logged_as_their_size_and_the_request_is_left_alone() -> None:
    payload = _chat(content=[{"type": "text", "text": "what is it"}, _IMAGE, _IMAGE])
    logged, count, size = redact_images(payload)
    assert (count, size) == (2, 8)
    assert logged["messages"][0]["content"][1] == {
        "type": "image",
        "media": "image/jpeg",
        "b64_chars": 4,
    }
    assert logged["messages"][0]["content"][0] == {"type": "text", "text": "what is it"}
    assert payload["messages"][0]["content"][1] == _IMAGE
    assert redact_images(_chat())[1:] == (0, 0)


def test_a_stream_is_rebuilt_with_its_timings_tokens_and_tools() -> None:
    got = ReplyCollector(stream=True, started=time.monotonic())
    raw = b"".join(
        _sse(
            {"choices": [{"delta": {"reasoning_content": "hm"}}]},
            {"choices": [{"delta": {"content": "hel"}}]},
            {"choices": [{"delta": {"content": "lo"}}]},
            {
                "choices": [
                    {"delta": {"tool_calls": [{"function": {"name": "move", "arguments": "{}"}}]}}
                ]
            },
            {
                "choices": [],
                "usage": {"prompt_tokens": 900, "completion_tokens": 3},
                "timings": {
                    "prompt_ms": 812.44,
                    "predicted_ms": 90,
                    "prompt_per_second": 1100.5,
                    "predicted_per_second": 33.3,
                    "cache_n": 640,
                },
            },
        )
    )
    # Split mid-line, as a network read would.
    for at in range(0, len(raw), 7):
        got.feed(raw[at : at + 7])
    got.finish()
    rec = CallRecord(kind="llm", ok=True, total_ms=1)
    got.fill(rec)
    assert rec.reply == "hello\n[tool] move{}"
    assert rec.reasoning == "hm"
    assert (rec.prompt_tokens, rec.completion_tokens, rec.cached_tokens) == (900, 3, 640)
    assert rec.timings["engine_prefill_ms"] == 812.4
    assert rec.timings["engine_decode_tps"] == 33.3
    assert isinstance(rec.timings["first_token_ms"], int)
    assert rec.ok


def test_a_body_reply_and_an_error_in_the_stream() -> None:
    got = ReplyCollector(stream=False, started=time.monotonic())
    got.feed(b'{"choices":[{"message":{"content":"hi"}}],')
    got.feed(b'"usage":{"prompt_tokens":5,"prompt_tokens_details":{"cached_tokens":2}}}')
    got.finish()
    rec = CallRecord(kind="llm", ok=True, total_ms=1)
    got.fill(rec)
    assert (rec.reply, rec.prompt_tokens, rec.cached_tokens) == ("hi", 5, 2)

    bad = ReplyCollector(stream=True, started=time.monotonic())
    bad.feed(b'data: {"error": {"message": "pool busy"}}\n\ndata: not json\n\n: ping\n')
    bad.finish()
    rec = CallRecord(kind="llm", ok=True, total_ms=1)
    bad.fill(rec)
    assert not rec.ok and rec.error == "pool busy" and rec.reply == ""


def test_a_runaway_reply_is_cut() -> None:
    got = ReplyCollector(stream=False, started=time.monotonic())
    got.feed(json.dumps({"choices": [{"message": {"content": "x" * (TEXT_MAX + 10)}}]}).encode())
    got.finish()
    assert got.reply.endswith("…[cut]") and len(got.reply) < TEXT_MAX + 20


# --- the model lane writes a row per turn ---------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_every_turn_is_logged_with_who_what_and_where_the_time_went(
    tmp_path: Path, stream: bool
) -> None:
    log = _Log()
    chunks = (
        _sse(
            {"choices": [{"delta": {"content": "I see a cat"}}]},
            {"choices": [], "timings": {"prompt_ms": 1500, "prompt_n": 1200}},
        )
        if stream
        else (b'{"choices":[{"message":{"content":"I see a cat"}}]}',)
    )
    app, auth = _app(tmp_path, log, _Gateway(chunks=chunks))
    headers = {**auth, "X-Growbot-Persona": _PERSONA, "X-Growbot-Robot": "gb-abc123"}
    with TestClient(app) as client:
        r = client.post(_COMPLETIONS, json=_chat(content=[_IMAGE], stream=stream), headers=headers)
        assert r.status_code == 200
        (rec,) = _wait(log)
    ctx = log.rows[0][0]
    assert ctx.principal_kind == "owner"
    assert rec.kind == "llm" and rec.ok and rec.model == _VL
    assert (rec.persona_id, rec.robot) == (_PERSONA, "gb-abc123")
    assert rec.reply == "I see a cat"
    assert (rec.images, rec.image_bytes) == (1, 4)
    assert rec.request["messages"][0]["content"][0]["type"] == "image"
    assert rec.request["reasoning_level"] == "none"
    assert {"lock_ms", "admit_ms", "first_byte_ms"} <= set(rec.timings)
    assert rec.total_ms >= rec.timings["first_byte_ms"] >= rec.timings["lock_ms"]
    if stream:
        assert rec.timings["engine_prefill_ms"] == 1500 and rec.prompt_tokens == 1200


def test_forged_who_headers_are_not_kept(tmp_path: Path) -> None:
    log = _Log()
    app, auth = _app(tmp_path, log)
    headers = {**auth, "X-Growbot-Persona": "'; drop", "X-Growbot-Robot": "../x"}
    with TestClient(app) as client:
        client.post(_COMPLETIONS, json=_chat(), headers=headers)
        (rec,) = _wait(log)
    assert (rec.persona_id, rec.robot) == (None, "")


def test_a_refused_turn_is_logged_as_failed(tmp_path: Path) -> None:
    from jbrain.llm.residency import ResidencyError

    log = _Log()
    app, auth = _app(tmp_path, log, residency=_Residency(error=ResidencyError("no room")))
    with TestClient(app) as client:
        client.post(_COMPLETIONS, json=_chat(), headers=auth)
        (rec,) = _wait(log)
    assert not rec.ok and rec.error.startswith("refused")


@pytest.mark.parametrize("stream", [False, True])
def test_a_gateway_error_is_logged_as_failed(tmp_path: Path, stream: bool) -> None:
    log = _Log()
    app, auth = _app(tmp_path, log, _Gateway(status=500))
    with TestClient(app) as client:
        client.post(_COMPLETIONS, json=_chat(stream=stream), headers=auth)
        (rec,) = _wait(log)
    assert not rec.ok and rec.error == "gateway HTTP 500"


def test_a_failing_log_never_fails_the_turn(tmp_path: Path) -> None:
    log = _Log(fail=True)
    app, auth = _app(tmp_path, log)
    with TestClient(app) as client:
        r = client.post(_COMPLETIONS, json=_chat(), headers=auth)
        assert r.status_code == 200 and r.text == "ok"
        time.sleep(0.05)
    assert log.rows == []


def test_no_log_configured_is_no_log(tmp_path: Path) -> None:
    app, auth = _app(tmp_path, _Log())
    del app.state.growbot_calls
    with TestClient(app) as client:
        assert client.post(_COMPLETIONS, json=_chat(), headers=auth).status_code == 200


# --- the sidecar's rows ---------------------------------------------------------------------


def test_the_sidecar_posts_transcripts_and_spoken_lines(tmp_path: Path) -> None:
    log = _Log()
    app, auth = _app(tmp_path, log)
    row = {
        "kind": "stt",
        "ok": True,
        "total_ms": 2300,
        "persona": _PERSONA,
        "robot": "gb-abc123",
        "model": "whisper",
        "text": "hello robot",
        "detail": {"audio_bytes": 64000, "language": "en"},
        "timings": {"upstream_ms": 2300, "audio_s": 2.0},
    }
    with TestClient(app) as client:
        assert client.post("/internal/growbot/calls", json=row).status_code == 401
        assert client.post("/internal/growbot/calls", json=row, headers=auth).status_code == 204
        (rec,) = _wait(log)
    assert (rec.kind, rec.reply, rec.total_ms, rec.persona_id) == (
        "stt",
        "hello robot",
        2300,
        _PERSONA,
    )
    assert rec.request == {"audio_bytes": 64000, "language": "en"}
    assert rec.timings == {"upstream_ms": 2300, "audio_s": 2.0}


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "llm"},
        {"persona": "nope"},
        {"robot": "x"},
        {"total_ms": -1},
        {"detail": {f"k{i}": i for i in range(21)}},
        {"detail": {"nested": {"a": 1}}},
    ],
)
def test_the_sidecar_route_refuses_odd_rows(tmp_path: Path, bad: dict[str, Any]) -> None:
    log = _Log()
    app, auth = _app(tmp_path, log)
    row = {"kind": "tts", "ok": True, "total_ms": 10, **bad}
    with TestClient(app) as client:
        assert client.post("/internal/growbot/calls", json=row, headers=auth).status_code in (
            400,
            422,
        )
    assert log.rows == []


# --- the debug reads ------------------------------------------------------------------------


def _seed(log: _Log) -> None:
    owner = SessionContext(principal_kind="owner")
    for i, (kind, images, total, prefill_ms) in enumerate(
        [
            ("llm", 1, 9000, 7000),
            ("llm", 1, 7000, 5000),
            ("llm", 0, 1200, 200),
            ("stt", 0, 2000, None),
        ]
    ):
        timings: dict[str, Any] = {"lock_ms": i * 100}
        if prefill_ms is not None:
            timings["engine_prefill_ms"] = prefill_ms
        rec = CallRecord(
            kind=kind,
            ok=i != 1,
            total_ms=total,
            images=images,
            image_bytes=40_000 * images,
            prompt_tokens=1000 if kind == "llm" else None,
            request={"messages": [{"role": "user", "content": "hi"}]},
            reply="ok",
            timings=timings,
        )
        log.rows.append((owner, rec))


def test_the_debug_list_hides_the_prompt_unless_asked(tmp_path: Path) -> None:
    log = _Log()
    _seed(log)
    app, _ = _app(tmp_path, log)
    client = TestClient(app)
    rows = client.get("/api/debug/growbot/calls", params={"kind": "llm", "limit": 2}).json()
    assert len(rows) == 2 and all(r["request"] is None for r in rows)
    assert log.asked["kind"] == "llm" and log.asked["limit"] == 2
    assert log.asked["ctx"].principal_kind == "owner"
    full = client.get("/api/debug/growbot/calls", params={"full": "true"}).json()
    assert full[0]["request"]["messages"][0]["content"] == "hi"
    assert client.get("/api/debug/growbot/calls", params={"kind": "bad"}).status_code == 422


def test_the_summary_splits_picture_turns_from_text_ones(tmp_path: Path) -> None:
    log = _Log()
    _seed(log)
    app, _ = _app(tmp_path, log)
    got = TestClient(app).get("/api/debug/growbot/calls/summary").json()
    assert got["calls"] == 4
    groups = got["groups"]
    assert set(groups) == {"llm_image", "llm_text", "stt"}
    image = groups["llm_image"]
    assert image["failed"] == 1
    assert image["total_ms"] == {"n": 2, "p50": 8000.0, "p90": 9000.0, "max": 9000.0}
    assert image["stages"]["engine_prefill_ms"]["max"] == 7000.0
    assert image["image_bytes"]["p50"] == 40000.0
    assert groups["llm_text"]["stages"]["engine_prefill_ms"]["p50"] == 200.0
    assert groups["stt"]["prompt_tokens"] == {"n": 0}


def test_the_debug_reads_need_a_debug_token(tmp_path: Path) -> None:
    from fastapi import HTTPException

    def refuse() -> PrincipalInfo:
        raise HTTPException(status_code=401)

    app, _ = _app(tmp_path, _Log())
    app.dependency_overrides[current_debug_principal] = refuse
    client = TestClient(app)
    assert client.get("/api/debug/growbot/calls").status_code == 401
    assert client.get("/api/debug/growbot/calls/summary").status_code == 401


def test_writes_are_held_until_they_land() -> None:
    async def go() -> None:
        log = _Log()
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(growbot_calls=log)))
        growbot_llm.log_call(request, CallRecord(kind="tts", ok=True, total_ms=1))  # type: ignore[arg-type]
        assert len(growbot_llm._WRITES) == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not growbot_llm._WRITES and len(log.rows) == 1

    asyncio.run(go())
