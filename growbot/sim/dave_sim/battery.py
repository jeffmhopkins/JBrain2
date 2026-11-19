"""Run an exploring battery: every house, a few starts each, one table.

    python -m dave_sim.battery --pose truth --vision truth --minutes 25 --parallel 2

Wave E0 of docs/plans/GROWBOT_EXPLORE_PLAN.md. One explore run takes 25 minutes of wall
clock at his walking pace, so tuning used to go by single runs, and a run with the live model
could not say which layer failed. A battery runs the flat and the three maze houses from
several starts with no model, unattended, and scores each run against the house itself:
rooms reached, how far he walked, how long he sat stuck, and falls. With `--pose truth` the
page's map gets the sim's true pose (growbot-map.js gbPose), so a planner can be judged on a
perfect pose before an estimated one.

The explorer under test is chosen with `--explorer`. `walls` is today's round of the walls
(growbot-pano.js), started by the page's own explore entry and started again whenever it
ends: the baseline the rebuild has to beat.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dave_sim import plan
from dave_sim.llm import scripted_model
from dave_sim.run import Options, Sim

# The flat's start, behind the couch in the living room's corner (scenarios._FLAT_START).
SCENES: dict[str, tuple[float, float, float] | None] = {
    "flat": (-3.3, -2.4, 180.0),
    "maze1": None,
    "maze2": None,
    "maze3": None,
}
# Each further seed starts him facing another third of the way round.
SEED_TURN_DEG = 120.0
# Stuck: under this far in a window this long.
STUCK_M, STUCK_S, STUCK_DEG = 0.05, 30.0, 10.0
# Down: not upright for this long in a row.
DOWN_S = 3.0
EXPLORERS = ("walls", "grid")
# Past its minutes, how long a child run may take to start, score and write its report.
CHILD_SLACK_S = 300.0


@dataclass
class Run:
    scene: str
    seed: int
    minutes: float
    pose: str
    vision: str
    explorer: str
    out: Path

    @property
    def name(self) -> str:
        return f"{self.scene}-s{self.seed}"


def _upright(st: dict[str, Any]) -> bool:
    # As scenarios._upright: from about 40 degrees nose-up at rest to the mid-60s standing tall.
    return bool(st["screen_up"]) and st["z"] > 0.04 and 25 < st["pitch_deg"] < 75


def score(sim: Sim, fed: bool = True) -> dict[str, Any]:
    """One run against the house: rooms reached of all, walked, stuck time, falls."""
    stats = plan.stats(sim)
    house = plan.layout(sim)
    rooms = stats.get("rooms_s") or {}
    reached = sorted(r for r, s in rooms.items() if s > 0 and r != "outside")
    states = sim.states
    # Stuck: a whole window with under STUCK_M of travel and under STUCK_DEG of turning (a
    # turn on the spot is doing what it was asked); every moment of such a window counts.
    marked = [False] * len(states)
    j = 0
    for i, s in enumerate(states):
        while j < len(states) and states[j]["wall"] < s["wall"] - STUCK_S:
            j += 1
        a = states[j]
        if s["wall"] - a["wall"] < STUCK_S - 1:
            continue
        span = states[j : i + 1]
        # The farthest he got, so backing off a wall and returning is not stuck.
        moved = max(math.hypot(p["x"] - a["x"], p["y"] - a["y"]) for p in span)
        turned = max(abs((p["nose_deg"] - a["nose_deg"] + 180.0) % 360.0 - 180.0) for p in span)
        if moved < STUCK_M and turned < STUCK_DEG:
            for k in range(j, i + 1):
                marked[k] = True
    stuck = sum(marked) * CONTROL_STEP_S
    falls, down_since = 0, None
    for s in states:
        if _upright(s):
            down_since = None
        elif down_since is None:
            down_since = s["wall"]
        elif down_since >= 0 and s["wall"] - down_since >= DOWN_S:
            falls += 1
            down_since = -1.0  # counted; wait until he is up again
    total = len(house.ROOMS) if house is not None else 0
    # Where his map ends up saying he is against where he is: the pose layer's error (with
    # --pose truth, the plumbing's; the map shares the house's frame then).
    off = None
    if sim.map_track:
        a = sim.map_track[-1]
        here = (float(a["here"].get("x", 0)), float(a["here"].get("y", 0)))
        # A fed map shares the house's frame; a reckoned one starts at its own origin, so it is
        # put into the house's frame from his first reading (plan.py's alignment).
        to_world = None if fed else plan._aligned(sim)
        if to_world is not None:
            here = to_world(*here)
        off = round(math.hypot(here[0] - a["x"], here[1] - a["y"]), 2)
    return {
        "pose_off_m": off,
        "rooms": len(reached),
        "rooms_total": total,
        "reached": reached,
        "walked_m": stats.get("walked_m", 0.0),
        "stuck_s": round(stuck),
        "falls": falls,
        "secs": round(states[-1]["wall"] - states[0]["wall"]) if states else 0,
    }


def start_of(scene: str, seed: int) -> tuple[float, float, float]:
    """Where a run sets him down: the scene's own start, turned a third of the way round per
    seed (only the heading changes: the start spot is known to be clear)."""
    at = SCENES[scene]
    if at is None:
        from growbot_train import house

        at = tuple(house.maze(int(scene.removeprefix("maze"))).start)
    x, y, yaw = at
    return x, y, (yaw + SEED_TURN_DEG * seed + 180.0) % 360.0 - 180.0


# Sim._record keeps a state every other physics step (run.CONTROL_DT).
CONTROL_STEP_S = 0.08


async def _walls(sim: Sim, until: float) -> None:
    """Today's explorer, with no model: a round of the walls, started again when it ends."""
    started = 0
    while sim.now() < until:
        try:
            idle = await sim.page.evaluate(
                "() => { var s = window.gbPano && window.gbPano.state && window.gbPano.state();"
                " return !!s && !s.trip && !(s.survey > 0); }"
            )
            if idle:
                await sim.page.evaluate("() => window.gbPano.reply({explore: {walls: true}})")
                started += 1
                sim.note("battery", started=started)
        except Exception as e:  # the page is reloading: try again on the next beat
            sim.note("battery", error=str(e)[:200])
        await sim.wait(5)


