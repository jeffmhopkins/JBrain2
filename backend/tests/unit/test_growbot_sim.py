"""The Dave simulator's pure parts (growbot/sim/dave_sim), loaded by path: the fake box the
page talks to, the chip model behind the legs' socket, and the debug-token and model
plumbing. The browser and physics halves need Chromium and MuJoCo and are run by hand
(growbot/sim/README.md); these pin the contracts they lean on."""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SIM = Path(__file__).resolve().parents[3] / "growbot" / "sim"
sys.path.insert(0, str(_SIM))

# By name rather than `from dave_sim import`: the package lives outside the backend's tree.
simbox = importlib.import_module("dave_sim.box")
simchip = importlib.import_module("dave_sim.chip")
simllm = importlib.import_module("dave_sim.llm")
simrun = importlib.import_module("dave_sim.run")
simscen = importlib.import_module("dave_sim.scenarios")


class Legs:
    def __init__(self) -> None:
        self.writes: list[tuple[float, float]] = []
        self.limp = 0

    def servos(self, l_deg: float, r_deg: float) -> None:
        self.writes.append((l_deg, r_deg))

    def release(self) -> None:
        self.limp += 1


class Clock:
    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> int:
        return self.ms


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------- the chip


def test_attach_answers_awake() -> None:
    chip = simchip.Chip(Legs(), Clock())
    (status,) = chip.frame(json.dumps({"t": "attach"}))
    assert status == {"t": "status", "awake": 1}  # no boot report: serve.py sends none


def test_pose_writes_at_once_clamped_and_dead_man_goes_limp() -> None:
    legs, clock = Legs(), Clock()
    chip = simchip.Chip(legs, clock)
    assert chip.frame(json.dumps({"t": "pose", "lr": "200,-5"})) == []
    assert legs.writes[-1] == (180.0, 0.0)
    clock.ms = simchip.DEADMAN_MS - 1
    chip.tick()
    assert legs.limp == 0
    clock.ms = simchip.DEADMAN_MS + 1
    chip.tick()
    assert legs.limp == 1 and chip.pose is None


def test_gesture_glides_through_the_firmware_engine_and_acks() -> None:
    legs, clock = Legs(), Clock()
    chip = simchip.Chip(legs, clock)
    steps = [{"l": 60, "r": 120, "ms": 400}, {"l": 90, "r": 90, "ms": 400}]
    (ack,) = chip.frame(json.dumps({"t": "act", "rid": 7, "steps": steps}))
    assert ack["rid"] == 7 and ack["ok"] == 1
    for clock.ms in range(0, 1000, 40):
        chip.tick()
    assert legs.writes[-1] == pytest.approx((90, 90), abs=1)
    assert len(legs.writes) > 5  # glided, not jumped


def test_stop_goes_limp_and_junk_is_ignored() -> None:
    legs = Legs()
    chip = simchip.Chip(legs, Clock())
    assert chip.frame("not json") == [] and chip.frame("[1]") == []
    (ack,) = chip.frame(json.dumps({"t": "stop", "rid": 3}))
    assert ack["ok"] == 1 and legs.limp == 1
    assert [f["t"] for f in chip.log] == ["stop"]


# ---------------------------------------------------------------------- the box


async def _never(req: dict[str, Any]) -> str:
    raise AssertionError("no model call expected")


def test_static_serves_the_site_and_refuses_traversal(tmp_path: Path) -> None:
    (tmp_path / "v3").mkdir()
    (tmp_path / "v3" / "a.js").write_text("x")
    (tmp_path.parent / "secret.txt").write_text("no")
    fb = simbox.FakeBox(model=_never, site=tmp_path)
    got = _run(fb.handle("GET", "/v3/a.js?v=1", None))
    assert got.status == 200 and got.ctype == "text/javascript"
    assert _run(fb.handle("GET", "/../secret.txt", None)).status == 404
    assert _run(fb.handle("GET", "/v3/missing.js", None)).status == 404


