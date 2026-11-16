"""Run Dave simulator scenarios.

    python -m dave_sim come_here --live --persona Dave
    python -m dave_sim all --scripted

`--live` asks the box's creature lane for every reply and starts from the named persona's
soul (read once, never written back); it needs an owner debug token. Without it a run uses a
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

from dave_sim.box import policy_id
from dave_sim.llm import BoxApi, debug_token, live_model, scripted_model
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
    opts = Options(out=out, **{**base, **SETUP[name]})
    sim = Sim(opts)
    try:
        await sim.open()
        await asyncio.wait_for(SCENARIOS[name](sim), args.timeout)
    except TimeoutError:
        sim.check(False, f"scenario finished within {args.timeout} s")
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
    ap.add_argument("--persona", default="Dave")
    ap.add_argument("--token-file", default=os.environ.get("JBRAIN_DEBUG_TOKEN_FILE", ""))
    ap.add_argument("--skills", default=json.dumps(DEFAULT_SKILLS), help="JSON skill -> slug")
    # Beside the package, where .gitignore covers it, wherever the run starts from: run
    # folders hold the persona's soul.
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "sim-runs"))
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args(argv)
    names = list(SCENARIOS) if args.scenarios == ["all"] else args.scenarios
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        ap.error(f"unknown scenario {unknown}")
    policies, skills = library(json.loads(args.skills))
    base: dict[str, Any] = {"policies": policies, "skills": skills}
    if args.live:
        api = BoxApi(*debug_token(args.token_file or None))
        persona = api.persona(args.persona)
        base.update(model=live_model(api), soul=persona["soul"], prefs=persona.get("prefs") or {})
    else:
        base["model"] = scripted_model(lambda req: {"say": "okay"})
    results = []
    for name in names:
        res = asyncio.run(run_one(name, args, base))
        results.append(res)
        mark = "PASS" if res["ok"] else "FAIL"
        print(f"{mark} {name}: {res['report']}")
        for c in res["checks"]:
            print(f"    {'ok ' if c['ok'] else 'NO '} {c['what']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
