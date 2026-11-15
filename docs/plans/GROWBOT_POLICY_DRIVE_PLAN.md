# GrowBot policy drive — trained skills on the real robot

> **Status:** In progress · **Last verified:** 2026-11-15 · **Waves:** P1🟡 P2🟡 P3◻️ P4◻️ P5◻️

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
- **Sharp turns** are stop, spin with look, then walk.
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

**The selector** picks the skill each tick. A walk needs the target within 30° to start and stops
past 45°. A speed inside ±0.15 to start (±0.05 to stop) stands and looks. Stopping is immediate,
but a walk only starts once the current skill has held 1.5 s. A walk never turns straight into the
other walk; it stops first. When a different file takes over, the legs go neutral for 400 ms and
it starts from a fresh history, as it trained.

**A skill set** is a library policy for each of look, forward and back. The box reads which
skills a file serves from the file itself (`policies.py` `Contract.modes`). A top-level `modes`
list wins, which the master policy will carry. Otherwise a still file is a look, a nose-first
file backs, and a camera-first file walks forward (and also looks and backs when its speed command
spans -1..+1). The api refuses a file put in a slot it doesn't serve. The Body tab's Drive builds the set from the enabled library (active, else newest,
per skill) and sends it as a move's `skills`. When the master policy lands, all three skills point
at one file and nothing reloads.

## Waves

| Wave | What | State |
| --- | --- | --- |
| P1 | Current best checkpoints kept in GrowBot_Training `policies/` so the Body tab imports them | 🟡 look v6 kept (#39); lift_fall ca607df3 and nose_walk 6e69e32c when they finish |
| P2 | Skill sets: `growbot-drive.js`, the page's skill switching, `skills` on the move, the Drive's skill set; the "stop stop" emergency stop | 🟡 this PR |
| P3 | Front-camera face/pose tracker and the follower, with a legs-off dry run showing the commands | ◻️ |
| P4 | Voice commands (follow me, come, get closer, back up, stay) and the speaker lock | ◻️ |
| P5 | Walk for X seconds or meters: distance from speed, floor calibration, camera stall/looming checks, hold-while-speaking | ◻️ |

The real-robot trials after P5 are GrowBot_Training's phase 6.

## Follower, voice and walk-for-X (P3–P5)

- **Tracker:** MediaPipe face first, pose as the fallback, on the front camera at 8 or more
  readings a second. Each reading gives a bearing, a vertical offset, a distance (from eye spacing
  or shoulder width, calibrated once at 1 m), a confidence and its age. The gyro carries the
  heading between frames. Check the image's left-right sign on the bench, since browsers mirror the
  selfie preview.
- **Follower:** heading = the bearing; speed from the distance error with a ±0.15 m dead band;
  pitch = the vertical offset, integrated. No reading for 0.5 s drops speed to 0. Lost for 3 s, it
  turns toward the last bearing and re-locks only on a strong match.
- **Voice:** parsed on the phone with no model call, extending `_ftWords`. The commands change only
  the follower's settings.
- **Walk for X:** holds the starting heading. Distance is summed from the speed estimate, and the
  settle and spins don't count. The camera checks for stall, looming and a covered lens, and stops
  early; it is not a ruler. Speech starting mid-walk holds it in place until the transcript lands.
  A few frames go to the run log. It ends with one line, e.g. "Walked about 0.9 m in 19 s".

## Known gaps (from the P2 review)

- **A shout may never be transcribed while it walks.** Upstream drops an unpitched clip heard
  during the robot's own motion (`self_drop`, next.html) before speech-to-text, so "stop stop"
  over servo noise may never reach `gbEStop`. Check it on the bench; if it bites, send those
  clips to speech-to-text for the stop check alone, never to a model turn (P4).
- **A master policy's mode inputs.** A file with `cmd_mode_*` channels gets slot 0 from the skill
  set today; the master's export will say how modes map to slots, and the page feeds it then.