async def _grid(sim: Sim, until: float) -> None:
    """The occupancy grid's frontier explorer (wave E2, growbot-grid.js), with no model, until
    it says it is done; it is started again if the page reloaded under it."""
    while sim.now() < until:
        try:
            st = await sim.page.evaluate("() => window.gbGrid.state()")
            if not st or not st.get("explore"):
                st = await sim.page.evaluate("() => window.gbGrid.explore(true)")
            sim.note("battery", grid=st)
            if st and st.get("explore") and st["explore"].get("done"):
                break
        except Exception as e:  # the page is reloading: try again on the next beat
            sim.note("battery", error=str(e)[:200])
        await sim.wait(10)


async def run_one(r: Run, policies: dict[str, Path], skills: dict[str, str]) -> dict[str, Any]:
    at = start_of(r.scene, r.seed)
    opts = Options(
        model=scripted_model(lambda req: {"say": "okay"}),
        out=r.out / r.name,
        scene=r.scene,
        start="standing",
        person=None,
        policies=policies,
        skills=skills,
        vision=r.vision,
        pose=r.pose,
        at=at,
    )
    sim = Sim(opts)
    try:
        await sim.open()
        await sim.wait(4)
        until = sim.now() + r.minutes * 60
        if r.explorer == "walls":
            await _walls(sim, until)
        elif r.explorer == "grid":
            await _grid(sim, until)
        res = score(sim, fed=r.pose == "truth")
    finally:
        report = await sim.close()
    return {"run": r.name, "scene": r.scene, "seed": r.seed, **res, "report": str(report)}


def table(rows: list[dict[str, Any]]) -> str:
    head = (
        "| run | rooms | walked m | stuck s | falls | map off m | min |\n"
        "| --- | --- | --- | --- | --- | --- | --- |"
    )
    lines = [head]
    for x in rows:
        if "error" in x:
            lines.append(f"| {x['run']} | error: {x['error'][:60]} | | | | | |")
            continue
        lines.append(
            f"| {x['run']} | {x['rooms']}/{x['rooms_total']} | {x['walked_m']} | {x['stuck_s']}"
            f" | {x['falls']} | {x['pose_off_m']} | {round(x['secs'] / 60, 1)} |"
        )
    done = [x for x in rows if "error" not in x]
    every = sum(1 for x in done if x["rooms"] == x["rooms_total"])
    lines.append("")
    lines.append(
        f"Every room reached in {every} of {len(rows)} runs; "
        f"falls {sum(x['falls'] for x in done)}; "
        f"stuck {sum(x['stuck_s'] for x in done)} s in all."
    )
    return "\n".join(lines)


