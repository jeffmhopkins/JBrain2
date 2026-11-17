# The Dave simulator

> **Status:** Living · **Last verified:** 2026-11-17

Runs Dave's whole loop with no robot and no phone: the real creature page
(`growbot/site/v3/next.html`) in headless Chromium, its legs and phone sensors played by
GrowBot_Training's MuJoCo sim body (`growbot_train/simbody.py`), and its model either the
box's real creature lane or a script. A scenario says something to Dave, shoves him, or
moves a person around the room, then checks what actually happened to the body.

It exists to catch, before Jeff does, the faults that only show on the whole creature:
Dave saying he's walking while the legs don't carry him, a walk on crossed leg pins, face
tracking lost once he moves, a fall he doesn't notice.

## What is real and what is played

| Part | In a run |
| --- | --- |
| The page, its prompts, face tracker, walk runner, reflexes | Real, served from this checkout |
| Dave's soul and prefs | Read once from the box (live), never written back |
| The model | The box's creature lane via the owner debug API (live), or a script |
| The legs | `growbot/firmware/act_engine.py` behind a chip model (`dave_sim/chip.py`) driving the sim body's servos through its wiring |
| Camera | The sim body's front camera (360x480, 75° tall), with a face on a person who can move |
| Motion sensors | The sim body's IMU, in `devicemotion` / `deviceorientation` form |
| Everything else on the box | `dave_sim/box.py`, in memory |

The page is served as written except for one patch: the face detector runs on MediaPipe's
CPU path, because headless Chromium's software GL takes ~250 ms a frame and the tracker
drops frames that old. `box.SIM_PATCHES` fails the run if the patch stops matching.

Live runs share the creature lane with the real robot: the box answers 409 while the real
Dave has spoken in the last two minutes, and a run waits briefly for quiet rather than
taking his slot, then lets that turn fail. A run is only meaningful while the robot is
quiet. Each replayed turn is logged on the box as a `replay` call row.

A scripted run answers every turn "okay", so it exercises the plumbing, not Dave: most
scenarios fail under it by design. `stock_wired` passes when the simulator shows the
wrong-wiring fault (a trained spin left turning clockwise).

The body is wired as the owner's robot (`run.ROBOT_WIRING`), not as the sim's stock build:
the robot's trained walks only turn the right way with the page's leg swap, so the stock
build under that swap turned every trained turn the wrong way. `trained_turns` holds that.

## Running it

From `growbot/sim/`, with GrowBot_Training checked out beside JBrain2 (or
`$GROWBOT_TRAINING` pointing at it):

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt \
  && .venv/bin/pip install -e ../../../GrowBot_Training \
  && .venv/bin/playwright install chromium
sudo apt-get install -y libosmesa6        # headless MuJoCo rendering
export MUJOCO_GL=osmesa PYOPENGL_PLATFORM=osmesa
.venv/bin/python -m dave_sim bump --scripted              # plumbing only
JBRAIN_DEBUG_TOKEN_FILE=path/to/token .venv/bin/python -m dave_sim come_here --live
```

`--fresh` runs a Dave with no past: the box soul's log, facts, scratch and learned identity
are dropped before the page loads (`box.forget`), keeping his name, his person and his body.
It shows what the prompts alone make of a turn, apart from what his history has taught him.

Each scenario writes a folder under `sim-runs/` (ignored by git): `report.html` reads top
to bottom (the checks, then what was said, what the legs got, and where the robot
actually was), `run.mp4` shows an onlooker's view beside the camera's, and
`model_calls.json` / `requests.json` hold every model exchange. The requests carry Dave's
soul, so a run folder stays on the machine that made it.

## Scenarios

`python -m dave_sim --help` lists them; each one's docstring in `dave_sim/scenarios.py`
names the fault it is there to catch. A new scenario is an `async def name(sim)` under
`@scenario(start=..., person=..., wiring=..., query=...)` using `sim.say`, `sim.push`,
`sim.person`, `sim.wait`, `sim.until` and `sim.check`.

`walk_compare` needs no model: it walks the creature's stock forward and then the box skill
set's forward, 8 s each, and reports how much closer each carried him. On the robot's
wiring the stock walk crawls (about 1.4 cm in 8 s) and the trained one walks straight at
about 1.9 cm/s (2026-11-17), a third of its trainer speed: the page's walk loop ticks at
18 to 20 Hz in the sim, not the 25 Hz the policy was trained at.

## Tests

The pure halves (the fake box, the chip model, the model plumbing) are tested by the
backend suite, which loads them by path: `backend/tests/unit/test_growbot_sim.py`. The sim
body has its own tests in GrowBot_Training. Nothing in CI runs the browser.
