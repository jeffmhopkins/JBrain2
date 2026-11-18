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
simplan = importlib.import_module("dave_sim.plan")
simbat = importlib.import_module("dave_sim.battery")


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
        "map": {"v": 1},
        "scratch": {"day": [3], "rules": ["my feet slip"], "reflexes": [{"on": "fall"}]},
        "goals": {"owner": "Jeff", "selfName": "", "wants": ["w"], "funLog": [4]},
    }
    got = simbox.forget(soul)
    assert got["meta"] == {"name": "Dave"} and got["body"] == {"id": "b"}
    assert got["identityCore"] == "core" and got["identity"].startswith("I am Dave.")
    assert got["log"] == [] and got["facts"] == [] and "map" not in got
    assert got["scratch"] == {"reflexes": [{"on": "fall"}]}
    assert got["goals"] == {"owner": "Jeff", "selfName": "Dave"}
    assert soul["log"] == [1]  # the original is untouched
    # The PWA's Forget history is the wipe these runs test.
    from jbrain.growbot import souls

    assert souls.forgotten(soul, "Dave") == got
    named = {**soul, "goals": {"owner": "Jeff", "selfName": "Davey", "call": ["do", "re"]}}
    assert souls.forgotten(named, "Dave") == simbox.forget(named)
    assert simbox.forget(named)["identity"].startswith("I am Davey.")
    assert simbox.forget(named)["goals"]["call"] == ["do", "re"]
    assert simbox.forget({**soul, "scratch": {}})["scratch"] == {}


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


# ---------------------------------------------------------------------- the house's floor plan


class _Item:
    def __init__(self, name: str, x: float, y: float) -> None:
        self.name, self.x, self.y, self.hx, self.hy, self.rgb = name, x, y, 0.2, 0.2, (9, 9, 9)
        self.room = "kitchen" if x > 0 else "living room"


class _Door:
    name, x, y, along, half = "doorway", 0.0, 0.0, "y", 0.4
    rooms = ("living room", "kitchen")


class _House:
    """GrowBot_Training's house.Layout as plan.py reads it (MuJoCo isn't in the backend's
    env)."""

    OUTER = (-3.0, -2.0, 2.0, 2.0)
    ROOMS = {"living room": (-3.0, -2.0, 0.0, 2.0), "kitchen": (0.0, -2.0, 2.0, 2.0)}
    WALLS = (
        ("divide_s", 0.0, -2.0, 0.0, -0.4, "m"),
        ("divide_n", 0.0, 0.4, 0.0, 2.0, "m"),
    )
    DOORS = (_Door(),)
    ITEMS = (_Item("piano", -2.0, 1.0), _Item("rug", -1.0, 0.0))

    @staticmethod
    def room_at(x: float, y: float) -> str:
        return "kitchen" if x > 0 else "living room"

    @staticmethod
    def distance_to(name: str, x: float, y: float) -> float:
        it = {i.name: i for i in _House.ITEMS}[name]
        return max(0.0, ((x - it.x) ** 2 + (y - it.y) ** 2) ** 0.5 - 0.2)


class _PlanSim:
    def __init__(self, scene: str = "house") -> None:
        self.o = type("O", (), {"scene": scene})()
        self.body = type("Body", (), {"layout": _House if scene == "house" else None})()
        # Two seconds still, then 4 cm/s along -x for 25 s, as states every 0.08 s.
        self.states = [
            {"wall": t * 0.08, "x": 0.5 - max(0.0, t * 0.08 - 2) * 0.04, "y": 0.0, "nose_deg": 0.0}
            for t in range(int(27 / 0.08))
        ]
        self.timeline = [
            {"t": 3.0, "kind": "dave", "text": json.dumps({"say": "Off to find the <piano>"})},
            {"t": 4.0, "kind": "dave", "text": json.dumps({"say": ""})},
            {"t": 5.0, "kind": "dave", "warm": True, "text": json.dumps({"say": "warm"})},
        ]


def test_the_floor_plan_scores_where_he_went() -> None:
    sim = _PlanSim()
    st = simplan.stats(sim)  # type: ignore[arg-type]
    assert st["walked_m"] == pytest.approx(1.0, abs=0.02)
    assert set(st["rooms_s"]) == {"living room", "kitchen"}
    assert abs(st["rooms_s"]["kitchen"] - 14.5) <= 1
    assert "rug" not in st["closest_m"] and st["closest_m"]["piano"] < 2.0
    tr = simplan.track(sim)  # type: ignore[arg-type]
    # The camera looks out of the tail: nose at 0 degrees faces 180.
    assert abs(tr[-1]["face"]) == 180 and tr[-1]["speed"] == pytest.approx(4.0, abs=0.2)
    assert tr[10]["speed"] == 0.0
    assert simplan.replies(sim) == [(3.0, "Off to find the <piano>")]  # type: ignore[arg-type]
    svg = simplan.svg(sim)  # type: ignore[arg-type]
    assert svg.startswith("<svg") and "piano" in svg and "&lt;piano&gt;" in svg
    assert "living room" in svg and 'marker-end="url(#ah)"' in svg


