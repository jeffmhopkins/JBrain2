"""Dave's view all around (growbot/patches/growbot-pano.js, owner 2026-11-17): the pictures
he took from one spot painted onto a cylinder at the headings they faced. The pure half
(which picture fills which column, the marks, how much he has seen) runs in node; the wiring
is checked in the built creature page."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_GROWBOT = Path(__file__).resolve().parents[3] / "growbot"
_SITE = _GROWBOT / "site"
_PANO = _GROWBOT / "patches" / "growbot-pano.js"


def _node(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prog = f"const P=require({json.dumps(str(_PANO))});{script}"
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_built_site_carries_the_pano_script_as_written() -> None:
    assert (_SITE / "growbot-pano.js").read_bytes() == _PANO.read_bytes()


def test_each_column_comes_from_the_picture_that_faced_that_way() -> None:
    got = _node(
        # Two pictures from one spot: one facing 90 (where he faces now), one facing 180, a
        # quarter turn to his left.
        "const s=[{h:90,id:'a'},{h:180,id:'b'}];"
        "const c=(r)=>{const x=P.column(s,90,r); return x&&{id:x.shot.id,sx:x.sx};};"
        "console.log(JSON.stringify([c(0),c(20),c(-20),c(90),c(135),c(-90),P.CAM_HFOV,P.FW/2]));"
    )
    ahead, left20, right20, left90, left135, right90, hfov, mid = got
    assert hfov == pytest.approx(59.0, abs=1)
    # Straight ahead is the middle of the picture that faced ahead.
    assert ahead == {"id": "a", "sx": mid}
    # To his left is toward the left edge of his picture, to his right toward the right.
    assert left20["id"] == "a" and left20["sx"] < mid < right20["sx"]
    # A quarter turn left is the middle of the picture that faced there.
    assert left90 == {"id": "b", "sx": mid}
    # Where no picture faced, nothing: the strip leaves it grey.
    assert left135 is None and right90 is None


def test_the_marks_run_from_behind_on_his_left_to_behind_on_his_right() -> None:
    got = _node("console.log(JSON.stringify([P.marks(), P.seenDeg([{h:0},{h:3}]), P.PX_DEG]))")
    marks, seen, px = got
    assert [m["t"] for m in marks] == [
        "behind",
        "135L",
        "90L",
        "45L",
        "ahead",
        "45R",
        "90R",
        "135R",
        "behind",
    ]
    assert marks[4]["x"] == 180 * px and marks[0]["x"] == 0 and marks[-1]["x"] == 360 * px
    # One camera's width, and a few degrees more for a second picture turned 3 degrees.
    assert 58 <= seen <= 62


def test_the_creature_page_loads_it_and_sends_the_strip_with_his_next_thought() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '<script src="/growbot-pano.js?v=' in page
    i = page.index("window.gbPano.take()")
    # Beside his camera's picture, before the turn's own words.
    assert page.index('parts.push({type:"text",text:userMsg});', i) > i
    assert 'logEv("pano",' in page


def test_the_eight_ways_run_left_to_right_between_the_marks() -> None:
    got = _node("console.log(JSON.stringify(P.ways(90)))")
    assert [w["n"] for w in got] == list(range(1, 9))
    # Way 1 is behind on his left, ways 4 and 5 either side of ahead, way 8 behind on his right.
    assert [w["rel"] for w in got] == [157.5, 112.5, 67.5, 22.5, -22.5, -67.5, -112.5, -157.5]
    assert got[3]["abs"] == 112.5 and got[4]["abs"] == 67.5
    assert [w["x"] for w in got] == sorted(w["x"] for w in got)


def test_a_trip_turns_to_its_way_walks_it_and_ends_a_metre_on() -> None:
    got = _node(
        "const tr={abs:90,x:0,y:0,t:0};"
        "console.log(JSON.stringify(["
        "P.step(tr,{x:0,y:0,h:0},1000,false),"  # facing away: turn to it
        "P.step(tr,{x:0,y:0.1,h:84},2000,false),"  # facing it: walk
        "P.step(tr,{x:0,y:0.4,h:84},3000,true),"  # walking: let it go on
        "P.step(tr,{x:0,y:0.95,h:90},4000,true),"  # a metre on: there
        "P.step(tr,{x:0,y:0.2,h:90},200000,false)]))"  # two minutes and more: over
    )
    turn, walk, going, there, late = got
    assert turn["walk"] == {"turn": 90}
    assert walk["walk"] == {"gait": "screen_forward", "dir": "fwd", "secs": 25}
    assert "walk" not in going and "done" not in going
    assert there["done"] == "arrived"
    assert late["done"] == "timed out"


def test_a_trip_whose_walk_gets_him_nowhere_ends_as_blocked() -> None:
    got = _node(
        "const tr={abs:90,x:0,y:0,t:0};"
        "const a=P.step(tr,{x:0,y:0,h:90},1000,false);"
        "const b=P.step(tr,{x:0,y:0.05,h:90},30000,false);"
        "console.log(JSON.stringify([a,b]))"
    )
    first, second = got
    assert first["walk"]["dir"] == "fwd"
    assert second["done"] == "something was in the way"


def test_the_trip_and_the_survey_move_him_between_thoughts() -> None:
    page = (_SITE / "growbot-pano.js").read_text(encoding="utf-8")
    # Steps go straight to the page's walk, once his last move has ended.
    assert "runGait(spec)" in page and "_motion" in page
    assert "turn: function (deg) { return turnSpec({ turn: deg }); }" in (
        _SITE / "growbot-map.js"
    ).read_text(encoding="utf-8")


def test_the_page_takes_his_explore_choice_before_the_map_turns_it_into_a_spin() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    hook = page.index("window.gbPano.reply(o,_mgen,_gbIssT);")
    assert hook < page.index("window.gbMap.reply(o,kind,extra)", hook)
    prompts = (_SITE / "v3" / "prompts.js").read_text(encoding="utf-8")
    assert '"explore":"OMIT unless you are exploring on your legs.' in prompts


_HARNESS = """
let now=1000; Date.now=()=>now;
const ticks=[]; globalThis.setInterval=(f,ms)=>{ if(ms===500) ticks.push(f); return 0; };
globalThis.window=globalThis; globalThis.document={hidden:false,createElement:()=>({})};
globalThis.MODE='legs'; globalThis.dreaming=false; globalThis.paused=false;
globalThis._motion=null; globalThis._motionGen=0; globalThis._gbDoneGen={};
globalThis._gbOnlyDone=(a,b)=>{
  for(let g=a+1;g<=b;g++) if(!_gbDoneGen[g]) return false; return b>a; };
