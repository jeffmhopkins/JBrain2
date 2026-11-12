/** The Body tab's one controls card per robot: Move (the driving pad, passed in as `move`;
 *  without it the card is Legs alone) and Legs. Legs is
 *  upstream's tester leg check (tester/motor.html) rebuilt here so nothing needs the original
 *  app: which leg is which and the swap, the by-eye trims, each leg's sweep and sign, the IMU
 *  signs and the walk gain. Each setting changed here is saved on the box for this robot and
 *  every creature that wears it copies it in; a change also moves the legs so it can be
 *  judged by eye, as upstream's sliders do. One Stop serves both: the chip's stop, plus
 *  `onStop` for a creature walking it; Move wears it in the middle of its pad, Legs under. */
import { type ReactNode, useEffect, useRef, useState } from "react";
import { type GrowbotCal, type GrowbotRobot, type GrowbotStep, api } from "../api/client";
import { GrowbotTabs } from "./GrowbotPersonaSheets";
import { useArm } from "./growbotArm";
import { StopIcon } from "./icons";

type Pane = "move" | "legs";

// Upstream body truth (v3/body.json walk.cal), what Reset puts back.
export const GB_CAL_DEFAULTS: GrowbotCal = {
  L_OFF: 0,
  R_OFF: 0,
  SWAP: 0,
  L_SIGN: -1,
  R_SIGN: 1,
  IMU_SIGN: [1, -1, 1],
  gain: 1,
};
const TRIM_MAX = 30;
// A run of slider drags is one save and one leg move, once the hand stops.
const SAVE_AFTER_MS = 400;
const OFF_HINT = "Turn the robot on to move it.";
const IMU_AXES = ["roll", "pitch", "yaw"] as const;

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** The moves upstream's leg check sends, in the robot's own left/right. */
export const gbMoves = {
  // Straight at rest, trimmed: the pose a trim is judged in.
  trim: (c: GrowbotCal): GrowbotStep[] => [{ l: 90 + c.L_OFF, r: 90 + c.R_OFF, ms: 300 }],
  // Which leg is which: one leg out and back, the other still.
  left: (): GrowbotStep[] => [
    { l: 125, r: 90, ms: 450 },
    { l: 90, r: 90, ms: 400 },
  ],
  right: (): GrowbotStep[] => [
    { l: 90, r: 125, ms: 450 },
    { l: 90, r: 90, ms: 400 },
  ],
  // Forward is a sweep down toward the screen; a leg that lifts instead has its sign flipped.
  sweepL: (c: GrowbotCal): GrowbotStep[] => [
    { l: 90 + c.L_SIGN * 30, r: 90, ms: 500 },
    { l: 90, r: 90, ms: 400 },
  ],
  sweepR: (c: GrowbotCal): GrowbotStep[] => [
    { l: 90, r: 90 + c.R_SIGN * 30, ms: 500 },
    { l: 90, r: 90, ms: 400 },
  ],
};

const ALL_KEYS = Object.keys(GB_CAL_DEFAULTS) as (keyof GrowbotCal)[];

function pick(c: GrowbotCal, keys: (keyof GrowbotCal)[]): Partial<GrowbotCal> {
  return Object.fromEntries(keys.map((k) => [k, c[k]])) as Partial<GrowbotCal>;
}

function clampTrim(n: number): number {
  return Math.max(-TRIM_MAX, Math.min(TRIM_MAX, Math.round(n)));
}

function TrimRow({
  side,
  value,
  onChange,
}: {
  side: "Left" | "Right";
  value: number;
  onChange: (n: number) => void;
}) {
  return (
    <div className="gb-trim">
      <label htmlFor={`gb-trim-${side}`}>{side} trim</label>
      <button
        type="button"
        className="gb-btn"
        aria-label={`${side} trim down one degree`}
        onClick={() => onChange(clampTrim(value - 1))}
      >
        −
      </button>
      <input
        id={`gb-trim-${side}`}
        type="range"
        min={-TRIM_MAX}
        max={TRIM_MAX}
        step={1}
        value={value}
        onChange={(e) => onChange(clampTrim(Number(e.target.value)))}
      />
      <button
        type="button"
        className="gb-btn"
        aria-label={`${side} trim up one degree`}
        onClick={() => onChange(clampTrim(value + 1))}
      >
        +
      </button>
      <output htmlFor={`gb-trim-${side}`} className="gb-mono">
        {value > 0 ? `+${value}` : value}°
      </output>
    </div>
  );
}