def _one(args: argparse.Namespace) -> int:
    """A child process's single run: its result as JSON on the last line."""
    from dave_sim.__main__ import DEFAULT_SKILLS, library

    policies, skills = library(json.loads(args.skills or json.dumps(DEFAULT_SKILLS)))
    r = Run(
        args.one, args.seed, args.minutes, args.pose, args.vision, args.explorer, Path(args.out)
    )
    try:
        res = asyncio.run(run_one(r, policies, skills))
    except Exception as e:  # one bad run must not cost the battery its table
        res = {"run": r.name, "scene": r.scene, "seed": r.seed, "error": repr(e)}
    print(json.dumps(res))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scenes", default=",".join(SCENES), help="comma list of " + ", ".join(SCENES))
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--minutes", type=float, default=25.0)
    ap.add_argument("--pose", choices=("reckoned", "truth"), default="truth")
    ap.add_argument("--vision", choices=("off", "truth", "noisy"), default="truth")
    ap.add_argument("--explorer", choices=EXPLORERS, default="walls")
    ap.add_argument(
        "--parallel", type=int, default=1, help="runs at once (each a browser and a sim)"
    )
    ap.add_argument("--skills", default="")
    ap.add_argument(
        "--out", default=str(Path(__file__).resolve().parents[1] / "sim-runs" / "battery")
    )
    ap.add_argument("--one", help=argparse.SUPPRESS)
    ap.add_argument("--seed", type=int, default=0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.explorer == "grid" and (args.pose != "truth" or args.vision == "off"):
        ap.error("--explorer grid needs --pose truth and --vision: its map fills only on both")
    if args.one:
        return _one(args)
    scenes = [s for s in args.scenes.split(",") if s]
    bad = [s for s in scenes if s not in SCENES]
    if bad:
        ap.error(f"unknown scene {bad}")
    out = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(s, k) for s in scenes for k in range(args.seeds)]
    common = [
        "--minutes",
        str(args.minutes),
        "--pose",
        args.pose,
        "--vision",
        args.vision,
        "--explorer",
        args.explorer,
        "--out",
        str(out),
    ] + (["--skills", args.skills] if args.skills else [])
    rows: list[dict[str, Any]] = []
    running: list[tuple[str, int, float, subprocess.Popen[str]]] = []
    while jobs or running:
        while jobs and len(running) < max(1, args.parallel):
            s, k = jobs.pop(0)
            cmd = [sys.executable, "-m", "dave_sim.battery", "--one", s, "--seed", str(k), *common]
            child = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, env=os.environ)
            running.append((s, k, time.monotonic(), child))
        for item in list(running):
            s, k, t0, p = item
            if p.poll() is None:
                # A hung browser must not hold up the whole battery's table.
                if time.monotonic() - t0 > args.minutes * 60 + CHILD_SLACK_S:
                    p.kill()
                    p.wait()
                    running.remove(item)
                    rows.append({"run": f"{s}-s{k}", "error": "timed out"})
                    print(f"timed out {s}-s{k}", flush=True)
                continue
            running.remove(item)
            lines = (p.stdout.read() if p.stdout else "").strip().splitlines()
            try:
                rows.append(json.loads(lines[-1]))
            except (IndexError, ValueError):
                rows.append({"run": f"{s}-s{k}", "error": f"exit {p.returncode}"})
            print(f"done {rows[-1]['run']}", flush=True)
        time.sleep(2)
    rows.sort(key=lambda x: (x.get("scene", ""), x.get("seed", 0), x["run"]))
    text = table(rows)
    (out / "battery.json").write_text(
        json.dumps(
            {
                "pose": args.pose,
                "vision": args.vision,
                "explorer": args.explorer,
                "minutes": args.minutes,
                "runs": rows,
            },
            indent=1,
        )
    )
    (out / "battery.md").write_text(text + "\n")
    print(text)
    print(f"\n{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