def test_the_bare_room_has_a_plan_too() -> None:
    sim = _PlanSim(scene="room")
    assert "rooms_s" not in simplan.stats(sim)  # type: ignore[arg-type]
    assert "living room" not in simplan.svg(sim)  # type: ignore[arg-type]
    assert simscen.SETUP["find_piano"]["scene"] == "house"
    assert simscen.SETUP["walk_to_piano"]["timeout"] > simscen.SETUP["find_piano"]["timeout"]
    assert simscen.SETUP["explore_alone"]["person"] is None
    assert simrun.Options(model=None).scene == "room"  # type: ignore[arg-type]


def test_his_map_is_scored_against_the_house_in_his_own_frame() -> None:
    """plan.map_score lays Dave's map (growbot-map.js) on the house by where he started: he
    starts at (0.5, 0) with his camera facing 180, where his map has him at the origin facing
    90, so his +y is the house's -x."""
    sim = _PlanSim()
    sim.dave_map = {  # type: ignore[attr-defined]
        "rooms": [
            {"n": "living room", "x": 0, "y": 2, "w": 4, "d": 3},
            {"n": "the kitchen", "x": 0, "y": -1, "w": 4, "d": 2},
            {"n": "hall", "x": 9, "y": 9, "w": 2, "d": 2},
        ],
        "doors": [{"x": 0, "y": 0.5, "a": "living room", "b": "the kitchen"}],
        "things": [
            {"n": "grand piano", "x": 1, "y": 2.5, "room": "living room"},
            {"n": "tuba", "x": 0, "y": 0, "room": "hall"},
        ],
    }
    sim.map_track = [  # type: ignore[attr-defined]
        {"t": 0, "here": {"x": 0, "y": 0, "h": 90}, "x": 0.5, "y": 0.0, "face": 180.0},
        {"t": 9, "here": {"x": 0, "y": 0.5}, "x": 0.0, "y": 0.0, "face": 180.0},
        {"t": 20, "here": {"x": 0, "y": 1.5}, "x": -0.5, "y": 0.0, "face": 180.0},
    ]
    got = simplan.map_score(sim)  # type: ignore[arg-type]
    assert got["rooms_found"] == ["kitchen", "living room"]
    assert got["things"] == {"piano": {"as": "grand piano", "room_ok": True, "off_m": 0.0}}
    assert got["doors"] == [{"door": "doorway", "joins_rooms": True, "off_m": 0.0}]
    assert got["here_off_m"] == 0.5 and got["drift_max_m"] == 0.5
    svg = simplan.svg(sim)  # type: ignore[arg-type]
    assert 'stroke="#7c3aed"' in svg and "grand piano" in svg
    assert simplan.map_score(_PlanSim()) == {}  # type: ignore[arg-type]
    assert simscen.SETUP["map_house"]["scene"] == "house"


def test_the_flat_has_its_own_explore_and_map_runs_from_the_living_room_corner() -> None:
    for name in ("explore_flat", "map_flat"):
        setup = simscen.SETUP[name]
        assert setup["scene"] == "flat" and setup["at"] == simscen.SETUP["flat_piano"]["at"]
    assert simscen.SETUP["explore_flat"]["person"] is None
    # The rooms that only walking out through the hallway reaches.
    assert set(simscen._FLAT_AWAY) == {"kitchen", "bedroom", "study"}


def test_a_room_he_calls_by_a_short_form_is_the_house_room() -> None:
    """mf9 drew the hallway as "hall"; "start" is no room of the flat's."""
    h = type("H", (), {"ROOMS": ["living room", "hallway", "kitchen"]})
    assert simplan._room_match(h, "hall") == "hallway"
    assert simplan._room_match(h, "the living room") == "living room"
    assert simplan._room_match(h, "start") == ""


def test_each_maze_house_has_its_own_explore_run_alone_from_its_start() -> None:
    for k in (1, 2, 3):
        name = f"explore_maze{k}"
        setup = simscen.SETUP[name]
        assert simscen.SCENARIOS[name].__name__ == name
        # No set-down of its own: the house puts him at its start, in the living room.
        assert setup["scene"] == f"maze{k}" and "at" not in setup and setup["person"] is None


def test_a_run_without_a_set_down_starts_at_its_houses_start() -> None:
    """The maze runs once stood him at the origin, which in maze2 is inside a wall."""
    house = type("L", (), {"start": (-2.9, 0.75, 45.0)})()
    assert simrun.Options(model="m").at is None
    assert simrun.set_down(None, house) == (-2.9, 0.75, 45.0)
    assert simrun.set_down(None, None) == (0.0, 0.0, 0.0)
    assert simrun.set_down((1.0, 2.0, 90.0), house) == (1.0, 2.0, 90.0)


simvision = importlib.import_module("dave_sim.vision")


