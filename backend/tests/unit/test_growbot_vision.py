"""Dave's depth sense (growbot/patches/growbot-vision.js, owner 2026-11-18): a source's depth and
label maps of one camera frame reduced to how far things are each way, the open ways and what
is in view, and what the page does with it. The maps here are drawn by hand from the camera's
geometry, so each test knows the true answer; the simulator's source is pinned in
test_growbot_sim.py."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_GROWBOT = Path(__file__).resolve().parents[3] / "growbot"
_SITE = _GROWBOT / "site"
_VISION = _GROWBOT / "patches" / "growbot-vision.js"
_PANO = _GROWBOT / "patches" / "growbot-pano.js"

# Draws maps of a scene for the camera as the sim has it (48x64 portrait, 75 degrees tall,
# 10 cm up, pitched 47 degrees). scene(fwd, left) gives what a ray meets as [label, distance
# along the floor] or null for nothing within 10 m; up is checked by the caller's band.
_DRAW = """
const V=require(VISION);
function draw(scene, o){
  o=Object.assign({w:48,h:64,vfov:75,pitch:47,camH:0.1},o||{});
  const D=Math.PI/180, w=o.w, h=o.h, fy=(h/2)/Math.tan(o.vfov*D/2);
  const hfov=2*Math.atan(Math.tan(o.vfov*D/2)*w/h)/D, fx=(w/2)/Math.tan(hfov*D/2);
  const cp=Math.cos(o.pitch*D), sp=Math.sin(o.pitch*D);
  const depth=[], labels=[];
  for(let r=0;r<h;r++) for(let c=0;c<w;c++){
    const yn=(h/2-(r+0.5))/fy, xn=(w/2-(c+0.5))/fx;
    const f=cp-yn*sp, deg=Math.atan2(xn,f)/D, rise=sp+yn*cp;
    const hit=scene(deg, rise/f);
    if(!hit){ depth.push(0); labels.push(0); continue; }
    // A vertical face hit at horizontal distance d along this column's bearing.
    const fwd=hit[1]*Math.cos(deg*D), z=fwd/f;
    depth.push(z); labels.push(V.VOCAB.indexOf(hit[0]));
  }
  return Object.assign({depth, labels}, o);
}
"""


def _node(script: str, pre: str = "") -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prog = _DRAW.replace("VISION", json.dumps(str(_VISION))) + pre + script
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_built_site_carries_the_vision_script_as_written() -> None:
    assert (_SITE / "growbot-vision.js").read_bytes() == _VISION.read_bytes()


def test_a_wall_ahead_is_as_far_as_it_is_in_every_direction_he_looks() -> None:
    sc = _node(
        "const s=V.reduce(draw((deg)=>['wall', 1.0/Math.cos(deg*Math.PI/180)]));"
        "console.log(JSON.stringify(s))"
    )
    assert len(sc["cols"]) == 9
    assert sc["hfov"] == pytest.approx(59.0, abs=1)
    for c in sc["cols"]:
        assert c["what"] == "wall"
        # Across the floor to the wall along that bearing.
        assert c["m"] == pytest.approx(1.0 / __import__("math").cos(c["deg"] / 57.2958), abs=0.04)
    assert sc["ahead"] == pytest.approx(1.0, abs=0.03)
    assert sc["blocked"] is True
    assert sc["openings"] == [] and sc["things"] == []


def test_nothing_near_his_height_is_clear_as_far_as_his_picture_reaches() -> None:
    # A wall 10 m off is seen only far above his height: clear, as far as the lowest ray
    # carries under half a metre (0.4 m / tan(47 - 37.5 degrees), about 2.4 m).
    sc = _node("console.log(JSON.stringify(V.reduce(draw(()=>['wall',10]))))")
    assert all(c["m"] is None for c in sc["cols"])
    assert sc["reach"] == pytest.approx(0.4 / 0.1673, abs=0.05)
    assert sc["ahead"] == sc["reach"] and sc["blocked"] is False


def test_his_left_is_plus_as_everywhere_on_the_page_and_a_gap_is_an_opening() -> None:
    # A couch filling the left half of the picture 0.8 m off, and the room open beyond on the
    # right.
    sc = _node(
        "const s=V.reduce(draw((deg)=>deg>0?['sofa',0.8]:['wall',10]));"
        "console.log(JSON.stringify(s))"
    )
    left = [c for c in sc["cols"] if c["deg"] > 5]
    right = [c for c in sc["cols"] if c["deg"] < -5]
    assert all(c["what"] == "sofa" and c["m"] < 1.0 for c in left)
    assert all(c["m"] is None for c in right)
    [sofa] = sc["things"]
    assert sofa["what"] == "sofa" and sofa["deg"] > 10 and sofa["m"] == pytest.approx(0.8, abs=0.1)
    [gap] = sc["openings"]
    assert gap["deg"] < 0 and gap["more"] is True


def test_a_table_top_above_his_head_is_a_thing_not_a_wall() -> None:
    # What rises past half a metre within a metre (a table top) does not stop his legs.
    sc = _node(
        "const s=V.reduce(draw((deg,slope)=>0.1+slope*0.9>0.6&&0.1+slope*0.9<0.8?['table',0.9]"
        ":['wall',10]));"
        "console.log(JSON.stringify(s))"
    )
    assert all(c["m"] is None for c in sc["cols"])
    assert [t["what"] for t in sc["things"]] == ["table"]


def test_his_own_body_and_the_ceiling_are_left_out() -> None:
    sc = _node(
        "const s=V.reduce(draw((deg)=>Math.abs(deg)<10?['self',0.05]:['ceiling',0.3]));"
        "console.log(JSON.stringify(s))"
    )
    assert all(c["m"] is None for c in sc["cols"]) and sc["things"] == []


def test_pushing_is_walking_on_with_something_close_that_gets_no_closer() -> None:
    got = _node(
        "const run=(ahead,walking)=>{const h=[];for(let t=0;t<=8000;t+=500)"
        "h.push({t,ahead:typeof ahead==='function'?ahead(t):ahead,walking});return V.pushing(h);};"
        "console.log(JSON.stringify(["
        "run(0.4,true),"  # stuck against it
        "run(t=>0.6-t/1000*0.04,true),"  # walking up to it at 4 cm/s
        "run(0.4,false),"  # standing in front of it
        "run(null,true),"  # nothing near ahead
        "run(1.2,true),"  # far: a wall at 1.2 m is not a push even if it does not close
        "V.pushing([{t:0,ahead:0.08,walking:true},{t:300,ahead:0.07,walking:true}]),"
        "V.pushing([{t:0,ahead:null,walking:true},{t:300,ahead:0.07,walking:true}]),"
        "V.pushing([{t:0,ahead:0.4,walking:true},{t:3000,ahead:0.4,walking:true}])]))"
    )
    assert got == [True, False, False, False, False, True, False, False]


def test_the_line_for_his_thought_reads_left_ahead_right_then_openings_and_things() -> None:
    line = _node(
        "console.log(JSON.stringify(V.line(V.reduce(draw((deg)=>deg>0?['sofa',0.8]:['wall',10])))))"
    )
    assert line.startswith("[your depth sense")
    assert line.index("left (") < line.index("ahead:") < line.index("right (")
    assert "sofa" in line and "or more, clear" in line and "open way" in line
    assert "map:{saw" in line
    assert _node("console.log(JSON.stringify(V.line(null)))") == ""


def test_how_far_a_named_thing_is_comes_from_the_direction_he_named() -> None:
    got = _node(
        "const s=V.reduce(draw((deg)=>deg>0?['sofa',0.8]:['wall',10]));"
        "console.log(JSON.stringify([V.distAt(s,20),V.distAt(s,-20),V.distAt(s,120),V.distAt(null,0)]))"
    )
    left, right, behind, none = got
    assert left == pytest.approx(0.85, abs=0.1)
    assert right is None and behind is None and none is None


_PAGE = """
let now=100000; Date.now=()=>now;
globalThis.window=globalThis; globalThis.document={};
globalThis.MODE='legs'; globalThis._motion=null; const log=[]; globalThis.logEv=(k,d)=>log.push(k);
let h=90; window.gbMap={ map:()=>({here:{x:0,y:0,h}}) };
delete require.cache[require.resolve(VISION)];
const G=require(VISION);
"""


def _page(script: str) -> Any:
    return _node(script, pre=_PAGE.replace("VISION", json.dumps(str(_VISION))))


def test_with_no_source_feeding_it_the_page_says_and_does_nothing() -> None:
    got = _page(
        "console.log(JSON.stringify([G.fresh(),G.take(),G.aheadNow(),G.isPushing(),G.distNow(0)]))"
    )
    assert got == [None, "", None, False, None]


def test_a_fed_frame_is_fresh_for_a_moment_and_only_ahead_where_he_still_faces() -> None:
    got = _page(
        "const mp=draw((deg)=>['wall',0.25/Math.cos(deg*Math.PI/180)]);"
        "mp.depth_mm=mp.depth.map(v=>Math.round(v*1000)); delete mp.depth; mp.t=now;"
        "G.feed(mp); const a=[G.aheadNow(), !!G.take(), G.distNow(0)];"
        "h=130; a.push(G.aheadNow(), G.distNow(-40));"  # turned 40 left since
        "MODE='desk'; a.push(G.take()); MODE='legs';"
        "now+=2000; a.push(G.fresh(), G.aheadNow(), G.distNow(-40));"
        "now+=9000; a.push(G.distNow(-40));"
        "console.log(JSON.stringify(a))"
    )
    ahead, said, here, turned_ahead, turned_dist, desk, stale, stale_ahead, kept, gone = got
    assert ahead == pytest.approx(0.25, abs=0.03) and said is True
    assert here == pytest.approx(0.25, abs=0.03)
    # Facing 40 degrees away, the frame no longer says what is ahead, but still where the wall
    # he saw is.
    assert turned_ahead is None and turned_dist == pytest.approx(0.25, abs=0.03)
    assert desk == ""
    assert stale is None and stale_ahead is None and kept == pytest.approx(0.25, abs=0.03)
    assert gone is None


def test_walking_into_a_wall_it_sees_is_pushing() -> None:
    got = _page(
        "globalThis._motion={kind:'walk',report:{mv:{}}};"
        "const mp=draw((deg)=>['wall',0.3/Math.cos(deg*Math.PI/180)]);"
        "const out=[];"
        "for(let i=0;i<16;i++){"
        " mp.t=now; G.feed(Object.assign({},mp)); out.push(G.isPushing()); now+=500; }"
        "G.clearHist(); mp.t=now; G.feed(Object.assign({},mp)); out.push(G.isPushing());"
        "console.log(JSON.stringify(out))"
    )
    # Not until he has walked on for six seconds, then until the history is cleared.
    assert got[:12] == [False] * 12
    assert got[13] is True
    assert got[-1] is False


def _pano(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prog = f"const P=require({json.dumps(str(_PANO))});{script}"
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_each_way_on_the_strip_says_how_far_it_is_clear_by_his_depth_sense() -> None:
    got = _pano(
        "const ws=P.ways(90);"
        "const seen=[{h:112,m:0.4},{h:118,m:0.9},{h:68,m:null,open:2.4},{h:250,m:1.5}];"
        "const cl=P.wayClear(seen,ws);"
        "console.log(JSON.stringify([cl,P.clearText(cl),P.clearText(P.wayClear([],ws))]))"
    )
    cl, text, none = got
    # Way 4 faces 112.5, way 5 67.5, way 8 -157.5 (202.5); way 1 faces 247.5.
    assert cl[3] == {"n": 4, "m": 0.4}
    assert cl[4] == {"n": 5, "m": None, "open": 2.4}
    assert cl[0] == {"n": 1, "m": 1.5}
    assert cl[1] is None and cl[7] is None
    assert "4 blocked at 0.4 m" in text and "5 clear 2.4 m or more" in text
    assert "2 not looked along" in text
    assert none == ""


def test_a_trip_or_a_round_of_the_walls_does_not_walk_into_what_it_sees_close_ahead() -> None:
    got = _pano(
        "const tr={abs:90,x:0,y:0,t:0};"
        "const far=P.step({abs:90,x:0,y:0,t:0},{x:0,y:0,h:90},1000,false,false,1.2);"
        "const near=P.step(tr,{x:0,y:0,h:90},1000,false,false,0.2);"
        "const w={wall:true,side:1,x:0,y:0,t:0,room:'',want:null,walked:false,blocks:0};"
        "const wall=P.wallStep(w,{x:0,y:0,h:90,room:''},1000,false,false,0.2);"
        "const open=P.wallStep({wall:true,x:0,y:0,t:0,room:'',want:null,walked:false,blocks:0},"
        "{x:0,y:0,h:90,room:''},1000,false,false,null);"
        "console.log(JSON.stringify([far,near,wall,w.blocks,w.why,open]))"
    )
    far, near, wall, blocks, why, open_ = got
    assert far["walk"]["dir"] == "fwd"
    assert near["done"] == "you saw something 0.2 m ahead on that way"
    # As if he had walked into it: turned 90 away, keeping it on his side, without the bump.
    assert wall["walk"] == {"turn": 90} and blocks == 1 and "seen 0.2 m" in why
    assert open_["walk"]["dir"] == "fwd"


def test_the_creature_page_loads_it_and_its_line_rides_his_next_thought() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '<script src="/growbot-vision.js?v=' in page
    i = page.index("window.gbVision.take()")
    assert page.index("window.gbPano.take()") < i
    assert page.index('parts.push({type:"text",text:userMsg});', i) > i
