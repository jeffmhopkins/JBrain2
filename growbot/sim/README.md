# The Dave simulator

> **Status:** Living · **Last verified:** 2026-11-18

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
second after. `turns_by_degrees` turns him 90, -45, 180, -120 and 30 degrees the way he and the explore
trip ask (`gbMap.turn`), on the box skill set's look skill, and checks each ends within 10
degrees of its angle by the sim's own yaw. `follow_walk` circles a standing person round him; with a seated face at 1 m the camera
tilts down, where the trained look barely turns, and he kept up only 2 runs in 5.

## His depth sense

`--vision truth` or `--vision noisy` (`Options.vision`, `dave_sim/vision.py`) feeds the page's
depth sense (`growbot-vision.js`, `docs/plans/GROWBOT_VISION_PLAN.md`) about three times a second
from the sim itself. MuJoCo renders the front camera's true depth and which geom each pixel
shows, at 48x64, and the geom's name gives its label in the page's vocabulary. `noisy` adds a
phone model's errors: a whole-frame scale that wanders by about 8%, 3% jitter per pixel, 2%
holes and 5% of labels lost. The page reduces the maps with the same code a phone source would
use, so a run tests that code and what Dave does with it, and the sim's blank walls stop
mattering. Off by default, as on today's phone. Against rays cast in the house, its distances
per direction came within a few centimetres of the truth.

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
It shows what the prompts alone make of a turn, apart from what his history has taught him. The PWA's
*Forget history* on a persona does the same wipe on the box (reflexes kept in both).

Each scenario writes a folder under `sim-runs/` (ignored by git): `report.html` reads top
to bottom (the checks, then what was said, what the legs got, and where the robot
actually was), `run.mp4` shows an onlooker's view beside the camera's, and
`model_calls.json` / `requests.json` hold every model exchange. The requests carry Dave's
soul, so a run folder stays on the machine that made it.

## The exploring battery

`python -m dave_sim.battery` (wave E0 of `docs/plans/GROWBOT_EXPLORE_PLAN.md`) runs exploring
with no model over the flat and the three maze houses, `--seeds` starts each (the scene's start
turned a third of the way round per seed), `--parallel` at once, for `--minutes` each, and
writes `battery.md` and `battery.json` under `sim-runs/battery/`: rooms reached of all, metres
walked, seconds stuck (under 5 cm in 30 s), falls (down 3 s or more), and how far his map's
idea of where he is ends from the truth. `--pose truth` feeds the page the sim's own pose a few
times a second (`growbot-map.js` `gbPose`), so a planner is judged on a perfect pose first;
`--pose reckoned` leaves it to the gyro and walking pace, as on today's phone. `--explorer
walls` is today's round of the walls. A 2-minute run in maze 2 ended with his reckoned map
2.0 m from the truth, and 0.0 m with `--pose truth`.

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

## The house

`Options(scene="house")` sets him down in GrowBot_Training's furnished house
(`growbot_train/house.py`) instead of the bare room: a living room, where he starts at the
origin facing its far wall, and a kitchen behind him through a doorway. The living room has a
piano in its far corner, out of his first view, a couch, a bookshelf, a plant and a lamp; the
kitchen a fridge, a counter and a table. Its walls and furniture stop him, where the bare
room's walls let him walk through. `Options.at` sets where he starts and which way he faces. `Options.up_vec` (on by default) seeds the page's learned "up" from his standing reading, as an owner's phone has long since learned it; a fresh browser's fallback reads his tall stance as lying on his back.

`Options(scene="flat")` is the bigger house (`house.FLAT`): a hallway across the middle with
a living room and kitchen south of it and a bedroom and study north, each through its own
doorway and none of them in line, with the couch between his start and the living room's
door. The scenarios read the house from `SimBody.layout` (rooms, walls, doors, furniture and
`route` between rooms), so the floor plan, the camera projection and the pilot work in either.

`find_piano` asks him to find the piano and checks he says so while it is really in his
view; `walk_to_piano` asks him to walk over to it and checks he gets within 60 cm and says he
is there only once he is; `piano_from_kitchen` asks the same from the kitchen, through the
doorway; `cat_errand` sends him from beside his person in the living room to find the cat in
the kitchen and come back to say where it is, and checks he saw it in that room, didn't claim
it sooner, came back within 1.5 m of them and named the cat and the room; `flat_piano` and
`flat_cat` are the same two asks across the flat. `explore_alone` leaves him alone for ten minutes and scores how far he went. `explore_flat` leaves him alone for twenty-five minutes in the flat's living-room corner, where the rest of the flat is out of sight, and checks he walks out into the hallway and on through another doorway. `explore_maze1`..`explore_maze3` do the same in three of the training repo's seeded maze houses (six 2.6 m rooms in a three by two grid, the doorways a random tree plus one loop), so a fix tuned to the flat's one layout has to carry over to rooms he has never seen. They run for
minutes, so their reports skip the onlooker video, which re-renders every pose. Every run
instead draws `plan.svg` (`dave_sim/plan.py`): his path from above, coloured by speed, with
arrows for where his camera faced and a numbered dot wherever he said something, and the
report lists how far he walked, how long he spent in each room and how close he came to each
piece of furniture.

`--pilot` swaps the box for a stand-in brain (`dave_sim/pilot.py`) that answers from the
sim's ground truth: told to find something it turns toward it, sends long forward walks and
says it found it once there (through the doorways `route` gives, and back to where it was
asked when the ask says to come back and tell); left alone it heads for the nearest furniture it hasn't been
near. It answers 4 s after each ask, near the box's pace. That tests the page's plumbing
(goals that stick, walks that carry on, looks while walking, the no-look stop) with no box,
so a live run only has to prove the thinking.

### His map

`map_house` asks him to explore the house and put what he finds on his map
(`growbot-map.js`), leaves him alone for eight minutes, asks where the piano is, then asks him
to use his map to get to the fridge in the other room. The run polls the page's map each second
(`Sim.dave_map`, `Sim.map_track`) and `plan.map_score` scores it against the true house: his
frame laid on the house's by where he started, which rooms he drew, whether each thing he put
down is in the room it is really in and how far off it lies, whether a doorway joins the two
rooms, and how far his idea of where he is drifted. `plan.svg` draws his map over the true plan
in purple: his rooms dashed, doorways as dots, things as crosses, and a dotted line for where he
thought he was.

`map_flat` is the same ask in the flat, left twenty minutes: the living room, the hallway and
another room on his map, a doorway that joins the rooms it really does, things in the right
rooms, and where he thinks he is.

Under `--pilot`, the stand-in brain keeps the map the way a careful Dave would: it names each
room it walks into, puts what is in view on the map (`view.py` projects the furniture and the
doorway into the camera's picture from the sim's ground truth), says `at` on arriving, and asked
to "use your map" it steers by `where` answers alone. So a pilot run tests the map's own
geometry (bearings, heading, dead reckoning, routes), and a live run only has to show that the
model uses it.

## Tests

The pure halves (the fake box, the chip model, the model plumbing) are tested by the
backend suite, which loads them by path: `backend/tests/unit/test_growbot_sim.py`. The sim
body has its own tests in GrowBot_Training. Nothing in CI runs the browser.
