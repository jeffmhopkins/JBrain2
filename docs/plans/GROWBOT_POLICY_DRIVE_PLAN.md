# GrowBot policy drive — trained skills on the real robot

> **Status:** In progress · **Last verified:** 2026-11-15 · **Waves:** P1🟡 P2✅ P3🟡 P4◻️ P5◻️

The owner's ask (2026-11-14/15): start wiring GrowBot_Training's locomotion policies into the
robot's software now, without waiting for training to finish. First, follow a face. Then answer the
follow-me voice commands. Later, walk forward for X seconds or X meters, estimated from the policy's
known speed while the camera watches the way. The training side is GrowBot_Training's
roadmap (its phase 5 is this work) and its follow-me plan, both in that repo's docs folder. The requirements were reviewed
with the owner in a Claude Doc on 2026-11-15, and this page carries them.

## Settled (owner, 2026-11-15)

- **The camera** is the phone's FRONT (screen-side) camera, pointing out the front of the body.
  Standing tall aims it at people. The simulator's lens is the same axis (GrowBot_Training #39).
- **One frame for everything:** the command's heading is the front camera's. Positive means the
  target is counter-clockwise seen from above, so turn left. The tall walk travels the way the
  camera looks. The nose walk travels opposite it: it backs away while the camera keeps facing the
  person, and it never turns round.
- **Sharp turns** are stop, spin with look, then walk. **Only the speed picks backing:** a negative
  speed is the nose walk whatever the heading; a target behind is a spin, never a reason to back.
- **At speed 0** it face-tracks: the look skill turns and tilts to keep a visible person centred.
- **Follow distance** is whatever distance the person is at when they say "follow me". "Get closer"
  and "back up" move it 0.25 m, kept between 0.4 m and 3 m.
- **Two people:** it keeps the one already tracked, and makes a best guess if they meet at the
  same distance. It stops rather than follow a stranger it can't match.
- **Emergency stop is "stop stop":** the word twice (in one clip, or two lone ones within
  2.5 s), so a passing or misheard "stop" doesn't trigger it. It is checked on the phone the moment the transcript lands, never through the model.
- **Position is never a policy input:** the phone can't measure it.
- **Walk limits:** up to 2 minutes or 3 m a walk, forward only in v1. Backing for X is left out
  because the camera faces away from where it's going.

## The interface

Everything above the policies speaks one command: `heading` (deg, front camera, left positive),
`speed` (-1..+1 of the skill's cap: ahead, 0 stand, back) and `pitch` (camera height, -1 low ..
+1 tall). These are the follow-me plan's `cmd_yaw`, `cmd_forward` and `cmd_pitch`.

`growbot/patches/growbot-drive.js` (copied into `site/` by `apply.py`, imported by
`growbot-box.js` as `growbot_runner.js` is) turns the command into each file's own inputs, read
from its `obs_spec`, never from code that knows the skill:

- **The heading** passes through unchanged for every file. A nose-first file's target is "away from
  the person", which is half a turn from the person. Measured from the nose (also half a turn
  round), that error is the same number. A turn is a turn, whichever end is called ahead.
- **The speed:** for a nose-first file, backing is its own +1. A camera-first file backs on its
  negatives, and one with no speed input is stop-or-go at its cap.
- **Ground speed** is the command times the file's `training.speed_cap_m_s`. It feeds the
  walk-for-X distance estimate, times a per-floor factor from a tape-measure calibration.

**The selector** picks the skill each tick. Only the speed's sign picks the walk (owner,
2026-11-15): a negative speed is the nose walk whatever the heading, and a target behind is never
a reason to back. A forward walk needs the target within 30° to start and stops past 45°. A
speed inside ±0.15 to start (±0.05 to stop) stands and looks. Stopping is immediate,
but a walk only starts once the current skill has held 1.5 s. A walk never turns straight into the
other walk; it stops first. When a different file takes over, the legs go neutral for 400 ms and
it starts from a fresh history, as it trained.

