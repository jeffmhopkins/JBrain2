"""The committed GrowBot site (growbot/site/, built by growbot/patches/apply.py).

The box serves growbot/site/ straight from the bind mount, so these hold the plan's promises
on the bytes that ship (docs/plans/GROWBOT_PLAN.md §2, §2a): every speech path goes through
the box, nothing points the page at a third-party origin, the CSP hashes match the inline
code they allow, the vendored assets are the pinned ones, and the firmware dials the box.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_GROWBOT = Path(__file__).resolve().parents[3] / "growbot"
_SITE = _GROWBOT / "site"
_FIRMWARE = _GROWBOT / "firmware"


def _load_apply():
    path = _GROWBOT / "patches" / "apply.py"
    spec = importlib.util.spec_from_file_location("growbot_apply", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["growbot_apply"] = module
    spec.loader.exec_module(module)
    return module


apply = _load_apply()

_PAGES = sorted(_SITE.rglob("*.html"))
# Upstream's own code, which the vendored bundles are not: those carry their projects' default
# CDN strings for code paths the app never takes, and the CSP refuses them regardless.
_PIN = json.loads((_GROWBOT / "patches" / "upstream.json").read_text())
_VENDORED = {e["path"] for e in _PIN["vendor"]}
_CODE = [
    p
    for p in sorted(_SITE.rglob("*"))
    if p.suffix in {".html", ".js", ".mjs"} and p.relative_to(_SITE).as_posix() not in _VENDORED
]


def test_site_has_pages_and_the_creature() -> None:
    assert (_SITE / "v3" / "next.html").is_file()
    assert _PAGES


@pytest.mark.parametrize("page", _PAGES, ids=lambda p: p.relative_to(_SITE).as_posix())
def test_every_page_loads_the_box_voice_first(page: Path) -> None:
    first = re.search(r"<script[^>]*>", page.read_text(encoding="utf-8"))
    assert first is not None
    assert first.group(0) == '<script src="/growbot-box.js">'


def test_the_box_voice_replaces_speech_synthesis_and_posts_to_the_box() -> None:
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert 'Object.defineProperty(window, "speechSynthesis"' in shim
    assert 'fetch("/api/fish-tts"' in shim
    assert re.search(r"https?://", shim) is None


def test_the_creature_loads_the_face_right_after_the_box_voice() -> None:
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    tags = re.findall(r"<script[^>]*>", creature)
    assert tags[:2] == ['<script src="/growbot-box.js">', '<script src="/growbot-face.js">']
    assert (_SITE / "growbot-face.js").read_bytes() == (
        _GROWBOT / "patches" / "growbot-face.js"
    ).read_bytes()


def test_the_creature_shows_what_it_is_doing_top_left() -> None:
    """The status strip loads right after the face, before any page script, so its wrappers
    see every model call, photo, voice clip and leg frame the page makes."""
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    tags = re.findall(r"<script[^>]*>", creature)
    assert tags[:3] == [
        '<script src="/growbot-box.js">',
        '<script src="/growbot-face.js">',
        '<script src="/growbot-status.js">',
    ]
    strip = (_SITE / "growbot-status.js").read_text(encoding="utf-8")
    assert strip == (_GROWBOT / "patches" / "growbot-status.js").read_text(encoding="utf-8")
    assert re.search(r"https?://", strip) is None
    # Each hook is the page's own call site: the model and voice lanes, relay-transport's
    # frames, and the globals the VAD and every voice route keep.
    assert 'u.pathname === "/api/fish-tts"' in strip
    assert 'u.pathname !== "/api/llm" && u.pathname !== "/api/llm-stream"' in strip
    assert "body.indexOf('\"image_url\"')" in strip
    assert 'if (kind === "see") snap();' in strip
    assert "WebSocket.prototype.send = function" in strip
    assert "window.vadOn || window.recording" in strip
    assert "window.speakingUntil" in strip
    # The camera refills its frame ring every 2.5 s: an encode is not a look, so no hook on it.
    assert "toDataURL" not in strip
    assert '"/api/llm?target=openrouter"' in creature
    assert "var listenMode=false, vadRec=null, vadOn=false," in creature
    assert "var micStream=null, recording=false," in creature
    assert "var robotReady=false, speakingUntil=0," in creature
    transport = (_SITE / "relay-transport.js").read_text(encoding="utf-8")
    assert "JSON.stringify({ t: 'act', rid, steps, mode })" in transport


def test_the_strip_keeps_what_is_under_way_at_the_left_edge() -> None:
    """Running chips sort ahead of finished ones each render, and the strip drops below an
    overlay's logo bar instead of covering it."""
    strip = (_SITE / "growbot-status.js").read_text(encoding="utf-8")
    assert "if (ao !== bo) return ao ? -1 : 1;" in strip
    assert "return ao ? b.start - a.start : b.end - a.end;" in strip
    assert strip.index("order(t);") < strip.index(
        "for (var i = 0; i < entries.length; i++) {\n      var e"
    )
    assert 'document.querySelectorAll(".gb-entry-nav")' in strip
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '<nav class="gb-entry-nav"' in creature


def test_the_bottom_bar_never_fades() -> None:
    """Brain, pause, mute and the type box stay on screen (owner, 2026-11-12)."""
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "setFade(true)" not in creature
    # The keyboard shrinks the page rather than panning the strip and the face off screen.
    assert "viewport-fit=cover, interactive-widget=resizes-content" in creature
    assert "function setFade(f){" in creature


def test_the_wake_gate_shows_no_store_card_on_the_box() -> None:
    """Upstream's adopted / members / adopt-me card is its store; the box has none."""
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "if(!a||!window._wakeGateUp||window.__gbBox) return;" in creature
    assert creature.index("gbStoreGuardFrom(j); }catch(e){}   // 0928") < creature.index(
        "||window.__gbBox) return;"
    )
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.__gbBox = true;" in shim


def test_the_pixel_eyes_give_way_only_while_the_new_face_is_on() -> None:
    """Upstream's eyes are skipped, not deleted: ?face=pixel or a throw brings them back."""
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "var _gbF=window.gbFace&&window.gbFace.on()?window.gbFace:null;" in creature
    assert "if(!_gbF) for(side=-1;side<=1;side+=2)" in creature
    assert "if(dreaming&&!_gbF){" in creature
    assert "if(_gbF) _gbF.draw(cx,W,H,{" in creature
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert re.search(r'var DEFAULT = "(ostrich|robot|lantern|ember)";', face)
    assert 'face !== "pixel" && !broken' in face
    assert "broken = true;" in face


def _face_in_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    shim = "var window={addEventListener:function(){}},location={search:''};"
    out = subprocess.run(
        [node, "-e", f"{shim}\n{face}\n{script}"], capture_output=True, text=True, check=True
    )
    return out.stdout


def test_every_face_keeps_the_pupil_open_in_every_mood() -> None:
    """Sleepy's lids met and happy's cheek covered the pupil for the whole mood (owner,
    2026-11-12): now every emotion leaves at least a third of each eye open on every face, and
    the lantern draws its pupil always, not only while thinking or looking."""
    openings = json.loads(_face_in_node("console.log(JSON.stringify(window.gbFace.openings()));"))
    assert set(openings) == {"calm", "curious", "happy", "alert", "scared", "sleepy", "tired"}
    for mood, faces in openings.items():
        for kind, room in faces.items():
            assert room >= 0.36 - 1e-9, (mood, kind, room)
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert "showPupil" not in face


def _voiced(**g: Any) -> bool:
    setup = "".join(f"window.{k}={json.dumps(v)};" for k, v in g.items() if k != "guard")
    out = _face_in_node(
        f"{setup}console.log(JSON.stringify(window.gbFace.voiced({json.dumps(g['guard'])})));"
    )
    return bool(json.loads(out))


def test_the_mouth_follows_the_voice_not_the_ears_guard() -> None:
    """speakingUntil is dated when a line is queued, at 140 ms a character, so a slow Kokoro
    line outlived it with the mouth still, and a fetch moved it in silence (owner, 2026-11-12).
    The players decide; the device synth only inside the guard, since iOS can stick `speaking`."""
    playing = {"paused": False, "ended": False, "src": "blob:x"}
    assert _voiced(vtEngine="fish", fishAud=playing, guard=False)
    assert _voiced(vtEngine="fish", _fishWaLn={"st": 1}, guard=False)
    assert not _voiced(vtEngine="fish", _fishWaLn={"st": 0}, guard=True)
    assert not _voiced(vtEngine="fish", fishAud={**playing, "paused": True}, guard=True)
    assert not _voiced(vtEngine="fish", fishAud={**playing, "ended": True}, guard=True)
    # fishPrime's silent unlock clip is a data: URL, never a line.
    assert not _voiced(vtEngine="fish", fishAud={**playing, "src": "data:audio/wav"}, guard=True)
    assert _voiced(vtEngine="device", speechSynthesis={"speaking": True}, guard=True)
    assert not _voiced(vtEngine="device", speechSynthesis={"speaking": True}, guard=False)
    assert _voiced(vtEngine="fish", _songUntil=4e12, guard=False)
    assert _voiced(vtEngine="robot", guard=True)
    # A dead Kokoro falls back to the larynx, which has no player to ask.
    assert _voiced(vtEngine="fish", fishDead=True, guard=True)
    assert _voiced(vtEngine="fish", _fishWaTail=4e12, guard=False)
    assert not _voiced(vtEngine="robot", guard=False)


_FAKE_CANVAS = """
var log=[];
function ctx(){ var o={canvas:{width:720,height:1480}};
  return new Proxy(o,{get:function(t,k){ if(k in t) return t[k];
    if(k==="getTransform") return function(){ return {a:2,b:0,c:0,d:2,e:0,f:0}; };
    if(/^create.*Gradient$/.test(k)) return function(){ log.push(k);
      return {addColorStop:function(o,c){ log.push("stop("+o+","+c+")"); }}; };
    return function(){ log.push(k+"("+Array.prototype.map.call(arguments,function(v){
      return typeof v==="number"?v.toFixed(2):typeof v; }).join(",")+")"); }; },
    set:function(t,k,v){ log.push(k+"="+v); return true; }}); }
var document={createElement:function(){ var c=ctx();
  return {width:0,height:0,getContext:function(){ return c; }}; }};
"""


