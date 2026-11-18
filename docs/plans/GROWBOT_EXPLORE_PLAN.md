# GrowBot exploring — a map and a planner under Dave's thinking

> **Status:** In progress · **Last verified:** 2026-11-18 · **Waves:** E0◻️ E1🟡 E2◻️ E3◻️ E4◻️ E5◻️ E6◻️

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

- **E0 — a fast, deterministic sim battery.** `--pose truth` (the page gets the sim's true x, y
  and yaw through `gbPose`), a scripted explore start with no model, and a batch runner over the
  three maze houses and the flat, three seeds each. *Pass:* the 12-run battery runs unattended
  and prints one table (rooms reached, major items identified, falls, time stuck).
- **E1 — turns on the master's heading control.** 🟡 A turn by degrees (`gbMap.turn`, used by
  his own `walk:{turn}` and the explore trip) runs on the box skill set's look skill, steered
  onto the angle at camera height 0 and ended once it holds it within 6°, or sits within 12°
  and stops closing (`growbot-box.js` `gbOwnWalk` move `"turn"`), with the stock spin kept for
  a robot with no skill set. Sim `turns_by_degrees` (2026-11-18, two runs of five turns): 7 of
  10 ended within 10° of the angle by the sim's true yaw and all 10 within 13°, at about 5°/s;
  the misses are the page's gyro heading and the body's settle after the turn disagreeing with
  the truth by up to 10°, which E4's scan matching is for.
  Next in this wave: `goHeading(deg)` / `follow(path)` driving forward walks continuously by
  heading, ending the 12-25 s chunks. *Pass:* 95% of turns within 10°; a scripted 1 m square on
  true pose closes within 25 cm; no falls in 20 runs.
- **E2 — grid, frontiers and inspection on true pose and depth.** This is the wave that proves
  the paradigm. *Pass:* 12/12 runs reach every room and identify ≥90% of the sim's major items
  (named right, within 0.75 m) in 30 sim-minutes.
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