**A skill set** is a library policy for each of look, forward and back. The box reads which
skills a file serves from the file itself (`policies.py` `Contract.modes`). A top-level `modes`
list wins, which the master policy will carry. Otherwise a still file is a look, a nose-first
file backs, and a camera-first file walks forward (and also looks and backs when its speed command
spans -1..+1). The api refuses a file put in a slot it doesn't serve. The Body tab's Drive builds the set from the enabled library (active, else newest,
per skill) and sends it as a move's `skills`. When the master policy lands, all three skills point
at one file and nothing reloads (back keeps a nose-first file while one is enabled, by design).

## Waves

| Wave | What | State |
| --- | --- | --- |
| P1 | Current best checkpoints kept in GrowBot_Training `policies/` so the Body tab imports them | 🟡 look v6 (#39) and nose-walk-1 6e69e32c (#40) kept; lift_fall ca607df3 when it finishes. nose-walk-1 is slower than its cap (1.9 cm/s at a full command, 0.5 cm/s creep at 0), so walk for X (P5) uses measured speed, not the cap |
| P2 | Skill sets: `growbot-drive.js`, the page's skill switching, `skills` on the move, the Drive's skill set; the "stop stop" emergency stop | ✅ #1912 |
| P3 | Front-camera face tracker and the follower, with a legs-off dry run showing the commands | 🟡 this PR; the bench checks below are open |
| P4 | Voice commands (follow me, come, get closer, back up, stay) and the speaker lock | ◻️ |
| P5 | Walk for X seconds or meters: distance from speed, floor calibration, camera stall/looming checks, hold-while-speaking | ◻️ |

The real-robot trials after P5 are GrowBot_Training's phase 6.

## Follower, voice and walk-for-X (P3–P5)

- **Tracker (built, P3):** `growbot/patches/growbot-follow.js`. It takes the face boxes the
  creature already detects (`growbot-eyes.js`: upstream's BlazeFace, in the leg tracker or in
  its own worker, whichever is running), so there is no second detector on the phone. Each box
  gives a bearing (from the frame's 60° assumed width of view), a vertical offset and a distance
  (from the face's width, 15 cm, corrected once by **Measure 1 m**). The frames aren't mirrored,
  so a face right of centre is on the robot's left; **Flip frames** reverses that per phone. The
  person is kept in the walk's own frame (the gyro's heading at the reading plus the bearing), so
  the robot turning doesn't look like the person moving. Each frame takes the face nearest where
  the person should be, within a gate that widens with time since the last sighting; a face
  outside it is never taken, so a stranger crossing doesn't steal the lock.
- **Follower (built, P3):** heading = the bearing. Pitch creeps toward the face once per
  reading, but only while standing within 20° of the person (back within 15°), since only look takes a camera
  height and a walk's stance pitches the lens; a wider turn or a search is sent with pitch no
  lower than 0, because look barely turns with the camera low (GrowBot_Training's follow-me
  test: 11–45° of a 90° turn in 10 s at −1, 80–88° at 0). The distance it acts on is the readings' average over about 1.5 s (a plain mean until
  then), because the face's distance swings about 0.3 m with each waddle. Too far is a step
  forward, the one the master's own follower trained with (GrowBot_Training `follow.py`): once
  the average holds a second of readings, more than 0.10 m too far with the person within 30° of
  the camera starts a walk at full speed (drive walks only above `SPEED_ON`), and it keeps
  walking until within 0.05 m (or 40° off), so a turn's drift is walked back rather than left in
  the band. Too close backs away at 1.5 × the error beyond 0.15 m. Half a second without a
  reading forgets the average, so the second-long wait starts over after any gap. Too close with the person more than 60° off, it turns to face them
  before it backs, since backing travels opposite the camera and would close on them. No reading for 0.5 s drops
  speed to 0. After 3 s it stands and turns toward where the person was last seen. A walk that
  never locks within 6 s, or loses the person for 8 s, ends with the legs released and says why.
  A follow runs up to 2 minutes and spends the legs' duty budget as any walk does. "stop stop",
  the Body tab's Stop and any other move end it.
- **Dry run (built, P3):** the same tracker and follower with the legs left alone. The
  creature's screen (which faces the person) shows an arrow the way it would turn, the skill the
  selector would pick, the speed, and the distance against the one it keeps.
- **From the Body tab (built, P3):** Follow me, Dry run, Measure 1 m and Flip frames, sent as a
  skill set's `look` with `follow` set (`walk`, `dry`, `calibrate`, `flip`). The camera settings
  live on the creature's phone.
- **Voice:** parsed on the phone with no model call, extending `_ftWords`. The commands change only
  the follower's settings.
- **Walk for X:** holds the starting heading. Distance is summed from the speed estimate, and the
  settle and spins don't count. The camera checks for stall, looming and a covered lens, and stops
  early; it is not a ruler. Speech starting mid-walk holds it in place until the transcript lands.
  A few frames go to the run log. It ends with one line, e.g. "Walked about 0.9 m in 19 s".

## Body test (built, owner 2026-11-15)

The Body tab's **Body test…** (`frontend/src/components/GrowbotBodyTest.tsx`, mock
`docs/mocks/growbot-body-test/screens.html`) sends a skill set's look with `follow: "test"`. The
creature's phone (`growbot/patches/growbot-bodytest.js`, page glue `gbBodyTest` in
`growbot-box.js`) then talks the person through setup on its own screen: place the phone on the
body (tap or "ready"), power on (moves on when the legs answer), "stand up" (get-up if lying,
else the tall stance, until upright 1 s), stand 1 m away and say "1 meter" (the follower's
calibration). Then, each check on a Drive-steered walk of its own: the left-right sign (legs still;
reversed frames are flipped), the camera tilt (pitch -1 then +1 with the face above centre), look
and hold 10 s, turns to +90 and -90 and back, creep over 15 s of hold after them, 4 s forward and
4 s back by face distance, and "stop stop" while backing. Falls are counted throughout; a fall
fails its check and the test carries on once the robot is upright. While it runs, every heard
line goes to the test, never a model turn ("stop stop" is still checked first and ends it).

