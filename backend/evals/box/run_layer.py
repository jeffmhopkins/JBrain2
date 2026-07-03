"""Run a committed eval corpus against the OWNER'S BOX (the local model) and score
it with the same scorers CI uses — the calibration track of docs/archive/CALIBRATION_LOOP.md.

  cd backend && JBRAIN_DEBUG_TOKEN=<payload> uv run python -m evals.box.run_layer <layer>

<layer> ∈ {extract, integrate, disambiguate}. `--samples N` repeats each case N
times (the model is non-deterministic; reports a per-case pass RATE). Owner-run
only — this is the ONLY eval path that calls the box, and it requires the minted
token in the environment. Never wired into CI.
"""

from __future__ import annotations

import argparse
import asyncio

from evals.box.client import DebugRouter
from jbrain.evals.disambiguate_runner import (
    eval_run_from_disambiguate,
    load_disambiguate_cases,
    score_disambiguate_cases,
)
from jbrain.evals.integrate_runner import (
    eval_run_from_integrate,
    load_integrate_cases,
    score_integrate_cases,
)
from jbrain.evals.runner import eval_run_from_cases, load_cases, score_cases

_LAYERS = {
    "extract": (load_cases, score_cases, eval_run_from_cases, "note-extract"),
    "integrate": (
        load_integrate_cases,
        score_integrate_cases,
        eval_run_from_integrate,
        "integrate",
    ),
    "disambiguate": (
        load_disambiguate_cases,
        score_disambiguate_cases,
        eval_run_from_disambiguate,
        "entity-disambiguate",
    ),
}


async def _run(layer: str, samples: int, limit: int | None) -> None:
    load, score, to_run, label = _LAYERS[layer]
    cases = load()
    if limit:
        cases = cases[:limit]
    router = DebugRouter()
    try:
        # Repeat the corpus `samples` times; the model is non-deterministic, so the
        # signal is a RATE, not a single binary run.
        passes: dict[str, int] = {c["name"]: 0 for c in cases}
        total_tokens = 0
        for s in range(samples):
            results, tokens = await score(router, cases, echo=True)
            total_tokens += tokens
            for r in results:
                if r.passed:
                    passes[r.name] += 1
            run = to_run(results, f"{label}-box")
            task = sum(x.task for x in run.scores) / len(run.scores)
            safety = sum(x.safety for x in run.scores) / len(run.scores)
            print(f"\n[sample {s + 1}/{samples}] {layer}: task={task:.3f} safety={safety:.3f}")
    finally:
        await router.aclose()
    print(f"\n=== {layer} over {samples} sample(s), {len(cases)} cases | tokens={total_tokens} ===")
    for name, n in sorted(passes.items(), key=lambda kv: kv[1]):
        print(f"  {n}/{samples}  {name}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("layer", choices=sorted(_LAYERS))
    ap.add_argument("--samples", type=int, default=1)
    ap.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    a = ap.parse_args()
    asyncio.run(_run(a.layer, a.samples, a.limit))


if __name__ == "__main__":
    main()