def _frames(face: str, voice: bool) -> dict[str, list[str]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    js = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    shim = f"var window={{addEventListener:function(){{}}}},location={{search:'?face={face}'}};"
    # The guard is the opposite of the player, so only the player can be moving the mouth.
    player = "true" if voice else "false"
    run = f"""
    window.vtEngine="fish";
    window.fishAud={{paused:!{player},ended:false,src:"blob:x"}};
    var out={{}};
    ["calm","curious","happy","alert","scared","sleepy","tired"].forEach(function(emo){{
      [false,true].forEach(function(dream){{
        var frames=[];
        for(var i=0;i<30;i++){{ log=[];
          window.gbFace.draw(ctx(),360,740,{{t:1000+i*66,emotion:emo,eye:1,smile:0.3,
            energy:0.4,blink:false,paused:false,dreaming:dream,think:false,orbit:0,
            looking:false,gx:0,gy:0,touchT:0,shake:0,talking:!{player},glow:"#fff"}});
          frames.push(log.join(";")); }}
        out[emo+"/"+dream]=frames; }}); }});
    console.log(JSON.stringify(out));
    """
    out = subprocess.run(
        [node, "-e", f"{_FAKE_CANVAS}{shim}\n{js}\n{run}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


@pytest.mark.parametrize("face", ["lantern", "ostrich", "robot", "ember"])
def test_every_face_moves_its_mouth_in_every_mood_while_it_talks(face: str) -> None:
    """The same frames drawn with and without a voice playing differ only in the mouth, so a
    face and mood whose mouth stood still while it talked would draw them identically; and the
    talking mouth keeps changing from frame to frame (owner, 2026-11-12: critical)."""
    quiet, talking = _frames(face, voice=False), _frames(face, voice=True)
    assert set(quiet) == set(talking) and len(quiet) == 14
    for key in quiet:
        differ = sum(q != t for q, t in zip(quiet[key][5:], talking[key][5:], strict=True))
        assert differ == 25, (face, key, differ)
        mouths = {t.replace(q, "") for q, t in zip(quiet[key][5:], talking[key][5:], strict=True)}
        assert len(mouths) > 5, (face, key)


def test_the_face_feels_the_phone_tilt() -> None:
    """Gravity's roll in the screen's plane rolls the pupils downhill and leans the head back
    toward level, through upstream's learned accel sign."""
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert 'window.addEventListener("devicemotion", function (ev) {' in face
    assert "window._IOS_G === -1 ? -1 : 1" in face
    # Landscape turns the screen with the phone, so its angle comes back off the roll.
    assert "Math.atan2(-x, y) + turn * Math.PI / 180" in face
    assert "aim.x = Math.max(-1.2, Math.min(1.2, aim.x + lean * 1.6));" in face
    assert "tilt: cur.tilt - lean * 180 / Math.PI * 0.45, sway: lean," in face
    assert face.count("d.sway * ") == 4


def test_the_eyes_load_after_the_status_strip_and_take_the_trackers_boxes() -> None:
    """The leg tracker hands every box it keeps to the eyes, from the worker and from the
    main-thread fallback, so its detector drives the pupils whenever it is running."""
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    tags = re.findall(r"<script[^>]*>", creature)
    assert tags[3] == '<script src="/growbot-eyes.js">'
    eyes = (_SITE / "growbot-eyes.js").read_text(encoding="utf-8")
    assert eyes == (_GROWBOT / "patches" / "growbot-eyes.js").read_text(encoding="utf-8")
    assert re.search(r"https?://", eyes) is None
    assert creature.count("if(window.gbEyes)window.gbEyes.boxes(boxes);") == 2
    # The tracker's glance is the eyes' only feed beside it, so it is quick while someone is
    # in view, and the face holds a look longer than the slowest glance.
    assert "if(sightOnly)idleEye=now-lastDetectAt<(now-lastFaceAt<5000?250:1000);" in creature
    face_js = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert "SEEN_MS = 1300;" in face_js
    assert 'import("/v3/face-worker-client.mjs")' in eyes
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert "see: function (x, y) {" in face


def _eyes_in_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    eyes = (_SITE / "growbot-eyes.js").read_text(encoding="utf-8")
    shim = (
        "var seen=[];var window={gbFace:{see:function(x,y){seen.push([x,y]);}}};"
        "var document={hidden:false};var setInterval=function(){};"
    )
    out = subprocess.run(
        [node, "-e", f"{shim}\n{eyes}\n{script}"], capture_output=True, text=True, check=True
    )
    return out.stdout


def test_the_eyes_look_toward_the_face_on_screen() -> None:
    """The front camera's frame is not mirrored, so a face on the frame's left is on the
    screen's right, and the biggest face wins."""
    seen = json.loads(
        _eyes_in_node(
            "window.gbEyes.boxes([{x:0.05,y:0.1,width:0.1,height:0.1},"
            "{x:0.1,y:0.6,width:0.3,height:0.3}]);"
            "window.gbEyes.boxes([]);"
            "console.log(JSON.stringify(seen));"
        )
    )
    assert len(seen) == 1
    x, y = seen[0]
    assert x > 0.5 and y > 0.2


def test_the_eyes_never_run_a_second_detector_beside_the_tracker() -> None:
    """A second GPU worker could make the tracker's frame miss its 350 ms deadline, which drops
    leg tracking to the main thread for good, so the eyes' own detector waits while it is on;
    and it needs the creature's open front camera, awake and visible."""
    out = _eyes_in_node(
        "var v={readyState:4,paused:false};"
        "function cam(o){window.camV=v;window.camOn=true;window.camFacing='user';"
        "window.paused=false;window.dreaming=false;document.hidden=false;"
        "window._faceTrack=null;for(var k in o)window[k]=o[k];return window.gbEyes.wants();}"
        "console.log(JSON.stringify([cam({}),cam({_faceTrack:{enabled:true}}),"
        "cam({_faceTrack:{enabled:false}}),cam({camOn:false}),cam({camFacing:'environment'}),"
        "cam({dreaming:true}),cam({paused:true})]));"
    )
    assert json.loads(out) == [True, False, True, False, False, False, False]


def test_a_persona_wears_its_own_face_lantern_by_default() -> None:
    """The box's face for the persona is read before the face loads, a poll's new one changes
    it where it stands, and only ?face= outranks it."""
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert 'var DEFAULT = "lantern";' in face
    assert (
        face.index("if (valid(asked)) return asked;")
        < face.index("if (valid(window.__gbBoxFace)) return window.__gbBoxFace;")
        < face.index("localStorage.gb_face ||")
    )
    assert 'window.addEventListener("gb-box-state", function (ev) {' in face
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert shim.index("window.__gbBoxFace = state.json.face;") < shim.index(
        "if (state.status === 204) {"
    )
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert page.index('<script src="/growbot-box.js">') < page.index(
        '<script src="/growbot-face.js">'
    )


def test_a_persona_speaks_in_its_own_voice() -> None:
    """The box's voice for the persona is read with the soul, a poll's new one is the next
    line's, and it outranks the phone's own on both speech paths and in the clip cache."""
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.__gbBoxVoice = state.json.voice;" in shim
    assert 'if (typeof v === "string") window.__gbBoxVoice = v;' in shim
    assert 'voice: window.__gbBoxVoice || "", model: "kokoro"' in shim
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "function fishKey(t,emo){ return t+'|'+(window.__gbBoxVoice||vtFish.voice||'')+" in page
    assert "body:JSON.stringify({text:t,voice:window.__gbBoxVoice||vtFish.voice||''," in page
    assert "voice:vtFish.voice||''" not in page


def test_the_creature_can_recolour_itself_like_the_sky() -> None:
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    assert prompts.index('\'"coat":"OMIT usually') < prompts.index('\'"sky":"OMIT usually')
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "window.gbFace.setColour(o.coat,_hueOf(o.coat))" in creature
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert "localStorage.gb_face_color" in face


def test_the_creature_switches_cameras_for_its_person_or_the_owner() -> None:
    """The eye's lens follows an owner edit from the box and a reply's `eye` on a turn that
    answers its person; an autonomous beat, a dream, or a lens it already has changes nothing."""
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    assert prompts.index('\'"eye":"OMIT almost always') < prompts.index('\'"coat":"OMIT usually')
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert (
        'if(_hreqAtIssue&&!dreaming&&(o.eye==="front"||o.eye==="back"))'
        ' gbEyeTo(o.eye==="back"?"environment":"user","self");'
    ) in page
    start = page.index("function gbEyeTo(to,by){")
    code = page[start : page.index("function flipEye(target){", start)]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = (
        "var camFacing='user', flips=[], logs=[], label={textContent:''}, h={};"
        "var window={addEventListener:function(n,f){ h[n]=f; }};"
        "function $(id){ return id==='bpmEye'?label:null; }"
        "function logEv(n,d){ logs.push([n,d]); }"
        "function flipEye(t){ flips.push(t); camFacing=t; }\n"
        + code
        + "h['gb-box-prefs']({detail:{changed:{gb_eye:'environment'}}});"
        "h['gb-box-prefs']({detail:{changed:{gb_eye:'environment'}}});"
        "h['gb-box-prefs']({detail:{changed:{gb_face:'x'}}});"
        "var mid=label.textContent; h['gb-box-prefs']({detail:{changed:{gb_eye:null}}});"
        "var bad=gbEyeTo('sideways','self');"
        "console.log(JSON.stringify({flips:flips,logs:logs,mid:mid,end:label.textContent,bad:bad}));"
    )
    out = subprocess.run([node, "-e", harness], capture_output=True, text=True, check=True)
    got = json.loads(out.stdout)
    assert got["flips"] == ["environment", "user"]
    assert got["logs"] == [
        ["eye_set", {"to": "environment", "by": "owner"}],
        ["eye_set", {"to": "user", "by": "owner"}],
    ]
    assert got["mid"] == "👁 eye: back"
    assert got["end"] == "👁 eye: front (screen side)"
    assert got["bad"] is False


def test_mespeak_is_not_shipped_and_kokoro_is_the_only_engine() -> None:
    creature = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "/v2/mespeak.js" not in creature
    assert not (_SITE / "v2" / "mespeak.js").exists()
    assert 'var vtEngine="fish"' in creature
    assert '<option value="robot">' not in creature
    assert '<option value="device">' not in creature
    assert re.search(r'vtEngine=(?!=)[^;]*"(robot|device)"', creature) is None


@pytest.mark.parametrize("path", _CODE, ids=lambda p: p.relative_to(_SITE).as_posix())
def test_no_third_party_origin_in_code(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for origin in (
        "growbot-relay.growbot.workers.dev",
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "cdn.jsdelivr.net",
        "storage.googleapis.com",
        "/feedback-widget.js",
    ):
        assert origin not in text, origin


def test_relay_dials_the_page_origin() -> None:
    relay = (_SITE / "relay-transport.js").read_text(encoding="utf-8")
    assert "RELAY_DEFAULT = (location.protocol === 'https:' ? 'wss://' : 'ws://')" in relay


def test_csp_hashes_match_the_inline_code_shipped() -> None:
    expected: set[str] = set()
    handlers = False
    for page in _PAGES:
        page_hashes, page_handlers = apply.csp_hashes(page.read_text(encoding="utf-8"))
        expected |= page_hashes
        handlers = handlers or page_handlers
    csp = json.loads((_SITE / "csp.json").read_text())
    assert set(csp["script_hashes"]) == expected
    assert csp["unsafe_hashes"] is handlers


@pytest.mark.parametrize("entry", _PIN["vendor"], ids=lambda e: e["path"])
def test_vendored_assets_are_the_pinned_bytes(entry: dict[str, str]) -> None:
    data = (_SITE / entry["path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert entry["url"].startswith("https://")


def test_firmware_dials_the_box_with_its_key() -> None:
    chip = (_FIRMWARE / "relay_chip.py").read_text(encoding="utf-8")
    assert 'HOST = getattr(_box, "GROWBOT_HOST", None)' in chip
    assert "socket.getaddrinfo(HOST, PORT)" in chip
    assert "s = _wrap(raw)" in chip
    assert "ctx.verify_mode = ssl.CERT_REQUIRED" in chip
    assert 'raise RuntimeError("secrets.py has no GROWBOT_HOST' in chip
    assert '"key": ROBOT_KEY' in chip
    assert "% (PATH, SNI, key)" in chip
    compile(chip, "relay_chip.py", "exec")
    # The clock is set only from a reply that echoes this request's random stamp.
    assert "query[40:48] = nonce" in chip and "msg[24:32] != nonce" in chip


def test_csp_hashes_cover_tags_handlers_and_innerhtml_handlers() -> None:
    page = (
        '<p onclick="a(&quot;x&quot;)">t</p>'
        '<script src="/x.js" onerror="e()"></script>'
        "<script>h('<b onclick=\"f(\\'y\\')\">');</script>"
    )
    hashes, handlers = apply.csp_hashes(page)
    assert handlers is True
    assert apply._sha(b'a("x")') in hashes
    assert apply._sha(b"e()") in hashes
    assert apply._sha(b"f('y')") in hashes
    assert apply._sha(b"h('<b onclick=\"f(\\'y\\')\">');") in hashes
    assert len(hashes) == 4


def test_a_patch_that_no_longer_matches_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "a.js").write_text("one\n")
    with pytest.raises(apply.PatchError, match="expected 1"):
        apply._patch(tmp_path, [("a.js", "two", "three", 1)])
    apply._patch(tmp_path, [("a.js", "one", "uno", 1)])
    assert (tmp_path / "a.js").read_text() == "uno\n"


def test_a_vendored_asset_with_the_wrong_hash_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.bin").write_bytes(b"tampered")
    entry = {"path": "x.bin", "url": "https://example.invalid/x", "sha256": "0" * 64}
    with pytest.raises(apply.PatchError, match="sha256"):
        apply._fetch(entry, tmp_path)


# A runtime load of an absolute URL: fetch, import, a socket, a worker, or a src/href that the
# browser follows on its own. A link a person taps is not one of these.
_RUNTIME_LOAD = re.compile(
    r"""(?:fetch|import|WebSocket|EventSource|Worker|SharedWorker|importScripts)\s*\(\s*["'`]https?://"""
    r"""|<(?:script|img|audio|video|source|iframe|link)\b[^>]*\s(?:src|href)=["']https?://""",
    re.I,
)


@pytest.mark.parametrize("path", _CODE, ids=lambda p: p.relative_to(_SITE).as_posix())
def test_no_runtime_load_of_any_absolute_url(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert _RUNTIME_LOAD.search(text) is None


def test_handler_shapes_the_build_cannot_hash_are_refused() -> None:
    assert apply._unsupported_handlers("el.setAttribute('onclick', 'x()')")
    assert apply._unsupported_handlers('h("<b onclick=\\"x()\\">")')
    assert apply._unsupported_handlers('<b onclick="x()">') == []


def test_a_failed_build_leaves_the_committed_tree_untouched(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    (upstream / "app").mkdir(parents=True)
    (upstream / "app" / "relay-transport.js").write_text("changed upstream\n")
    out = tmp_path / "out"
    (out / "site").mkdir(parents=True)
    (out / "site" / "kept.txt").write_text("committed\n")
    with pytest.raises(apply.PatchError):
        apply.build(upstream, out, None)
    assert (out / "site" / "kept.txt").read_text() == "committed\n"
    assert sorted(p.name for p in out.iterdir()) == ["site"]


def test_the_creature_installs_as_an_app() -> None:
    """Android's Install app needs a manifest with a start page and 192/512 icons; the
    sidecar serves exactly these paths without a session (serve.py PUBLIC_PATHS)."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '<link rel="manifest" href="/manifest.webmanifest">' in page
    manifest = json.loads((_SITE / "manifest.webmanifest").read_text())
    assert manifest["start_url"] == "/start" and manifest["scope"] == "/"
    assert manifest["display"] in ("fullscreen", "standalone")
    sizes = set()
    for icon in manifest["icons"]:
        data = (_SITE / icon["src"].lstrip("/")).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        assert icon["sizes"] == f"{width}x{height}"
        sizes.add(width)
    assert {192, 512} <= sizes


def test_the_service_worker_is_registered_and_caches_nothing() -> None:
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert 'navigator.serviceWorker.register("/sw.js"' in shim
    worker = (_SITE / "sw.js").read_text(encoding="utf-8")
    assert 'event.request.mode !== "navigate"' in worker
    assert 'pathname === "/open") return;' in worker
    assert (_SITE.parent / "patches" / "pwa" / "sw.js").read_text(encoding="utf-8") == worker
    assert "caches." not in worker and "cache.put" not in worker


def test_the_setup_page_follows_only_this_boxs_ticket_link() -> None:
    """The second QR is the only thing the setup page opens, so a code from anywhere else
    (another site, another box) goes nowhere."""
    page = (_SITE / "setup.html").read_text(encoding="utf-8")
    assert 'href="/box-root.crt"' in page and '<script src="/setup.js">' in page
    scanner = (_SITE / "setup.js").read_text(encoding="utf-8")
    assert "url.origin !== location.origin" in scanner
    assert 'url.pathname !== "/open"' in scanner
    assert "location.replace(link)" in scanner


def test_the_creature_page_starts_on_the_boxs_soul() -> None:
    """G4b (plan §2c): a persona's soul is pulled into pb2_soul before soul.js reads it (the
    loader is the creature's first script, and the pull is synchronous), only on the creature
    page, and every write of the key goes back to the box."""
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert shim == (_GROWBOT / "patches" / "growbot-box.js").read_text(encoding="utf-8")
    sync = shim[shim.index('if (location.pathname !== "/v3/next.html") return;') :]
    assert "r.open(method, path, false);" in sync
    assert 'var KEY = "pb2_soul"' in sync and 'PATH = "/api/box/persona"' in sync
    assert "Storage.prototype.setItem = function" in sync
    # The pull lands before the hook, so the box's own copy is never pushed back as a write.
    assert sync.index("put(KEY, JSON.stringify(pulled.json.soul))") < sync.index(
        "Storage.prototype.setItem ="
    )
    # An unsent write goes only into the persona it came from: the page checks, and every push
    # names it for the box to check too.
    assert "m.persona === persona" in sync
    soul_only = sync[: sync.index("/* A persona's settings live on the box too")]
    assert soul_only.count("JSON.stringify({ persona: ") == 2
    # The phone's own (G2) creature is set aside while a persona has the key.
    assert 'OWN = "gb_phone_soul"' in sync and "put(OWN, mine)" in sync


def test_the_creatures_own_pairing_box_opens_nothing() -> None:
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.prompt = function (message)" in shim
    assert "/pairing code/i.test(String(message))" in shim
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert 'window.prompt("Pairing code shown for your robot:"' in page


def test_visual_memories_follow_the_persona() -> None:
    """G4d (plan §2c): each persona's saved views are its own namespace, merged in from the box
    before the creature opens its store, and every write the creature makes to it is sent."""
    vm = (_SITE / "v3" / "visual-memory.js").read_text(encoding="utf-8")
    assert "function vmNs(){ try{ return window.__gbBoxVmNs||" in vm
    assert "w.then(vmOpenNow,vmOpenNow)" in vm and "function vmOpenNow(){" in vm
    assert 'if(mode==="readwrite"&&typeof window.gbBoxMemChanged==="function")' in vm
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "/v3/visual-memory.js?v=1005-mem-box" in page
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    mem = shim[shim.index('var PREFIX = "gb_box_vm/"') :]
    assert 'PATH = "/api/box/memories"' in mem
    assert "window.gbBoxMemReady = Promise.race(" in mem and "window.gbBoxMemChanged = " in mem
    # Pinned per tab, never through shared storage; a G2 session (no persona) pins nothing.
    assert "window.__gbBoxVmNs = NS;" in mem and "gb_box_vm_ns" not in shim
    # Moved, deleted or a G2 session: every persona's faces leave the phone.
    assert "var leaving = !!(soul.g2 || soul.away);" in mem and 'purge(d, "")' in mem
    assert "window.__gbBoxSoul = { away: true };" in shim
    # A record the merge could not fetch never reloads the page, nor does one version twice.
    assert "missing[id] !== fp(box.rows[id])" in mem and "m.mergedAt === box.version" in mem
    # Writes name the persona this tab runs as, for the sidecar to check.
    assert mem.count("{ persona: persona, ") == 2
    # Only a phone with unsent writes forgets on the box what it lacks.
    assert "known[id] !== undefined && m.dirty" in mem


def test_the_creature_says_why_the_box_failed_it() -> None:
    """A slow, stopped or refusing local model reads as itself, not as upstream's cloud lines
    or a bare abort message, and a dropped clip says which check dropped it."""
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.gbBoxEars = function (e)" in shim
    assert "window.gbBoxBrainWhy = function (e)" in shim
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert page.count("window.gbBoxEars?window.gbBoxEars(e)") == 2
    assert "window.gbBoxBrainWhy?window.gbBoxBrainWhy(e):e.message" in page
    # The box's words ride a property: the page's catch pattern-matches the message.
    assert "if(streamErr&&!L)_ee.boxSaid=streamErr;" in page
    assert 'if (e && typeof e.boxSaid === "string")' in shim
    # Spoken turns wait 90 s, ticks keep 30, and a timed-out stream is not retried.
    assert page.count("_decisionTokens,90000") == 2 and page.count("_decisionTokens,30000") == 1
    assert 'e.gateFail||(e&&e.name==="AbortError")||' in page
    assert 'toast("\u2026didn\'t catch that")' not in page
    assert "the speech model heard no words" in page
    # Both transcriber lanes are the box's whisper: no 8 s/12 s cloud leash re-sending the clip.
    assert page.count("_lease=65000;") == 2 and "_lease=_pri?8000:12000" not in page
    assert "diTranscribe(blob,type,meta); return;" not in page
    assert "groqTranscribe(blob,type,meta); return;" not in page


def test_the_system_prompt_holds_only_what_stays_put() -> None:
    """The box's hybrid model reuses a cached prompt only up to a checkpoint at a message
    boundary, so a turn is fast only when the system message is the same text as the last
    turn's. The state, clock, diary, self-stats and recall ride the turn's own message."""
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    start = prompts.index("function buildSystem(")
    body = prompts[start : prompts.index("\n}\n", start)]
    assert body.endswith("\n  return _gbStatic + buildOutputSchema(soul.body);")
    live = body[body.index("buildSystem.live = ") : body.index("  return _gbStatic")]
    moving = ("languageContext(", "== LONG-ARC", "== SCRATCHPAD", "clock", "== RECENT DIARY")
    for part in moving:
        assert part in live, part
    static = body[body.index("var _gbStatic = ") : body.index("buildSystem.live = ")]
    assert "SAFETY_FLOOR" in static and "== YOUR IDENTITY" in static and "clock" not in static
    # The automatic reply-language line rewrites itself once speech settles a language.
    assert "languageContext(" not in static

    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '_gbLive=(buildSystem.live||"")+(moveGuideText.live||"")' in page
    # The movement guide's live lines (latest move, seek beat, stance) leave it in every mode.
    guide = page[page.index("function moveGuideText(){") : page.index("/* ---- MIMIC")]
    assert guide.count("moveGuideText.live=") == 3
    for moving in (
        "goalBlock()",
        "gaitList()",
        "_recStatus()",
        "_namedStatus()",
        "_expressionGuide()",
    ):
        for line in guide.splitlines():
            if moving in line and "return " in line:
                assert line.index("moveGuideText.live=") < line.index(moving), moving
    assert "_gbLive+=MEMLOG_ASK; if(_recall&&_recall.text) _gbLive+=_recall.text;" in page
    fixed = (
        'sys+=(typeof gaitList==="function"&&gaitList.fixed||"")'
        '+(typeof selfStats==="function"&&selfStats.fixed||"")'
        '+(typeof vmPrompt==="function"&&vmPrompt.fixed||"");'
    )
    assert "sys+=" not in page[
        page.index("var sys=buildSystem(") : page.index("var slug=brainOverride(")
    ].replace("sys+=_gbLive;", "").replace(fixed, "")
    assert '<script src="/v3/prompts.js?v=1008-motion-box-lang-eye-talk-recall-sleep"' in page
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.gbBoxLive = function (msgs, live)" in shim


def test_the_fixed_rules_ride_the_system_message_and_the_state_stays_live() -> None:
    """The gait rules, the picture-memory contract and the fixed self-description are the same
    every turn, so they join the cached system message; every sentence that depends on state
    stays in the turn's own message, or one change would make each turn re-read it all."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    stats = page[page.index("function selfStats(){") : page.index("/* ===== BRAIN TAB")]
    fixed = page[page.index('var GB_SELF_FIXED="') : page.index("function selfStats(){")]
    live = stats[stats.index('return "== WHAT YOU ARE RIGHT NOW') :]
    for moving in ("_bootT", "callsSinceBoot", "seekGoal", "camOn", "moving", "lastMimic"):
        assert moving in live and moving not in fixed, moving
    for rule in ("You think whenever", "Quiet does not put you to sleep", "don't deflect"):
        assert rule in fixed and rule not in live, rule
    assert 'selfStats.fixed=GB_SELF_FIXED.replace("{model}",model);' in stats

    spin = page[page.index("function normalSpinTruth(){") : page.index("function gaitList(){")]
    assert "normalSpinTruth.now=normal;" in spin
    assert '"+normal+"' not in spin
    gaits = page[page.index("function gaitList(){") : page.index("// one-mover-at-a-time")]
    rules = gaits[gaits.index("gaitList.fixed=") : gaits.index("  return spinFeedback")]
    now = gaits[gaits.index("  return spinFeedback") :]
    assert "== TRAINED WALKS" in rules and "normalSpinTruth()" in rules
    for moving in ("forwardWalkState", "lines.join", "normalSpinTruth.now", "spinMeasLine"):
        assert moving in now and moving not in rules, moving

    vm = (_SITE / "v3" / "visual-memory.js").read_text(encoding="utf-8")
    prompt = vm[vm.index("function vmPrompt(c){") : vm.index("function vmContract(){")]
    contract = vm[
        vm.index("function vmContract(){") : vm.index("\n}", vm.index("function vmContract"))
    ]
    assert 'vmPrompt.fixed=vmOn()?vmContract():"";' in prompt
    assert "VISUAL MEMORY overrides" in contract and "vmCatalogLine" not in contract
    assert "vmCatalogLine()" in prompt and "VM_PROMPT_OFF" in prompt
    assert '<script src="/v3/visual-memory.js?v=1005-mem-box-fixed2"' in page


def test_the_brain_panel_keeps_only_what_a_box_creature_uses() -> None:
    """The box picks the model and holds the soul, and there are no credits or keys: those
    rows hide behind the shim's flag, and the page without it keeps upstream's panel."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    for row in (
        'h+=wRow("dial","brain","","the brain dial"',
        'h+=wRow("soul","soul","#9A86FF","soul"',
        'h+=wRow("byok","brain","#FFB24D","your own key"',
        'h+=hatch("mind");',
        'h+=\'<button type="button" id="gbkAdvTog"',
    ):
        assert page.count("if(!window.__gbBox)" + row) == 1, row
    assert 'function brainLogsSection(){ if(window.__gbBox) return "";' in page
    assert 'function moveMeRow(){ if(window.__gbBox) return "";' in page
    assert "if(W.adv&&!window.__gbBox){" in page
    assert page.count('data-gb-cloud="1"') == 1
    assert '(_t==="c"&&!window.__gbBox)' in page
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "window.__gbBox = true;" in shim
    assert "[data-gb-cloud]{display:none!important}" in shim
    assert "#giftBtn,#bpTabByok,#bpTabCredits{display:none!important}" in shim


def test_the_text_is_phone_sized_even_in_desktop_site_mode() -> None:
    """The spoken line and the brain panel are raised to phone sizes, and a phone that lays the
    page out ~980px wide (desktop site) has its controls zoomed back, never the face's canvas."""
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert '"#say{font-size:" + 15 * k + "px!important' in shim
    assert '"#gbkBBody{zoom:1.2}"' in shim
    assert '"body>*:not(#wrap){zoom:"' in shim
    assert "return k > 1.25 ? Math.min(k, 3) : 1;" in shim
    assert 'window.addEventListener("resize", paint);' in shim


def test_past_replies_go_back_to_the_model_in_its_own_json_shape() -> None:
    """Replayed as bare words, the creature's history taught the model to answer in clipped
    plain text; each past reply now rides as {"say", "emotion"}, a page note as an empty say."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert 'msgs.push({role:"assistant",content:gbTraceReply(tr[i])});' in page
    assert 'content:tr[i].d?tr[i].a+"\\n(did: "' not in page
    assert ',e:(o&&typeof o.emotion==="string"?o.emotion.slice(0,12):undefined)};' in page
    start = page.index("function gbTraceReply(t){")
    fn = page[start : page.index("\n", start)]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    cases = (
        "[gbTraceReply({a:'Hi \"Jeff\"',e:'happy'}),"
        "gbTraceReply({a:'(stayed quiet)',d:'spin 2 s'}),"
        "gbTraceReply({a:'(brain hiccup \u2014 out of energy, no answer)'}),"
        "gbTraceReply({a:'(purrs) hi'}),gbTraceReply({})]"
    )
    script = f"{fn}\nconsole.log(JSON.stringify({cases}));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    said, quiet, hiccup, spoken, empty = json.loads(out.stdout)
    assert json.loads(said) == {"say": 'Hi "Jeff"', "emotion": "happy"}
    assert quiet == '{"say":""}\n(stayed quiet)\n(did: spin 2 s)'
    assert hiccup.startswith('{"say":""}\n(brain hiccup')
    # A say the model wrote in brackets is still words it spoke.
    assert json.loads(spoken) == {"say": "(purrs) hi"}
    assert json.loads(empty) == {"say": ""}


def test_a_spoken_turn_sends_one_frame_unless_something_is_moving() -> None:
    """Each frame is ~300 tokens of prefill on the box; three only when there is change to see."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    line = page[page.index("var _nf=(kind===") :]
    line = line[: line.index(";")]
    assert line.endswith(
        ":((frameDiff>3||_rideOn||Date.now()<selfMoveUntil||(extra&&extra.beat&&seekGoal)"
        '||(extra&&typeof extra.visualUser==="string"&&_GB_LOOK_WORDS.test(extra.visualUser)))'
        "?3:1)"
    )
    words = page[page.index("var _GB_LOOK_WORDS=/") :]
    words = words[len("var _GB_LOOK_WORDS=/") : words.index("/i;")]
    look = re.compile(words.replace("\\b", r"\b"), re.IGNORECASE)
    for said in ("watch this", "what am I holding?", "What's this", "can you see me"):
        assert look.search(said), said
    for said in ("how are you", "spin to your left", "tell me a story"):
        assert not look.search(said), said
    assert "each thought you see the freshest one, or the 3 freshest" in page


def _stream_js(page: str) -> str:
    start = page.index("var _gbPieceNext=null")
    return page[start : page.index("function _gbPcmUttStop0(")]


_STREAM_HARNESS = r"""
var _pcmRate=1000, _pcmLive=true, _pcmUtt=null, vadVoiceT=0, _vadHold=1500, now=0;
Date.now=function(){ return now; };
var timers=[]; setTimeout=function(f,ms){ timers.push({f:f,at:now+(ms||0)}); };
function run(){ timers.sort(function(a,b){ return a.at-b.at; });
  while(timers.length&&timers[0].at<=now){ timers.shift().f(); } }
var sent=[], shown=[], stt=[], old=[];
function encodeWav(f,r,d,c){ return {len:f.length}; }
var _vadEndT=0, _imitationCtl=null; var document={getElementById:function(){ return null; }};
function Blob(parts){ this.len=parts[0].len; }
function transcribe(b,t,ms){ stt.push({p:_gbPieceNext,len:b.len}); _gbPieceNext=null; }
function showYouSaid(t){ shown.push(t); } function sendTxtFrom(t,m){ sent.push(t); }
function logEv(){} function _convLangSaw(){}
function _gbPcmUttStop0(s,t){ old.push(s); }
function utt(){ _pcmUtt={buf:new Float32Array(13000),n:0,pre:0,t0:now,nf:0}; _pcmLive=true; }
function feed(ms,voice){ for(var i=0;i<ms;i+=100){ var d=new Float32Array(100), u=_pcmUtt;
  var take=Math.min(u.buf.length-u.n,100); u.buf.set(d.subarray(0,take),u.n); u.n+=take;
  if(voice) vadVoiceT=now; _gbRollCheck(d,take); now+=100; run(); } }
function answer(i,txt){ var p=stt[i].p; _gbPiece(p,txt,{lang:"en"},"en"); }
"""


def _run_stream(page: str, scenario: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        _stream_js(page)
        + _STREAM_HARNESS
        + scenario
        + "\nconsole.log(JSON.stringify({sent:sent,shown:shown,stt:stt.length,old:old}));"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_speech_is_transcribed_in_pieces_and_reaches_the_creature_as_one_turn() -> None:
    """A long message goes to the transcriber at its pauses and at the buffer's end while the
    person keeps talking, nothing past the old 12 s cap is lost, and the creature gets one turn
    with every piece in order."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    long_talk = """
    utt(); feed(5000,true); feed(600,false);            // 5 s then a pause: piece 0
    feed(13000,true);                                   // a full buffer: piece 1, no audio lost
    feed(2000,true); pcmUttStop(true,300); run();       // the end: piece 2, the final
    answer(1,"second part"); answer(0,"first part,");
    answer(2,"and the end.");
    """
    got = _run_stream(page, long_talk)
    assert got["stt"] == 3
    assert got["sent"] == ["first part, second part and the end."]
    assert got["old"] == []
    assert "first part, second part …" in got["shown"]

    # A short message never splits and goes the old way.
    short = "utt(); feed(3000,true); pcmUttStop(true,300); run();"
    got = _run_stream(page, short)
    assert got["stt"] == 0 and got["old"] == [True] and got["sent"] == []


def test_a_lost_or_silent_piece_never_holds_the_turn() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    # Piece 0 never answers; the end is silence, so no final piece is sent: the turn goes after
    # the 12 s give-up with what did come back.
    lost = """
    utt(); feed(5000,true); feed(600,false); feed(4500,true); feed(600,false);
    feed(1200,false); pcmUttStop(true,300); run();
    answer(1,"the words that came back");
    now+=25000; run();
    answer(0,"late words");
    """
    got = _run_stream(page, lost)
    # What came back goes after the give-up; a piece landing later still reaches the creature.
    assert got["stt"] == 2 and got["sent"] == ["the words that came back", "late words"]

    # A cancelled utterance (its own voice, pause, a hidden tab) sends nothing, as upstream.
    cancelled = """
    utt(); feed(5000,true); feed(600,false); feed(1000,true); pcmUttStop(false,0); run();
    answer(0,"hello there");
    """
    got = _run_stream(page, cancelled)
    assert got["sent"] == []


def test_a_streamed_utterance_outlives_the_old_twelve_second_cap() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "else if(now-vadStartT > (_pcmLive&&_pcmUtt?GB_UTT_MAX_MS:12000)){ vadStop();" in page
    assert "_gbFinal(_gbU,true,0)" in page[page.index("function pcmTapDown(){") :][:300]
    # A long message gets a longer pause before its turn is over.
    assert (
        "if(now-vadVoiceT > (_pcmUtt&&_pcmUtt.gbId?Math.max(_vadHold,GB_LONG_HOLD_MS):_vadHold))"
    ) in page
    # Its own beats hold while a streamed message is live or its pieces are landing.
    assert (
        "if((vadOn&&(now-vadStartT<6000||(_pcmLive&&_pcmUtt&&_pcmUtt.gbId)))||now<vadHoldUntil"
        "||Object.keys(_gbUtts).length){ lastActT=now; return; }"
    ) in page
    # 30 s of talk with no pause: the full buffer rolls twice and nothing is dropped.
    nonstop = """
    utt(); feed(30000,true); pcmUttStop(true,300); run();
    for(var i=0;i<stt.length;i++) answer(i,"part"+i);
    """
    got = _run_stream(page, nonstop)
    assert got["stt"] == 3 and got["sent"] == ["part0 part1 part2"]


def test_an_ordinary_quiet_beat_waits_ninety_seconds_after_the_last_activity() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "var GB_QUIET_AFTER_MS=90000;" in page
    assert "if(idle>=GB_QUIET_AFTER_MS && now-lastBeatT>240000){" in page
    assert "idle>=8000 && now-lastBeatT" not in page
    assert page.index("var GB_QUIET_AFTER_MS") < page.index("if(idle>=GB_QUIET_AFTER_MS")
    assert "90 seconds after anything last happened" in page


# A browser just big enough for growbot-box.js's settings sync: a Storage whose prototype the
# sync hooks, a synchronous XHR and a fetch answered by `box`, and timers run by hand.
_PREFS_HARNESS = r"""
const calls = [], events = [];
let timers = [];
class Storage {
  constructor() { this._d = new Map(); }
  get length() { return this._d.size; }
  key(i) { return [...this._d.keys()][i] ?? null; }
  getItem(k) { return this._d.has(k) ? this._d.get(k) : null; }
  setItem(k, v) { this._d.set(String(k), String(v)); }
  removeItem(k) { this._d.delete(String(k)); }
}
const store = new Storage();
for (const [k, v] of Object.entries(START)) store.setItem(k, v);
const listeners = {};
const window = {
  localStorage: store, __gbBoxSoul: SOUL,
  addEventListener(t, f) { (listeners[t] = listeners[t] || []).push(f); },
  dispatchEvent(ev) {
    if (ev.type === "gb-box-prefs") events.push(ev.detail);
    (listeners[ev.type] || []).forEach(f => f(ev));
  },
};
function answer(method, body) {
  calls.push({ method, body: body ? JSON.parse(body) : null });
  const [s, j] = box(method, body ? JSON.parse(body) : null);
  // The sidecar names the session's persona on every answer.
  return [s, j && typeof j === "object" && !("persona" in j) ? { ...j, persona: SOUL.persona } : j];
}
class XMLHttpRequest {
  open(m, p) { this.m = m; }
  setRequestHeader() {}
  send(b) {
    const [s, j] = answer(this.m, b);
    this.status = s;
    this.responseText = JSON.stringify(j);
  }
}
function fetch(path, opts) {
  const [s, j] = answer((opts && opts.method) || "GET", opts && opts.body);
  return Promise.resolve({ status: s, ok: s === 200, json: () => Promise.resolve(j) });
}
class CustomEvent { constructor(type, o) { this.type = type; this.detail = o && o.detail; } }
const location = { pathname: "/v3/next.html" };
const document = { hidden: false, addEventListener() {} };
function setTimeout(f) { timers.push(f); return timers.length; }
function clearTimeout() {}
const intervals = [];
function setInterval(f) { intervals.push(f); }
function tick() { intervals.forEach(f => f()); }
async function settle() {
  for (let i = 0; i < 5; i++) {
    const t = timers;
    timers = [];
    t.forEach(f => f());
    await new Promise(r => global.setImmediate(r));
  }
}
function local() { return Object.fromEntries(store._d); }
"""


def _prefs_in_node(start: dict[str, str], soul: dict[str, Any], box: str, script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    begin = shim.index("/* A persona's settings live on the box too")
    end = shim.index("/* A persona's visual memories live on the box too")
    program = (
        f"const START = {json.dumps(start)}; const SOUL = {json.dumps(soul)};\n"
        f"function box(method, body) {{ {box} }}\n{_PREFS_HARNESS}\n"
        f"(function(window, Storage, XMLHttpRequest, fetch){{ {shim[begin:end]} }})"
        "(window, Storage, XMLHttpRequest, fetch);\n"
        f"(async () => {{ {script} }})();"
    )
    out = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_a_personas_settings_come_from_the_box_and_its_changes_go_back() -> None:
    """Migration 0263: the phone is only a display. The box's settings replace this phone's
    before the page reads them, the phone's own are set aside, keys that are not the
    creature's are left alone, and a change goes back as a patch of just that key."""
    got = _prefs_in_node(
        {"gb_vol": "0.8", "gb_eye": "environment", "gbk_tab": "s", "gb_up_vec": "[0,0,1]"},
        {"persona": "p1"},
        'return method === "GET" ? [200, {version: 3, prefs: {gb_vol: "0.5"}}]'
        " : [200, {version: 4}];",
        "const loaded = local();"
        ' store.setItem("gb_face_color", "#ffffff");'
        ' window.localStorage.removeItem("gb_vol");'
        " await settle();"
        " console.log(JSON.stringify({loaded, calls, meta: JSON.parse(local().gb_box_prefs)}));",
    )
    loaded = got["loaded"]
    assert loaded["gb_vol"] == "0.5" and "gb_eye" not in loaded
    # This phone's tilt calibration and its open tab are the phone's.
    assert loaded["gbk_tab"] == "s" and loaded["gb_up_vec"] == "[0,0,1]"
    assert json.loads(loaded["gb_phone_prefs"]) == {"gb_vol": "0.8", "gb_eye": "environment"}
    posts = [c["body"] for c in got["calls"] if c["method"] == "POST"]
    assert posts == [{"persona": "p1", "set": {"gb_face_color": "#ffffff"}, "drop": ["gb_vol"]}]
    assert got["meta"] == {
        "persona": "p1",
        "dirty": False,
        "base": {"gb_face_color": "#ffffff"},
        "version": 4,
    }


def test_the_first_phone_gives_a_persona_its_own_settings() -> None:
    got = _prefs_in_node(
        {"gb_vol": "0.8", "gb_eye": "environment"},
        {"persona": "p1"},
        'return method === "GET" ? [200, {version: 0, prefs: null}] : [200, {version: 1}];',
        "console.log(JSON.stringify({calls, local: local()}));",
    )
    assert got["calls"][1] == {
        "method": "POST",
        "body": {
            "persona": "p1",
            "set": {"gb_vol": "0.8", "gb_eye": "environment"},
            "drop": [],
        },
    }
    assert got["local"]["gb_eye"] == "environment"


def test_another_personas_settings_never_seed_a_new_one() -> None:
    meta = json.dumps({"persona": "p0", "dirty": False, "base": {}, "version": 2})
    got = _prefs_in_node(
        {"gb_vol": "0.3", "gb_box_prefs": meta},
        {"persona": "p1"},
        'return method === "GET" ? [200, {version: 0, prefs: null}] : [200, {version: 1}];',
        "console.log(JSON.stringify({calls, local: local()}));",
    )
    assert "gb_vol" not in got["local"]
    assert got["calls"][1]["body"] == {"persona": "p1", "set": {}, "drop": []}


def test_an_owners_edit_lands_where_the_creature_stands() -> None:
    got = _prefs_in_node(
        {},
        {"persona": "p1"},
        "if (method === 'POST') return [200, {version: 9}];"
        " return calls.length > 1 ? [200, {version: 8, prefs: {gb_vol: '0.5', gb_eye:"
        " 'environment'}}] : [200, {version: 7, prefs: {gb_vol: '0.5'}}];",
        'window.dispatchEvent(new CustomEvent("gb-box-state", {detail: {prefs: 7}}));'
        'window.dispatchEvent(new CustomEvent("gb-box-state", {detail: {prefs: 8}}));'
        " await settle();"
        " console.log(JSON.stringify({calls, events, local: local()}));",
    )
    # Only a newer version pulls, and taking the owner's edit sends nothing back.
    assert [c["method"] for c in got["calls"]] == ["GET", "GET"]
    assert got["events"] == [{"changed": {"gb_eye": "environment"}}]
    assert got["local"]["gb_eye"] == "environment"


def test_a_change_the_box_never_got_is_sent_before_its_copy_is_taken() -> None:
    meta = json.dumps({"persona": "p1", "dirty": True, "base": {"gb_vol": "0.5"}, "version": 3})
    got = _prefs_in_node(
        {"gb_vol": "0.2", "gb_box_prefs": meta},
        {"persona": "p1"},
        "if (method === 'POST') return [200, {version: 4}];"
        " return [200, {version: calls.length > 1 ? 4 : 3, prefs: {gb_vol: calls.length > 1"
        " ? '0.2' : '0.5'}}];",
        "console.log(JSON.stringify({calls, local: local()}));",
    )
    assert got["calls"][1]["body"] == {"persona": "p1", "set": {"gb_vol": "0.2"}, "drop": []}
    assert got["local"]["gb_vol"] == "0.2"


def test_a_g2_session_gets_the_phones_own_settings_back() -> None:
    got = _prefs_in_node(
        {
            "gb_vol": "0.5",
            "gb_phone_prefs": json.dumps({"gb_vol": "0.8"}),
            "gb_box_prefs": json.dumps({"persona": "p1"}),
        },
        {"g2": True},
        "return [500, null];",
        "console.log(JSON.stringify({calls, local: local()}));",
    )
    assert got["calls"] == []
    assert got["local"] == {"gb_vol": "0.8", "gb_box_prefs": "{}"}


def test_a_box_that_does_not_answer_never_overwrites_a_persona() -> None:
    meta = json.dumps({"persona": "p0", "dirty": False, "base": {}, "version": 2})
    got = _prefs_in_node(
        {"gb_vol": "0.3", "gb_box_prefs": meta},
        {"persona": "p1"},
        "return [503, null];",
        'store.setItem("gb_vol", "0.9"); await settle();'
        " console.log(JSON.stringify({calls, local: local()}));",
    )
    assert [c["method"] for c in got["calls"]] == ["GET"]
    assert json.loads(got["local"]["gb_box_prefs"])["persona"] == "p0"


def test_settings_answered_for_another_persona_are_never_taken() -> None:
    got = _prefs_in_node(
        {"gb_vol": "0.3"},
        {"persona": "p1"},
        "return method === 'GET' ? [200, {version: 3, persona: 'p2', prefs: {gb_vol: '0.9'}}]"
        " : [200, {version: 4}];",
        'store.setItem("gb_vol", "0.4"); await settle();'
        " console.log(JSON.stringify({calls, local: local()}));",
    )
    assert got["local"]["gb_vol"] == "0.4"
    assert [c["method"] for c in got["calls"]] == ["GET"]


def test_secrets_never_leave_the_phone_as_settings() -> None:
    got = _prefs_in_node(
        {
            "pb2_voicetest": json.dumps({"fish": {"key": "sk-fish"}}),
            "gb_travel_cards_v1": json.dumps([{"ssid": "x", "setup_key": "pw"}]),
            "pb_byok": "sk-or-x",
            "gb_vol": "0.3",
        },
        {"persona": "p1"},
        'return method === "GET" ? [200, {version: 0, prefs: null}] : [200, {version: 1}];',
        "console.log(JSON.stringify({calls}));",
    )
    assert got["calls"][1]["body"]["set"] == {"gb_vol": "0.3"}


def test_a_g2_session_on_a_phone_with_none_of_its_own_gets_the_defaults() -> None:
    meta = json.dumps({"persona": "p1", "dirty": False, "base": {}, "version": 2})
    got = _prefs_in_node(
        {"gb_vol": "0.5", "gb_nx_sid": "nx-p1", "gb_box_prefs": meta, "gbk_tab": "s"},
        {"g2": True},
        "return [500, null];",
        "console.log(JSON.stringify({local: local()}));",
    )
    assert got["local"] == {"gbk_tab": "s", "gb_box_prefs": "{}"}


def test_a_change_whose_resend_fails_is_kept_and_sent_again() -> None:
    meta = json.dumps({"persona": "p1", "dirty": True, "base": {"gb_vol": "0.5"}, "version": 3})
    got = _prefs_in_node(
        {"gb_vol": "0.2", "gb_box_prefs": meta},
        {"persona": "p1"},
        "if (method === 'GET') return [200, {version: 3, prefs: {gb_vol: '0.5'}}];"
        " return calls.filter(c => c.method === 'POST').length > 1"
        " ? [200, {version: 4}] : [503, {error: 'busy'}];",
        "const booted = local().gb_vol; await settle();"
        " console.log(JSON.stringify({booted, calls, meta: JSON.parse(local().gb_box_prefs)}));",
    )
    assert got["booted"] == "0.2"
    posts = [c["body"] for c in got["calls"] if c["method"] == "POST"]
    assert posts == [{"persona": "p1", "set": {"gb_vol": "0.2"}, "drop": []}] * 2
    assert got["meta"]["dirty"] is False and got["meta"]["base"] == {"gb_vol": "0.2"}


def test_a_busy_box_keeps_the_retrys_backoff() -> None:
    got = _prefs_in_node(
        {},
        {"persona": "p1"},
        "return method === 'GET' ? [200, {version: 1, prefs: {}}] : [503, {error: 'busy'}];",
        'store.setItem("gb_vol", "0.4");'
        " const step = async () => { const t = timers; timers = []; t.forEach(f => f());"
        " await new Promise(r => global.setImmediate(r)); };"
        " await step(); const first = timers.length; tick(); tick();"
        " console.log(JSON.stringify({first, after: timers.length, calls}));",
    )
    # One send, then a single backoff timer that the 2 s checks leave alone.
    assert [c["method"] for c in got["calls"]] == ["GET", "POST"]
    assert got["first"] == 1 and got["after"] == 1


def test_an_owners_edit_between_the_phones_own_writes_is_pulled() -> None:
    got = _prefs_in_node(
        {},
        {"persona": "p1"},
        "if (method === 'POST') return [200, {version: 5}];"
        " return calls.length > 1 ? [200, {version: 5, prefs: {gb_face_color: '#ffffff',"
        " gb_eye: 'environment'}}] : [200, {version: 3, prefs: {}}];",
        'store.setItem("gb_face_color", "#ffffff"); await settle();'
        " console.log(JSON.stringify({calls, events, local: local()}));",
    )
    assert [c["method"] for c in got["calls"]] == ["GET", "POST", "GET"]
    assert got["events"] == [{"changed": {"gb_eye": "environment"}}]


def test_an_owners_edit_made_while_a_change_waited_still_lands() -> None:
    meta = json.dumps(
        {"persona": "p1", "dirty": True, "base": {"gb_vol": "0.5", "gb_eye": "user"}, "version": 3}
    )
    got = _prefs_in_node(
        {"gb_vol": "0.2", "gb_eye": "user", "gb_box_prefs": meta},
        {"persona": "p1"},
        "if (method === 'GET') return [200, {version: 4, prefs: {gb_vol: '0.5', gb_eye:"
        " 'environment'}}];"
        " return calls.filter(c => c.method === 'POST').length > 1"
        " ? [200, {version: 5}] : [503, {error: 'busy'}];",
        "await settle(); console.log(JSON.stringify({calls, local: local()}));",
    )
    assert got["local"]["gb_eye"] == "environment" and got["local"]["gb_vol"] == "0.2"
    posts = [c["body"] for c in got["calls"] if c["method"] == "POST"]
    assert posts[-1] == {"persona": "p1", "set": {"gb_vol": "0.2"}, "drop": []}


def test_a_backoff_ends_when_there_is_nothing_left_to_send() -> None:
    got = _prefs_in_node(
        {},
        {"persona": "p1"},
        "if (method === 'GET') return [200, {version: 1, prefs: {}}];"
        " return calls.filter(c => c.method === 'POST').length > 1"
        " ? [200, {version: 2}] : [503, {error: 'busy'}];",
        " const step = async () => { const t = timers; timers = []; t.forEach(f => f());"
        " await new Promise(r => global.setImmediate(r)); };"
        ' store.setItem("gb_vol", "0.4"); await step();'
        ' store.removeItem("gb_vol"); await step();'
        ' store.setItem("gb_eye", "environment"); await step();'
        " console.log(JSON.stringify({calls}));",
    )
    posts = [c["body"] for c in got["calls"] if c["method"] == "POST"]
    assert posts[-1] == {"persona": "p1", "set": {"gb_eye": "environment"}, "drop": []}


def _talk_in_node(style: str | None, script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    talk = prompts[
        prompts.index("function gbTalkGrown(") : prompts.index(
            "/* ---- the clean, gate-free system prompt."
        )
    ]
    held = {} if style is None else {"gb_talk": style}
    shim = (
        f"var _held={json.dumps(held)};"
        "var localStorage={getItem:function(k){return k in _held?_held[k]:null;}};"
    )
    out = subprocess.run(
        [node, "-e", f"{shim}\n{talk}\n{script}"], capture_output=True, text=True, check=True
    )
    return out.stdout


_TALK_PROBE = (
    "var all=[];for(var i=0;i<20;i++)all.push({u:'u'+i,a:'a'+i});"
    "console.log(JSON.stringify({grown:gbTalkGrown(),n:gbTalkTraces(all).length,"
    "last:gbTalkTraces(all).slice(-1)[0].u,none:gbTalkTraces(undefined).length}));"
)


@pytest.mark.parametrize(
    ("style", "grown", "n"),
    [(None, False, 4), ("kid", False, 4), ("nonsense", False, 4), ("grownup", True, 12)],
)
def test_a_grown_up_creature_remembers_more_of_the_conversation(
    style: str | None, grown: bool, n: int
) -> None:
    """An adult found the creature forgot a meal he named over ten minutes (owner, 2026-11-12):
    a persona set to grown-up replays its last 12 exchanges, anything else keeps upstream's 4."""
    got = json.loads(_talk_in_node(style, _TALK_PROBE))
    assert got == {"grown": grown, "n": n, "last": "u19", "none": 0}


def test_a_storage_error_reads_as_the_kid_style() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    talk = prompts[prompts.index("function gbTalkGrown(") : prompts.index("var GB_TALK_TRACES")]
    shim = "var localStorage={getItem:function(){throw new Error('blocked');}};"
    out = subprocess.run(
        [node, "-e", f"{shim}\n{talk}\nconsole.log(gbTalkGrown());"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "false"


def test_the_talk_style_shapes_the_prompt() -> None:
    """Grown-up lengthens the reply cap, adds its block to the cached prefix, and
    the ring holds enough turns for it; the setting rides the persona's synced prefs."""
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    start = prompts.index("function buildSystem(")
    body = prompts[start : prompts.index("\n}\n", start)]
    static = body[body.index("var _gbStatic = ") : body.index("buildSystem.live = ")]
    assert 'person + (gbTalkGrown() ? GB_TALK_GROWN_BLOCK : "")' in static
    assert "var diary = soul.log.slice(-5).map(" in body
    assert '(gbTalkGrown() ? "≤45 words" : "≤18 words")' in prompts
    assert "≤18 words spoken aloud" not in prompts
    assert "try it once; after that, let it go" in prompts
    assert "not every line," not in prompts and "never as a tag on the end of a line" in prompts
    block = json.loads(
        _talk_in_node("grownup", "console.log(JSON.stringify(GB_TALK_GROWN_BLOCK));")
    )
    assert "45 words" in block and "fact field" in block

    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "var tr=gbTalkTraces(_live.scratch.traces), msgs=[],i;" in page
    assert "_live.scratch.traces.slice(-4)" not in page
    assert page.count("if(_live.scratch.traces.length>GB_TALK_TRACE_KEEP)") == 2
    assert "_live.scratch.traces.length>8" not in page
    assert "GB_TALK_TRACE_KEEP=16" in prompts

    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert re.search(r"\bgb_talk: 1\b", shim)


def test_the_box_creature_keeps_no_cloud_rooms_or_their_leftovers() -> None:
    """Owner, 2026-11-12: remove what does not apply to a box-run creature. The soul download,
    the move to a new phone, the watch link and the cloud notes are hidden, the visual
    memories say where they live, and a model, key or credits pick left on a phone is dropped
    before the page reads it."""
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert 'if(!window.__gbBox)h+=bCard(bHead("soul file")+' in page
    assert '(window.__gbBox?"Visual memories":"Visual memories · on this device")' in page
    vm = (_SITE / "v3" / "visual-memory.js").read_text(encoding="utf-8")
    assert vm.count("window.__gbBox?") == 2 and "Saved on the box for this creature." in vm
    shim = (_SITE / "growbot-box.js").read_text(encoding="utf-8")
    assert "#bpmDownloadSoul,#bpmXfer,#bpmXferBox,#bpmWatch,#bpData{display:none!important}" in shim
    assert '["gb_next_log", "pb2_journal"].forEach' in shim
    for key in ("gb_brain", "gb_effort", "pb_byok", "pb_llm_url", "pb_llm_key", "pb_energy_out"):
        assert f'"{key}"' in shim[shim.index("window.__gbBox = true;") :]


def test_the_owners_settings_set_before_any_phone_go_over_the_phones_own() -> None:
    got = _prefs_in_node(
        {"gb_vol": "0.8", "gb_face_color": "#ffffff"},
        {"persona": "p1"},
        "return method === 'GET' ? [200, {version: 2, adopt: true, prefs: {gb_eye:"
        " 'environment', gb_vol: '0.4'}}] : [200, {version: 3}];",
        "console.log(JSON.stringify({calls, local: local()}));",
    )
    mine = {"gb_vol": "0.4", "gb_face_color": "#ffffff", "gb_eye": "environment"}
    assert {k: got["local"][k] for k in mine} == mine
    # Only what the box lacked, so an owner edit landing meanwhile is not undone.
    assert got["calls"][1]["body"] == {
        "persona": "p1",
        "set": {"gb_face_color": "#ffffff"},
        "drop": [],
    }


def test_the_mouth_keeps_time_with_the_measured_voice() -> None:
    """The line now playing is decoded offline into 20 ms loudness steps and read at the
    player's own time, so a pause between words closes the mouth; never through the element's
    own Web Audio graph, which a suspended iOS context would mute."""
    out = _face_in_node(
        "var waits=[];window.fetch=function(src){ return Promise.resolve({arrayBuffer:"
        "function(){ return Promise.resolve(src); }}); };"
        "window.OfflineAudioContext=function(){ this.decodeAudioData=function(buf,ok){"
        "var d=new Float32Array(400);for(var i=0;i<400;i++) d[i]=i<200?0.5:i<300?0:0.125;"
        "ok({sampleRate:1000,getChannelData:function(){ return d; }}); }; };"
        "var fetch=window.fetch;"
        "window.fishAud={paused:false,ended:false,src:'blob:a',currentTime:0.05};"
        "var first=window.gbFace.level();"
        "setTimeout(function(){ var a=window.fishAud, r=[first];"
        "[0.05,0.25,0.35,0.9].forEach(function(t){"
        " a.currentTime=t; r.push(window.gbFace.level()); });"
        "a.paused=true; r.push(window.gbFace.level());"
        "console.log(JSON.stringify(r)); },0);"
    )
    first, loud, gap, soft, past, paused = json.loads(out)
    assert first is None and paused is None
    assert loud == 1 and gap == 0 and soft == pytest.approx(0.5) and past == 0
    face = (_SITE / "growbot-face.js").read_text(encoding="utf-8")
    assert "createMediaElementSource" not in face


def test_the_mouth_keeps_time_with_the_web_audio_line() -> None:
    """On an iPhone the Kokoro line plays from a decoded buffer on Web Audio, with no element
    holding a blob: the face reads that buffer at the context's clock from when it first saw
    the line play, and a line still preparing tells it nothing."""
    out = _face_in_node(
        "var d=new Float32Array(400);for(var i=0;i<400;i++) d[i]=i<200?0.5:i<300?0:0.125;"
        "var buf={sampleRate:1000,getChannelData:function(){ return d; }};"
        "window.actx={currentTime:10,outputLatency:0.1};"
        "window._fishWaLn={st:0,arm:2,src:{buffer:buf}};var r=[window.gbFace.level()];"
        "window._fishWaLn.st=1;r.push(window.gbFace.level());"
        "[10.15,10.35,10.45].forEach(function(t){ window.actx.currentTime=t;"
        " r.push(window.gbFace.level()); });"
        "window._fishWaLn=null;window._fishWaTail=Date.now()+1000;r.push(window.gbFace.level());"
        "window._fishWaTail=0;r.push(window.gbFace.level());"
        "window.actx.currentTime=20;window._fishWaLn={st:1,arm:1,src:{buffer:buf}};"
        "r.push(window.gbFace.level());window.actx.currentTime=20.95;"
        "r.push(window.gbFace.level());"
        # Stopped, not ended: the stale tail stamp is the one it began under, so no tail.
        "window._fishWaTail=Date.now()+1000;window._fishWaLn={st:1,arm:2,src:{buffer:buf}};"
        "window.gbFace.level();window._fishWaLn=null;r.push(window.gbFace.level());"
        # Ended, but the next line is already playing from fishAud: the tail is not its mouth.
        "window._fishWaLn={st:1,arm:2,src:{buffer:buf}};window.gbFace.level();"
        "window._fishWaLn=null;window._fishWaTail=Date.now()+1200;"
        "window.fishAud={paused:false,ended:false,src:'blob:b',currentTime:0};"
        "r.push(window.gbFace.level());"
        "console.log(JSON.stringify(r));"
    )
    got = json.loads(out)
    preparing, early, loud, gap, soft, tail, ended, arm1_early, arm1_heard = got[:9]
    stopped, next_line = got[9:]
    assert preparing is None
    # Heard only after the output latency: before it nothing, then the line in time.
    assert early == 0 and loud == 1 and gap == 0 and soft == pytest.approx(0.5)
    # The line's end is still heard through the tail, then the clock rhythm takes over.
    assert tail == pytest.approx(0.5) and ended is None
    # Arm 1's second element holds the line back further still.
    assert arm1_early == 0 and arm1_heard == 1
    # A stop leaves no tail; a fishAud line after an ended one is measured itself (null until
    # decoded, here never, so the clock rhythm runs), not read off the previous line.
    assert stopped is None and next_line is None


_PROMPTS_IN_NODE = (
    "const fs=require('fs'),vm=require('vm');"
    "const ctx={console,URLSearchParams,location:{search:''},"
    "localStorage:{getItem:()=>null}};vm.createContext(ctx);"
    "vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);"
)


def _prompts_in_node(script: str) -> object:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    out = subprocess.run(
        [node, "-e", _PROMPTS_IN_NODE + script, str(_SITE / "v3" / "prompts.js")],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def test_recall_finds_facts_the_diary_and_this_conversation() -> None:
    """The owner's lost meal (2026-11-12): a fact told earlier, the diary, bright moments, rules
    and this session's talk are all searched; a stop word alone finds nothing."""
    got = _prompts_in_node(
        "const now=Date.now();"
        "const soul={log:[{t:now-86400000,txt:'Jeff saw the hematologist on Courtenay Parkway'}],"
        "scratch:{glows:[{t:now-5000,g:'the first time Jeff laughed'}],rules:['greet Quorra']}};"
        "const added=[ctx.gbFactAdd(soul,'Lunch today: a chalupa and a taco',now),"
        "ctx.gbFactAdd(soul,'Lunch today: a chalupa and a taco',now),ctx.gbFactAdd(soul,'OMIT')];"
        "const sess=[{t:now-60000,txt:'they said: I got a Taco Bell box | you said: Ooh'}];"
        "console.log(JSON.stringify({added,facts:soul.facts.length,"
        "lunch:ctx.gbRecall(soul,sess,'taco lunch',now),"
        "doctor:ctx.gbRecall(soul,sess,'hematologist',now),"
        "laugh:ctx.gbRecall(soul,[],'laughed',now),rule:ctx.gbRecall(soul,[],'quorra',now),"
        "none:ctx.gbRecall(soul,sess,'the',now),empty:ctx.gbRecall({},null,'taco',now)}));"
    )
    assert isinstance(got, dict)
    assert got["added"] == [True, False, False] and got["facts"] == 1
    lunch = got["lunch"]["text"].splitlines()
    assert got["lunch"]["n"] == 2
    assert lunch[1].startswith("- (fact, ") and "chalupa" in lunch[1]
    assert lunch[2].startswith("- (this conversation, ") and "Taco Bell" in lunch[2]
    assert got["doctor"]["n"] == 1 and "(diary, yesterday)" in got["doctor"]["text"]
    assert "(bright moment, " in got["laugh"]["text"]
    assert "(standing rule) greet Quorra" in got["rule"]["text"]
    assert got["none"] == {"n": 0, "text": ""} and got["empty"] == {"n": 0, "text": ""}


def test_the_fact_book_keeps_its_newest_lines() -> None:
    got = _prompts_in_node(
        "const soul={};for(let i=0;i<450;i++)ctx.gbFactAdd(soul,'fact number '+i,i+1);"
        "console.log(JSON.stringify([soul.facts.length,soul.facts[0].txt,"
        "soul.facts[399].txt]));"
    )
    assert got == [400, "fact number 50", "fact number 449"]


_RECALL_HARNESS = r"""
var logs=[], thinks=[], pendingUser=null, inFlight=false, paused=false, coolUntil=0;
var document={hidden:false}, _live={}, timers=[], energyState="awake", starts=true;
function setTimeout(f){ timers.push(f); }
function logEv(k,d){ logs.push([k,d]); }
function think(ev,kind,extra){
  thinks.push([ev,kind,extra,_gbRecallCarry]);
  if(starts){ inFlight=true; _gbRecallCarry=""; }
}
function gbRecall(soul,sess,q){
  return q==="taco"?{n:1,text:"\n- (fact) lunch: a taco"}:{n:0,text:""};
}
function traceMoment(u){ return u; }
function gbVisitKeep(){}
"""


def _recall_page(script: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    code = page[page.index("var _gbSess=[]") : page.index("function traceMoment(u){")]
    out = subprocess.run(
        [node, "-e", f"{_RECALL_HARNESS}\n{code}\n{script}"],
        capture_output=True,
        text=True,
        check=True,
    )
    got = json.loads(out.stdout)
    assert isinstance(got, dict)
    return got


def test_a_recall_answers_in_a_follow_up_once_the_asking_turn_is_done() -> None:
    """Asked from inside the reply handler, the follow-up waits for that turn to finish (its
    trace and speech) and carries the find in its own live block, not in its trace."""
    got = _recall_page(
        "gbRecallAsk('taco','event',{});var before=thinks.length;timers.shift()();"
        "gbRecallAsk('taco','event',{});"
        "console.log(JSON.stringify({before,thinks,logs,carry:_gbRecallCarry}));"
    )
    assert got["before"] == 0
    [(ev, kind, extra, carried)] = got["thinks"]
    assert kind == "event" and extra == {"recallFollow": True}
    assert ev.startswith('you looked back through your memory for "taco" (what you found')
    assert "lunch" not in ev and len(ev) < 300
    assert carried.endswith("found:\n- (fact) lunch: a taco") and got["carry"] == ""
    assert got["logs"] == [["recall", {"n": 1}], ["recall_skip", {"why": "gap"}]]


@pytest.mark.parametrize(
    ("q", "kind", "extra", "why"),
    [
        ("OMIT almost always. when they ask", "event", {}, None),
        ("taco", "tick", {}, "kind"),
        ("taco", "event", {"recallFollow": True}, "follow"),
    ],
)
def test_a_recall_is_not_searched_from_an_echo_a_tick_or_a_follow_up(
    q: str, kind: str, extra: dict[str, Any], why: str | None
) -> None:
    got = _recall_page(
        f"gbRecallAsk({json.dumps(q)},{json.dumps(kind)},{json.dumps(extra)});"
        "console.log(JSON.stringify({timers:timers.length,logs}));"
    )
    assert got["timers"] == 0
    assert got["logs"] == ([] if why is None else [["recall_skip", {"why": why}]])


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ("pendingUser='hello';", "person"),
        ("starts=false;", "refused"),
        ("inFlight=true;", "timeout"),
    ],
)
def test_a_find_that_cannot_be_answered_rides_the_next_turn(setup: str, why: str) -> None:
    """Never a "let me think back" left hanging: the person spoke, the creature could not think,
    or it stayed busy past 30 s, and the find goes into its next turn instead."""
    got = _recall_page(
        f"gbRecallAsk('nothing here','event',{{}});{setup}"
        "var n=0;while(timers.length&&n<200){timers.shift()();n++;}"
        "console.log(JSON.stringify({n,carry:_gbRecallCarry,last:logs[logs.length-1]}));"
    )
    assert "found nothing about it." in got["carry"]
    assert got["last"] == ["recall_carry", {"why": why}]
    if why == "timeout":
        assert got["n"] == 120


def test_this_conversation_is_kept_in_page_memory_only() -> None:
    got = _recall_page(
        "gbSessAdd('moment: they just said to you: \"I said \"hi\" twice\" — answer THEM','Hello');"
        "gbSessAdd('QUIET COMPANY','a remark');"
        "gbSessAdd('they just said to you: \"x\" — answer THEM','(stayed quiet)');"
        "gbSessAdd('you looked back through your memory for \"taco\"','A taco and chips.');"
        "var first=_gbSess.map(function(x){return x.txt;});"
        "for(var i=0;i<450;i++) gbSessAdd('they just said to you: \"n'+i+'\" — answer THEM','ok');"
        "console.log(JSON.stringify({n:_gbSess.length,first,oldest:_gbSess[0].txt}));"
    )
    assert got["first"] == [
        'they said: I said "hi" twice | you said: Hello',
        "they said: x",
        "you remembered: A taco and chips.",
    ]
    assert got["n"] == 400 and got["oldest"] == "they said: n50 | you said: ok"


def test_recall_is_wired_into_the_turn() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "else _live.scratch.traces.push(_tr);\n      gbSessAdd(_tr.u,_tr.a);" in page
    assert 'gbFactAdd(_live,o.fact)) logEv("fact",{n:_live.facts.length});' in page
    assert "gbRecallAsk(o.recall,kind,extra);" in page
    assert '_gbLive+="\\n\\n== WHAT YOU JUST LOOKED UP ==\\n"+_gbRecallCarry;' in page
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    assert '\'"fact":"OMIT unless your person just told you' in prompts
    assert '\'"recall":"OMIT almost always.' in prompts
    soul = (_SITE / "v3" / "soul.js").read_text(encoding="utf-8")
    assert "s.facts = unionByTime(newer.facts," in soul
    assert "if (Array.isArray(raw.facts)) s.facts = inTimeOrder(raw.facts);" in soul
    assert "if (Array.isArray(rt.facts)) soul.facts = rt.facts;" in soul


_SLEEP_HARNESS = r"""
var lastDreamT=0, callsSinceDream=0, _gbSess=[], _live={log:[],scratch:{}}, logs=[], facts=[];
function soulLog(s,t){ s.log.push(String(t).slice(0,140)); }
function gbFactAdd(s,t){ if(facts.indexOf(t)>=0) return false; facts.push(t); return true; }
function logEv(k,d){ logs.push([k,d]); }
"""


def _sleep_page(script: str) -> dict[str, Any]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    code = page[page.index("var _gbSleepOK=false") : page.index("var _humanT=0;")]
    out = subprocess.run(
        [node, "-e", f"{_SLEEP_HARNESS}\n{code}\n{script}"],
        capture_output=True,
        text=True,
        check=True,
    )
    got = json.loads(out.stdout)
    assert isinstance(got, dict)
    return got


def test_a_long_visit_makes_the_creature_sleepy_and_it_asks() -> None:
    """Owner, 2026-11-12: a long session makes it say it is tired and ask to sleep; it dreams
    only once allowed. 60 replies, or 20 and half an hour awake, and never once allowed."""
    got = _sleep_page(
        "var r={};callsSinceDream=59;r.fresh=gbTiredLine();callsSinceDream=60;r.many=gbTiredLine();"
        "callsSinceDream=20;_gbBootT=Date.now()-1800000;r.long=gbTiredLine();"
        "lastDreamT=Date.now()-60000;r.slept=gbTiredLine();"
        "callsSinceDream=60;gbSleepGrant();r.allowed=gbTiredLine();"
        "console.log(JSON.stringify(r));"
    )
    assert got["fresh"] == "" and got["slept"] == "" and got["allowed"] == ""
    assert got["many"] == got["long"] and "== GETTING SLEEPY ==" in got["many"]
    assert "ask if you may sleep" in got["many"] and "set sleep to true" in got["many"]


def test_permission_to_sleep_lapses_after_a_few_minutes() -> None:
    """A dream that cannot start must not leave the person's 30 s guard off for good."""
    got = _sleep_page(
        "var r={before:gbSleepAllowed()};gbSleepGrant();r.granted=gbSleepAllowed();"
        "_gbSleepAt=Date.now()-GB_SLEEP_HOLD_MS-1;r.lapsed=gbSleepAllowed();r.flag=_gbSleepOK;"
        "r.logs=logs;console.log(JSON.stringify(r));"
    )
    assert got["before"] is False and got["granted"] is True
    assert got["lapsed"] is False and got["flag"] is False
    assert got["logs"] == [["sleep_ok", {"csd": 0}], ["sleep_expired", {}]]


def test_a_dream_keeps_the_visit_once_even_across_a_reload() -> None:
    """The undreamed visit rides the soul, so a dream on the next open still sees it; a
    landed dream files its summary a sentence a line (the diary keeps 140 characters) and
    clears it, so the same talk is never summarised twice."""
    long = "We ate Taco Bell in the car and talked about hot sauce for a long while. " * 2
    got = _sleep_page(
        "var r={empty:gbDreamSession()};"
        "for(var i=0;i<90;i++)gbVisitKeep({t:i,txt:'line '+i});"
        "r.kept=_live.scratch.visit.length;r.sess=gbDreamSession();"
        f"gbDreamKeep({{session_summary:{json.dumps(long + 'Then he went to work. Extra.')},"
        "facts:['lunch: a taco','up to 5 short plain facts','None','a','b','c']});"
        "r.after=gbDreamSession();r.visit=_live.scratch.visit;"
        "gbDreamKeep({session_summary:'OMIT'});"
        "r.log=_live.log;r.facts=facts;r.logs=logs;console.log(JSON.stringify(r));"
    )
    assert got["empty"] == "" and got["kept"] == 80
    sess = got["sess"]
    assert "- line 10\n" in sess and "- line 9\n" not in sess and "- line 89\n" in sess
    assert '"session_summary":' in sess and '"facts":[' in sess
    assert got["after"] == "" and got["visit"] == []
    assert len(got["log"]) == 3 and all(x.startswith("📖 ") for x in got["log"])
    assert got["log"][2] == "📖 Then he went to work."
    assert all(len(x) < 140 for x in got["log"])
    assert got["facts"] == ["lunch: a taco", "a", "b"]
    assert got["logs"] == [["dream_keep", {"s": 3, "f": 3}], ["dream_keep", {"s": 0, "f": 0}]]


@pytest.mark.parametrize(
    ("said", "goal"),
    [
        ("okay, go to sleep", None),
        ("go to bed little fish", None),
        ("you can go to sleep now", None),
        ("go to the kitchen", "the kitchen"),
        ("find my keys", "my keys"),
    ],
)
def test_go_to_sleep_lets_it_sleep_instead_of_seeking_sleep(said: str, goal: str | None) -> None:
    """Field test, 2026-11-12: "go to sleep" set a seek for "sleep", and a seek holds every
    dream back, so the creature it was given leave to sleep never slept."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    start = page.index("function _seekWords(t){")
    code = page[start : page.index("function _localCmd(t)", start)]
    out = subprocess.run(
        [node, "-e", f"{code}\nconsole.log(JSON.stringify(_seekWords({json.dumps(said)}).goal))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout) == goal


def test_leave_to_sleep_ends_what_it_was_doing() -> None:
    """A seek still under way when its person lets it sleep ends, after the reply that took the
    leave, so the dream is not held back as "activity" until the leave lapses."""
    got = _sleep_page(
        "var timers=[],ended=[],seekGoal='my keys',actOn=true,mimicOn=false;"
        "setTimeout=function(f){timers.push(f);};"
        "function endActivity(r){ended.push(r);seekGoal='';actOn=false;}"
        "gbSleepGrant();var before=ended.slice();timers.shift()();"
        "_gbSleepOK=true;gbSleepGrant();timers.shift()();"
        "console.log(JSON.stringify({before,ended}));"
    )
    assert got["before"] == [] and got["ended"] == ["sleep"]


def test_sleep_replaces_the_every_twelve_replies_dream() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert "due=callsSinceDream>=12" not in page
    assert (
        "due=gbSleepAllowed() || _gbDreamOnOpen || (idle>GB_SLEEP_IDLE_MS && callsSinceDream>=3);"
    ) in page
    assert 'Date.now()-_humanT<30000&&!gbSleepAllowed()?"human":""' in page
    assert "  if(due) dream(gbSleepAllowed()); }" in page
    assert (
        "if((gbSleepAllowed()||_gbDreamOnOpen)&&!_speechBusy()&&!pendingUser){ maybeDream();"
        in page
    )
    assert "IDLE_DREAM_MS=300000" in page and "IDLE_DREAM_MS=120000" not in page
    assert "_gbSleepOK=false; _gbDreamOnOpen=false;" in page
    assert '+(_gbSleepWhy||"a quiet stillness came over it")+gbDreamSession();' in page
    assert '_t.indexOf("🌙 dreamed")!==0 && _t.indexOf("📖")!==0;' in page
    assert "applyDream(o,lived); gbDreamKeep(o); }" in page
    assert "brainOverride(orCfg.dream,true),1000,90000)" in page
    assert "_gbLive+=gbTiredLine(); if(_gbRecallCarry){" in page
    assert "_gbSess.push({t:Date.now(),txt:got}); gbVisitKeep(" in page
    assert "Date.now()-(+_gbLastV.t||0)>GB_OPEN_GAP_MS) _gbDreamOnOpen=true;" in page
    assert '(o.sleep===true||o.sleep==="true")&&kind==="event") gbSleepGrant();' in page
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    assert '\'"sleep":"OMIT unless your person just said you may sleep' in prompts


@pytest.mark.parametrize(
    ("explore", "want"),
    [
        (None, "This time is yours"),
        ("true", "This time is yours"),
        ("false", "QUIET COMPANY"),
    ],
)
def test_the_quiet_beat_hands_the_creature_its_own_time_unless_its_persona_says_not(
    explore: str | None, want: str
) -> None:
    """Upstream's 11-11 quiet-beat text unless the persona turned exploring off; a page the
    box gave no setting (no persona yet) explores too. The choice lives on the box, so the
    page reads no URL or phone storage for it."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    words = next(line for line in page.splitlines() if line.startswith("var GB_EXPLORE_TXT="))
    beat = re.search(
        r'think\((window\.__gbBoxExplore!==false\?GB_EXPLORE_TXT:"QUIET COMPANY[^"]*")', page
    )
    assert beat is not None
    # The beat's own gates are the ones the 90 s test above pins; only its words are chosen.
    assert page.index(words) < page.index("if(idle>=GB_QUIET_AFTER_MS") < beat.start()
    assert "gb_quiet" not in page and 'get("quiet")' not in page
    setting = "" if explore is None else f"window.__gbBoxExplore={explore};"
    # Inside a function, so the page's globals never meet whatever this Node version ships.
    script = (
        f"(function(){{var window={{}};{setting}{words}\n"
        f"console.log(JSON.stringify({beat.group(1)}));}})();"
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert want in json.loads(out.stdout)
