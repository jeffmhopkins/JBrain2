// The Body tab's movement remote, laid out as the 2026-11-16 redesign's variant A "remote plus
// rows" (docs/mocks/growbot-body-tab/a-remote-rows.html): the walk is one row opening the Walk
// with sheet, Tap a move | Steer live makes the timed pad and the live steering one control,
// tricks run on one line that scrolls sideways, and Follow me and Body test are rows opening
// sheets, so the everyday remote fits about a screen. Its history (owner, 2026-11-12: "if the
// body is turned on, I want to be able to call the policies directly"; then "this looks like
// crap, need a way better GUI").
// A driving pad instead of a form: up/down walk, left/right spin, Stop in the middle, how
// long as chips under it, and the gestures as tricks. The walk and spin policies need the
// motion sensors of the phone riding the robot, so every move goes to the creature page on
// that phone (growbot-box.js), which runs it as if its own brain had asked; this panel only
// says so, and shows the creature's answer. "Walk with" picks the creature's own walks or a
// trained one from the box's policy library (owner, 2026-11-14), which the pad then steers by
// heading: up walks ahead, left and right turn it 90°, down backs away or turns it around.
// The tricks are what the live app's creature does for its own brain besides walking (owner,
// 2026-11-14: "the live app also has other policies for doing tricks"): its stock gestures and
// tall stance, the escape spin it falls back to when a spin stalls, and any gesture its soul has
// saved, which the creature reports to the box. The library's own switches (on or off, walk,
// turn or trick, one per family: GrowbotPoliciesSheet `offered`) decide what is shown: walks
// under "Walk with", turns under "Turn with" for the left and right keys, tricks as buttons
// that hold the policy facing ahead for the picked seconds.

import { type ReactNode, useEffect, useRef, useState } from "react";
import { type GrowbotMove, type GrowbotPolicy, type GrowbotRobot, api } from "../api/client";
import { GrowbotBodyTest } from "./GrowbotBodyTest";
import { GrowbotDrive } from "./GrowbotDrive";
import { type FollowRunning, GrowbotFollow } from "./GrowbotFollow";
import {
  GrowbotPoliciesSheet,
  offered,
  offeredFor,
  skillSet,
  walkKind,
} from "./GrowbotPoliciesSheet";
import { Sheet } from "./Sheet";
import { gistOf, shortName } from "./growbotNames";
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
  { id: "stand_tall", label: "Stand tall" },
  // Upstream's tested get-up, from lying on its back; the creature says why when it can't.
  { id: "get_up", label: "Get up" },
];
const STOCK_GESTURES = new Set<string>(["wiggle", "bow", "sway"]);

// The escape spin: upstream's harder-kicking spin for when the ordinary one stalls. It runs for
// the picked seconds, like the pad's spins.
const ESCAPES: { id: GrowbotMove; label: string; run: string }[] = [
  { id: "spin_escape_left", label: "Escape spin left", run: "Escape spin left" },
  { id: "spin_escape_right", label: "Escape spin right", run: "Escape spin right" },
];

// Inside the box's MOVE_SECS bounds (1-15, growbot/server/serve.py); upstream's remote runs 5 s.
const SECS = [2, 5, 10, 15] as const;
const DEFAULT_SECS = 5;

const DOING: Record<GrowbotMove, string> = {
  forward: "Walking forward",
  back: "Walking backward",
  spin_right: "Spinning right",
  spin_left: "Spinning left",
  spin_escape_right: "Escape-spinning right",
  spin_escape_left: "Escape-spinning left",
  stand_tall: "Standing tall",
  get_up: "Getting up",
  wiggle: "Wiggling",
  bow: "Bowing",
  sway: "Swaying",
  gesture: "Playing it",
  look: "Holding it",
  stop: "Stopped",
};

type Said = { text: string; bad: boolean };

function WalkChoice({
  group,
  name,
  gist,
  kind,
  on,
  pick,
  done,
}: {
  group: string;
  name: string;
  gist?: string | undefined;
  kind?: string;
  on: boolean;
  pick: () => void;
  done: () => void;
}) {
  return (
    <label className={`gb-choice${on ? " sel" : ""}`}>
      <input
        type="radio"
        name={group}
        checked={on}
        onChange={pick}
        // A tap picks and closes; arrow keys only move the pick, so the sheet stays for them.
        onClick={(e) => {
          if (e.detail > 0) done();
        }}
      />
      <span className="gb-tx">
        {name}
        {gist ? <em>{gist}</em> : null}
        {kind ? <em className="gb-dim-t">{kind}</em> : null}
      </span>
    </label>
  );
}

// Which walk (or turn) this phone last drove a robot with: a per-viewer convenience, so storage
// may fail.
function storedWalk(code: string, kind = "walk"): string {
  try {
    return localStorage.getItem(`gb-${kind}-with:${code}`) ?? "";
  } catch {
    return "";
  }
}