def test_the_detector_patch_still_matches_the_page() -> None:
    fb = simbox.FakeBox(model=_never)
    got = fb.static("/v3/agent-face-track.mjs")
    assert got.status == 200 and b"delegate:'CPU'" in got.body


def test_a_stale_patch_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "v3").mkdir()
    (tmp_path / "v3" / "agent-face-track.mjs").write_text("nothing to patch")
    with pytest.raises(RuntimeError):
        simbox.FakeBox(model=_never, site=tmp_path).static("/v3/agent-face-track.mjs")


def test_persona_is_fresh_without_a_soul_and_kept_locally_with_one() -> None:
    assert _run(simbox.FakeBox(model=_never).handle("GET", "/api/box/persona", None)).status == 204
    fb = simbox.FakeBox(model=_never, soul={"name": "Dave"})
    got = json.loads(_run(fb.handle("GET", "/api/box/persona", None)).body)
    assert got["soul"] == {"name": "Dave"} and got["wearing"] == simbox.ROBOT
    state = json.loads(_run(fb.handle("GET", "/api/box/persona/state", None)).body)
    assert "soul" not in state
    other = json.dumps({"persona": "someone-else", "soul": {}}).encode()
    assert _run(fb.handle("POST", "/api/box/persona", other)).status == 409
    mine = json.dumps({"persona": simbox.PERSONA, "soul": {"name": "Dave2"}}).encode()
    assert json.loads(_run(fb.handle("POST", "/api/box/persona", mine)).body)["version"] == 2
    assert fb.soul == {"name": "Dave2"}


def test_prefs_set_and_drop() -> None:
    fb = simbox.FakeBox(model=_never, soul={}, prefs={"a": "1", "b": "2"})
    body = json.dumps({"set": {"c": "3"}, "drop": ["a"]}).encode()
    got = json.loads(_run(fb.handle("POST", "/api/box/prefs", body)).body)
    assert got["prefs"] == {"b": "2", "c": "3"}


def test_cal_is_only_the_sim_robots_and_policies_come_from_the_library(tmp_path: Path) -> None:
    pol = tmp_path / "p.json"
    pol.write_text('{"id": "x"}')
    pid = simbox.policy_id("master-v3")
    fb = simbox.FakeBox(model=_never, policies={pid: pol}, skills={"forward": pid})
    cal = json.loads(_run(fb.handle("GET", f"/api/box/cal?id={simbox.ROBOT}", None)).body)
    assert cal["cal"]["L_SIGN"] == -1
    other = json.loads(_run(fb.handle("GET", "/api/box/cal?id=gb-other", None)).body)
    assert other["cal"] is None
    assert _run(fb.handle("GET", f"/api/box/policy?id={pid}", None)).body == pol.read_bytes()
    assert _run(fb.handle("GET", "/api/box/policy?id=nope", None)).status == 404
    skills = json.loads(_run(fb.handle("GET", "/api/box/skills", None)).body)
    assert skills["skills"] == {"forward": pid}
    assert simbox.policy_id("master-v3") == pid  # stable across runs


def test_voice_and_hearing_are_stubbed_without_a_model() -> None:
    fb = simbox.FakeBox(model=_never)
    wav = _run(fb.handle("POST", "/api/fish-tts", b"{}"))
    assert wav.ctype == "audio/wav" and wav.body[:4] == b"RIFF"
    stt = _run(fb.handle("POST", "/api/llm?target=groq-audio", b""))
    assert json.loads(stt.body) == {"text": ""}
    assert _run(fb.handle("POST", "/api/ev", b"{}")).status == 204
    assert _run(fb.handle("GET", "/api/nope", None)).status == 404