const ran=[], log=[]; globalThis.logEv=(k,d)=>log.push(k);
let refuse=false;
globalThis.runGait=(s)=>{ ran.push(s); _motionGen+=2;
  if(!refuse) _motion={kind:'walk',report:{state:'started'}}; };
window.gbMap={ map:()=>({here:{x:0,y:0,h:90}}),
  turn:(d)=>({gait:d>0?'spin_left':'spin_right',secs:3}) };
const P=require(PANO);
const tick=()=>ticks.forEach(f=>f());
"""


def _page_node(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prog = _HARNESS.replace("PANO", json.dumps(str(_PANO))) + script
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_a_chosen_way_moves_him_between_thoughts_without_refusing_the_next_one() -> None:
    got = _page_node(
        "const o={say:'heading out to explore',explore:{go:5},walk:{dir:'fwd'}};P.reply(o);"
        "tick(); now+=2000; tick();"  # settles, then the trip's first step
        "const first=ran.slice(); const done=Object.keys(_gbDoneGen).map(Number);"
        "_motion=null; now+=2000; tick(); now+=2000; tick();"  # that move ended: the next step
        "console.log(JSON.stringify({o,first,done,n:ran.length,log}))"
    )
    # His choice is the trip's: no move of his own left in the reply, and no claim nudge.
    assert "walk" not in got["o"] and got["o"]["_gbExplored"] == 1
    # Way 5 is 22.5 degrees right of ahead: the first step turns him that way.
    assert got["first"] == [{"gait": "spin_right", "secs": 3}]
    # The trip's own motion generations are marked done, so a thought in flight still lands.
    assert got["done"] == [1, 2]
    assert got["n"] == 2 and "explore_go" in got["log"]


def test_a_trip_step_the_page_refuses_ends_the_trip_without_calling_it_a_wall() -> None:
    got = _page_node(
        "P.reply({explore:{go:4}}); refuse=true; tick(); now+=2000; tick();"
        "console.log(JSON.stringify({st:P.state(),log}))"
    )
    assert got["st"]["trip"] is None
    assert got["st"]["last"]["why"] == "your legs could not move just then"


def test_paused_or_a_move_of_his_own_holds_or_ends_the_trip() -> None:
    got = _page_node(
        "P.reply({explore:{survey:true}}); paused=true; tick(); now+=2000; tick();"
        "const whilePaused=ran.length; paused=false;"
        "P.reply({walk:{dir:'fwd',secs:20}}); tick(); now+=2000; tick();"
        "console.log(JSON.stringify({whilePaused,after:ran.length,st:P.state()}))"
    )
    assert got["whilePaused"] == 0
    # His own walk outranks the survey he chose before: it ends, and nothing more is sent.
    assert got["after"] == 0 and got["st"]["survey"] == 0


def test_a_stop_ends_the_trip_and_a_thought_it_overtook_starts_none() -> None:
    got = _page_node(
        # Walking way 4; his person's stop button cancels the step that is running.
        "P.reply({explore:{go:4}},0,now); tick(); now+=2000; tick(); const before=ran.length;"
        "_motion.report.state='cancelled'; _motion=null; _motionGen++;"
        "now+=2000; tick(); now+=2000; tick();"
        "const afterStop={n:ran.length-before, st:P.state()};"
        # A thought issued before another stop lands after it, choosing a survey.
        "const g=_motionGen; _motionGen++; const o={explore:{survey:true},walk:{dir:'fwd'}};"
        "P.reply(o,g,now-5000); now+=2000; tick(); now+=2000; tick();"
        "console.log(JSON.stringify({afterStop,late:ran.length-before,o,log}))"
    )
    assert got["afterStop"]["n"] == 0
    assert got["afterStop"]["st"]["trip"] is None
    assert got["afterStop"]["st"]["last"]["why"] == "you were stopped"
    assert got["late"] == 0 and "explore_refused" in got["log"]
    assert "walk" not in got["o"] and "_gbExplored" not in got["o"]


def test_a_turn_the_gyro_ends_or_a_gesture_beside_it_does_not_end_the_trip() -> None:
    got = _page_node(
        "const o={explore:{go:5},gesture:'wave'}; P.reply(o,0,now);"
        # The page's own gesture-time generations, not marked done, do not stop the trip.
        "_motionGen+=3; tick(); now+=2000; tick();"
        # The gyro stops the turn as asked: cancelled, but turned.
        "_motion.report.state='cancelled'; _motion.report._gbTurned=1; _motion=null;"
        "now+=2000; tick(); now+=2000; tick();"
        "console.log(JSON.stringify({o,n:ran.length,st:P.state()}))"
    )
    assert "gesture" not in got["o"]
    assert got["n"] == 2 and got["st"]["trip"] is not None


def test_the_stop_button_between_steps_ends_the_trip() -> None:
    got = _page_node(
        "P.reply({explore:{go:4}},0,now); tick(); now+=2000; tick(); _motion=null;"
        "globalThis._pauseHard=true; paused=true; tick(); paused=false; now+=2000; tick();"
        "console.log(JSON.stringify({n:ran.length,st:P.state()}))"
    )
    assert got["n"] == 1 and got["st"]["trip"] is None


def test_following_the_walls_turns_right_after_a_clear_walk_and_left_after_a_blocked_one() -> None:
    got = _node(
        "const tr={wall:true,x:0,y:0,t:0,room:'living room',want:null,walked:false,blocks:0};"
        "const h=(x,y,hd,room)=>({x,y,h:hd,room:room||'living room'});"
        "console.log(JSON.stringify(["
        "P.wallStep(tr,h(0,0,90),1000,false),"  # first: walk
        "P.wallStep(tr,h(0,0.5,90),20000,false),"  # clear: probe 45 right
        "P.wallStep(tr,h(0,0.5,48),25000,false),"  # facing it: walk
        "P.wallStep(tr,h(0.3,0.8,48),40000,true),"  # blocked: 90 left
        "P.wallStep(tr,h(0.3,0.8,135),45000,false),"  # facing it: walk
        "P.wallStep(tr,h(0,1.2,135,'hallway'),60000,false),"  # another room: done
        "P.wallStep({wall:true,x:0,y:0,t:0,want:null,walked:false,blocks:10},h(0,0,0),1,false),"
        "P.blockedRun([1,2.4,0.3]),P.blockedRun([1,2.6,0.3]),P.blockedRun([0,0])]))"
    )
    first, probe, walk, away, walk2, through, boxed, yes, no, short = got
    assert first["walk"]["dir"] == "fwd" and first["walk"]["secs"] == 15
    assert probe["walk"] == {"turn": -45}
    assert walk["walk"]["dir"] == "fwd"
    assert away["walk"] == {"turn": 90}
    assert walk2["walk"]["dir"] == "fwd"
    assert through["done"] == "you went through into the hallway"
    assert boxed["done"] == "something kept being in the way"
    assert (yes, no, short) == (True, False, False)


def test_a_way_whose_walk_is_blocked_ends_there() -> None:
    got = _node(
        "console.log(JSON.stringify(P.step({abs:90,x:0,y:0,t:0},{x:0,y:0.3,h:90},1000,false,true)))"
    )
    assert got["done"] == "something was in the way"


def test_walls_is_an_explore_choice_and_runs_by_itself() -> None:
    got = _page_node(
        "const o={explore:{walls:true},walk:{dir:'fwd'}}; P.reply(o,0,now);"
        "tick(); now+=2000; tick();"
        "console.log(JSON.stringify({o,ran,st:P.state(),log}))"
    )
    assert "walk" not in got["o"] and got["o"]["_gbExplored"] == 1
    assert got["ran"] == [{"gait": "screen_forward", "dir": "fwd", "secs": 15}]
    assert got["st"]["trip"]["wall"] is True and "explore_walls" in got["log"]


def test_asking_for_the_walls_again_carries_the_same_round_on() -> None:
    got = _page_node(
        "P.reply({explore:{walls:true}},0,now); const t0=now; now+=5000;"
        "P.reply({explore:{walls:true}},0,now);"
        "console.log(JSON.stringify({log}))"
    )
    assert got["log"].count("explore_walls") == 1


def test_his_own_turns_do_not_cut_a_round_of_the_walls_short_but_a_stop_does() -> None:
    got = _page_node(
        "P.reply({explore:{walls:true}},0,now); now+=5000;"
        "const spin={walk:{gait:'spin_left',dir:'left',secs:5}}; P.reply(spin,0,now);"
        "const kept=!!P.state().trip; P.reply({walk:{dir:'stop'}},0,now);"
        "console.log(JSON.stringify({spin,kept,st:P.state(),log}))"
    )
    assert "walk" not in got["spin"] and got["kept"] and "explore_kept" in got["log"]
    assert got["st"]["trip"] is None
