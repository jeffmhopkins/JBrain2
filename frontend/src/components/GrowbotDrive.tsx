import { useEffect, useRef, useState } from "react";
import {
  ApiError,
  type GrowbotPolicy,
  type GrowbotSkillSet,
  type GrowbotSteer,
  api,
} from "../api/client";
import { GrowbotSteerDial, type SteerAim } from "./GrowbotSteerDial";
import { shortName } from "./growbotNames";

// Continuous steering for a trained walk (owner, 2026-11-14: steer heading, speed and camera
// height as it walks, not only the pad's fixed moves). Drive starts the picked walk for a
// moment under a fresh drive id, then steers that walk (and no other) a couple of times a
// second with the sliders' values; each steer keeps it going 1.5 s past it (growbot-box.js
// gbSteer), so the walk stops soon after the tab stops steering, whether by Stop driving, a
// closed tab or a dropped connection, and a walk that ended on the phone answers 410, which
// ends the drive.
//
// With a skill set (growbot-drive.js, 2026-11-15) the creature picks the skill from the sliders:
// speed 0 stands and turns with the look, ahead walks tall camera first, and back backs away
// nose first still facing ahead, never turning round. The heading is the front camera's.

const EVERY_MS = 500;

export type DriveSaid = { text: string; bad: boolean };

/** A fresh id for one drive, so only the walk it starts takes its steering. */
function driveId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Where the heading points, in words: 0 is the way it faced when the drive began. */
export function headingWords(turn: number): string {
  if (turn === 0) return "straight";
  return `${Math.abs(turn)}° ${turn < 0 ? "left" : "right"}`;
}

/** The skills a set covers, in words: "look, forward and back". */
export function skillWords(set: GrowbotSkillSet): string {
  const names = (["look", "forward", "back"] as const).filter((k) => set[k]);
  return names.length > 1
    ? `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`
    : names.join("");
}

