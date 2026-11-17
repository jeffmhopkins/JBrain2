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
        "console.log(JSON.stringify([c(0),c(20),c(-20),c(90),c(135),c(-90),P.CAM_HFOV]));"
    )
    ahead, left20, right20, left90, left135, right90, hfov = got
    assert hfov == pytest.approx(59.0, abs=1)
    # Straight ahead is the middle of the picture that faced ahead.
    assert ahead == {"id": "a", "sx": 48}
    # To his left is toward the left edge of his picture, to his right toward the right.
    assert left20["id"] == "a" and left20["sx"] < 48 < right20["sx"]
    # A quarter turn left is the middle of the picture that faced there.
    assert left90 == {"id": "b", "sx": 48}
    # Where no picture faced, nothing: the strip leaves it grey.
    assert left135 is None and right90 is None


def test_the_marks_run_from_behind_on_his_left_to_behind_on_his_right() -> None:
    got = _node("console.log(JSON.stringify([P.marks(), P.seenDeg([{h:0},{h:3}])]))")
    marks, seen = got
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
    assert marks[4]["x"] == 360 and marks[0]["x"] == 0 and marks[-1]["x"] == 720
    # One camera's width, and a few degrees more for a second picture turned 3 degrees.
    assert 58 <= seen <= 62


def test_the_creature_page_loads_it_and_sends_the_strip_with_his_next_thought() -> None:
    page = (_SITE / "v3" / "next.html").read_text(encoding="utf-8")
    assert '<script src="/growbot-pano.js?v=' in page
    i = page.index("window.gbPano.take()")
    # Beside his camera's picture, before the turn's own words.
    assert page.index('parts.push({type:"text",text:userMsg});', i) > i
    assert 'logEv("pano",' in page