def test_model_answers_streamed_and_plain_and_failures_reach_the_page() -> None:
    fb = simbox.FakeBox(model=simllm.scripted_model([{"say": "hi"}]))
    sse = _run(fb.handle("POST", "/api/llm-stream?target=openrouter", b'{"messages": []}'))
    assert sse.ctype == "text/event-stream" and sse.body.endswith(b"data: [DONE]\n\n")
    first = json.loads(sse.body.split(b"\n\n")[0][len(b"data: ") :])
    assert json.loads(first["choices"][0]["delta"]["content"]) == {"say": "hi"}
    # The run logs a reply from the answer it delivered, not when the model returned.
    assert sse.call is fb.calls[0] and sse.call["reply"] == '{"say": "hi"}'
    plain = _run(fb.handle("POST", "/api/llm", b"{}"))
    content = json.loads(plain.body)["choices"][0]["message"]["content"]
    assert json.loads(content) == {"say": ""} and plain.call is fb.calls[1]
    assert _run(fb.handle("GET", "/api/box/skills", None)).call is None

    async def boom(req: dict[str, Any]) -> str:
        raise RuntimeError("lane down")

    bad = simbox.FakeBox(model=boom)
    got = _run(bad.handle("POST", "/api/llm", b"{}"))
    assert got.status == 502 and bad.calls[0]["error"] == "lane down"


def test_silent_wav_is_well_formed() -> None:
    wav = simbox.silent_wav(ms=100, rate=8000)
    assert len(wav) == 44 + 1600 and wav[8:12] == b"WAVE"


# ---------------------------------------------------------------------- the model plumbing


def _token(u: str = "https://box.example/", k: str = "k" * 48) -> str:
    raw = base64.urlsafe_b64encode(json.dumps({"u": u, "k": k}).encode()).decode()
    return raw.rstrip("=")


def test_debug_token_from_env_or_a_file_with_other_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JBRAIN_DEBUG_TOKEN", raising=False)
    monkeypatch.delenv("JBRAIN_DEBUG_TOKEN_FILE", raising=False)
    f = tmp_path / "t.txt"
    f.write_text(f"debug token for the box:\n{_token()}\n")
    assert simllm.debug_token(str(f)) == ("https://box.example", "k" * 48)
    monkeypatch.setenv("JBRAIN_DEBUG_TOKEN", _token(u="https://other.example"))
    assert simllm.debug_token(str(f))[0] == "https://other.example"
    monkeypatch.delenv("JBRAIN_DEBUG_TOKEN")
    with pytest.raises(RuntimeError):
        simllm.debug_token(None)


class FakeApi:
    def __init__(self, answers: list[Any]) -> None:
        self.answers = answers
        self.bodies: list[Any] = []

    def call(self, method: str, path: str, body: Any = None, timeout: float = 0) -> Any:
        assert (method, path) == ("POST", "/api/debug/growbot/replay")
        self.bodies.append(body)
        out = self.answers.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def _busy() -> Exception:
    import urllib.error

    return urllib.error.HTTPError("u", 409, "busy", {}, None)  # type: ignore[arg-type]


def test_live_model_skips_seals_waits_out_a_busy_lane_and_never_streams() -> None:
    ok = {"choices": [{"message": {"content": '{"say": "yo"}'}}]}
    api = FakeApi([_busy(), ok])
    ask = simllm.live_model(api, busy_wait_s=0)  # type: ignore[arg-type]
    assert _run(ask({"max_tokens": 1})) == "" and api.bodies == []
    assert _run(ask({"max_tokens": 512, "stream": True})) == '{"say": "yo"}'
    assert [b["stream"] for b in api.bodies] == [False, False]
    stuck = simllm.live_model(FakeApi([_busy(), _busy()]), busy_wait_s=0, tries=2)  # type: ignore[arg-type]
    with pytest.raises(Exception, match="busy"):
        _run(stuck({"max_tokens": 512}))


