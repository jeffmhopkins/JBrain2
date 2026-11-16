"""growbot-camtest.js: the standalone camera test (owner, 2026-11-15: "set up the phone and you do
face tracking on me and make sure that it can keep up ... we want to be able to track faces at a
further distance than straight up very close"). Its pure parts run here in Node: the geometry,
the zoom tracker's planner, the per-station numbers and the verdicts, and the run id the box's
body test log takes."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3] / "growbot"
_PATCH = _ROOT / "patches" / "growbot-camtest.js"


def _eval(body: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        f"const m = await import({json.dumps(_PATCH.as_uri())});"
        f"console.log(JSON.stringify(await (async () => {{ {body} }})()));"
    )
    out = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_site_serves_the_patch_files() -> None:
    js = "growbot-camtest.js"
    assert (_ROOT / "site" / js).read_bytes() == (_ROOT / "patches" / js).read_bytes()
    # The page differs only by the build's cache-busting `?v=` on our scripts.
    page = (_ROOT / "site" / "camtest.html").read_text(encoding="utf-8")
    page = re.sub(r"\?v=[0-9a-f]{12}", "", page)
    assert page == (_ROOT / "patches" / "camtest.html").read_text(encoding="utf-8")


def test_the_page_loads_its_script_by_content_hash() -> None:
    page = (_ROOT / "site" / "camtest.html").read_text(encoding="utf-8")
    assert re.search(r'<script type="module" src="/growbot-camtest\.js\?v=[0-9a-f]{12}">', page)


def test_its_run_ids_are_ones_the_box_keeps() -> None:
    serve = (_ROOT / "server" / "serve.py").read_text(encoding="utf-8")
    pattern = re.search(r'BODYTEST_RUN_RE = re\.compile\(r"([^"]+)"\)', serve)
    assert pattern
    ids = _eval("return [m.runId(Date.now()), m.runId(1), m.runId(1e15)];")
    for run in ids:
        assert re.fullmatch(pattern.group(1), run), run


def test_the_box_serves_the_pose_model() -> None:
    serve = (_ROOT / "server" / "serve.py").read_text(encoding="utf-8")
    assert '".task": "application/octet-stream"' in serve


def test_the_wake_screen_opens_the_camera_test() -> None:
    box = (_ROOT / "site" / "growbot-box.js").read_text(encoding="utf-8")
    assert 'b.id = "wgCamTest"' in box
    assert 'location.href = "/camtest.html"' in box


def test_geometry() -> None:
    got = _eval(
        """
        return {
          portrait: m.hfov(720, 960), landscape: m.hfov(960, 720),
          centre: m.angleOf(0.5, 60), edge: m.angleOf(1, 60), left: m.angleOf(0, 60),
          // A 15 cm face at 1 m spans 8.6° of 60°.
          oneMetre: m.distanceOf(2 * Math.atan(0.075) * 180 / Math.PI / 60, 60),
          calibrated: m.distanceOf(0.05, 60, 0.1), none: m.distanceOf(0, 60),
        };
        """
    )
    assert got["portrait"] == 60 and got["landscape"] == 75
    assert got["centre"] == 0
    assert got["edge"] == pytest.approx(30) and got["left"] == pytest.approx(-30)
    assert got["oneMetre"] == pytest.approx(1.0)
    assert got["calibrated"] == pytest.approx(2.0)
    assert got["none"] is None


def test_search_tiles_cover_the_whole_frame() -> None:
    covered = _eval(
        """
        const out = [];
        for (const [w, h] of [[720, 960], [480, 640], [960, 720], [320, 320]]) {
          const ts = m.tiles(w, h);
          let all = true;
          for (let x = 0; x < w; x += 7) for (let y = 0; y < h; y += 7)
            if (!ts.some(t => x >= t.x && x < t.x + t.w && y >= t.y && y < t.y + t.h)) all = false;
          out.push([all, ts.every(t => t.x >= 0 && t.y >= 0 && t.x + t.w <= w && t.y + t.h <= h)]);
        }
        return out;
        """
    )
    assert covered == [[True, True]] * 4


def test_the_zoom_tracker_searches_then_crops_around_the_face() -> None:
    got = _eval(
        """
        const z = new m.Zoom(), out = {};
        out.search = [z.plan(720, 960, 0).why, z.plan(720, 960, 66).why, z.plan(720, 960, 132).why];
        // A small, far face found in a tile: the crop is five face-widths, centred on it.
        out.hit = !!z.update([{x: 300, y: 300, w: 40, h: 40}], 200);
        out.crop = z.plan(720, 960, 266);
        // A second face far from ours is not taken for it.
        out.other = z.update([{x: 10, y: 900, w: 40, h: 40}], 300);
        // A big near face, on a fresh tracker: the crop is the frame's short side, inside it.
        const z2 = new m.Zoom();
        z2.update([{x: 600, y: 100, w: 200, h: 200}], 330);
        out.near = z2.plan(720, 960, 340);
        // Four misses in a row and it searches again.
        for (let i = 0; i < 4; i++) z.update([], 400 + i * 66);
        out.after = z.plan(720, 960, 700).why;
        return out;
        """
    )
    assert got["search"] == ["frame", "tile", "frame"]
    assert got["hit"] is True
    assert got["crop"]["why"] == "track"
    assert got["crop"]["w"] == 200 and got["crop"]["h"] == 200
    assert got["crop"]["x"] + 100 == pytest.approx(320, abs=2)
    assert got["other"] is None
    assert got["near"]["w"] == 720 and got["near"]["x"] == 0
    assert got["after"] in ("frame", "tile")


def test_station_numbers_and_verdicts() -> None:
    got = _eval(
        """
        // `now` sees the face only at 0.7 m; `zoom` everywhere; `full` to 2 m. Face widths are
        // the true fraction for a 15 cm face at that distance in a 60° frame.
        const width = d => 2 * Math.atan(0.075 / d) * 180 / Math.PI / 60;
        const stations = {};
        for (const s of m.STATIONS) {
          const rows = [];
          for (let i = 0; i < 30; i++) {
            const d = s.m || 1.5, wf = width(d), x = 0.5 - wf / 2;
            const hit = [x, 0.3, wf, wf, 0.9, 10];
            rows.push({
              t: i * 66,
              now: d <= 0.7 ? hit : [5], full: d <= 2 ? hit : [20],
              zoom: hit, body: [0.3, 0.2, 0.4, 0.7, 1, 40],
            });
          }
          stations[s.id] = m.stationStats(rows, 60, 0);
        }
        return {d25: stations.d25, v: m.verdicts(stations, {hdeg: 60})};
        """
    )
    d25 = got["d25"]
    assert d25["zoom"]["rate"] == 1 and d25["now"]["rate"] == 0 and d25["full"]["rate"] == 0
    assert d25["zoom"]["dist"] == pytest.approx(2.5, abs=0.1)
    res = got["v"]["results"]
    assert res["now"]["v"] == "fail" and "sees you to 0.7 m" in res["now"]["why"]
    assert res["full"]["v"] == "warn" and "sees you to 2 m" in res["full"]["why"]
    assert res["zoom"]["v"] == "ok" and "sees you to 3 m" in res["zoom"]["why"]
    # The Body tab reads every verdict as {v, why}.
    assert all(set(r) == {"v", "why"} and r["v"] in ("ok", "warn", "fail") for r in res.values())


def test_losses_and_edges_are_measured() -> None:
    got = _eval(
        """
        // Sweeps right until the face is lost at the frame's edge, three misses in a row.
        const rows = [];
        for (let i = 0; i < 20; i++)
          rows.push({t: i * 66, zoom: [0.05 * i, 0.3, 0.1, 0.1, 0.9, 10]});
        for (let i = 20; i < 25; i++) rows.push({t: i * 66, zoom: [12]});
        return m.stationStats(rows, 60, 0).zoom;
        """
    )
    assert got["rate"] == 0.8
    assert got["losses"] == [pytest.approx(got["right"])]
    assert got["right"] > 20 and got["left"] < 0
    assert got["speed"] > 0