The phone shows the camera with its face box, live numbers and the checklist, then a verdict per
check. It posts its log every 2 s (`/api/box/bodytest` → `/internal/growbot/bodytests`, migration
0274, each robot's newest 50 runs kept, a run capped at 60,000 rows): ~20 samples a second, its events, the page's diary rows during it, and
a camera still per step. The Body tab mirrors a running test (step, clock, skill, heading, face,
falls) with Stop, and lists past runs with their JSON log; `GET /api/debug/growbot/bodytests[/{n}]`
reads the same for review.

## Known gaps

- **Bench checks for P3** (the Body test runs each of these and logs it): the left-right sign (a dry run's arrow should point at the person;
  else Flip frames), the 60° width of view, whether a positive pitch raises the camera toward a
  face above centre, and the box's latency (a reading is placed with the heading when it lands,
  not when the frame was taken).
- **The Body tab doesn't hear a follow end on the creature** (lost, nobody seen, 2 minutes):
  its button stays on Stop following until tapped. The creature's own screen and caption say it.
- **Pose as the fallback** (a person facing away has no face box) isn't built; a face is needed.

- **A shout may never be transcribed while it walks.** Upstream drops an unpitched clip heard
  during the robot's own motion (`self_drop`, next.html) before speech-to-text, so "stop stop"
  over servo noise may never reach `gbEStop`. Check it on the bench; if it bites, send those
  clips to speech-to-text for the stop check alone, never to a model turn (P4).
- **A master policy's mode inputs.** A file with `cmd_mode_*` channels gets slot 0 from the skill
  set today; the master's export will say how modes map to slots, and the page feeds it then.