def test_persona_lookup_by_name() -> None:
    class Api(simllm.BoxApi):  # pyright: ignore[reportUntypedBaseClass]
        def call(self, method: str, path: str, body: Any = None, timeout: float = 0) -> Any:
            if path.endswith("/personas"):
                return {"personas": [{"id": "p1", "name": "Dave"}]}
            return {"id": path.rsplit("/", 1)[1]}

    api = Api("https://b", "k")
    assert api.persona("dave") == {"id": "p1"}
    with pytest.raises(RuntimeError):
        api.persona("Nobody")


def test_scripted_model_from_a_function() -> None:
    ask = simllm.scripted_model(lambda req: {"say": str(len(req))})
    assert json.loads(_run(ask({"a": 1}))) == {"say": "1"}


def test_debug_token_refuses_a_plain_http_box(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JBRAIN_DEBUG_TOKEN", _token(u="http://box.example"))
    with pytest.raises(RuntimeError, match="https"):
        simllm.debug_token(None)


def test_the_owner_key_never_follows_a_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.request

    sent: list[urllib.request.Request] = []

    class Resp:
        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_open(req: urllib.request.Request, timeout: float = 0) -> Resp:
        sent.append(req)
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    simllm.BoxApi("https://b", "secret").call("GET", "/x")
    (req,) = sent
    assert req.unredirected_hdrs.get("Authorization") == "Bearer secret"
    assert "Authorization" not in req.headers


def test_forget_keeps_who_and_whose_and_drops_history() -> None:
    soul = {
        "meta": {"name": "Dave"},
        "body": {"id": "b"},
        "identityCore": "core",
        "identity": "grown",
        "log": [1],
        "facts": [2],
        "scratch": {"day": [3]},
        "goals": {"owner": "Jeff", "selfName": "", "wants": ["w"], "funLog": [4]},
    }
    got = simbox.forget(soul)
    assert got["meta"] == {"name": "Dave"} and got["body"] == {"id": "b"}
    assert got["identityCore"] == "core" and got["identity"].startswith("I am Dave.")
    assert got["log"] == [] and got["facts"] == [] and got["scratch"] == {}
    assert got["goals"] == {"owner": "Jeff", "selfName": "Dave"}
    assert soul["log"] == [1]  # the original is untouched


def test_live_model_says_what_the_box_said_when_its_model_is_down() -> None:
    down = {"error": {"message": "model needs more memory", "code": "model_unavailable"}}
    ask = simllm.live_model(FakeApi([down]), busy_wait_s=0)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="more memory"):
        _run(ask({"max_tokens": 512}))
    flat = simllm.live_model(FakeApi([{"error": "model_unavailable"}]), busy_wait_s=0)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="model_unavailable"):
        _run(flat({"max_tokens": 512}))


def test_the_sim_body_is_mirrored_as_the_robot_and_one_scenario_unmirrors_it() -> None:
    # The page swaps the legs for trained walks because the robot needs it; a stock-wired
    # sim body under that swap turns every trained turn the wrong way, and crossed pins with
    # the stock signs send the trained forward backwards (2026-11-17).
    async def model(_: dict[str, Any]) -> str:
        return "{}"

    opts = simrun.Options(model=model)
    mirrored = {"swap": True, "l_sign": 1, "r_sign": -1}
    assert opts.wiring == mirrored == simrun.ROBOT_WIRING
    opts.wiring["swap"] = False
    assert mirrored == simrun.ROBOT_WIRING
    assert "wiring" not in simscen.SETUP["trained_turns"]
    assert simscen.SETUP["stock_wired"]["wiring"] == {"swap": False}
    assert "swapped_legs" not in simscen.SCENARIOS
    # On that wiring the follower turns away from the face unless the camera flip is set.
    assert opts.cam_flip is True
    assert simscen.SETUP["follow_walks_after"]["person"]["face_z"] == 1.55
    # The body test's asks have a scenario each: come here, follow me by voice, stop stop.
    assert {"come_here", "follow_me_away", "stop_stop"} <= set(simscen.SCENARIOS)
