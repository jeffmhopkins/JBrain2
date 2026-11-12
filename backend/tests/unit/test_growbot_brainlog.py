"""The GrowBot brain log (jbrain.growbot.brainlog, api/growbot_brainlog.py): the zip holds the
window's calls, diary and summary; it carries no ids, codes or secrets of the box's; redacted
follows upstream's diary rule with upstream's own hash; listed names never leave; and the
routes are the owner's, a debug token's and the sidecar's. Fake stores: no database.
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from jbrain.api import growbot_brainlog
from jbrain.api.deps import current_debug_principal, owner_only
from jbrain.api.growbot_calls import summarize
from jbrain.auth.service import PrincipalInfo
from jbrain.db.session import SessionContext
from jbrain.growbot import brainlog
from jbrain.growbot.brainlog import Scrub, h8, hashed, redact_quotes
from jbrain.growbot.calls import CallRecord, LoggedCall
from jbrain.growbot.journal import DiaryBatch, DiaryRow
from jbrain.growbot.personas import Memory, Persona
from jbrain.growbot.repo import RobotInfo
from jbrain.storage import FsInternalSecrets

_PID = "0b7c2f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f"
_CODE = "gb-Fish42"
_NOW = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
OWNER = PrincipalInfo(id="o1", kind="owner", label="owner")
DEBUG = PrincipalInfo(id="cap1", kind="capability_token", label="token")
_SOUL = (
    "You are Sprout, a small creature. You live with Quorra and Tamsin. "
    'Last time someone said "we are going to the beach tomorrow". ' + "x" * 600
)


class _Calls:
    def __init__(self, calls: list[LoggedCall]) -> None:
        self.calls = calls
        self.pages: list[tuple[datetime, int, int]] = []

    async def page(
        self, ctx: SessionContext, *, since: datetime, after_id: int = 0, limit: int = 200
    ) -> list[LoggedCall]:
        self.pages.append((since, after_id, limit))
        got = [c for c in self.calls if c.id > after_id and c.created_at >= since]
        return got[:limit]


class _Journal:
    def __init__(self, rows: list[DiaryRow] | None = None) -> None:
        self.rows = rows or []
        self.kept: list[tuple[SessionContext, DiaryBatch]] = []

    async def record(self, ctx: SessionContext, batch: DiaryBatch) -> int:
        self.kept.append((ctx, batch))
        return len(batch.rows)

    async def page(
        self, ctx: SessionContext, *, since: datetime, after_id: int = 0, limit: int = 500
    ) -> list[DiaryRow]:
        return [r for r in self.rows if r.id > after_id and r.created_at >= since][:limit]


class _Personas:
    async def list_personas(self, ctx: SessionContext) -> list[Persona]:
        return [
            Persona(
                id=_PID,
                name="Sprout",
                version=7,
                made_from="fresh",
                phone_id="phone-secret-id-000000",
                last_phone_id=None,
                opened_at=None,
                created_at=_NOW,
                updated_at=_NOW,
                checkpoints=2,
                wearing=_CODE,
                memories=5,
            )
        ]

    async def get_persona(self, ctx: SessionContext, pid: str) -> Persona | None:
        (p,) = await self.list_personas(ctx)
        return Persona(**{**p.__dict__, "soul": {"core": {"goals": {"owner": "Jeffrey"}}}})

    async def memories(self, ctx: SessionContext, pid: str) -> list[Memory]:
        return [
            Memory(id="r1", meta={"kind": "person", "name": "Tamsin"}, views=[]),
            Memory(id="r2", meta={"kind": "any", "name": "red mug"}, views=[]),
        ]


class _Robots:
    async def list_robots(self, ctx: SessionContext) -> list[RobotInfo]:
        return [RobotInfo(code=_CODE, created_at=_NOW, rotated_at=None, name="Fish")]


def _call(
    i: int, kind: str = "llm", ago: timedelta = timedelta(minutes=5), **kw: Any
) -> LoggedCall:
    base: dict[str, Any] = {
        "kind": kind,
        "ok": True,
        "total_ms": 900 + i,
        "persona_id": _PID,
        "robot": _CODE,
        "model": "qwen3-vl",
        "timings": {"lock_ms": 2, "first_token_ms": 400},
    }
    if kind == "llm":
        base |= {
            "request": {
                "model": "qwen3-vl",
                "messages": [
                    {"role": "system", "content": _SOUL},
                    {"role": "user", "content": 'heard "can you dance for Quorra" near gb-Fish42'},
                    {"role": "assistant", "content": 'I said "hello there"'},
                ],
            },
            "reply": "I wiggle for Tamsin!",
            "images": 1,
            "image_bytes": 40_000,
        }
    elif kind == "stt":
        base |= {"reply": "can you dance for Quorra", "request": {"prompt": "Sprout Quorra"}}
    else:
        base |= {"reply": "Hello Quorra", "request": {"voice": "af_heart"}}
    base |= kw
    return LoggedCall(i, _NOW - ago, CallRecord(**base))


def _diary(i: int, row: dict[str, Any]) -> DiaryRow:
    return DiaryRow(
        id=i,
        created_at=_NOW - timedelta(minutes=3),
        persona_id=_PID,
        robot=_CODE,
        sid="nx-abc123",
        t=row["t"],
        row=row,
        head={"mode": "v3", "boot": {"t": 111, "bld": "deadbeef", "hh": "0badf00d"}},
    )


async def _build(
    calls: list[LoggedCall],
    diary: list[DiaryRow] | None = None,
    *,
    hours: int = 1,
    redact: bool = True,
    names: list[str] | None = None,
) -> tuple[dict[str, bytes], str]:
    data, filename = await brainlog.build(
        ctx=SessionContext(principal_kind="owner"),
        calls=_Calls(calls),
        journal=_Journal(diary),
        personas=await _Personas().list_personas(SessionContext()),
        robots=await _Robots().list_robots(SessionContext()),
        summarize=summarize,
        hours=hours,
        redact=redact,
        names=names or [],
        now=_NOW,
        jbrain="abc1234",
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        root = filename.removesuffix(".zip")
        return {n.removeprefix(root + "/"): zf.read(n) for n in zf.namelist()}, filename


def _jsonl(raw: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in raw.decode().splitlines()]


# --- the hash and the scrub ---------------------------------------------------------------------


def test_h8_is_upstreams_djb2() -> None:
    # next.html: h=5381; h=((h<<5)+h+charCode)>>>0, as eight hex digits.
    assert h8("") == "00001505"
    assert h8("a") == f"{(5381 * 33 + 97) & 0xFFFFFFFF:08x}"
    assert h8("hello there") == h8("hello there") != h8("hello there!")
    # Over UTF-16 code units, as charCodeAt counts them.
    assert h8("😀") == f"{((5381 * 33 + 0xD83D) * 33 + 0xDE00) & 0xFFFFFFFF:08x}"


def test_quoted_speech_is_hashed_like_upstreams_redactq() -> None:
    out = redact_quotes('she said "go to the park" and “come home now” but "ok" stays')
    assert out == (
        f'she said «{h8("go to the park")}·4w» and «{h8("come home now")}·3w» but "ok" stays'
    )


def test_ids_codes_secrets_and_names_never_survive_the_scrub() -> None:
    scrub = Scrub(redact=False, names=["Quorra", " tamsin ", "x"])
    out = scrub.text(
        f"Quorra and TAMSIN met {_CODE} as {_PID.upper()}; Authorization: Bearer abcdefghijklmnop"
        " sk-0123456789abcdefXYZ Quorrastan"
    )
    assert out == (
        "[name] and [name] met robot-1 as persona-1; Authorization: Bearer [secret]"
        " [secret] Quorrastan"
    )
    assert sorted(scrub.names) == ["Quorra", "tamsin"]


# --- the zip --------------------------------------------------------------------------------------


async def test_the_zip_holds_the_window_and_nothing_older() -> None:
    files, filename = await _build(
        [
            _call(1, ago=timedelta(hours=2)),
            _call(2),
            _call(3, "stt"),
            _call(4, "tts"),
        ],
        [_diary(1, {"t": 5, "e": "heard", "q": '"dance for me"'})],
    )
    assert filename == "growbot-brain-log-20261112-0300-1h.zip"
    assert set(files) >= {
        "README.md",
        "manifest.json",
        "summary.json",
        "calls.jsonl",
        "diary.jsonl",
        "diary-pages.json",
        "box.json",
    }
    calls = _jsonl(files["calls.jsonl"])
    assert [c["id"] for c in calls] == [2, 3, 4]
    manifest = json.loads(files["manifest.json"])
    assert manifest["counts"] == {"llm": 1, "stt": 1, "tts": 1, "diary": 1}
    assert manifest["jbrain"] == "abc1234" and manifest["redacted"] is True
    summary = json.loads(files["summary.json"])
    assert set(summary) == {"llm_image", "stt", "tts"}
    assert summary["llm_image"]["stages"]["first_token_ms"]["n"] == 1
    box = json.loads(files["box.json"])
    assert box["personas"] == [
        {
            "persona": "persona-1",
            "face": "lantern",
            "soul_version": 7,
            "memories": 5,
            "checkpoints": 2,
            "wearing": "robot-1",
        }
    ]
    assert box["robots"] == [{"robot": "robot-1", "last_seen_at": None, "key_revoked": False}]
    assert box["models"] == ["qwen3-vl"]


async def test_redacted_hashes_speech_and_keeps_the_creatures_words() -> None:
    files, _ = await _build([_call(1), _call(2, "stt"), _call(3, "tts")], names=["Quorra"])
    llm, stt, tts = _jsonl(files["calls.jsonl"])
    messages = llm["request"]["messages"]
    # The soul is shared once under prompts/, with its quoted line hashed and names hidden.
    ref = messages[0]["content"]["shared"]
    prompt = files[ref].decode()
    assert "[name] and Tamsin" in prompt and "Quorra" not in prompt
    assert f"«{h8('we are going to the beach tomorrow')}·7w»" in prompt
    assert messages[1]["content"] == f"heard «{h8('can you dance for Quorra')}·5w» near robot-1"
    assert llm["reply"] == "I wiggle for Tamsin!"
    assert llm["persona"] == "persona-1" and llm["robot"] == "robot-1"
    # A transcript is someone in the house: hashed whole, joinable to the diary's hash.
    assert stt["reply"] == f"«{h8('can you dance for Quorra')}·5w»"
    assert stt["request"]["prompt"] == f"«{h8('Sprout Quorra')}·2w»"
    assert tts["reply"] == "Hello [name]"
    everything = b"".join(files.values()).decode()
    for leak in ("Quorra", _PID, _CODE, "Fish", "phone-secret-id", "0badf00d"):
        assert leak not in everything, leak


async def test_full_mode_keeps_speech_but_still_hides_names() -> None:
    files, _ = await _build([_call(1), _call(2, "stt")], redact=False, names=["Quorra", "Tamsin"])
    llm, stt = _jsonl(files["calls.jsonl"])
    assert (
        llm["request"]["messages"][1]["content"] == 'heard "can you dance for [name]" near robot-1'
    )
    assert stt["reply"] == "can you dance for [name]"
    assert llm["reply"] == "I wiggle for [name]!"
    assert json.loads(files["manifest.json"])["redacted"] is False


async def test_one_soul_is_written_once_for_every_turn() -> None:
    files, _ = await _build([_call(i) for i in range(1, 6)])
    assert len([n for n in files if n.startswith("prompts/")]) == 1
    refs = {c["request"]["messages"][0]["content"]["shared"] for c in _jsonl(files["calls.jsonl"])}
    assert len(refs) == 1


async def test_the_diary_rows_and_their_pages() -> None:
    files, _ = await _build(
        [],
        [
            _diary(1, {"t": 10, "e": "heard", "q": '"dance for Quorra"'}),
            _diary(2, {"t": 11, "e": "act", "verb": "spin", "who": ["Quorra"]}),
        ],
        names=["Quorra"],
    )
    rows = _jsonl(files["diary.jsonl"])
    assert [r["e"] for r in rows] == ["heard", "act"]
    assert rows[0]["diary"] == "diary-1" and rows[0]["boot"] == 111
    assert rows[0]["q"] == hashed('"dance for Quorra"')
    assert rows[1]["who"] == ["[name]"]
    pages = json.loads(files["diary-pages.json"])
    assert pages == {"diary-1@111": {"mode": "v3", "boot": {"t": 111, "bld": "deadbeef"}}}


async def test_a_long_window_is_read_a_page_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brainlog, "_PAGE", 2)
    calls = _Calls([_call(i, "tts") for i in range(1, 6)])
    data, _ = await brainlog.build(
        ctx=SessionContext(principal_kind="owner"),
        calls=calls,
        journal=None,
        personas=[],
        robots=[],
        summarize=summarize,
        hours=6,
        redact=True,
        names=[],
        now=_NOW,
        jbrain="x",
    )
    assert [p[1] for p in calls.pages] == [0, 2, 4]
    assert calls.pages[0][0] == _NOW - timedelta(hours=6)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        got = zf.read(next(n for n in names if n.endswith("calls.jsonl")))
        assert len(got.splitlines()) == 5
        assert zf.read(next(n for n in names if n.endswith("diary.jsonl"))) == b""


# --- the routes -----------------------------------------------------------------------------------


def _refuse() -> None:
    raise HTTPException(401)


def _app(tmp_path: Path, journal: _Journal, calls: list[LoggedCall]) -> tuple[FastAPI, str]:
    app = FastAPI()
    app.include_router(growbot_brainlog.router, prefix="/api")
    app.include_router(growbot_brainlog.debug_router, prefix="/api")
    app.include_router(growbot_brainlog.internal_router, prefix="/internal")
    app.state.growbot_calls = _Calls(calls)
    app.state.growbot_journal = journal
    app.state.growbot_personas = _Personas()
    app.state.growbot_repo = _Robots()
    secrets = FsInternalSecrets(tmp_path)
    secrets.ensure("growbot")
    app.state.growbot_secrets = secrets
    app.state.settings = SimpleNamespace(git_sha="abc1234")
    return app, (secrets.read("growbot") or b"").decode()


def test_the_sidecar_keeps_a_diary_snapshot(tmp_path: Path) -> None:
    journal = _Journal()
    app, token = _app(tmp_path, journal, [])
    client = TestClient(app)
    body = {
        "sid": "nx-abc123",
        "persona": _PID,
        "robot": _CODE,
        "head": {"mode": "v3"},
        "rows": [{"t": 1, "e": "boot"}, {"t": 2, "e": "heard"}],
    }
    assert client.post("/internal/growbot/journal", json=body).status_code == 401
    got = client.post(
        "/internal/growbot/journal", json=body, headers={"Authorization": f"Bearer {token}"}
    )
    assert got.status_code == 200 and got.json() == {"ok": True, "new": 2}
    ctx, batch = journal.kept[0]
    assert ctx.principal_kind == "owner"
    assert (batch.sid, batch.persona_id, batch.robot) == ("nx-abc123", _PID, _CODE)
    assert batch.head == {"mode": "v3"}


@pytest.mark.parametrize(
    "bad",
    [
        {"sid": "nx/../x", "rows": []},
        {"sid": "nx-abc", "robot": "Fish", "rows": []},
        {"sid": "nx-ab", "rows": []},
        {"sid": "nx-abc", "persona": "not-a-uuid", "rows": []},
        {"sid": "nx-abc", "rows": [{"t": i} for i in range(501)]},
    ],
)
def test_the_sidecar_route_refuses_odd_snapshots(tmp_path: Path, bad: dict[str, Any]) -> None:
    journal = _Journal()
    app, token = _app(tmp_path, journal, [])
    got = TestClient(app).post(
        "/internal/growbot/journal", json=bad, headers={"Authorization": f"Bearer {token}"}
    )
    assert got.status_code == 422 and journal.kept == []


def test_the_owner_downloads_the_zip(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, _Journal(), [_call(1, ago=timedelta(seconds=30))])
    client = TestClient(app)
    app.dependency_overrides[owner_only] = _refuse
    assert client.post("/api/growbot/brain-log", json={"hours": 3}).status_code == 401
    app.dependency_overrides[owner_only] = lambda: OWNER
    got = client.post(
        "/api/growbot/brain-log", json={"hours": 3, "redact": False, "names": ["Quorra"]}
    )
    assert got.status_code == 200
    assert got.headers["content-type"] == "application/zip"
    assert got.headers["cache-control"] == "no-store"
    assert '-3h.zip"' in got.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        manifest = json.loads(zf.read(next(n for n in zf.namelist() if "manifest" in n)))
        everything = b"".join(zf.read(n) for n in zf.namelist())
    assert manifest["hours"] == 3 and manifest["redacted"] is False
    assert manifest["counts"]["llm"] == 1 and b"Quorra" not in everything


@pytest.mark.parametrize("body", [{"hours": 2}, {"hours": 24}, {"names": ["x" * 61]}])
def test_the_owner_route_refuses_other_windows(tmp_path: Path, body: dict[str, Any]) -> None:
    app, _ = _app(tmp_path, _Journal(), [])
    app.dependency_overrides[owner_only] = lambda: OWNER
    assert TestClient(app).post("/api/growbot/brain-log", json=body).status_code == 422


def test_the_debug_zip_is_always_redacted(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, _Journal(), [_call(1, "stt", ago=timedelta(seconds=30))])
    client = TestClient(app)
    app.dependency_overrides[current_debug_principal] = _refuse
    assert client.get("/api/debug/growbot/brain-log").status_code == 401
    app.dependency_overrides[current_debug_principal] = lambda: DEBUG
    got = client.get("/api/debug/growbot/brain-log", params={"hours": 6})
    assert got.status_code == 200
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        manifest = json.loads(zf.read(next(n for n in zf.namelist() if "manifest" in n)))
        calls = zf.read(next(n for n in zf.namelist() if n.endswith("calls.jsonl")))
    assert manifest["hours"] == 6 and manifest["redacted"] is True
    assert b"can you dance" not in calls
    assert client.get("/api/debug/growbot/brain-log", params={"hours": 5}).status_code == 422


async def test_raw_speech_in_the_diary_and_the_models_thinking_are_hashed() -> None:
    # A `.local` box name makes upstream's page think it is staging, so it writes raw.
    files, _ = await _build(
        [_call(1, reasoning="The child asked me to dance, so I will.")],
        [
            _diary(1, {"t": 10, "e": "user", "txt": "mum is at the dentist at 3"}),
            _diary(2, {"t": 11, "e": "body_sound", "words": ["hello", "there you"]}),
            _diary(3, {"t": 12, "e": "act", "verb": "spin", "diary": "x", "note": "nx-abc123"}),
        ],
    )
    (llm,) = _jsonl(files["calls.jsonl"])
    assert llm["reasoning"] == f"«{h8('The child asked me to dance, so I will.')}·9w»"
    user, sound, act = _jsonl(files["diary.jsonl"])
    assert user["txt"] == f"«{h8('mum is at the dentist at 3')}·7w»"
    assert sound["words"] == [f"«{h8('hello')}·1w»", f"«{h8('there you')}·2w»"]
    # A row's own field never overwrites the box's label, and its diary id is numbered.
    assert act["diary"] == "diary-1" and act["_diary"] == "x" and act["note"] == "diary-1"
    assert act["verb"] == "spin"
    everything = b"".join(files.values()).decode()
    assert "dentist" not in everything and "nx-abc123" not in everything


async def test_full_mode_keeps_the_diarys_speech_and_the_thinking() -> None:
    files, _ = await _build(
        [_call(1, reasoning="thinking")],
        [_diary(1, {"t": 10, "e": "user", "txt": "hi Quorra"})],
        redact=False,
        names=["Quorra"],
    )
    assert _jsonl(files["calls.jsonl"])[0]["reasoning"] == "thinking"
    assert _jsonl(files["diary.jsonl"])[0]["txt"] == "hi [name]"


def test_token_shapes_are_cut() -> None:
    scrub = Scrub(redact=False)
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"
    assert scrub.text(f"t={jwt} k={'ab' * 20}") == "t=[secret] k=[secret]"
    assert scrub.text("build deadbeef") == "build deadbeef"


async def test_a_window_past_the_size_cap_is_cut_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(brainlog, "OUT_MAX", 1)
    monkeypatch.setattr(brainlog, "_PAGE", 1)
    files, _ = await _build([_call(i, "tts") for i in range(1, 5)])
    assert len(_jsonl(files["calls.jsonl"])) < 4
    assert json.loads(files["manifest.json"])["cut"] is True


def test_one_brain_log_at_a_time(tmp_path: Path) -> None:
    app, _ = _app(tmp_path, _Journal(), [])
    app.dependency_overrides[owner_only] = lambda: OWNER
    lock = asyncio.Lock()
    app.state.growbot_brainlog_lock = lock
    asyncio.run(lock.acquire())
    got = TestClient(app).post("/api/growbot/brain-log", json={"hours": 1})
    assert got.status_code == 409
    lock.release()
    assert TestClient(app).post("/api/growbot/brain-log", json={"hours": 1}).status_code == 200


def test_the_names_the_box_already_knows() -> None:
    souls = [{"soul_format": 3, "core": {"goals": {"owner": "Jeffrey", "wants": []}}}, {}]
    metas = [{"kind": "person", "name": "Quorra"}, {"kind": "any", "name": "red mug"}]
    assert brainlog.known_names(souls, metas) == ["Jeffrey", "Quorra"]


def test_the_owner_route_hides_the_names_the_box_knows_with_no_list(tmp_path: Path) -> None:
    call = _call(1, ago=timedelta(seconds=30), reply="Hi Jeffrey and Tamsin, I am Sprout on Fish")
    app, _ = _app(tmp_path, _Journal(), [call])
    app.dependency_overrides[owner_only] = lambda: OWNER
    got = TestClient(app).post("/api/growbot/brain-log", json={"hours": 1, "redact": False})
    with zipfile.ZipFile(io.BytesIO(got.content)) as zf:
        calls = zf.read(next(n for n in zf.namelist() if n.endswith("calls.jsonl")))
    assert json.loads(calls)["reply"] == "Hi [name] and [name], I am [name] on [name]"


def test_a_name_never_rewrites_the_exports_own_labels() -> None:
    scrub = Scrub(redact=False, names=["Robot", "robot", "Persona"])
    scrub.robot("gb-x")
    assert scrub.text("robot-1 says hi to Robot and persona-2") == (
        "robot-1 says hi to [name] and persona-2"
    )
    assert scrub.names == ["Robot", "Persona"]


def test_diary_ids_are_relabelled_whole() -> None:
    scrub = Scrub(redact=False)
    scrub.diary("nx-abcd")
    scrub.diary("nx-ab12")
    assert scrub.text("nx-abcd and nx-ab12 but not nx-abcde") == (
        "diary-1 and diary-2 but not nx-abcde"
    )


async def test_prompts_past_their_cap_are_not_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(brainlog, "PROMPTS_MAX", 1000)
    calls = [_call(1), _call(2, request={"messages": [{"role": "system", "content": "y" * 600}]})]
    files, _ = await _build(calls)
    rows = _jsonl(files["calls.jsonl"])
    assert rows[0]["request"]["messages"][0]["content"]["shared"].startswith("prompts/")
    assert rows[1]["request"]["messages"][0]["content"]["shared"].startswith("[not kept")
    assert json.loads(files["manifest.json"])["cut"] is True


def test_known_names_are_deduplicated() -> None:
    metas = [{"kind": "person", "name": "Quorra"}] * 300 + [{"kind": "person", "name": "QUORRA"}]
    assert brainlog.known_names([], metas) == ["Quorra"]