function Legs({ robot }: { robot: GrowbotRobot }) {
  const code = robot.code;
  const [cal, setCal] = useState<GrowbotCal | null>(null);
  const [saved, setSaved] = useState(false);
  const [note, setNote] = useState<{ text: string; bad?: boolean } | null>(null);
  const { isArmed, arm } = useArm();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pending = useRef<GrowbotCal | null>(null);
  const dirty = useRef(new Set<keyof GrowbotCal>());
  const chain = useRef<Promise<void>>(Promise.resolve());
  const live = robot.connected && !robot.revoked_at;
  const liveRef = useRef(live);
  liveRef.current = live;

  useEffect(() => {
    let gone = false;
    api.growbotCal(code).then(
      (got) => {
        if (gone) return;
        setCal(got.cal);
        setSaved(got.saved);
      },
      (e: unknown) => !gone && setNote({ text: message(e), bad: true }),
    );
    return () => {
      gone = true;
    };
  }, [code]);

  // A drag that never settled is still saved when the card goes away.
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
      const left = pending.current;
      if (left) void api.saveGrowbotCal(code, pick(left, [...dirty.current])).catch(() => {});
    },
    [code],
  );

  // Saves and moves run one after another, in the order asked: a move right after a swap
  // is made with that swap, and the last click is the last write.
  function queue(job: () => Promise<void>) {
    chain.current = chain.current.then(job, job);
  }

  async function act(steps: GrowbotStep[], what: string) {
    if (!liveRef.current) return;
    try {
      await api.growbotAct(code, { kind: "act", steps });
      setNote({ text: what });
    } catch (e) {
      setNote({ text: message(e), bad: true });
    }
  }

  function send(steps: GrowbotStep[], what: string) {
    queue(() => act(steps, what));
  }

  // Saves only `keys` of `next` (the rest stays each phone's own until set here), says
  // `what`, and with `steps` then moves the legs so it can be judged.
  function save(next: GrowbotCal, keys: (keyof GrowbotCal)[], what: string, steps?: GrowbotStep[]) {
    queue(async () => {
      try {
        const out = await api.saveGrowbotCal(code, pick(next, keys));
        setSaved(out.saved);
      } catch (e) {
        setNote({ text: message(e), bad: true });
        return;
      }
      if (steps && liveRef.current) await act(steps, what);
      else setNote({ text: what });
    });
  }

  // Takes over a slider's unsaved keys, so cancelling its timer never loses them.
  function takePending(): (keyof GrowbotCal)[] {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    pending.current = null;
    const keys = [...dirty.current];
    dirty.current.clear();
    return keys;
  }

  function change(next: GrowbotCal, keys: (keyof GrowbotCal)[], what: string) {
    const held = takePending();
    setCal(next);
    save(next, [...held, ...keys], what);
  }

  // A slider saves once the hand stops; a trim then stands the legs at it to be judged.
  function later(next: GrowbotCal, key: keyof GrowbotCal, what: string, steps?: GrowbotStep[]) {
    setCal(next);
    pending.current = next;
    dirty.current.add(key);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => save(next, takePending(), what, steps), SAVE_AFTER_MS);
  }

  function setTrim(key: "L_OFF" | "R_OFF", n: number) {
    if (!cal) return;
    const next = { ...cal, [key]: n };
    const what = `Saved. Legs at rest, trim left ${next.L_OFF}°, right ${next.R_OFF}°.`;
    later(next, key, what, gbMoves.trim(next));
  }

  if (!cal) {
    return (
      <p className="gb-hint" aria-live="polite">
        {note ? note.text : "Reading its calibration…"}
      </p>
    );
  }
  const imu = cal.IMU_SIGN;
  return (
    <div className="gb-legs">
      <p className="gb-hint">
        {saved
          ? "Saved on the box for this robot: every phone that wears it uses it."
          : "Not set from here yet: each phone uses what it holds. A change saves just that setting for every phone."}
      </p>
      {!live && <p className="gb-hint dim">{OFF_HINT}</p>}

      <h4 className="gb-step">
        <span className="gb-step-n" aria-hidden="true">
          1
        </span>
        Which leg
      </h4>
      <p className="gb-hint">
        Left and right are the robot's own, so its left is on your right when you face its screen.
        Tap each and check which leg moves; ignore which way.
      </p>
      <div className="gb-acts two">
        <button
          type="button"
          className="gb-btn"
          disabled={!live}
          onClick={() => send(gbMoves.left(), "Moved the left leg.")}
        >
          Move left leg
        </button>
        <button
          type="button"
          className="gb-btn"
          disabled={!live}
          onClick={() => send(gbMoves.right(), "Moved the right leg.")}
        >
          Move right leg
        </button>
      </div>
      <label className="gb-switch">
        <input
          type="checkbox"
          checked={cal.SWAP === 1}
          onChange={(e) =>
            change(
              { ...cal, SWAP: e.target.checked ? 1 : 0 },
              ["SWAP"],
              `Left/right swap ${e.target.checked ? "on" : "off"}. Test each leg again.`,
            )
          }
        />
        <span>
          Swap left and right
          <em>Only if Move left moves the right leg. Walking forward needs it off.</em>
        </span>
      </label>

      <h4 className="gb-step">
        <span className="gb-step-n" aria-hidden="true">
          2
        </span>
        Straighten
      </h4>
      <p className="gb-hint">Trim until both legs hang straight down.</p>
      <TrimRow side="Left" value={cal.L_OFF} onChange={(n) => setTrim("L_OFF", n)} />
      <TrimRow side="Right" value={cal.R_OFF} onChange={(n) => setTrim("R_OFF", n)} />

      <h4 className="gb-step">
        <span className="gb-step-n" aria-hidden="true">
          3
        </span>
        Direction
      </h4>
      <p className="gb-hint">
        Each leg should sweep down toward the screen (forward). If it lifts or sweeps backward, flip
        that leg below.
      </p>
      <div className="gb-acts two">
        <button
          type="button"
          className="gb-btn"
          disabled={!live}
          onClick={() => send(gbMoves.sweepL(cal), "Swept the left leg forward.")}
        >
          Sweep left forward
        </button>
        <button
          type="button"
          className="gb-btn"
          disabled={!live}
          onClick={() => send(gbMoves.sweepR(cal), "Swept the right leg forward.")}
        >
          Sweep right forward
        </button>
      </div>

      <details className="gb-adv">
        <summary>Advanced: flip a leg or an IMU axis, walk gain</summary>
        <div className="gb-acts two">
          {(["L_SIGN", "R_SIGN"] as const).map((k) => (
            <button
              key={k}
              type="button"
              className={`gb-btn${cal[k] !== GB_CAL_DEFAULTS[k] ? " armed" : ""}`}
              aria-pressed={cal[k] !== GB_CAL_DEFAULTS[k]}
              onClick={() =>
                change(
                  { ...cal, [k]: cal[k] < 0 ? 1 : -1 },
                  [k],
                  `${k === "L_SIGN" ? "Left" : "Right"} leg sign is now ${cal[k] < 0 ? "+1" : "−1"}.`,
                )
              }
            >
              {k === "L_SIGN" ? "Left" : "Right"} leg {cal[k] < 0 ? "−1" : "+1"}
            </button>
          ))}
        </div>
        <p className="gb-hint">
          The IMU is the phone on the robot. Flip an axis only if a walk leans or splits.
        </p>
        <div className="gb-acts">
          {IMU_AXES.map((axis, i) => {
            const flippedNow = (imu[i] ?? 1) < 0;
            // Lit when it differs from the stock body, so a lit button is a change.
            const changed = imu[i] !== GB_CAL_DEFAULTS.IMU_SIGN[i];
            return (
              <button
                key={axis}
                type="button"
                className={`gb-btn${changed ? " armed" : ""}`}
                aria-pressed={changed}
                onClick={() => {
                  const flipped = [...imu] as GrowbotCal["IMU_SIGN"];
                  flipped[i] = flippedNow ? 1 : -1;
                  change({ ...cal, IMU_SIGN: flipped }, ["IMU_SIGN"], `IMU ${axis} sign flipped.`);
                }}
              >
                IMU {axis} {flippedNow ? "−" : "+"}
              </button>
            );
          })}
        </div>
        <div className="gb-trim">
          <label htmlFor={`gb-gain-${code}`}>Walk gain</label>
          <input
            id={`gb-gain-${code}`}
            type="range"
            min={0}
            max={200}
            step={5}
            value={Math.round(cal.gain * 100)}
            onChange={(e) => {
              const gain = Number(e.target.value) / 100;
              later({ ...cal, gain }, "gain", `Saved. Walk gain ${gain.toFixed(2)}.`);
            }}
          />
          <output htmlFor={`gb-gain-${code}`} className="gb-mono">
            {cal.gain.toFixed(2)}
          </output>
        </div>
      </details>

      <button
        type="button"
        className={`gb-btn block${isArmed(`cal:${code}`) ? " armed" : ""}`}
        onClick={() => {
          if (!arm(`cal:${code}`)) return;
          change({ ...GB_CAL_DEFAULTS }, ALL_KEYS, "Calibration reset to the stock body's.");
        }}
      >
        {isArmed(`cal:${code}`) ? "Tap again to reset" : "Reset calibration"}
      </button>
      {note && (
        <p className={`gb-hint${note.bad ? " gb-bad-t" : ""}`} aria-live="polite">
          {note.text}
        </p>
      )}
    </div>
  );
}

