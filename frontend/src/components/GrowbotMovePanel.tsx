// The Body tab's movement remote (owner, 2026-11-12: "if the body is turned on, I want to be
// able to call the policies directly"; then "this looks like crap, need a way better GUI").
// A driving pad instead of a form: up/down walk, left/right spin, Stop in the middle, how
// long as chips under it, and the gestures as tricks. The walk and spin policies need the
// motion sensors of the phone riding the robot, so every move goes to the creature page on
// that phone (growbot-box.js), which runs it as if its own brain had asked; this panel only
// says so, and shows the creature's answer. "Walk with" picks the creature's own walks or a
// trained one from the box's policy library (owner, 2026-11-14), which the pad then steers by
// heading: up walks ahead, left and right turn it 90°, down backs away or turns it around.

import { type ReactNode, useEffect, useRef, useState } from "react";
import { type GrowbotMove, type GrowbotPolicy, type GrowbotRobot, api } from "../api/client";
import { GrowbotPoliciesSheet, walkKind } from "./GrowbotPoliciesSheet";
import { ChevronDownIcon, ChevronUpIcon, RotateCcwIcon, RotateCwIcon, StopIcon } from "./icons";

type Policy = "forward" | "back" | "spin_right" | "spin_left";

// Where each sits on the pad: forward is up, toward the phone's screen.
const PAD: { id: Policy; label: string; run: string; at: string; icon: ReactNode }[] = [
  {
    id: "forward",
    label: "Forward",
    run: "Walk forward",
    at: "n",
    icon: <ChevronUpIcon size={30} />,
  },
  { id: "spin_left", label: "Spin", run: "Spin left", at: "w", icon: <RotateCcwIcon size={26} /> },
  { id: "spin_right", label: "Spin", run: "Spin right", at: "e", icon: <RotateCwIcon size={26} /> },
  { id: "back", label: "Back", run: "Walk backward", at: "s", icon: <ChevronDownIcon size={30} /> },
];

// The same pad steering a trained walk: each key is a heading (growbot-box.js gbTrainedWalk).
function trainedPad(
  p: GrowbotPolicy,
): Record<Policy, { label: string; run: string; doing: string }> {
  const back = p.contract.speed
    ? { label: "Back", run: "Back away", doing: "Backing away" }
    : { label: "Turn", run: "Turn around", doing: "Turning around" };
  return {
    forward: { label: "Ahead", run: "Walk ahead", doing: "Walking ahead" },
    spin_left: { label: "Left", run: "Turn left", doing: "Turning left" },
    spin_right: { label: "Right", run: "Turn right", doing: "Turning right" },
    back,
  };
}

const TRICKS: { id: GrowbotMove; label: string }[] = [
  { id: "wiggle", label: "Wiggle" },
  { id: "bow", label: "Bow" },
  { id: "sway", label: "Sway" },
];

// Inside the box's MOVE_SECS bounds (1-15, growbot/server/serve.py); upstream's remote runs 5 s.
const SECS = [2, 5, 10, 15] as const;
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

type Said = { text: string; bad: boolean };

// Which walk this phone last drove a robot with: a per-viewer convenience, so storage may fail.
function storedWalk(code: string): string {
  try {
    return localStorage.getItem(`gb-walk-with:${code}`) ?? "";
  } catch {
    return "";
  }
}

function storeWalk(code: string, id: string): void {
  try {
    if (id) localStorage.setItem(`gb-walk-with:${code}`, id);
    else localStorage.removeItem(`gb-walk-with:${code}`);
  } catch {
    // A private window keeps the pick for this visit only.
  }
}

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

/** `onStop`: the card's shared Stop (chip and creature both), which the pad's middle button
 *  then calls instead of stopping the creature alone; it answers with what to say. */
