# GrowBot exploring — a map and a planner under Dave's thinking

> **Status:** In progress · **Last verified:** 2026-11-19 · **Waves:** E0✅ E1✅ E2🟡 E3◻️ E4◻️ E5◻️ E6◻️

The owner's ask (2026-11-18): "I feel like maybe we have built a wrong paradigm." Dave went 0
for 10 in the flat's explore test, and each fix since #1940 (wall-following, the doorway pull,
the swing rule, the pocket escape) met a new failure in the next house. This plan replaces how
he explores. It does not replace his thinking: the model still understands the ask, chooses
where is promising, names rooms and things, answers, and talks. It stops steering each step.

## Why the old way can't converge

- **No position.** His place on his map is 0.04 m/s times the seconds a forward walk ran
  (`growbot-map.js` `PACE`), clamped inside room rectangles the model declared. The trained
  walk's real pace varies 2-2.5x, and pushing a wall counts as walking. In the sim plots (f19,
  mz1, mz3) his believed room is often not his true one, and "have I left the room", the
  doorway routing and the wall round's end all read that belief.
- **No memory of where walls and floor are.** The depth layer (#1945) measures the room, then
  `reduce()` folds it to nine directions used as a stop trigger and prompt text. Wall-following
  is a bug algorithm, which needs a contact or range sensor he does not have, so each round
  infers "am I against a wall" from proxies (swing, image flow, stalled spins), each tuned on
  one run.
- **The wrong motion layer.** GrowBot_Training's master is a closed-loop heading follower that
  turns to within 3-4°. Turns went to upstream's stock spin instead, which reached its angle in
  131 of 684 turns, and walks ran as fixed 12-25 s chunks with a stance glide around each.
- **The model picks the mode every 10-20 s** from that map, and can cancel a round.
- **Tests take 25 minutes each** with a live model, so tuning was done from single runs.

## The target

| Layer | What | Owner |
| --- | --- | --- |
| Perception | `gbVision`: depth and labels per frame, as a 64-column pseudo-scan of the nearest obstacle at body height, plus labelled things with range. Sim truth or noisy now, phone models later (`GROWBOT_VISION_PLAN.md` V2). | code |
| Pose | `gbPose`: gyro yaw (as today), speed from the walk's mode and command, corrected by matching each scan to the grid; "not moving" when scans agree while a walk is commanded. Sources: `sim-truth`, `estimated`. Web page only (owner): no ARCore. | code |
| Map | `gbGrid`: a 5 cm occupancy grid kept in the soul. Rooms are regions of it, doorways the narrow passages between them, things are labels anchored to cells. The rectangle sketch is drawn from it. | code |
| Planner | Frontier exploration, then inspection (below), with A* on the inflated grid. Goals: a frontier, a named room, a named thing, a viewpoint, home. | code |
| Motion | One heading controller on the master skill set: a carrot point on the path gives the heading error and a stop-or-go speed, the look skill for turns over 30°. No stock spin, no timed chunks. Estop, get-up and the stuck-spin back-out stay. | code |
| Thinking | Qwen: turns an ask into goals, scores which numbered way on the strip looks most promising for the goal, names rooms when a new one opens and things the labels can't name, answers questions from close-ups, narrates. It is woken by events (new room, goal object seen, stuck, a person speaks), not each step. | Qwen |

**Explored means every major item identified** (owner, 2026-11-18): furniture, appliances and
big fixtures (sofa, bed, table, fridge, piano, TV, desk), not small things on them, unless asked
("take a closer look at the fridge" is an inspect goal). Phase 1 reaches every room and closes
every frontier. Phase 2 visits a viewpoint 0.8-1.5 m from each grid cluster at least about
0.3 m across that has no confident name yet, until none is left. Smaller clusters are still
obstacles. There are no stairs or drops (owner), so no cliff sensor; there is no range sensor
(owner), so obstacles seen from farther away stay on the grid as he closes in, the planner never
plans through unseen cells, and a scan that stops changing while he walks forward backs him out.

**A goal-directed explore, end to end.** "Go find the kitchen and tell me what colour the fridge
handle is": Qwen makes the goal chain (room kitchen → thing fridge → look and answer); with no
kitchen on the map he explores, the planner taking the frontier with the best mix of Qwen's
"leads to a kitchen" score and distance; a new room wakes Qwen to name it; once the fridge is
labelled he goes to a viewpoint about 1 m in front of it, and Qwen answers from the close-up.
The answer is kept on the fridge in the things layer. He stops when the goal is met.

## Waves

Each wave is tested in the sim first on true pose and depth, so a failure points at one layer.

- **E0 — a fast, deterministic sim battery.** ✅ `python -m dave_sim.battery` (growbot/sim)
  runs the flat and the three maze houses, three starts each (the scene's start turned a third
  of the way round per seed), with no model, unattended, in parallel, and writes one table
  (`battery.md` / `battery.json`): rooms reached of all, metres walked, seconds stuck, falls, and
  how far his map's idea of where he is ends from the truth. `--pose truth` feeds the page the
  sim's true pose (`growbot-map.js` `gbPose`: while fixes keep coming they replace the gyro
  heading and walking pace, and his room is the one his map has there); `--pose reckoned` is
  today's phone. `--explorer walls` is today's round of the walls, started by the page's own
  explore entry and again whenever it ends: the baseline E2 has to beat. Major items identified
  joins the table with E2's inspection.
