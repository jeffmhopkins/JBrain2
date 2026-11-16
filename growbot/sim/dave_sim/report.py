"""A run's report: `timeline.json` (everything), `run.mp4` (an onlooker's view of the robot
beside what its camera saw), and `report.html`, one page that reads top to bottom: the
checks, then what Dave heard, said and did, next to where the robot actually went."""

from __future__ import annotations

import html
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from dave_sim.run import Sim

# What a reader of the timeline wants from the page's own event log; the rest stays in
# timeline.json.
PAGE_EVENTS = {
    "skill",
    "move",
    "walk",
    "walk_start",
    "walk_end",
    "walk_stop",
    "follow",
    "follow_start",
    "follow_stop",
    "follow_lost",
    "face",
    "faces",
    "tip",
    "fall",
    "fell",
    "getup",
    "get_up",
    "gait",
    "stt",
    "brain_error",
    "estop",
    "bump",
    "motion_perm",
    "verb",
    "act",
    "body",
}

CSS = (
    "body{font:15px/1.4 system-ui,sans-serif;margin:16px;max-width:960px}"
    "table{border-collapse:collapse;width:100%}"
    "td{border-bottom:1px solid #ddd;padding:4px 6px;vertical-align:top}"
    "tr.ok td:nth-child(2){color:#17803d;font-weight:600}"
    "tr.bad td:nth-child(2){color:#b42318;font-weight:600}"
    "video{max-width:100%}"
)


def _said(text: str) -> str:
    try:
        reply = json.loads(text)
    except ValueError:
        return text[:300]
    if not isinstance(reply, dict):
        return text[:300]
    bits = []
    if reply.get("say"):
        bits.append(f'says "{reply["say"]}"')
    for k, v in reply.items():
        if k in ("say", "log", "ladder", "ladder_for", "goal", "now", "emotion", "sound", "burst"):
            continue
        bits.append(f"{k}={json.dumps(v)[:120]}")
    return "; ".join(bits) or text[:300]


def _row(e: dict[str, Any]) -> tuple[str, str] | None:
    k = e["kind"]
    if k == "you":
        return "you", f'"{e["text"]}"'
    if k == "dave":
        return ("Dave (warm-up)" if e.get("warm") else "Dave"), _said(e["text"])
    if k == "legs":
        f = e["frame"]
        if f.get("t") == "act":
            steps = f.get("steps", [])
            return (
                "legs",
                f"gesture, {len(steps)} keyframes, {sum(s.get('ms', 0) for s in steps)} ms",
            )
        return "legs", f.get("t", "")
    if k == "check":
        return ("PASS" if e["ok"] else "FAIL"), e["what"]
    if k in ("push", "person", "move"):
        return k, json.dumps({x: y for x, y in e.items() if x not in ("t", "kind")})[:200]
    if k == "page" and e.get("event") in PAGE_EVENTS:
        return f"page {e['event']}", json.dumps(e.get("data"))[:200]
    if k in ("page_error", "sim_error"):
        return k.replace("_", " "), e["error"]
    return None


def _last_user(req: dict[str, Any]) -> str:
    msgs = req.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
            return str(c)[-1500:]
    return ""


def _video(sim: Sim, path: Path) -> bool:
    """The onlooker's view, replayed from the recorded poses, beside the camera's pictures."""
    try:
        import imageio.v3 as iio
    except ImportError:
        return False
    b = sim.body
    d = b.data
    keep_q, keep_m = d.qpos.copy(), d.mocap_pos.copy()
    shots = []
    frames = sorted(sim.frames)
    try:
        for i, st in enumerate(sim.states[::3]):  # ~8 frames a second
            d.qpos[:] = st["qpos"]
            d.mocap_pos[:] = st["person_at"]
            import mujoco

            mujoco.mj_kinematics(b.model, d)
            out = b.outside_view()
            cam = None
            for t, jpeg in frames:
                if t <= st["wall"]:
                    cam = jpeg
            right = iio.imread(io.BytesIO(cam)) if cam else np.zeros_like(out)
            shots.append(np.concatenate([out, right[: out.shape[0], : out.shape[1]]], axis=1))
            if i > 3000:
                break
        if not shots:
            return False
        iio.imwrite(path, np.stack(shots), fps=25 / 3, codec="libx264")
        return True
    except Exception:
        return False
    finally:
        d.qpos[:], d.mocap_pos[:] = keep_q, keep_m
        import mujoco

        mujoco.mj_forward(b.model, d)


def write(sim: Sim) -> Path:
    out = sim.o.out
    out.mkdir(parents=True, exist_ok=True)
    states = [{k: v for k, v in s.items() if k not in ("qpos", "person_at")} for s in sim.states]
    (out / "timeline.json").write_text(
        json.dumps(
            {"checks": sim.checks, "timeline": sim.timeline, "states": states[::5]}, indent=1
        )
    )
    # The requests carry the persona's soul: kept beside the run, never in the repo.
    calls = [
        {
            "t": c.get("t"),
            "reply": c.get("reply"),
            "error": c.get("error"),
            "max_tokens": c["request"].get("max_tokens"),
            "format": (c["request"].get("response_format") or {}).get("type"),
            "last": _last_user(c["request"]),
        }
        for c in sim.box.calls
    ]
    (out / "model_calls.json").write_text(json.dumps(calls, indent=1))
    (out / "requests.json").write_text(json.dumps([c["request"] for c in sim.box.calls]))
    video = _video(sim, out / "run.mp4")
    for i, (t, jpeg) in enumerate(sim.frames[:: max(1, len(sim.frames) // 12)][:12]):
        (out / f"camera_{i:02d}_{t:06.1f}s.jpg").write_bytes(jpeg)

    passed = sum(c["ok"] for c in sim.checks)
    rows = []
    for e in sorted(sim.timeline, key=lambda e: e["t"]):
        r = _row(e)
        if r is None:
            continue
        st = min(sim.states, key=lambda s: abs(s["wall"] - e["t"])) if sim.states else {}
        where = (
            f"{st.get('x', 0) * 100:+.0f},{st.get('y', 0) * 100:+.0f} cm, "
            f"pitch {st.get('pitch_deg', 0):.0f}°"
            if st
            else ""
        )
        cls = {"PASS": "ok", "FAIL": "bad"}.get(r[0], "")
        rows.append(
            f'<tr class="{cls}"><td>{e["t"]:.1f}</td><td>{html.escape(r[0])}</td>'
            f"<td>{html.escape(r[1])}</td><td>{where}</td></tr>"
        )
    page = (
        '<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        f"<title>Dave sim run</title><style>{CSS}</style>"
        f"<h1>Dave sim run: {passed} of {len(sim.checks)} checks passed</h1>"
        + ('<video src="run.mp4" controls muted></video>' if video else "")
        + "<table><tr><th>s</th><th>who</th><th>what</th><th>robot</th></tr>"
        + "".join(rows)
        + "</table>\n"
    )
    (out / "report.html").write_text(page)
    return out / "report.html"
