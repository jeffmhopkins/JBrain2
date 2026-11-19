"""Every look Dave takes, kept until he dreams (growbot/patches/growbot-looks.js, owner
2026-11-19). The pure half (which turn each look rides in, the dream's clearing, the cap, the
words beside each picture) runs in node; the wiring is checked in the built creature page."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_GROWBOT = Path(__file__).resolve().parents[3] / "growbot"
_SITE = _GROWBOT / "site"
_LOOKS = _GROWBOT / "patches" / "growbot-looks.js"


def _node(script: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    prog = f"const L=require({json.dumps(str(_LOOKS))});{script}"
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_the_built_site_carries_the_looks_script_as_written() -> None:
    assert (_SITE / "growbot-looks.js").read_bytes() == _LOOKS.read_bytes()


def test_each_look_rides_in_the_turn_it_was_taken_in() -> None:
    got = _node(
        # Turns replied at 100 and 200; looks before the first, inside each, and after the last.
        "const s=L.split([100,200],[{t:5},{t:90},{t:100},{t:150},{t:250}]);"
        "console.log(JSON.stringify({by:s.by.map(a=>a.map(l=>l.t)),pending:s.pending.map(l=>l.t)}))"
    )
    # A look older than every turn the history replays rides in its first one; one sent the
    # moment a reply landed belongs to the next turn.
    assert got["by"] == [[5, 90], [100, 150]]
    # One newer than every turn waits at the history's end for the next kept turn.
    assert got["pending"] == [250]


def test_looks_whose_turn_the_window_dropped_ride_ahead_of_the_talk() -> None:
    """A window that jumps forward moves the dropped turns' looks onto the end of one early
    message: what the box read of that message before still reads the same."""
    got = _node(
        "const looks=[{t:5,j:'a'},{t:90,j:'b'},{t:150,j:'c'},{t:250,j:'d'}];"
        "const a=L.split([100,200,300],looks,0), b=L.split([200,300],looks,100),"
        " c=L.split([300],looks,200);"
        "const e=(s)=>JSON.stringify(L.early(s.early));"
        "console.log(JSON.stringify([a.early.length,b.early.map(l=>l.t),b.by.map(x=>x.map(l=>l.t)),"
        "e(c).startsWith(e(b).slice(0,-1)),L.early([])[0].text]))"
    )
    none, early, by, grows, head = got
    assert none == 0
    assert early == [5, 90] and by == [[150], [250]]
    assert grows is True
    assert head.startswith("(what you saw earlier")


def test_the_history_ahead_of_a_new_turn_reads_the_same() -> None:
    """The box resumes from the longest prefix it read before, so a new turn and its looks must
    leave every earlier turn's message exactly as it was."""
    got = _node(
        "const looks=[{t:50,j:'a'},{t:150,j:'b'}], one=[100], two=[100,200];"
        "const msg=(ts,ls)=>{const s=L.split(ts,ls);"
        " return ts.map((t,i)=>L.content(s.by[i],'turn '+t));};"
        "const before=msg(one,looks), after=msg(two,looks.concat([{t:180,j:'c'}]));"
        "console.log(JSON.stringify([JSON.stringify(before[0])===JSON.stringify(after[0]),"
        "after[1].filter(p=>p.type==='image_url').length]))"
    )
    assert got == [True, 2]


def test_a_look_rides_in_the_turn_that_sent_it_not_one_already_read() -> None:
    """A thought that began just after the last reply landed may send a picture taken before
    it: it rides in its own turn, not inside the one the box has already read."""
    got = _node(
        "const s=L.split([100,200],[{t:95,at:101},{t:99},{t:150,at:200},{t:150,at:150}],0);"
        "console.log(JSON.stringify([s.by.map(a=>a.map(l=>l.at||l.t)),s.pending.map(l=>l.at)]))"
    )
    by, pending = got
    assert by == [[99], [101, 150]]
    # A look its turn's reply has not landed after yet is pending.
    assert pending == [200]


def test_a_tick_keeps_its_look_only_after_a_while_or_a_move() -> None:
    got = _node(
        "const p={x:0,y:0,h:90}, last={t:0,p};"
        "console.log(JSON.stringify([L.tickKeeps(last,{t:10000,p}),L.tickKeeps(last,{t:30000,p}),"
        "L.tickKeeps(last,{t:10000,p:{x:0.6,y:0,h:90}}),L.tickKeeps(last,{t:10000,p:{x:0,y:0,h:130}}),"
        "L.tickKeeps(undefined,{t:5,p}),L.tickKeeps({t:0},{t:10000,p})]))"
    )
    assert got == [False, True, True, True, True, False]


def test_a_dream_clears_the_looks_before_it() -> None:
    got = _node("console.log(JSON.stringify(L.since([{t:30},{t:10},{t:20}],15).map(l=>l.t)))")
    assert got == [20, 30]


