"""Dave's occupancy grid and frontier planner (growbot/patches/growbot-grid.js, wave E2 of
docs/plans/GROWBOT_EXPLORE_PLAN.md): scans from his depth sense build the map, and exploring
goes to the nearest edge of what he has seen. The rooms here are drawn as wall segments and
scanned by ray casting, so each test knows the true answer."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_GROWBOT = Path(__file__).resolve().parents[3] / "growbot"
_SITE = _GROWBOT / "site"
_GRID = _GROWBOT / "patches" / "growbot-grid.js"
_VISION = _GROWBOT / "patches" / "growbot-vision.js"

# A 4 x 3 m room with a 0.8 m doorway in its right wall (y 1 to 1.8) onto a 2 m corridor.
# look(x, y) scans a full turn from there as the sim's camera would: 24 directions across 60
# degrees, clear past 2.4 m (what the lowest ray reaches at his walking pitch).
_ROOM = """
const G=require(GRID);
const ROOM=[[0,0,4,0],[0,3,4,3],[0,0,0,3],[4,0,4,1],[4,1.8,4,3],
  [4,1,6,1],[4,1.8,6,1.8],[6,1,6,1.8]];
function cast(segs,x,y,a){
  let best=null; const dx=Math.cos(a),dy=Math.sin(a);
  for(const [x1,y1,x2,y2] of segs){
    const ex=x2-x1,ey=y2-y1,den=dx*ey-dy*ex; if(Math.abs(den)<1e-9) continue;
    const t=((x1-x)*ey-(y1-y)*ex)/den, u=((x1-x)*dy-(y1-y)*dx)/den;
    if(t>0&&u>=0&&u<=1&&(best===null||t<best)) best=t;
  }
  return best;
}
function look(g,x,y,segs){
  segs=segs||ROOM;
  for(let h=0;h<360;h+=20){
    const scan=[];
    for(let q=0;q<24;q++){
      const deg=30-2.5*(q+0.5), d=cast(segs,x,y,(h+deg)*Math.PI/180);
      scan.push({deg, m: d!==null&&d<2.4?d:null});
    }
    g.add({x,y,h},scan,2.4);
  }
}
function cell(x,y){ return [Math.floor(x/G.CELL),Math.floor(y/G.CELL)]; }
function at(g,x,y){ const c=cell(x,y); return g.at(c[0],c[1]); }
"""


def _node(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prog = _ROOM.replace("GRID", json.dumps(str(_GRID))) + script
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_built_site_carries_the_grid_script_after_the_vision() -> None:
    assert (_SITE / "growbot-grid.js").read_bytes() == _GRID.read_bytes()
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    vision = page.index('<script src="/growbot-vision.js?v=')
    assert page.index('<script src="/growbot-grid.js?v=') > vision


def test_a_scan_frees_the_floor_it_crosses_and_marks_the_wall_it_ends_on() -> None:
    got = _node(
        "const g=new G.Grid();"
        "g.add({x:1,y:1,h:0},[{deg:0,m:1.0},{deg:20,m:null}],2.0);"
        "console.log(JSON.stringify({"
        "near:at(g,1.5,1.0), wall:at(g,2.0,1.0), past:at(g,2.3,1.0),"
        "open:at(g,1+1.5*Math.cos(20*Math.PI/180),1+1.5*Math.sin(20*Math.PI/180)),"
        "beyond:at(g,1+2.3*Math.cos(20*Math.PI/180),1+2.3*Math.sin(20*Math.PI/180))}))"
    )
    # One scan only reaches the free mark twice over where its fanned rays overlap: seen free
    # needs two looks, the wall one.
    assert got["wall"] == 1
    assert got["past"] == 0 and got["beyond"] == 0
    twice = _node(
        "const g=new G.Grid();"
        "for(let k=0;k<2;k++) g.add({x:1,y:1,h:0},[{deg:0,m:1.0},{deg:20,m:null}],2.0);"
        "console.log(JSON.stringify({near:at(g,1.5,1.0),"
        "open:at(g,1+1.5*Math.cos(20*Math.PI/180),1+1.5*Math.sin(20*Math.PI/180))}))"
    )
    assert twice == {"near": -1, "open": -1}


def test_exploring_heads_for_the_doorway_then_down_the_corridor_then_is_done() -> None:
    got = _node(
        "const g=new G.Grid(), out=[];"
        "look(g,1,1.5); out.push(g.next(1,1.5,[]));"
        "look(g,3.3,1.5); out.push(g.next(3.3,1.5,[]));"
        "look(g,5.3,1.4); look(g,1,0.6); look(g,1,2.4); look(g,3,2.4); look(g,3,0.6);"
        "out.push(g.next(5.3,1.4,[]));"
        "console.log(JSON.stringify(out))"
    )
    first, second, last = got
    # From the room's middle, the far reach toward the doorway is the nearest unseen edge.
    assert first["goal"]["x"] > 3.0 and 1.0 < first["goal"]["y"] < 1.8
    # From near the doorway, the corridor beyond it.
    assert second["goal"]["x"] > 4.2 and 1.0 < second["goal"]["y"] < 1.8
    # Every corner seen: nothing left he can reach.
    assert last is None


def test_a_route_through_a_doorway_keeps_off_its_jambs() -> None:
    got = _node(
        "const g=new G.Grid();"
        "look(g,1,1.4); look(g,3.3,1.4); look(g,5.3,1.4);"
        "const P=g.paths(1,2.5), cs=[]; let best=null;"
        "for(const [k,d] of P.dist){ const c=[Math.floor(k/65536)-32768,(k%65536)-32768];"
        "  if(c[0]*G.CELL>5.0&&(!best||d<best.d)) best={k,d}; }"
        "const pts=g.route(P,best.k);"
        "console.log(JSON.stringify(pts))"
    )
    crossing = [p for p in got if 3.5 < p["x"] < 4.5]
    # Any point at the doorway is in its middle third, and the route gets past it.
    assert all(1.15 <= p["y"] <= 1.65 for p in crossing)
    assert got[-1]["x"] > 5.0


def test_the_planner_never_plans_through_floor_it_has_not_seen() -> None:
    got = _node(
        "const g=new G.Grid();"
        # Only a narrow look ahead: the floor beside him was never seen.
        "g.add({x:1,y:1,h:0},Array.from({length:24},(_,q)=>({deg:30-2.5*(q+0.5),m:null})),2.0);"
        "g.add({x:1,y:1,h:0},Array.from({length:24},(_,q)=>({deg:30-2.5*(q+0.5),m:null})),2.0);"
        "const P=g.paths(1,1);"
        "console.log(JSON.stringify({ahead:P.dist.has((()=>{const c=cell(2.5,1);"
        "return (c[0]+32768)*65536+(c[1]+32768);})()),"
        "side:P.dist.has((()=>{const c=cell(1,2);return (c[0]+32768)*65536+(c[1]+32768);})())}))"
    )
    assert got == {"ahead": True, "side": False}


def test_a_goal_not_reached_is_not_tried_again() -> None:
    got = _node(
        "const g=new G.Grid();"
        "look(g,1,1.5); const a=g.next(1,1.5,[]); const b=g.next(1,1.5,[a.goal]);"
        "console.log(JSON.stringify({a:a.goal,b:b&&b.goal}))"
    )
    a, b = got["a"], got["b"]
    assert b is None or abs(a["x"] - b["x"]) + abs(a["y"] - b["y"]) > 0.3


def test_things_seen_again_near_the_same_spot_are_one_thing() -> None:
    got = _node(
        "const g=new G.Grid();"
        "g.saw({x:0,y:0,h:0},'sofa',0,2.0); g.saw({x:0,y:0,h:90},'sofa',-90,2.2);"
        "g.saw({x:0,y:0,h:0},'sofa',90,2.0); g.saw({x:0,y:0,h:0},'piano',0,2.1);"
        "console.log(JSON.stringify(g.things))"
    )
    sofas = [t for t in got if t["what"] == "sofa"]
    assert len(sofas) == 2 and len(got) == 3
    first = next(t for t in sofas if t["n"] == 2)
    assert first["x"] == pytest.approx(2.1, abs=0.01) and first["y"] == pytest.approx(0, abs=0.01)


def test_the_depth_sense_hands_the_map_a_fine_scan() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    # A wall 1 m ahead across the sim camera's view (as test_growbot_vision.py draws it).
    prog = (
        f"const V=require({json.dumps(str(_VISION))});"
        "const D=Math.PI/180,w=48,h=64,vfov=75,pitch=47,camH=0.1,fy=(h/2)/Math.tan(vfov*D/2);"
        "const hfov=2*Math.atan(Math.tan(vfov*D/2)*w/h)/D,fx=(w/2)/Math.tan(hfov*D/2);"
        "const cp=Math.cos(pitch*D),sp=Math.sin(pitch*D),depth=[],labels=[];"
        "for(let r=0;r<h;r++)for(let c=0;c<w;c++){const yn=(h/2-(r+0.5))/fy,xn=(w/2-(c+0.5))/fx;"
        "const f=cp-yn*sp,deg=Math.atan2(xn,f)/D;const fwd=1.0/Math.cos(deg*D)*Math.cos(deg*D);"
        "depth.push(fwd/f);labels.push(V.VOCAB.indexOf('wall'));}"
        "const s=V.reduce({w,h,vfov,pitch,camH,depth,labels});"
        "console.log(JSON.stringify(s.scan))"
    )
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True)
    scan = json.loads(out.stdout)
    assert len(scan) == 24
    assert scan[0]["deg"] > 25 and scan[-1]["deg"] < -25
    import math

    for c in scan:
        assert c["m"] == pytest.approx(1.0 / math.cos(math.radians(c["deg"])), abs=0.05)


def test_floor_glimpsed_once_is_still_a_way_on() -> None:
    # Seen free twice out to 1 m and once beyond: the edge at 1 m is a frontier, not a wall.
    got = _node(
        "const g=new G.Grid(), cols=n=>Array.from({length:24},(_,q)=>({deg:30-2.5*(q+0.5),"
        "m:null,open:n}));"
        "for(let h=0;h<360;h+=30) for(let k=0;k<2;k++) g.add({x:0,y:0,h},cols(1.0),0);"
        "g.add({x:0,y:0,h:0},cols(2.0),0);"
        "const n=g.next(0,0,[]); console.log(JSON.stringify(n&&n.goal))"
    )
    assert got is not None
    assert 0.8 <= got["x"] <= 1.1 and abs(got["y"]) <= 0.6


def test_he_looks_toward_what_he_has_not_seen_and_not_through_walls() -> None:
    got = _node(
        "const g=new G.Grid(), cols=n=>Array.from({length:24},(_,q)=>({deg:30-2.5*(q+0.5),"
        "m:null,open:n}));"
        # Everything round him seen out to 3 m except the quarter facing -y.
        "for(let h=0;h<360;h+=10) if(h<225||h>315) for(let k=0;k<2;k++)"
        " g.add({x:0,y:0,h},cols(3.0),0);"
        "const a=g.bestLook(0,0);"
        # A wall close in front of that quarter: nothing past it can be seen from here.
        "for(let k=0;k<3;k++) g.add({x:0,y:0,h:270},Array.from({length:24},(_,q)=>"
        "({deg:30-2.5*(q+0.5),m:0.5})),0);"
        "const b=g.bestLook(0,0);"
        "console.log(JSON.stringify({a,b}))"
    )
    assert 240 <= got["a"]["h"] <= 300 and got["a"]["n"] > 200
    assert got["b"]["n"] < got["a"]["n"] / 4


def test_a_frontier_running_back_to_his_feet_is_still_a_way_on() -> None:
    # Each look's wedge starts at his lens, so the frontier it leaves joins up with his own
    # spot; its far reaches must still be gone to, not dropped with the cells at his feet.
    got = _node(
        "const g=new G.Grid(), cols=n=>Array.from({length:24},(_,q)=>({deg:30-2.5*(q+0.5),"
        "m:null,open:n}));"
        "for(let h=0;h<360;h+=30) for(let k=0;k<2;k++) g.add({x:0,y:0,h},cols(1.0),0);"
        "const a=g.next(0,0,[]), b=g.bestLook(a.goal.x,a.goal.y);"
        "for(let k=0;k<2;k++) g.add({x:a.goal.x,y:a.goal.y,h:b.h},cols(1.0),0);"
        "const n=g.next(a.goal.x,a.goal.y,[]);"
        "console.log(JSON.stringify(n&&{cells:n.cells}))"
    )
    # The unseen ring a metre out is still ahead of him: the old ring plus the new wedge.
    assert got is not None and got["cells"] > 100


# The page half with its neighbours stubbed: a fed pose (FED) that moves FRAC of the way to his
# last route's end each beat, routes that end WHY at once, and a beat run by hand until he is
# done or has set off STOP times. The first REFUSE routes asked for are refused, as the page
# refuses a walk while his legs cool after an abort.
_PAGE = """
let now=0, beatFn=null, pos={x:1,y:1.5,h:0}, isFed=FED;
global.document={}; global.window=global;
global.setInterval=(f)=>{beatFn=f; return 1;}; global.clearInterval=()=>{};
Date.now=()=>now;
global.logEv=()=>{};
global.runGait=()=>{};
global.gbPose={source:()=>isFed?"fed":"reckoned"};
let goes=0, last=null, refuse=REFUSE, asked=0;
global.gbMap={map:()=>({here:pos}), turn:()=>null,
  go:(pts)=>{asked++; if(refuse-->0) return false; goes++; last=pts[pts.length-1]; return true;},
  goState:()=>last?{why:WHY}:null};
