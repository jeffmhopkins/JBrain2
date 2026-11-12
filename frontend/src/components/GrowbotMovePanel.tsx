// The Body tab's movement remote (owner, 2026-11-12: "if the body is turned on, I want to be
// able to call the policies directly"). Laid out like upstream's Policy Remote
// (growbot/site/tester/policy.html): pick a policy, set how long, run it, stop. The walk and
// spin policies need the motion sensors of the phone riding the robot, so every move goes to
// the creature page on that phone (growbot-box.js), which runs it as if its own brain had
// asked; this panel only says so, and shows the creature's answer.

import { useEffect, useRef, useState } from "react";
import { type GrowbotMove, type GrowbotRobot, api } from "../api/client";

type Policy = "forward" | "back" | "spin_right" | "spin_left";

const POLICIES: { id: Policy; label: string; run: string }[] = [
  { id: "forward", label: "Forward", run: "Walk forward" },
  { id: "back", label: "Backward", run: "Walk backward" },
  { id: "spin_right", label: "Spin ↻", run: "Spin right" },
  { id: "spin_left", label: "Spin ↺", run: "Spin left" },
];

const GESTURES: { id: GrowbotMove; label: string }[] = [
  { id: "wiggle", label: "Wiggle" },
  { id: "bow", label: "Bow" },
  { id: "sway", label: "Sway" },
];

// The box's MOVE_SECS bounds (growbot/server/serve.py); upstream's remote runs 5 s.
const MIN_SECS = 1;
const MAX_SECS = 15;
const DEFAULT_SECS = 5;

const DOING: Record<GrowbotMove, string> = {
  forward: "Walking forward",
  back: "Walking backward",
  spin_right: "Spinning right",
  spin_left: "Spinning left",
  wiggle: "Wiggling",
  bow: "Bowing",
  sway: "Swaying",
  stop: "Stopped",
};

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Why the remote cannot move `r` now, in words; null when it can. */
export function moveBlocked(r: GrowbotRobot): string | null {
  if (r.revoked_at) return "Its key is revoked. Reflash it to move it.";
  if (!r.connected) return "Turn the robot on to move it.";
  if (!r.creature) {
    return "Open a persona wearing it and keep the creature on screen. Its phone's motion sensors run the walk.";
  }
  return null;
}

/** The shared Stop's half for the creature: halts the policy it runs, which a chip-side stop
 *  alone would not (the policy streams a new pose a frame later). Best effort, never throws. */
export function stopCreature(r: GrowbotRobot): void {
  if (!r.creature) return;
  api.moveGrowbotRobot(r.code, "stop").catch(() => undefined);
}

/** `withStop`: its own Stop, for when no shared one sits beside it (GrowbotBodyControls has
 *  one, which calls `stopCreature`). */
export function GrowbotMovePanel({
  robot,
  withStop = true,
}: {
  robot: GrowbotRobot;
  withStop?: boolean;
}) {
  const [policy, setPolicy] = useState<Policy>("forward");
  const [secs, setSecs] = useState(DEFAULT_SECS);
  const [busy, setBusy] = useState(false);
  const [said, setSaid] = useState<{ text: string; bad: boolean } | null>(null);
  const mounted = useRef(true);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );
  const blocked = moveBlocked(robot);
  const name = robot.name || robot.code;
  const run = POLICIES.find((p) => p.id === policy)?.run ?? "Run";

  async function send(move: GrowbotMove, s = 0) {
    setBusy(true);
    try {
      const out = await api.moveGrowbotRobot(robot.code, move, s);
      if (!mounted.current) return;
      setSaid(
        out.ok
          ? { text: `${DOING[move]}${s ? ` for ${s} s` : ""}.`, bad: false }
          : { text: out.why || `${name} didn't start it.`, bad: true },
      );
    } catch (e) {
      if (mounted.current) setSaid({ text: message(e), bad: true });
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  function setClamped(raw: string) {
    const n = Math.round(Number(raw));
    if (Number.isFinite(n)) setSecs(Math.min(MAX_SECS, Math.max(MIN_SECS, n)));
  }

  const off = !!blocked || busy;
  return (
    <div className="gb-move">
      {blocked ? (
        <p className="gb-hint">{blocked}</p>
      ) : (
        <p className="gb-hint dim">
          Forward is toward the phone's screen. Set {name} down still for a moment so its sensors
          settle, then run.
        </p>
      )}
      <div className="gb-seg inner gb-move-pick">
        {POLICIES.map((p) => (
          <button
            key={p.id}
            type="button"
            aria-pressed={policy === p.id}
            className={policy === p.id ? "on" : ""}
            onClick={() => setPolicy(p.id)}
          >
            {p.label}
          </button>
        ))}
      </div>
      <div className="gb-move-run">
        <label className="gb-move-secs">
          <span>Seconds</span>
          <input
            type="number"
            inputMode="numeric"
            min={MIN_SECS}
            max={MAX_SECS}
            value={secs}
            onChange={(e) => setClamped(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="gb-btn primary"
          disabled={off}
          onClick={() => void send(policy, secs)}
        >
          {run} {secs} s
        </button>
      </div>
      <div className="gb-acts">
        {GESTURES.map((g) => (
          <button
            key={g.id}
            type="button"
            className="gb-btn"
            disabled={off}
            onClick={() => void send(g.id)}
          >
            {g.label}
          </button>
        ))}
      </div>
      {withStop && (
        <button
          type="button"
          className="gb-btn danger block gb-move-stop"
          disabled={!!blocked}
          onClick={() => void send("stop")}
        >
          Stop
        </button>
      )}
      <output className={`gb-hint gb-move-said${said?.bad ? " gb-bad-t" : ""}`} aria-live="polite">
        {said?.text ?? ""}
      </output>
    </div>
  );
}
