# Box calibration track

The owner-run half of the calibration loop (`docs/archive/CALIBRATION_LOOP.md`): drive the
committed eval corpora through the **local model on the box** and score them with
the **same scorers CI uses** (`jbrain.evals.{runner,integrate_runner,disambiguate_runner}`).

This is the ONLY eval path that calls the box. It is never wired into CI, and it
refuses to run without the minted capability token in the environment.

## Run

```bash
cd backend
JBRAIN_DEBUG_TOKEN=<minted-payload> uv run python -m evals.box.run_layer <layer> [--samples N] [--limit N]
```

`<layer>` ∈ `extract` | `integrate` | `disambiguate`. `--samples` repeats the
corpus (the model is non-deterministic — the signal is a per-case pass *rate*).

## Rules (non-negotiable)

1. **Never call the box without explicit owner permission.** The token gates it;
   it lives only in the environment, never in the repo.
2. **One job at a time.** The box is a single GPU; concurrent jobs contend and
   stall (a stall once produced false nulls). `DebugRouter` is strictly serial.
3. **Async jobs.** A long extraction would exceed the Cloudflare tunnel's ~100s
   edge timeout, so every call is submit + poll.
4. **The local prompt is what runs.** The scorers send the prompt/registry from
   the working tree, so an UNCOMMITTED edit is validated before it ships (how
   `note-extract-v22` was validated without deploying).

## The loop

1. Run a layer; read the per-case pass rates + the `{task, safety}` split.
2. Cluster the failures into a named failure mode.
3. Fix: a prompt nudge (version + digest bump — `note.extract` is
   `SELF_EDIT_LOCKED`, so a human PR) or a registry addition.
4. Re-run; confirm the rate moved and nothing regressed.
5. Add ~10 cases around the failure mode so it can't silently return.
6. Record the box output as a CI golden transcript (planned, phase E) so CI
   replays the same cases deterministically through the fake LLM.