def test_past_the_cap_the_older_half_goes_in_one_step() -> None:
    got = _node(
        "const a=[...Array(11).keys()].map(t=>({t}));"
        "console.log(JSON.stringify([L.trim(a.slice(0,10),10).length,L.trim(a,10).map(l=>l.t),"
        "L.LOOK_MAX,L.LOOK_PX]))"
    )
    kept_at_cap, over, cap, px = got
    assert kept_at_cap == 10
    assert over == [6, 7, 8, 9, 10]
    assert cap == 60 and px == 160


def test_each_picture_says_when_and_where_it_was_taken() -> None:
    got = _node(
        "const t=new Date(2026,10,19,14,3,9).getTime();"
        "const l={t,j:'QUJD',eye:'b',p:{x:1.234,y:-0.46,h:134.6,room:'kitchen'}};"
        "console.log(JSON.stringify([L.tag(l),L.tag({t,j:'x'}),L.content([l],'words')]))"
    )
    full, bare, content = got
    assert full == (
        "[camera, a look you took at 2:03:09 pm with your rear eye, standing at (1.2, -0.5) m"
        " on your map facing 135 degrees in the kitchen]"
    )
    assert bare == "[camera, a look you took at 2:03:09 pm with your forward eye]"
    # A stored pose missing a number never throws: the look goes out without one.
    assert (
        _node(
            "const t=new Date(2026,10,19,14,3,9).getTime();"
            "console.log(JSON.stringify(L.tag({t,j:'x',p:{x:'a',y:1}})))"
        )
        == bare
    )
    # The look's words, its picture, then the turn's own words: as the live turn had them. The
    # tag opens "[camera" so a lane that cannot see drops it with the picture.
    assert content == [
        {"type": "text", "text": full},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        {"type": "text", "text": "words"},
    ]
    assert _node("console.log(JSON.stringify(L.content([],'words')))") == "words"


def test_a_look_that_shows_what_the_last_did_is_not_kept() -> None:
    got = _node(
        "const g=Array(64).fill(100), h=Array(64).fill(130), p={x:0,y:0,h:90};"
        "console.log(JSON.stringify(["
        "L.same({g,p},{g:g.map(v=>v+3),p:{x:0.1,y:0,h:100}}),"  # same view, same spot
        "L.same({g,p},{g:h,p}),"  # a different view
        "L.same({g,p},{g,p:{x:0.5,y:0,h:90}}),"  # walked half a metre
        "L.same({g,p},{g,p:{x:0,y:0,h:-150}}),"  # turned round
        "L.same({g,p:{x:0,y:0,h:179}},{g,p:{x:0,y:0,h:-179}}),"  # across the wrap
        "L.same(undefined,{g,p}),L.same({g},{g})]))"
    )
    assert got == [True, False, False, False, True, False, True]


def test_the_page_keeps_each_sent_picture_and_replays_the_looks() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '<script src="/growbot-looks.js?v=' in page
    # The picture a thought sends is kept, before the turn's frames are built.
    assert 'window.gbLooks.add(frames[frames.length-1],_eyeAt,_gbIssT,kind==="tick")' in page
    # The history weaves the looks into the turns they were taken in, pending ones at its end.
    hist = page[page.index("function gbHist(now)") : page.index("function gbWarmKey()")]
    assert "_gbL.split(" in hist and "_gbL.content(_gbLk.by[i]" in hist
    assert "_gbL.parts(_gbLk.pending)" in hist
    assert "_gbL.early(_gbLk.early)" in hist
    # A new look is new history: a warm-up or seal from before it no longer fits.
    assert "window.gbLooks.key()" in page[page.index("function gbWarmKey()") :][:400]


def test_the_pictures_leave_this_phone_with_the_creature() -> None:
    """As the faces do (growbot-box.js): a page whose creature has gone (moved away, deleted, a
    G2 session) deletes the store, and a page not running a box persona writes nothing there."""
    src = _LOOKS.read_text(encoding="utf-8")
    homeless = src[src.index("function homeless()") :][:200]
    assert "s.away" in homeless and "s.g2" in homeless and "!s.persona" in homeless
    load = src[src.index("function load()") :][:500]
    assert "if (bs && (bs.away || bs.g2))" in load and "deleteDatabase(DB)" in load
    # A page with no soul yet (redirecting to the robot it wears) leaves the store alone.
    assert load.index("deleteDatabase") < load.index("if (homeless()) { loaded = true; return; }")
    tx = src[src.index("function tx(fn)") :][:80]
    assert "if (homeless()) return;" in tx
    # Another persona's looks are not kept, whatever their age.
    assert "if (l.soul === me && l.t > dt) keep.push(l); else gone.push(l);" in src