- **E1 — turns on the master's heading control.** ✅ A turn by degrees (`gbMap.turn`, used by
  his own `walk:{turn}` and the explore trip) runs on the box skill set's look skill, steered
  onto the angle at camera height 0 and ended once it holds it within 6°, or sits within 12°
  and stops closing (`growbot-box.js` `gbOwnWalk` move `"turn"`), with the stock spin kept for
  a robot with no skill set. Sim `turns_by_degrees` (2026-11-18, two runs of 90, -45, 180, -120 and 30
  degrees): 9 of 10 ended within 10° of the angle by the sim's true yaw, at about 5°/s. The
  miss, a half turn 29° past, is the page's gyro heading disagreeing with the truth, which
  E4's scan matching is for.
  A route (`gbMap.go(points)`, `growbot-box.js` `gbOwnGo`) is one forward walk on the skill set
  steered by heading each tick toward the next point not yet reached (within 15 cm), in place of
  timed chunks with a stance glide around each: more than 30° off he stops and turns on the look
  skill, and walks again within 10°. It ends "arrived" at the last point, or "stuck" after 12 s
  with no headway on a fed pose (no range sensor says a wall stopped him; a reckoned pose
  can't show headway, so there it ends after 1.5 times its expected time, counting only time
  on the reckoned pose), and is kept going past its first seconds while it runs, up to 5
  minutes, paying the legs' duty budget as it goes. Sim `square_by_path`: a 1 m square as one
  route on true pose arrived in 126 s and closed within 14 cm (2026-11-18). *Pass:* 95% of turns within 10°; a scripted 1 m square on
  true pose closes within 25 cm; no falls in 20 runs.
- **E2 — grid, frontiers and inspection on true pose and depth.** This is the wave that proves
  the paradigm. *Pass:* 12/12 runs reach every room and identify ≥90% of the sim's major items
  (named right, within 0.75 m) in 30 sim-minutes.
  🟡 Part 1, the map and frontiers, landed: `growbot-grid.js` (`gbGrid`) keeps a 5 cm
  log-odds grid in page memory, filled from each depth frame's 24-column scan (`growbot-vision.js`
  `reduce` `scan`; a column with no hit is clear only as far as floor was seen, and one the lens
  is too close to read is blind and skipped), with the camera's own pose per frame where the sim
  gives it. Exploring looks round once, then goes to the nearest reachable frontier (Dijkstra on
  8-connected cells, costed to keep off walls, never through floor not seen), turning on arrival
  only toward headings that still hide unseen floor, backing off after a stuck route and never
  retrying a goal it failed. Things he names are kept on the grid, joined within 0.75 m.
  Battery, 25 sim-minutes, true pose and depth (2026-11-19):

  | run | old (`walls`) rooms | grid rooms | grid walked m | grid stuck s | grid falls |
  | --- | --- | --- | --- | --- | --- |
  | flat-s0 | 1/5 | timed out | | | |
  | flat-s1 | 1/5 | 1/5 | 3.46 | 0 | 1 |
  | flat-s2 | 1/5 | 3/5 | 25.94 | 168 | 0 |
  | maze1-s0 | 1/6 | 5/6 | 29.21 | 237 | 0 |
  | maze1-s1 | 2/6 | 6/6 | 22.66 | 218 | 0 |
  | maze1-s2 | 4/6 | 5/6 | 17.21 | 522 | 0 |
  | maze2-s0 | 3/6 | 6/6 | 31.9 | 0 | 0 |
  | maze2-s1 | 3/6 | 5/6 | 31.71 | 0 | 0 |
  | maze2-s2 | 1/6 | 5/6 | 30.81 | 108 | 0 |
  | maze3-s0 | 1/6 | 6/6 | 26.54 | 0 | 0 |
  | maze3-s1 | 2/6 | 6/6 | 31.72 | 0 | 0 |
  | maze3-s2 | 1/6 | 4/6 | 13.22 | 394 | 0 |

  Every room in 4 of 12 (old 0 of 12), 52 of 66 rooms in the runs that finished (old 21 of 71),
  falls 1 (old 6). The table predates the review's fixes (a frontier reaching back to his feet
  is no longer dropped, an edge arrived at twice is given up), so the next battery re-measures.
  Still to do in E2: the flat (one run hung, one fell by the lamp, and the
  kitchen is the room most missed), fewer and shorter looks so the houses finish inside the
  time, and the inspection phase with major items identified in the table.
- **E3 — noisy depth** (`--vision noisy`), true pose. *Pass:* ≥10/12, ≥80% identified.
- **E4 — estimated pose**, web only. *Pass:* ≥9/12, ≥75% identified, never in the wrong room by
  his belief for more than 60 s. If it misses, re-anchor to known places by matching a survey
  strip against each room's kept strips before anything else.
- **E5 — thinking on top.** Qwen's goal chain, way scoring, room and thing naming, and answers.
  *Pass:* `map_flat` checks, "where is the piano" and "go to the cat" from the map, and
  `flat_fridge_handle` (from the bedroom with no map, the handle's colour varied per run) right
  in ≥10/12.
- **E6 — the real robot.** The phone's depth source, then one real room and one doorway. *Pass:*
  he crosses a real doorway on his own and the PWA's map shows both rooms.

The old explore code (`wallStep`, `step`'s `go:N` trips, `blockedRun`, `PACE` dead reckoning,
model-declared rooms as geometry, the `explore:{survey|go|walls}` prompt) is deleted at the end
of E4, not before: until then it is the fallback on the real phone. Wall-following is not tuned
further (owner, 2026-11-18).
