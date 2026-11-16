"""growbot-face-zoom.js: the creature's face finder, BlazeFace full-range on a crop that follows
the face (owner, 2026-11-16, after camera test run #223: "you're good to build then to update
the face tracking mechanism"). Its planner and its one-frame call run here in Node, with a fake
model that sees a face only when the face is inside what it was shown."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3] / "growbot"
_ZOOM = _ROOT / "patches" / "growbot-face-zoom.js"

# A fake FaceDetector: the "image" is a canvas whose last drawImage said which part of the
# frame it holds, or the frame itself; it reports every face wholly inside that part and at
# least 10 px across once scaled to a 192 px input, in the image's own pixels, as MediaPipe
# does.
_FAKE = """
const W = 720, H = 960;
let faces = [];
const frame = { w: W, h: H };
function canvas(side) {
  const c = { width: side, height: side, part: null };
  c.getContext = () => ({ drawImage: (src, x, y, w, h) => { c.part = { x, y, w, h }; } });
  return c;
}
const model = { calls: [], detectForVideo(src, at) {
  this.calls.push(at);
  const part = src === frame ? { x: 0, y: 0, w: W, h: H } : src.part;
  const sx = (src === frame ? W : src.width) / part.w;
  const sy = (src === frame ? H : src.height) / part.h;
  const detections = faces.filter(f => f.x >= part.x && f.y >= part.y &&
      f.x + f.w <= part.x + part.w && f.y + f.h <= part.y + part.h &&
      f.w * 192 / Math.max(part.w, part.h) >= 10)
    .map(f => ({ boundingBox: { originX: (f.x - part.x) * sx, originY: (f.y - part.y) * sy,
      width: f.w * sx, height: f.h * sy }, categories: [{ score: 0.9 }] }));
  return { detections };
} };
const face = (cx, cy, w) => ({ x: cx - w / 2, y: cy - w / 2, w, h: w });
"""


def _eval(body: str) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = (
        f"const z = await import({json.dumps(_ZOOM.as_uri())});{_FAKE}"
        f"console.log(JSON.stringify(await (async () => {{ {body} }})()));"
    )
    out = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def test_the_site_and_the_camera_test_use_the_patch_file() -> None:
    assert (_ROOT / "site" / "growbot-face-zoom.js").read_bytes() == _ZOOM.read_bytes()
    camtest = (_ROOT / "patches" / "growbot-camtest.js").read_text(encoding="utf-8")
    assert 'import { tiles, Zoom } from "./growbot-face-zoom.js";' in camtest


def test_a_far_face_found_in_a_tile_is_then_cropped_and_mapped_back_to_the_frame() -> None:
    got = _eval(
        """
        const zoom = new z.Zoom(), c = canvas(z.CROP_PX), out = [];
        faces = [face(500, 800, 26)];   // about 3 m away, low and to one side
        for (let i = 0; i < 12; i++) {
          const r = z.find(model, frame, W, H, i * 100, zoom, c);
          out.push([r.crop.why, r.face ? [r.face.x, r.face.width, r.face.score] : null]);
        }
        return out;
        """
    )
    whys = [why for why, _ in got]
    first = next(i for i, (_, f) in enumerate(got) if f)
    assert whys[first] == "tile"
    # Every frame after it is a crop around the face, and each finds it where it is.
    assert all(why == "track" for why in whys[first + 1 :])
    for _, f in got[first:]:
        assert f[0] == pytest.approx((500 - 13) / 720, abs=1e-3)
        assert f[1] == pytest.approx(26 / 720, abs=1e-3)
        assert f[2] == 0.9


def test_a_crop_that_misses_looks_at_the_whole_frame_next() -> None:
    """Quick side steps beat the crop's prediction (run #223: 81% with crops alone, 99% on the
    whole frame), so a miss is followed by a whole-frame look, and the one face it sees is
    taken wherever it went."""
    got = _eval(
        """
        const zoom = new z.Zoom(), c = canvas(z.CROP_PX), out = [];
        faces = [face(200, 400, 60)];
        z.find(model, frame, W, H, 0, zoom, c);            // the whole frame: found
        out.push(z.find(model, frame, W, H, 100, zoom, c).crop.why);
        faces = [face(600, 420, 60)];                        // stepped well past the crop
        const miss = z.find(model, frame, W, H, 200, zoom, c);
        const again = z.find(model, frame, W, H, 300, zoom, c);
        const next = z.find(model, frame, W, H, 400, zoom, c);
        return [out[0], miss.crop.why, miss.face, again.crop.why, !!again.face,
                next.crop.why, !!next.face];
        """
    )
    assert got == ["track", "track", None, "again", True, "track", True]


def test_a_whole_frame_look_does_not_take_a_stranger_among_several() -> None:
    got = _eval(
        """
        const zoom = new z.Zoom(), c = canvas(z.CROP_PX);
        faces = [face(100, 300, 60)];
        z.find(model, frame, W, H, 0, zoom, c);
        z.find(model, frame, W, H, 100, zoom, c);
        faces = [face(650, 900, 60), face(620, 100, 60)];   // ours gone, two others far off
        z.find(model, frame, W, H, 200, zoom, c);            // the crop misses
        const again = z.find(model, frame, W, H, 300, zoom, c);
        return [again.crop.why, again.face, again.boxes.length];
        """
    )
    assert got == ["again", None, 2]


def test_a_moving_face_gets_a_wider_crop() -> None:
    got = _eval(
        """
        const still = new z.Zoom(), moving = new z.Zoom();
        still.plan(W, H, 0); moving.plan(W, H, 0);
        still.update([{ x: 330, y: 400, w: 50, h: 50 }], 0);
        still.update([{ x: 330, y: 400, w: 50, h: 50 }], 100);
        moving.update([{ x: 230, y: 400, w: 50, h: 50 }], 0);
        moving.update([{ x: 330, y: 400, w: 50, h: 50 }], 100);
        return [still.plan(W, H, 200).w, moving.plan(W, H, 200).w];
        """
    )
    assert got[0] == 250
    assert got[1] > got[0] + 100


def test_one_model_call_a_frame_with_rising_timestamps() -> None:
    """MediaPipe's VIDEO mode wants each frame's timestamp above the last: the tracker calls the
    model exactly once a frame, crop or whole frame, never both."""
    got = _eval(
        """
        const zoom = new z.Zoom(), c = canvas(z.CROP_PX);
        faces = [face(300, 500, 60)];
        for (let i = 1; i <= 10; i++) {
          if (i === 5) faces = [];
          if (i === 7) faces = [face(320, 520, 60)];
          z.find(model, frame, W, H, i * 50, zoom, c);
        }
        return model.calls;
        """
    )
    assert got == [i * 50 for i in range(1, 11)]


def test_a_crop_maps_both_axes_back_and_puts_the_followed_face_first() -> None:
    got = _eval(
        """
        const zoom = new z.Zoom(), c = canvas(z.CROP_PX);
        faces = [face(300, 500, 60)];
        z.find(model, frame, W, H, 0, zoom, c);
        faces = [face(300, 500, 60), face(380, 560, 30)];   // a smaller face inside our crop
        const r = z.find(model, frame, W, H, 100, zoom, c);
        return [r.crop.why, r.boxes, r.face];
        """
    )
    why, boxes, followed = got
    assert why == "track"
    assert len(boxes) == 2 and boxes[0] == followed
    assert followed["x"] == pytest.approx(270 / 720, abs=1e-3)
    assert followed["y"] == pytest.approx(470 / 960, abs=1e-3)
    assert followed["width"] == pytest.approx(60 / 720, abs=1e-3)
    assert followed["height"] == pytest.approx(60 / 960, abs=1e-3)


def test_a_far_face_missed_is_looked_for_in_a_wider_crop_not_the_whole_frame() -> None:
    """At 3 m a face is a few px of the model's input once the whole frame is shrunk to it, so
    a whole-frame look can't find it: the look after a miss is a crop three times as wide."""
    got = _eval(
        """
        const zoom = new z.Zoom(), c = canvas(z.CROP_PX);
        faces = [face(400, 500, 26)];
        let r;
        for (let i = 0; i < 20 && !(r && r.face); i++) {
          r = z.find(model, frame, W, H, i * 100, zoom, c);
        }
        z.find(model, frame, W, H, 2100, zoom, c);
        faces = [face(520, 520, 26)];                        // stepped out of the crop
        const miss = z.find(model, frame, W, H, 2200, zoom, c);
        const wide = z.find(model, frame, W, H, 2300, zoom, c);
        return [miss.face, wide.crop, !!wide.face];
        """
    )
    miss, crop, found = got
    assert miss is None
    assert crop["why"] == "wide" and crop["w"] == 26 * 5 * 3
    assert found is True


