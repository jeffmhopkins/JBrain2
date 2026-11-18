"""Run Dave simulator scenarios.

    python -m dave_sim come_here --live --persona Dave
    python -m dave_sim all --scripted

`--live` asks the box's creature lane for every reply and starts from the named persona's
soul (read once, never written back); it needs an owner debug token. `--live --fresh` wipes
that soul's memories first (`forget`): the same creature, with no history. Without it a run uses a
fresh creature and a scripted model, which only tests the plumbing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from dave_sim.box import forget, policy_id
from dave_sim.llm import BoxApi, debug_token, live_model, scripted_model
from dave_sim.pilot import Pilot
from dave_sim.run import Options, Sim, _growbot_train
from dave_sim.scenarios import SCENARIOS, SETUP

# The skill set the box picks today (backend growbot/policies.py skill_set): the newest
# policy serving each skill, a nose-first one first for back.
DEFAULT_SKILLS = {"look": "master-v3", "forward": "master-v3", "back": "nose-walk-2"}


def library(skills: dict[str, str]) -> tuple[dict[str, Path], dict[str, str]]:
    """The kept walks in GrowBot_Training's policies/index.toml, as the box's library."""
    _growbot_train()
    root = Path(
        os.environ.get("GROWBOT_TRAINING", Path(__file__).resolve().parents[4] / "GrowBot_Training")
    )
    index = tomllib.loads((root / "policies" / "index.toml").read_text())
    files = {p["slug"]: root / p["file"] for p in index["policy"]}
    missing = [s for s in skills.values() if s not in files]
    if missing:
        raise SystemExit(f"no kept policy {missing}; known: {', '.join(sorted(files))}")
    return {policy_id(s): f for s, f in files.items()}, {k: policy_id(v) for k, v in skills.items()}


async def run_one(name: str, args: argparse.Namespace, base: dict[str, Any]) -> dict[str, Any]:
    out = Path(args.out) / time.strftime("%Y%m%d-%H%M%S") / name
    setup = dict(SETUP[name])
    # A scenario that runs long (left alone for ten minutes) says how long it may take.
    limit = max(args.timeout, float(setup.pop("timeout", 0)))
    opts = Options(out=out, **{**base, **setup})
    sim = Sim(opts)
    if isinstance(opts.model, Pilot):
        opts.model.bind(sim)
    try:
        await sim.open()
        await asyncio.wait_for(SCENARIOS[name](sim), limit)
    except TimeoutError:
        sim.check(False, f"scenario finished within {limit:.0f} s")
    finally:
        report = await sim.close()
    ok = all(c["ok"] for c in sim.checks)
    return {"scenario": name, "ok": ok, "checks": sim.checks, "report": str(report)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenarios", nargs="+", help=f"'all' or any of: {', '.join(SCENARIOS)}")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="the box's model and persona")
    mode.add_argument("--scripted", action="store_true", help="a fresh creature, canned replies")
    mode.add_argument(
        "--pilot", action="store_true", help="a fresh creature whose stand-in brain reads the "
        "sim's ground truth (pilot.py): the page's plumbing, with no box"
    )
    ap.add_argument("--persona", default="Dave")
    ap.add_argument(
        "--fresh", action="store_true", help="with --live: the persona with its memories wiped "
        "(name, owner, body and core kept), to tell the prompts' faults from its history"
    )
    ap.add_argument("--token-file", default=os.environ.get("JBRAIN_DEBUG_TOKEN_FILE", ""))
    ap.add_argument("--skills", default=json.dumps(DEFAULT_SKILLS), help="JSON skill -> slug")
    # Beside the package, where .gitignore covers it, wherever the run starts from: run
    # folders hold the persona's soul.
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "sim-runs"))
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument(
        "--vision", choices=("off", "truth", "noisy"), default="off", help="his depth sense "
        "(growbot-vision.js) fed from the sim's true depth and labels, as rendered or with a "
        "phone model's noise; off as on today's phone"
    )
    ap.add_argument(
        "--pose", choices=("reckoned", "truth"), default="reckoned", help="where his map says "
        "he is: reckoned by the page's gyro and walking pace, as on today's phone, or fed the "
        "sim's true pose (growbot-map.js gbPose)"
    )
    args = ap.parse_args(argv)
    names = list(SCENARIOS) if args.scenarios == ["all"] else args.scenarios
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        ap.error(f"unknown scenario {unknown}")
    policies, skills = library(json.loads(args.skills))
    base: dict[str, Any] = {
        "policies": policies, "skills": skills, "vision": args.vision, "pose": args.pose
    }
    if args.live:
        api = BoxApi(*debug_token(args.token_file or None))
        base["model"] = live_model(api)
        persona = api.persona(args.persona)
        soul = forget(persona["soul"]) if args.fresh else persona["soul"]
        base.update(soul=soul, prefs={} if args.fresh else persona.get("prefs") or {})
    elif not args.pilot:
        base["model"] = scripted_model(lambda req: {"say": "okay"})
    results = []
    for name in names:
        if args.pilot:
            base["model"] = Pilot()  # a fresh one per scenario: it remembers what it found
        res = asyncio.run(run_one(name, args, base))
        results.append(res)
        mark = "PASS" if res["ok"] else "FAIL"
        print(f"{mark} {name}: {res['report']}")
        for c in res["checks"]:
            print(f"    {'ok ' if c['ok'] else 'NO '} {c['what']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