export function GrowbotMovePanel({
  robot,
  onStop,
}: {
  robot: GrowbotRobot;
  onStop?: () => Promise<Said>;
}) {
  const [secs, setSecs] = useState<number>(DEFAULT_SECS);
  const [busy, setBusy] = useState(false);
  // What runs now, for the lit pad button and the bar draining under it; `n` restarts the bar.
  const [running, setRunning] = useState<{ move: GrowbotMove; secs: number; n: number } | null>(
    null,
  );
  // Only the latest press speaks and frees the buttons: a slow walk's answer must not land
  // over a Stop pressed after it.
  const latest = useRef(0);
  const [said, setSaid] = useState<Said | null>(null);
  const [policies, setPolicies] = useState<GrowbotPolicy[]>([]);
  const [walkWith, setWalkWith] = useState(() => storedWalk(robot.code));
  const [library, setLibrary] = useState(false);
  const mounted = useRef(true);
  const done = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      mounted.current = false;
      if (done.current) clearTimeout(done.current);
    },
    [],
  );
  useEffect(() => {
    let gone = false;
    // No library (an older box, or it's down) is just no trained walks to pick.
    api.growbotPolicies().then(
      (got) => !gone && setPolicies(got),
      () => undefined,
    );
    return () => {
      gone = true;
    };
  }, []);
  const blocked = moveBlocked(robot);
  const name = robot.name || robot.code;
  const trained = policies.find((p) => p.id === walkWith) ?? null;
  const pad = trained ? trainedPad(trained) : null;

  function pick(id: string) {
    setWalkWith(id);
    storeWalk(robot.code, id);
  }
  // The pad's Stop works whenever the chip is on: a shared Stop also stops the chip itself,
  // creature or not.
  const canStop = onStop ? robot.connected && !robot.revoked_at : !blocked;

  function finish(move: GrowbotMove, s: number, mine: number) {
    if (done.current) clearTimeout(done.current);
    setRunning({ move, secs: s, n: mine });
    done.current = setTimeout(() => {
      if (mounted.current && mine === latest.current) setRunning(null);
    }, s * 1000);
  }

  async function send(move: GrowbotMove, s = 0) {
    const mine = ++latest.current;
    setBusy(true);
    const live = () => mounted.current && mine === latest.current;
    try {
      const walk = trained && pad && move in pad ? trained : null;
      const out = await api.moveGrowbotRobot(robot.code, move, s, walk?.id);
      if (!live()) return;
      const doing = walk && pad ? `${pad[move as Policy].doing} with ${walk.name}` : DOING[move];
      setSaid(
        out.ok
          ? { text: `${doing}${s ? ` for ${s} s` : ""}.`, bad: false }
          : { text: out.why || `${name} didn't start it.`, bad: true },
      );
      if (out.ok && s) finish(move, s, mine);
      else setRunning(null);
    } catch (e) {
      if (live()) {
        setSaid({ text: message(e), bad: true });
        setRunning(null);
      }
    } finally {
      if (live()) setBusy(false);
    }
  }

  function stop() {
    if (!onStop) {
      void send("stop");
      return;
    }
    const mine = ++latest.current;
    setBusy(false);
    setRunning(null);
    void onStop().then((out) => {
      if (mounted.current && mine === latest.current) setSaid(out);
    });
  }

  const off = !!blocked || busy;
  const lit = running?.move;
  return (
    <div className="gb-move">
      <fieldset className="gb-chips gb-walk-with">
        <legend className="gb-move-h">Walk with</legend>
        <button
          type="button"
          className={`gb-chip${trained ? "" : " on"}`}
          aria-pressed={!trained}
          onClick={() => pick("")}
        >
          Its own walks
        </button>
        {policies.map((p) => (
          <button
            key={p.id}
            type="button"
            className={`gb-chip${trained?.id === p.id ? " on" : ""}`}
            aria-pressed={trained?.id === p.id}
            onClick={() => pick(p.id)}
          >
            {p.name}
          </button>
        ))}
        <button type="button" className="gb-chip add" onClick={() => setLibrary(true)}>
          {policies.length ? "Trained walks…" : "Add a trained walk…"}
        </button>
      </fieldset>

      <p className={`gb-move-tip${blocked ? " blocked" : ""}`}>
        {blocked ??
          (trained
            ? `${trained.name}: ${walkKind(trained)}. Ahead walks the way it faces; left and right turn it 90°.`
            : `Forward is toward the phone's screen. Set ${name} down still a moment first.`)}
      </p>

      <div className="gb-pad">
        {PAD.map((p) => (
          <button
            key={p.id}
            type="button"
            className={`gb-pad-b ${p.at}${lit === p.id ? " on" : ""}`}
            aria-label={`${pad ? pad[p.id].run : p.run} ${secs} s`}
            disabled={off}
            onClick={() => void send(p.id, secs)}
          >
            {p.icon}
            <span aria-hidden="true">{pad ? pad[p.id].label : p.label}</span>
          </button>
        ))}
        <button
          type="button"
          className="gb-pad-stop"
          aria-label="Stop"
          disabled={!canStop}
          onClick={stop}
        >
          <StopIcon size={22} />
          <span aria-hidden="true">Stop</span>
        </button>
      </div>

      <div className="gb-move-bar" aria-hidden="true">
        {running && (
          <span
            key={running.n}
            className="gb-move-drain"
            style={{ animationDuration: `${running.secs}s` }}
          />
        )}
      </div>
      <output className={`gb-move-said${said?.bad ? " gb-bad-t" : ""}`} aria-live="polite">
        {said?.text ?? ""}
      </output>

      <fieldset className="gb-chips">
        <legend className="gb-move-h">Walks and spins run for</legend>
        {SECS.map((s) => (
          <button
            key={s}
            type="button"
            className={`gb-chip${secs === s ? " on" : ""}`}
            aria-pressed={secs === s}
            onClick={() => setSecs(s)}
          >
            {s} s
          </button>
        ))}
      </fieldset>

      <div className="gb-move-h">Tricks</div>
      <div className="gb-chips">
        {TRICKS.map((g) => (
          <button
            key={g.id}
            type="button"
            className="gb-chip trick"
            disabled={off}
            onClick={() => void send(g.id)}
          >
            {g.label}
          </button>
        ))}
      </div>
      {library && (
        <GrowbotPoliciesSheet
          policies={policies}
          onChange={(update) => setPolicies(update)}
          onClose={() => setLibrary(false)}
        />
      )}
    </div>
  );
}