def test_a_frame_smaller_than_the_least_crop_crops_to_itself() -> None:
    got = _eval(
        """
        const zoom = new z.Zoom();
        zoom.plan(176, 144, 0);
        zoom.update([{ x: 80, y: 60, w: 20, h: 20 }], 0);
        return zoom.plan(176, 144, 50);
        """
    )
    assert got["w"] == 144 and got["h"] == 144
    assert 0 <= got["x"] <= 176 - 144 and got["y"] == 0


def test_a_new_frame_size_drops_the_face_and_the_search_cycles_the_tiles() -> None:
    got = _eval(
        """
        const zoom = new z.Zoom();
        zoom.plan(W, H, 0);
        zoom.update([{ x: 300, y: 300, w: 60, h: 60 }], 0);
        const before = zoom.plan(W, H, 50).why;
        const after = zoom.plan(240, 180, 100).why;   // low power's frame
        const seen = new Set();
        for (let i = 0; i < 40; i++) {
          const p = zoom.plan(240, 180, 150 + i * 50);
          if (p.why === "tile") seen.add(p.x + "," + p.y);
        }
        return [before, after, seen.size, z.tiles(240, 180).length];
        """
    )
    assert got[0] == "track"
    assert got[1] in ("frame", "tile")
    assert got[2] == got[3]