delete require.cache[require.resolve(GRID)];
const P=require(GRID);
look(P.grid(),1,1.5);
P.explore(true);
for(let b=0;b<3000&&!P.state().explore.done&&goes<STOP;b++){
  now+=500; beatFn();
  if(last){ pos={x:pos.x+(last.x-pos.x)*FRAC,y:pos.y+(last.y-pos.y)*FRAC,h:0}; }
}
console.log(JSON.stringify({st:P.state().explore, goes, asked, now}));
"""


def _page(
    fed: bool, why: str = "arrived", frac: float = 1.0, stop: int = 10**6, refuse: int = 0
) -> Any:
    return _node(
        _PAGE.replace("GRID", json.dumps(str(_GRID)))
        .replace("FED", json.dumps(fed))
        .replace("WHY", json.dumps(why))
        .replace("FRAC", str(frac))
        .replace("STOP", str(stop))
        .replace("REFUSE", str(refuse))
    )


def test_the_explorer_ends_rather_than_shuttling_between_edges_it_cannot_clear() -> None:
    # The map never grows here (nothing feeds it), so every edge stays: he must give each up
    # after arriving twice, and finish.
    got = _page(True)
    assert got["st"]["done"] == "explored"
    assert got["goes"] < 40


def test_with_no_fed_pose_the_explorer_says_so_and_stops() -> None:
    got = _page(False)
    assert got["st"]["done"] == "no_pose" and got["goes"] == 0


def test_a_frame_with_no_floor_labels_leaves_clear_columns_to_the_picture_s_reach() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    # Nothing near: every column is clear. Labelled with floor, each says how far floor showed;
    # unlabelled, none can, and the map falls back to the picture's reach.
    prog = (
        f"const V=require({json.dumps(str(_VISION))});"
        "const w=48,h=64,depth=new Array(w*h).fill(30);"
        "const fr=(labels)=>V.reduce({w,h,vfov:75,pitch:47,camH:0.1,depth,labels}).scan;"
        "console.log(JSON.stringify({bare:fr(undefined),"
        "floor:fr(new Array(w*h).fill(V.VOCAB.indexOf('floor')))}))"
    )
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True)
    got = json.loads(out.stdout)
    assert all(c["m"] is None and "open" not in c for c in got["bare"])
    assert all("open" in c for c in got["floor"])


def test_a_walk_cut_off_on_the_way_is_progress_not_a_failed_goal() -> None:
    # Every walk ends early ("ended": the legs' budget, say) but halfway there: he keeps going.
    got = _page(True, why="ended", frac=0.5, stop=6)
    assert got["goes"] == 6 and got["st"]["failed"] == 0
    # Ending where he started, twice, gives the goal up.
    stalled = _page(True, why="ended", frac=0.0, stop=3)
    assert stalled["st"]["failed"] >= 1


def test_a_stuck_route_gives_up_the_frontier_it_was_for() -> None:
    # Stuck where he stands every time: each give-up names a frontier next() then skips, so he
    # runs out of edges long before the tries cap. (A route cut short of its frontier, where
    # the frontier and the route's end differ, needs a frontier out of sight of where he
    # stands, which a map seen from one spot can't hold; that case is the same push.)
    got = _page(True, why="stuck", frac=0.0)
    assert got["st"]["done"] == "explored" and got["goes"] < 40


def test_a_refused_walk_is_waited_out_not_counted_against_the_frontier() -> None:
    # Cooling legs after an abort refused every route for a while: in the sim he gave up
    # every frontier in those seconds and called the house explored three minutes in.
    got = _page(True, why="ended", frac=0.5, stop=3, refuse=5)
    assert got["goes"] == 3 and got["st"]["failed"] == 0
    # He waited between asks rather than asking every beat.
    assert got["asked"] == 8 and got["now"] >= 5 * 5000
    # Legs that never move end the run as that, not as a house explored.
    never = _page(True, refuse=10**6)
    assert never["st"]["done"] == "no_legs" and never["st"]["failed"] == 0
