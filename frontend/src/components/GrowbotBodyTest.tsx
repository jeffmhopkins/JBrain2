import { useCallback, useEffect, useRef, useState } from "react";
import {
  type GrowbotBodyTestRun,
  type GrowbotBodyTestVerdict,
  type GrowbotSkillSet,
  api,
} from "../api/client";
import { ago } from "../growbot";
import type { DriveSaid } from "./GrowbotDrive";

// The Body tab's Body test (owner, 2026-11-15; mock docs/mocks/growbot-body-test/screens.html,
// screens 1 and 7). It starts the guided trial on the creature's phone, the one on the body
// (growbot-bodytest.js, sent as a skill set's look with follow "test"), which then talks the
// person through setup and runs the checks on its own screen. Here: a mirror of the run from
// the box (its step, clock and a few numbers, every 2 s while it runs), Stop, and the past runs,
// each a JSON log to download and the number to give Claude for a review.

const LIVE_MS = 2000;
// A run that hasn't posted in this long has lost its phone (closed, asleep, off the Wi-Fi).
const STALE_MS = 20_000;
// After Start, the run shows up once the phone's first chunk lands.
const START_WAIT_MS = 20_000;

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function live(r: GrowbotBodyTestRun, now: number): boolean {
  // A camera test shares the log but not the body: it is no body test under way to mirror or stop.
  if (r.summary.kind === "camera") return false;
  return r.status === "running" && now - Date.parse(r.updated_at) < STALE_MS;
}

function clock(secs = 0): string {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${m < 10 ? "0" : ""}${m}:${s < 10 ? "0" : ""}${s}`;
}

function verdicts(r: GrowbotBodyTestRun): GrowbotBodyTestVerdict[] {
  return Object.values(r.summary.results ?? {});
}

function pastLine(r: GrowbotBodyTestRun): string {
  const v = verdicts(r);
  const warn = v.filter((x) => x.v === "warn").length;
  const fail = v.filter((x) => x.v === "fail").length;
  const said =
    r.status === "running"
      ? r.summary.kind === "camera"
        ? "running"
        : "lost contact"
      : r.status === "done"
        ? fail || warn
          ? [fail && `${fail} failed`, warn && `${warn} to look at`].filter(Boolean).join(", ")
          : "all passed"
        : r.status === "stopped"
          ? `stopped at ${r.summary.label ?? "setup"}`
          : "hit an error";
  const what = r.summary.kind === "camera" ? "camera test · " : "";
  return `${what}${ago(r.started_at)} · ${clock(r.summary.secs)} · ${said}`;
}

export function GrowbotBodyTest({
  code,
  skills,
  disabled,
  onSaid,
}: {
  code: string;
  skills: GrowbotSkillSet;
  disabled: boolean;
  onSaid: (said: DriveSaid) => void;
}) {
  const [runs, setRuns] = useState<GrowbotBodyTestRun[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [waitUntil, setWaitUntil] = useState(0);
  const mounted = useRef(true);
  useEffect(
    () => () => {
      mounted.current = false;
    },
    [],
  );

  const load = useCallback(async () => {
    try {
      const got = await api.growbotBodyTests(code);
      if (mounted.current) setRuns(got);
    } catch {
      // An older box keeps no body tests: the button still works, there's just no list.
      if (mounted.current) setRuns([]);
    }
  }, [code]);

  useEffect(() => {
    void load();
  }, [load]);

  const now = Date.now();
  const running = runs?.find((r) => live(r, now)) ?? null;
  const polling = running !== null || waitUntil > now;
  useEffect(() => {
    if (!polling) return;
    const t = setInterval(() => void load(), LIVE_MS);
    return () => clearInterval(t);
  }, [polling, load]);

  async function start() {
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
        "test",
      );
      if (!mounted.current) return;
      if (!out.ok) {
        onSaid({ text: out.why || "The creature didn't start the test.", bad: true });
        return;
      }
      setWaitUntil(Date.now() + START_WAIT_MS);
      onSaid({ text: "Body test started: follow the steps on the robot's phone.", bad: false });
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
      onSaid(
        out.ok
          ? { text: "Stopped the body test.", bad: false }
          : { text: out.why || "It didn't stop.", bad: true },
      );
      void load();
    } catch (e) {
      if (mounted.current) onSaid({ text: message(e), bad: true });
    }
  }

  const s = running?.summary;
  const past = (runs ?? []).filter((r) => r !== running).slice(0, 5);
  return (
    <div className="gb-drive gb-bodytest">
      {running && s ? (
        <section className="gb-bt-live" aria-label="Body test running">
          <div className="gb-bt-h">
            <span className="gb-t1">
              Body test · {s.phase === "setup" ? "setting up" : "running"}
            </span>
            <span className="gb-tag violet">live</span>
          </div>
          <p className="gb-hint">
            {s.phase === "run" ? `Step ${s.step_n} of ${s.steps}: ` : ""}
            {s.label}
          </p>
          {s.phase === "run" && s.steps ? (
            <div className="gb-bt-bar" aria-hidden="true">
              <i style={{ width: `${Math.round((100 * ((s.step_n ?? 1) - 1)) / s.steps)}%` }} />
            </div>
          ) : null}
          <dl className="gb-bt-grid">
            <dt>clock</dt>
            <dd>{clock(s.secs)}</dd>
            {s.ask ? (
              <>
                <dt>asking</dt>
                <dd>{s.ask}</dd>
              </>
            ) : null}
            <dt>skill</dt>
            <dd>{s.mode || "still"}</dd>
            <dt>heading</dt>
            <dd>{s.heading == null ? "–" : `${Math.round(s.heading)}°`}</dd>
            <dt>face</dt>
            <dd>
              {s.face && s.face.dist != null
                ? `${s.face.dist.toFixed(2)} m · ${Math.round(s.face.bearing ?? 0)}°`
                : "none"}
            </dd>
            <dt>falls</dt>
            <dd>{s.falls ?? 0}</dd>
          </dl>
          <button type="button" className="gb-chip gb-bt-stop" onClick={() => void stop()}>
            Stop test
          </button>
        </section>
      ) : (
        <>
          <button
            type="button"
            className="gb-btn primary block gb-bt-go"
            disabled={disabled || busy}
            onClick={() => void start()}
          >
            {waitUntil > now ? "Starting on the robot's phone…" : "Start a body test"}
          </button>
          <p className="gb-hint dim">
            Walks you through a scripted trial on the robot's phone and saves every reading to the
            box. With only the one phone, tap Body test on the creature's wake screen instead.
          </p>
        </>
      )}
      {past.length > 0 && <div className="gb-move-h">Past runs</div>}
      {past.length > 0 && (
        <ul className="gb-bt-past" aria-label="Past body tests">
          {past.map((r) => (
            <li key={r.number}>
              <span className="gb-bt-n">#{r.number}</span>
              <span className="gb-bt-what">{pastLine(r)}</span>
              <a
                href={`/api/growbot/bodytests/${r.number}`}
                download={`body-test-${r.number}.json`}
              >
                log
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
