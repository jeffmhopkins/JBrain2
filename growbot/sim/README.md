# The Dave simulator

> **Status:** Living · **Last verified:** 2026-11-17

Runs Dave's whole loop with no robot and no phone: the real creature page
(`growbot/site/v3/next.html`) in headless Chromium, its legs and phone sensors played by
GrowBot_Training's MuJoCo sim body (`growbot_train/simbody.py`), and its model either the
box's real creature lane or a script. A scenario says something to Dave, shoves him, or
moves a person around the room, then checks what actually happened to the body.

It exists to catch, before Jeff does, the faults that only show on the whole creature:
Dave saying he's walking while the legs don't carry him, legs wired against the page's swap, face
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
build under that swap turned every trained turn the wrong way. The robot's legs are
mirrored (pins crossed, signs flipped): crossed pins alone also negate both swings and send
the trained forward away from the person. `trained_turns` holds the turns.

The follower's camera flip (`run.Options.cam_flip`) is on, as a phone whose owner set it
from the PWA's Body tab (Follow dry run, then Flip if the arrow points away). In the sim,
on the robot's wiring as modelled, every unflipped follow turned away from the face; that
is a sim finding for a body test to confirm, and `follow_unflipped` pins it. `follow_walks_after`
walks a standing person (face at 1.55 m; a 0.6 m face drops out of the frame's bottom as
they back off) away and sideways for a minute: he walked 0.85 m after them and held
1.3 to 1.7 m (2026-11-17). `follow_me_away` asks the same by voice, through the model, and
`stop_stop` says "stop stop" in the middle of a 15 s trained walk: he is still from half a
second after. `follow_walk` circles a standing person round him; with a seated face at 1 m the camera
tilts down, where the trained look barely turns, and he kept up only 2 runs in 5.

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

`walk_compare` needs no model: it walks the pad's built-in (stock) forward and then the box
skill set's forward, 8 s each, and fails unless the trained one carries him 10 cm closer.
On the robot's wiring the trained walk goes straight at him, about 20 to 39 cm in 8 s
(2026-11-17), up to nearly the trainer's 45 cm, where the stock forward manages about 9.
Dave's own reply walks take the trained one (`?ownwalk=0` turns that off in a tab), which
`come_here` holds; the pad's Built-in walk stays stock, for the A/B. The owner's Dave, whose
log remembers his stock walk slipping, sometimes says he is walking with no walk; the page
then asks him again at once (`_gbClaimFix`), though he often answers with why his feet slip;
he walked in 10 of 16 runs (2026-11-17), where a fresh Dave walks every time. In
2 of about 36 trained forward walks across all runs he tipped onto his back 2 s in. Part of the shortfall
is the page's walk loop ticking at 18 to 20 Hz in the sim, late and uneven, not the steady
25 Hz the policy was trained at: replaying those late ticks
in the trainer halves its distance.

## Tests

The pure halves (the fake box, the chip model, the model plumbing) are tested by the
backend suite, which loads them by path: `backend/tests/unit/test_growbot_sim.py`. The sim
body has its own tests in GrowBot_Training. Nothing in CI runs the browser.
