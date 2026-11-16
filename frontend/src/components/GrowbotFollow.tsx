import { useEffect, useRef, useState } from "react";
import { type GrowbotFollow as FollowMove, type GrowbotSkillSet, api } from "../api/client";
import type { DriveSaid } from "./GrowbotDrive";

// The face follower from the Body tab (growbot-follow.js, docs/plans/GROWBOT_POLICY_DRIVE_PLAN.md
// wave P3). Follow me locks onto the nearest face the creature's front camera sees and keeps the
// distance it was at; Dry run does the same with the legs left alone and shows the command as an
// arrow on the creature's screen, which faces the person, so the frame and the distances can be
// checked before anything walks. Measure 1 m and Flip frames set that phone's camera, so none of
// it needs a terminal. Stopping sends a plain stop move, which ends a follow or a dry run.

export type FollowRunning = "walk" | "dry" | null;

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// The body test is GrowbotBodyTest's own move.
const SAID: Record<Exclude<FollowMove, "test">, string> = {
  walk: "Following the nearest face. Say “stop stop” or tap Stop following to end it.",
  dry: "Dry run: the arrow on the creature's screen shows which way it would go.",
  calibrate: "Measuring: stand 1 m from the camera, facing it.",
  flip: "Flipped which way the camera's frames run.",
};

export function GrowbotFollow({
  code,
  skills,
  disabled,
  onSaid,
  running,
  setRunning,
}: {
  code: string;
  skills: GrowbotSkillSet;
  disabled: boolean;
  onSaid: (said: DriveSaid) => void;
  /** Held by the remote, so a follow outlives its sheet closing and its row can say so. */
  running: FollowRunning;
  setRunning: (r: FollowRunning) => void;
}) {
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );

  async function send(what: Exclude<FollowMove, "test">) {
    setBusy(true);
    try {
      const out = await api.moveGrowbotRobot(
        code,
        "look",
        0,
        undefined,
        undefined,
        undefined,
        skills,
        what,
      );
      if (!mounted.current) return;
      if (!out.ok) {
        onSaid({ text: out.why || "The creature didn't start it.", bad: true });
        return;
      }
      if (what === "walk" || what === "dry") setRunning(what);
      onSaid({ text: SAID[what], bad: false });
    } catch (e) {
      if (mounted.current) onSaid({ text: message(e), bad: true });
    } finally {
      if (mounted.current) setBusy(false);
    }
  }

  async function stop() {
    try {
      const out = await api.moveGrowbotRobot(code, "stop");
      if (!mounted.current) return;
      if (!out.ok) {
        onSaid({ text: out.why || "It didn't stop.", bad: true });
        return;
      }
      // Only once it stopped: a failed stop leaves the button to try again.
      setRunning(null);
      onSaid({ text: "Stopped following.", bad: false });
    } catch (e) {
      if (mounted.current) onSaid({ text: message(e), bad: true });
    }
  }

  const off = disabled || busy;
  return (
    <div className="gb-follow">
      <p className="gb-hint">
        It locks onto the nearest face the front camera sees and keeps the distance it was at. A dry
        run leaves the legs alone and shows the way it would go as an arrow on its screen.
      </p>
      <div className="gb-acts two">
        {running ? (
          <button
            type="button"
            className="gb-btn danger block"
            aria-pressed
            onClick={() => void stop()}
          >
            {running === "dry" ? "Stop dry run" : "Stop following"}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="gb-btn primary"
              disabled={off}
              onClick={() => void send("walk")}
            >
              Follow me
            </button>
            <button
              type="button"
              className="gb-btn"
              disabled={off}
              onClick={() => void send("dry")}
            >
              Dry run
            </button>
          </>
        )}
      </div>
      <div className="gb-move-h">Camera setup</div>
      <div className="gb-acts two">
        <button
          type="button"
          className="gb-btn"
          disabled={off}
          onClick={() => void send("calibrate")}
        >
          Measure 1 m
        </button>
        <button type="button" className="gb-btn" disabled={off} onClick={() => void send("flip")}>
          Flip frames
        </button>
      </div>
    </div>
  );
}