export function GrowbotDrive({
  code,
  policy,
  skills,
  disabled,
  halted,
  onSaid,
  onStop,
  onDriving,
}: {
  code: string;
  /** The pad's picked walk; null when only a skill set can be driven. */
  policy: GrowbotPolicy | null;
  /** The library's skill set, when it covers at least two skills (skillSet). */
  skills?: GrowbotSkillSet | null;
  disabled: boolean;
  /** Bumped by the pad whenever it sends a move of its own, which ends the drive. */
  halted: number;
  onSaid: (said: DriveSaid) => void;
  onStop: () => void;
  /** Told whenever a drive starts or ends, so the remote can stop it on leaving Steer live. */
  onDriving?: (driving: boolean) => void;
}) {
  // The dial's arrow (GrowbotSteerDial), right positive, and Forward | Back.
  const [aim, setAim] = useState<SteerAim>({ angle: 0, length: 0.5 });
  const [back, setBack] = useState(false);
  const [pitch, setPitch] = useState(1);
  const [driving, setDriving] = useState(false);
  // biome-ignore lint/correctness/useExhaustiveDependencies: only a change of driving is news.
  useEffect(() => onDriving?.(driving), [driving]);
  // biome-ignore lint/correctness/useExhaustiveDependencies: on leaving, the drive is over.
  useEffect(() => () => onDriving?.(false), []);
  // A skill set when there is one: it is what the follower will drive.
  const [withSet, setWithSet] = useState(true);
  const set = skills && (withSet || !policy) ? skills : null;
  const speedOn = set ? true : !!policy?.contract.speed;
  const pitchOn = set ? true : !!policy?.contract.pitch;
  // Back backs a speed walk away still facing the arrow's way; pointing the arrow behind turns
  // it round instead.
  const turn = aim.angle;
  const speed = speedOn ? (back ? -aim.length : aim.length) : 1;
  // The creature's heading is left positive.
  const now = useRef({ heading: 0, speed: 0.5, pitch: 1 });
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
      // Also retires a start still on its way, so it never sets a timer for a gone panel.
      gen.current++;
      if (timer.current) clearInterval(timer.current);
    },
    [],
  );
  // A different walk, a robot nobody can drive, or a move from the pad ends the drive.
  // biome-ignore lint/correctness/useExhaustiveDependencies: halt reads only refs and setters.
  useEffect(() => halt(), [policy?.id, set ? JSON.stringify(set) : "", disabled, halted]);

  async function start() {
    setDriving(true);
    const mine = ++gen.current;
    const drive = driveId();
    const live = () => mine === gen.current;
    try {
      // A skill set starts standing (the look); the first steer, a moment later, moves it.
      const out = set
        ? await api.moveGrowbotRobot(code, "look", 2, undefined, undefined, drive, set)
        : policy
          ? await api.moveGrowbotRobot(code, "forward", 2, policy.id, undefined, drive)
          : null;
      if (!out) return;
      if (!live()) return;
      if (!out.ok) {
        halt();
        onSaid({ text: out.why || "It didn't start the walk.", bad: true });
        return;
      }
      onSaid({
        text: set
          ? `Driving the skill set (${skillWords(set)}).`
          : `Driving with ${policy ? shortName(policy.name) : "the walk"}.`,
        bad: false,
      });
      // One steer at a time, so a slow answer never lands an older slider value last.
      let sending = false;
      const tick = () => {
        if (sending || !live()) return;
        sending = true;
        const steer: GrowbotSteer = { drive, ...now.current };
        api
          .steerGrowbotRobot(code, steer)
          .catch((e: unknown) => {
            if (!live()) return;
            halt();
            // A walk that ended on the phone (done, or stopped from elsewhere) is not a fault.
            const ended = e instanceof ApiError && e.status === 410;
            onSaid({ text: message(e), bad: !ended });
          })
          .finally(() => {
            sending = false;
          });
      };
      tick();
      timer.current = setInterval(tick, EVERY_MS);
    } catch (e) {
      if (!live()) return;
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
  // A skill set walks only past growbot-drive.js's SPEED_ON (0.15); below it, it stands and looks.
  const stands = aim.length === 0 || (!!set && aim.length < 0.15);
  const said = speedOn
    ? `${headingWords(turn)}, ${stands ? "standing" : `speed ${speed.toFixed(1)}`}`
    : headingWords(turn);
  return (
    <fieldset className="gb-drive" aria-label="Steer live">
      {skills && policy && (
        <p className="gb-hint gb-steer-with">
          Steering {withSet ? `the skill set (${skillWords(skills)})` : shortName(policy.name)}.{" "}
          <button type="button" className="gb-linkbtn inline" onClick={() => setWithSet(!withSet)}>
            {withSet ? `Steer ${shortName(policy.name)} instead` : "Steer the skill set instead"}
          </button>
        </p>
      )}
      <GrowbotSteerDial
        aim={aim}
        onAim={setAim}
        lengthOn={speedOn}
        label={speedOn ? "Heading and speed" : "Heading"}
        valueText={said}
      />
      <output className="gb-dial-said gb-mono" aria-hidden="true">
        {said}
      </output>
      {speedOn && (
        <fieldset className="gb-seg pick gb-dial-way" aria-label="Which way">
          <button
            type="button"
            className={back ? "" : "on"}
            aria-pressed={!back}
            onClick={() => setBack(false)}
          >
            Forward
          </button>
          <button
            type="button"
            className={back ? "on" : ""}
            aria-pressed={back}
            onClick={() => setBack(true)}
          >
            Back
          </button>
        </fieldset>
      )}
      {pitchOn && (
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
        className={`gb-btn block gb-drive-go${driving ? " danger" : " primary"}`}
        aria-pressed={driving}
        disabled={disabled && !driving}
        onClick={toggle}
      >
        {driving ? "Stop driving" : "Start driving"}
      </button>
    </fieldset>
  );
}
