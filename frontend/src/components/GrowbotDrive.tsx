import { useEffect, useRef, useState } from "react";
import { type GrowbotPolicy, type GrowbotSteer, api } from "../api/client";

// Continuous steering for a trained walk (owner, 2026-11-14: steer heading, speed and camera
// height as it walks, not only the pad's fixed moves). Drive starts the picked walk for a
// moment, then steers it a couple of times a second with the sliders' values; each steer keeps
// the walk going 1.5 s past it (growbot-box.js gbSteer), so the walk stops soon after the tab
// stops steering, whether by Stop driving, a closed tab or a dropped connection.

const EVERY_MS = 500;

export type DriveSaid = { text: string; bad: boolean };

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Where the heading slider points, in words: 0 is the way it faced when the drive began. */
export function headingWords(turn: number): string {
  if (turn === 0) return "straight on";
  return `${Math.abs(turn)}° ${turn < 0 ? "left" : "right"}`;
}

export function GrowbotDrive({
  code,
  policy,
  disabled,
  halted,
  onSaid,
  onStop,
}: {
  code: string;
  policy: GrowbotPolicy;
  disabled: boolean;
  /** Bumped by the pad whenever it sends a move of its own, which ends the drive. */
  halted: number;
  onSaid: (said: DriveSaid) => void;
  onStop: () => void;
}) {
  // The slider turns right as it goes right; the creature's heading is left positive.
  const [turn, setTurn] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [pitch, setPitch] = useState(1);
  const [driving, setDriving] = useState(false);
  const now = useRef<GrowbotSteer>({ heading: 0, speed: 1, pitch: 1 });
  now.current = { heading: -turn, speed, pitch };
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  // A halt while the walk's start is still on its way must win over that start's answer.
  const gen = useRef(0);

  function halt() {
    gen.current++;
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    setDriving(false);
  }
  useEffect(
    () => () => {
      if (timer.current) clearInterval(timer.current);
    },
    [],
  );
  // A different walk, a robot nobody can drive, or a move from the pad ends the drive.
  // biome-ignore lint/correctness/useExhaustiveDependencies: halt reads only refs and setters.
  useEffect(() => halt(), [policy.id, disabled, halted]);

  async function start() {
    setDriving(true);
    const mine = ++gen.current;
    try {
      const out = await api.moveGrowbotRobot(code, "forward", 2, policy.id);
      if (mine !== gen.current) return;
      if (!out.ok) {
        halt();
        onSaid({ text: out.why || "It didn't start the walk.", bad: true });
        return;
      }
      onSaid({ text: `Driving with ${policy.name}.`, bad: false });
      const tick = () =>
        api.steerGrowbotRobot(code, now.current).catch((e: unknown) => {
          halt();
          onSaid({ text: message(e), bad: true });
        });
      void tick();
      timer.current = setInterval(() => void tick(), EVERY_MS);
    } catch (e) {
      if (mine !== gen.current) return;
      halt();
      onSaid({ text: message(e), bad: true });
    }
  }

  function toggle() {
    if (!driving) {
      void start();
      return;
    }
    halt();
    onStop();
  }

  const id = `gb-drive-${code}`;
  return (
    <fieldset className="gb-drive">
      <legend className="gb-move-h">Drive</legend>
      <div className="gb-trim">
        <label htmlFor={`${id}-turn`}>Heading</label>
        <input
          id={`${id}-turn`}
          type="range"
          min={-180}
          max={180}
          step={5}
          value={turn}
          onChange={(e) => setTurn(Number(e.target.value))}
        />
        <output htmlFor={`${id}-turn`} className="gb-mono">
          {headingWords(turn)}
        </output>
      </div>
      {policy.contract.speed && (
        <div className="gb-trim">
          <label htmlFor={`${id}-speed`}>Speed</label>
          <input
            id={`${id}-speed`}
            type="range"
            min={-1}
            max={1}
            step={0.1}
            value={speed}
            onChange={(e) => setSpeed(Number(e.target.value))}
          />
          <output htmlFor={`${id}-speed`} className="gb-mono">
            {speed.toFixed(1)}
          </output>
        </div>
      )}
      {policy.contract.pitch && (
        <div className="gb-trim">
          <label htmlFor={`${id}-pitch`}>Camera height</label>
          <input
            id={`${id}-pitch`}
            type="range"
            min={-1}
            max={1}
            step={0.1}
            value={pitch}
            onChange={(e) => setPitch(Number(e.target.value))}
          />
          <output htmlFor={`${id}-pitch`} className="gb-mono">
            {pitch.toFixed(1)}
          </output>
        </div>
      )}
      <button
        type="button"
        className={`gb-chip${driving ? " on" : ""}`}
        aria-pressed={driving}
        disabled={disabled && !driving}
        onClick={toggle}
      >
        {driving ? "Stop driving" : "Drive"}
      </button>
    </fieldset>
  );
}