export type GrowbotStopSaid = { text: string; bad: boolean };

export function GrowbotBodyControls({
  robot,
  move,
  onStop,
}: {
  robot: GrowbotRobot;
  /** The Move pane, handed the card's Stop to wear on its pad; without it the card is Legs
   *  alone. */
  move?: (stop: () => Promise<GrowbotStopSaid>) => ReactNode;
  /** Also called by Stop, alongside the chip-direct stop: a creature's walk keeps streaming
   *  poses, so it must be told too. */
  onStop?: () => void;
}) {
  const [pane, setPane] = useState<Pane>(move ? "move" : "legs");
  const [stopNote, setStopNote] = useState<GrowbotStopSaid | null>(null);
  const name = robot.name || robot.code;
  const live = robot.connected && !robot.revoked_at;
  const base = `gb-ctl-${robot.code}`;
  const shown: Pane = move ? pane : "legs";
  const state = !live
    ? { dot: "", word: "Off" }
    : robot.creature
      ? { dot: "ok", word: "Ready" }
      : { dot: "warn", word: "No creature" };

  function stop(): Promise<GrowbotStopSaid> {
    onStop?.();
    return api.growbotAct(robot.code, { kind: "stop" }).then(
      () => ({ text: "Stopped. The legs are limp.", bad: false }),
      (e: unknown) => ({ text: message(e), bad: true }),
    );
  }

  if (robot.revoked_at) return null;
  return (
    <section className="gb-card gb-ctl" aria-label={`Controls for ${name}`}>
      <div className="gb-card-head">
        <h3>Controls for {name}</h3>
        <span className={`gb-state${state.dot === "warn" ? " gb-warn-t" : ""}`}>
          <span className={`gb-dot ${state.dot}`} aria-hidden="true" />
          {state.word}
        </span>
      </div>
      {move && (
        <GrowbotTabs<Pane>
          base={base}
          label={`Controls for ${name}`}
          inner
          tabs={[
            { id: "move", label: "Move" },
            { id: "legs", label: "Legs" },
          ]}
          value={pane}
          onPick={setPane}
        />
      )}
      <div
        id={`${base}-panel`}
        role={move ? "tabpanel" : undefined}
        aria-labelledby={move ? `${base}-tab-${shown}` : undefined}
      >
        {shown === "move" && move ? move(stop) : <Legs robot={robot} />}
      </div>
      {shown === "legs" && (
        <>
          <button
            type="button"
            className="gb-btn danger block gb-stop"
            disabled={!live}
            onClick={() => void stop().then(setStopNote)}
          >
            <StopIcon size={16} />
            Stop
          </button>
          {stopNote && (
            <p className={`gb-hint${stopNote.bad ? " gb-bad-t" : ""}`} aria-live="polite">
              {stopNote.text}
            </p>
          )}
        </>
      )}
    </section>
  );
}