function storeWalk(code: string, id: string, kind = "walk"): void {
  try {
    if (id) localStorage.setItem(`gb-${kind}-with:${code}`, id);
    else localStorage.removeItem(`gb-${kind}-with:${code}`);
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
  more,
}: {
  robot: GrowbotRobot;
  onStop?: () => Promise<Said>;
  /** Rows the controls card adds under Follow me and Body test (its Legs). */
  more?: ReactNode;
}) {
  const [secs, setSecs] = useState<number>(DEFAULT_SECS);
  const [busy, setBusy] = useState(false);
  // What runs now, for the lit pad button and the bar draining under it; `n` restarts the bar.
  const [running, setRunning] = useState<{
    move: GrowbotMove;
    secs: number;
    n: number;
    policy?: string | undefined;
  } | null>(null);
  // Only the latest press speaks and frees the buttons: a slow walk's answer must not land
  // over a Stop pressed after it.
  const latest = useRef(0);
  const [said, setSaid] = useState<Said | null>(null);
  const [policies, setPolicies] = useState<GrowbotPolicy[]>([]);
  const [walkWith, setWalkWith] = useState(() => storedWalk(robot.code));
  const [turnWith, setTurnWith] = useState(() => storedWalk(robot.code, "turn"));
  const [sheet, setSheet] = useState<"walks" | "follow" | "test" | "library" | null>(null);
  const [mode, setMode] = useState<"tap" | "steer">("tap");
  // Lifted out of their sheets, so closing one never forgets that the robot is still going.
  const [following, setFollowing] = useState<FollowRunning>(null);
  const [driving, setDriving] = useState(false);
  const [halted, setHalted] = useState(0);
  const mounted = useRef(true);
  const done = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Set again on mount: StrictMode's rehearsal unmount would otherwise leave it false for good.
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      if (done.current) clearTimeout(done.current);
    };
  }, []);
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
  // Any move of the pad's own ends a follow on the creature (a new move replaces it), as does a
  // robot nobody can drive any more.
  // biome-ignore lint/correctness/useExhaustiveDependencies: halted and blocked are the triggers.
  useEffect(() => setFollowing(null), [halted, blocked]);
  const name = robot.name || robot.code;
  const walks = offered(policies, "walk");
  const turns = offered(policies, "turn");
  const tricks = offered(policies, "trick");
  const trained = offeredFor(policies, walks, walkWith);
  const turner = offeredFor(policies, turns, turnWith);
  const pad = trained ? trainedPad(trained) : null;
  const skills = skillSet(policies);
  const turnPad = turner ? trainedPad(turner) : null;
  // The policy each pad key runs: a picked turn takes the left and right keys from the walk.
  const keyPolicy = (k: Policy): GrowbotPolicy | null =>
    (k === "spin_left" || k === "spin_right") && turner ? turner : trained;
  const keyPad = (k: Policy) =>
    (k === "spin_left" || k === "spin_right") && turnPad ? turnPad : pad;

  function pick(id: string) {
    setWalkWith(id);
    storeWalk(robot.code, id);
  }

  function pickTurn(id: string) {
    setTurnWith(id);
    storeWalk(robot.code, id, "turn");
  }
  // The pad's Stop works whenever the chip is on: a shared Stop also stops the chip itself,
  // creature or not.
  const canStop = onStop ? robot.connected && !robot.revoked_at : !blocked;

  function finish(move: GrowbotMove, s: number, mine: number, policy?: string) {
    if (done.current) clearTimeout(done.current);
    setRunning({ move, secs: s, n: mine, policy });
    done.current = setTimeout(() => {
      if (mounted.current && mine === latest.current) setRunning(null);
    }, s * 1000);
  }

  async function send(move: GrowbotMove, s = 0, gesture?: string, trick?: GrowbotPolicy) {
    setHalted((n) => n + 1);
    const mine = ++latest.current;
    setBusy(true);
    const live = () => mounted.current && mine === latest.current;
    try {
      const key = PAD.some((p) => p.id === move) ? (move as Policy) : null;
      const walk = trick ?? (key ? keyPolicy(key) : null);
      const words = key ? keyPad(key) : null;
      const out = await api.moveGrowbotRobot(robot.code, move, s, walk?.id, gesture);
      if (!live()) return;
      const doing = trick
        ? `Running ${trick.name}`
        : walk && words && key
          ? `${words[key].doing} with ${walk.name}`
          : gesture
            ? `Playing ${gesture}`
            : DOING[move];
      setSaid(
        out.ok
          ? { text: `${doing}${s ? ` for ${s} s` : ""}.`, bad: false }
          : { text: out.why || `${name} didn't start it.`, bad: true },
      );
      if (out.ok && s) finish(move, s, mine, walk?.id);
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
    setHalted((n) => n + 1);
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
  const saved = (robot.gestures ?? []).filter((g) => !STOCK_GESTURES.has(g));
  const steerable = !!(trained || skills);
  const steering = steerable && mode === "steer";
  // A walk that can't be steered sends the remote back to taps, so picking one later
  // doesn't land on live steering unasked.
  // biome-ignore lint/correctness/useExhaustiveDependencies: only steerable turning off counts.
  useEffect(() => {
    if (steerable) return;
    // A drive that loses its walk is stopped, not left to its lease.
    if (driving) stop();
    setMode("tap");
  }, [steerable]);
  const saidLine = (
    <output className={`gb-move-said${said?.bad ? " gb-bad-t" : ""}`} aria-live="polite">
      {said?.text ?? ""}
    </output>
  );
  return (
    <div className="gb-move">
      <button
        type="button"
        className="gb-picker"
        aria-haspopup="dialog"
        onClick={() => setSheet("walks")}
      >
        <span className="gb-tx">
          <span className="gb-t2">Walking with</span>
          <span className="gb-t1">
            {trained ? shortName(trained.name) : "Built-in walk"}
            {turner ? ` · turns with ${shortName(turner.name)}` : ""}
          </span>
        </span>
        <span className="gb-chev" aria-hidden="true">
          ›
        </span>
      </button>

      {steerable && (
        <fieldset className="gb-seg pick gb-mode" aria-label="How the pad drives">
          <button
            type="button"
            className={steering ? "" : "on"}
            aria-pressed={!steering}
            onClick={() => {
              // Leaving live steering ends the drive now, not when its lease runs out.
              if (driving) stop();
              setMode("tap");
            }}
          >
            Tap a move
          </button>
          <button
            type="button"
            className={steering ? "on" : ""}
            aria-pressed={steering}
            onClick={() => {
              // A timed move still under way stops here, since its pad and bar go away.
              // busy covers a move whose answer is still on its way, and untimed tricks.
              if (running || busy) stop();
              setMode("steer");
            }}
          >
            Steer live
          </button>
        </fieldset>
      )}

      <p className={`gb-move-tip${blocked ? " blocked" : ""}`}>
        {blocked ??
          (steering
            ? "Start driving, then move the sliders as it goes. It stops a moment after you stop."
            : trained
              ? `${shortName(trained.name)}: ${walkKind(trained)}. Ahead walks the way it faces; left and right turn it 90°.`
              : `Forward is toward the phone's screen. Set ${name} down still a moment first.`)}
      </p>

      {steering ? (
        <GrowbotDrive
          code={robot.code}
          policy={trained}
          skills={skills}
          disabled={!!blocked}
          halted={halted}
          onSaid={setSaid}
          onStop={stop}
          onDriving={(d) => {
            setDriving(d);
            // A drive's start replaces a follow on the creature.
            if (d) setFollowing(null);
          }}
        />
      ) : (
        <>
          <div className="gb-pad">
            {PAD.map((p) => (
              <button
                key={p.id}
                type="button"
                className={`gb-pad-b ${p.at}${lit === p.id ? " on" : ""}`}
                aria-label={`${keyPad(p.id)?.[p.id].run ?? p.run} ${secs} s`}
                disabled={off}
                onClick={() => void send(p.id, secs)}
              >
                {p.icon}
                <span aria-hidden="true">{keyPad(p.id)?.[p.id].label ?? p.label}</span>
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
        </>
      )}
      {steering && !driving && (
        <button
          type="button"
          className="gb-btn danger block gb-steer-stop"
          disabled={!canStop}
          onClick={stop}
        >
          <StopIcon size={16} />
          Stop
        </button>
      )}
      {!sheet && saidLine}

      {!steering && (
        <fieldset className="gb-seg pick gb-secs" aria-label="Walks and spins run for">
          {SECS.map((s) => (
            <button
              key={s}
              type="button"
              className={secs === s ? "on" : ""}
              aria-pressed={secs === s}
              onClick={() => setSecs(s)}
            >
              {s} s
            </button>
          ))}
        </fieldset>
      )}

      <div className="gb-move-h">Tricks</div>
      <fieldset className="gb-strip" aria-label="Tricks">
        {TRICKS.map((g) => (
          <button
            key={g.id}
            type="button"
            className={`gb-chip trick${lit === g.id ? " on" : ""}`}
            disabled={off}
            onClick={() => void send(g.id)}
          >
            {g.label}
          </button>
        ))}
        {ESCAPES.map((g) => (
          <button
            key={g.id}
            type="button"
            className={`gb-chip trick${lit === g.id ? " on" : ""}`}
            aria-label={`${g.run} ${secs} s`}
            disabled={off}
            onClick={() => void send(g.id, secs)}
          >
            {g.label}
          </button>
        ))}
        {tricks.map((p) => (
          <button
            key={p.id}
            type="button"
            className={`gb-chip trick${lit === "look" && running?.policy === p.id ? " on" : ""}`}
            aria-label={`${p.name} ${secs} s`}
            disabled={off}
            onClick={() => void send("look", secs, undefined, p)}
          >
            {shortName(p.name)}
          </button>
        ))}
        {saved.map((g) => (
          <button
            key={`saved-${g}`}
            type="button"
            className="gb-chip trick"
            disabled={off}
            onClick={() => void send("gesture", 0, g)}
          >
            {g}
          </button>
        ))}
      </fieldset>

      {(skills || more) && (
        <div className="gb-list gb-more">
          {skills && (
            <button
              type="button"
              className="gb-li"
              aria-haspopup="dialog"
              onClick={() => setSheet("follow")}
            >
              <span className="gb-tx">
                <span className="gb-t1">Follow me</span>
                <span className={`gb-t2${following ? " gb-ok-t" : ""}`}>
                  {following === "walk"
                    ? "Following now"
                    : following === "dry"
                      ? "Dry run on"
                      : "Walks after the nearest face"}
                </span>
              </span>
              <span className="gb-chev" aria-hidden="true">
                ›
              </span>
            </button>
          )}
          {skills && (
            <button
              type="button"
              className="gb-li"
              aria-haspopup="dialog"
              onClick={() => setSheet("test")}
            >
              <span className="gb-tx">
                <span className="gb-t1">Body test</span>
                <span className="gb-t2">A guided trial, with past runs and their logs</span>
              </span>
              <span className="gb-chev" aria-hidden="true">
                ›
              </span>
            </button>
          )}
          {more}
        </div>
      )}

      {sheet === "walks" && (
        <Sheet title="Walk with" onClose={() => setSheet(null)}>
          <fieldset className="gb-choices gb-walks">
            {/* The sheet's title names it, until a Turn with list needs telling apart. */}
            <legend className={turns.length ? undefined : "gb-sr-only"}>Walk with</legend>
            <WalkChoice
              group={`gb-walk-${robot.code}`}
              name="Built-in walk"
              gist={`${name}'s stock walk and spins`}
              kind="no training needed"
              on={!trained}
              pick={() => pick("")}
              done={() => setSheet(null)}
            />
            {walks.map((p) => (
              <WalkChoice
                key={p.id}
                group={`gb-walk-${robot.code}`}
                name={shortName(p.name)}
                gist={gistOf(p.name)}
                kind={walkKind(p)}
                on={trained?.id === p.id}
                pick={() => pick(p.id)}
                done={() => setSheet(null)}
              />
            ))}
          </fieldset>
          {turns.length > 0 && (
            <fieldset className="gb-choices gb-walks">
              <legend>Turn with</legend>
              <WalkChoice
                group={`gb-turn-${robot.code}`}
                name={trained ? "The walk's turns" : "Built-in spins"}
                gist={trained ? "Left and right use the walk above" : undefined}
                on={!turner}
                pick={() => pickTurn("")}
                done={() => setSheet(null)}
              />
              {turns.map((p) => (
                <WalkChoice
                  key={p.id}
                  group={`gb-turn-${robot.code}`}
                  name={shortName(p.name)}
                  gist={gistOf(p.name)}
                  kind={walkKind(p)}
                  on={turner?.id === p.id}
                  pick={() => pickTurn(p.id)}
                  done={() => setSheet(null)}
                />
              ))}
            </fieldset>
          )}
          <button type="button" className="gb-btn block" onClick={() => setSheet("library")}>
            {policies.length ? "Trained walks…" : "Add a trained walk…"}
          </button>
        </Sheet>
      )}
      {sheet === "follow" && skills && (
        <Sheet title="Follow me" onClose={() => setSheet(null)}>
          <GrowbotFollow
            code={robot.code}
            skills={skills}
            disabled={!!blocked}
            onSaid={setSaid}
            running={following}
            setRunning={setFollowing}
          />
          {saidLine}
        </Sheet>
      )}
      {sheet === "test" && skills && (
        <Sheet title="Body test" onClose={() => setSheet(null)}>
          <GrowbotBodyTest
            code={robot.code}
            skills={skills}
            disabled={!!blocked}
            onSaid={setSaid}
            onStarted={() => setFollowing(null)}
          />
          {saidLine}
        </Sheet>
      )}
      {sheet === "library" && (
        <GrowbotPoliciesSheet
          policies={policies}
          onChange={(update) => setPolicies(update)}
          onClose={() => setSheet(null)}
        />
      )}
    </div>
  );
}