def test_the_sims_depth_sense_labels_in_the_pages_own_words() -> None:
    page = (_SIM.parent / "patches" / "growbot-vision.js").read_text(encoding="utf-8")
    start = page.index("var VOCAB = [")
    words = json.loads(page[start + len("var VOCAB = ") : page.index("];", start) + 1])
    # A label is an index into the page's list: the two must not drift apart.
    assert list(simvision.VOCAB) == words
    got = {
        n: simvision.label_of(n)
        for n in (
            "floor",
            "rug",
            "house_ceiling",
            "house_wall_w",
            "house_lintel_0",
            "house_divide_s",
            "piano_case",
            "couch_arm_1",
            "bookshelf",
            "lamp_shade",
            "cat_body",
            "table_leg_1_1",
            "counter_top",
            "kettle",
            "face",
            "torso",
            "torso_core",
            "lower_leg_1",
            "phone",
            "wall_n",
            "ceiling",
            "mystery",
        )
    }
    assert got == {
        "floor": "floor",
        "rug": "floor",
        "house_ceiling": "ceiling",
        "house_wall_w": "wall",
        "house_lintel_0": "wall",
        "house_divide_s": "wall",
        "piano_case": "piano",
        "couch_arm_1": "sofa",
        "bookshelf": "shelf",
        "lamp_shade": "lamp",
        "cat_body": "animal",
        "table_leg_1_1": "table",
        "counter_top": "counter",
        "kettle": "counter",
        "face": "person",
        "torso": "person",
        "torso_core": "self",
        "lower_leg_1": "self",
        "phone": "self",
        "wall_n": "wall",
        "ceiling": "ceiling",
        "mystery": "unknown",
    }


def test_the_sims_phone_like_noise_wanders_in_scale_and_loses_some_pixels() -> None:
    np = pytest.importorskip("numpy")
    noise = simvision.Noise(rng=np.random.default_rng(1))
    depth = np.full((64, 48), 2.0)
    labels = np.full((64, 48), simvision.VOCAB.index("wall"), dtype=np.uint8)
    scales = []
    for _ in range(40):
        d, lab = noise.apply(depth, labels)
        live = d > 0
        scales.append(float(np.median(d[live])) / 2.0)
        assert 0.005 < 1 - live.mean() < 0.05
        assert 0.02 < (lab == 0).mean() < 0.09
    # Off by up to a few tenths, not the same every frame, and about right on the whole.
    assert 0.03 < float(np.std(scales)) < 0.15
    assert abs(float(np.mean(scales)) - 1) < 0.08
    # The input is left as it was.
    assert (depth == 2.0).all() and (labels == simvision.VOCAB.index("wall")).all()


def test_a_run_leaves_his_depth_sense_off_unless_asked() -> None:
    assert simrun.Options(model=None).vision == "off"


# ------------------------------------------------------------------ the exploring battery


def test_a_battery_start_turns_a_third_of_the_way_round_per_seed() -> None:
    assert simbat.start_of("flat", 0) == (-3.3, -2.4, -180.0)
    assert simbat.start_of("flat", 1) == (-3.3, -2.4, -60.0)
    assert simbat.start_of("flat", 2) == (-3.3, -2.4, 60.0)


class _Layout:
    ROOMS = {"living room": (0, 0, 2, 2), "kitchen": (2, 0, 4, 2), "hall": (0, 2, 4, 3)}

    def room_at(self, x: float, y: float) -> str:
        for n, (x0, y0, x1, y1) in self.ROOMS.items():
            if x0 <= x < x1 and y0 <= y < y1:
                return n
        return ""

    def distance_to(self, item: str, x: float, y: float) -> float:
        return 1.0

    ITEMS: list[Any] = []


class _Body:
    layout = _Layout()


def _state(t: float, x: float, up: bool = True) -> dict[str, Any]:
    return {
        "wall": t,
        "x": x,
        "y": 1.0,
        "z": 0.08 if up else 0.01,
        "nose_deg": 0.0,
        "pitch_deg": 50.0 if up else 5.0,
        "screen_up": up,
    }


def test_a_battery_run_is_scored_against_the_house() -> None:
    """Rooms he was really in, how long he sat still, and a fall counted once per time down."""
    sim = type("S", (), {})()
    sim.body = _Body()
    # 60 s still in the living room, then over into the kitchen, then down for 5 s and up.
    states = [_state(t * 0.08, 1.0) for t in range(750)]
    states += [_state(60 + t * 0.08, 1.0 + t * 0.002) for t in range(1, 750)]
    states += [_state(120 + t * 0.08, 2.5, up=t > 60) for t in range(1, 120)]
    sim.states = states
    sim.map_track = [{"here": {"x": 2.5, "y": 1.3}, "x": 2.5, "y": 1.0}]
    got = simbat.score(sim)
    assert got["reached"] == ["kitchen", "living room"] and (got["rooms"], got["rooms_total"]) == (
        2,
        3,
    )
    # The first 30 s can't be judged; still from 30 s to 60 s.
    assert 25 <= got["stuck_s"] <= 35
    assert got["falls"] == 1 and got["pose_off_m"] == 0.3
    md = simbat.table(
        [{"run": "flat-s0", "scene": "flat", "seed": 0, **got}, {"run": "x-s1", "error": "boom"}]
    )
    assert "| flat-s0 | 2/3 |" in md and "| x-s1 | error: boom |" in md
    assert "Every room reached in 0 of 2 runs" in md
