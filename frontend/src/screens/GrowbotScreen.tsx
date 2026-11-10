// GrowBot — the desk robot's screen (DESIGN.md "GrowBot — the desk robot's screen"; binding mock
// docs/mocks/growbot-flash/b-growbot-screen.html). The owner opens a robot every day and flashes
// one once, so the robots come first with Open GrowBot as each card's biggest button, and
// flashing is a guided sheet behind the pinned foot.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  type GrowbotFlashJob,
  type GrowbotFlashRequest,
  type GrowbotRobot,
  type GrowbotStatus,
  api,
} from "../api/client";
import { GrowbotAddSheet } from "../components/GrowbotAddSheet";
import { GrowbotFail, GrowbotLog, GrowbotPhases } from "../components/GrowbotJob";
import { Sheet } from "../components/Sheet";
import { CERT_URL, certNoted, day, keyMadeAt, noteCert, robotState } from "../growbot";
import { useForeground } from "../visibility";
import "./growbot.css";

const STATUS_POLL_MS = 15_000;
const JOB_POLL_MS = 1_000;
const ARM_MS = 3_000;

type SheetState =
  | { kind: "add"; target: string }
  | { kind: "rekey"; code: string }
  | { kind: "cert"; code: string }
  | null;

interface GrowbotScreenProps {
  /** jpanel's Flash tab handed a Pico over: open straight on Add a robot. */
  openAdd?: boolean;
  onOpenAddConsumed?: () => void;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Arm-then-confirm, as on the panels: the first tap arms for 3 s and the button disarms on
 *  its own, so a stray tap never revokes anything. */
function useArm() {
  const [armed, setArmed] = useState<Record<string, number>>({});
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  useEffect(() => {
    const t = timers.current;
    return () => {
      for (const id of t.values()) clearTimeout(id);
    };
  }, []);
  const isArmed = (key: string) => (armed[key] ?? 0) > Date.now();
  const arm = (key: string): boolean => {
    const prev = timers.current.get(key);
    if (prev) clearTimeout(prev);
    timers.current.delete(key);
    if (isArmed(key)) {
      setArmed(({ [key]: _, ...rest }) => rest);
      return true;
    }
    setArmed((a) => ({ ...a, [key]: Date.now() + ARM_MS }));
    timers.current.set(
      key,
      setTimeout(() => {
        timers.current.delete(key);
        setArmed(({ [key]: _, ...rest }) => rest);
      }, ARM_MS),
    );
    return false;
  };
  return { isArmed, arm };
}

export function GrowbotScreen({ openAdd, onOpenAddConsumed }: GrowbotScreenProps = {}) {
  const [status, setStatus] = useState<GrowbotStatus | null>(null);
  const [error, setError] = useState("");
  const [job, setJob] = useState<GrowbotFlashJob | null>(null);
  const [sheet, setSheet] = useState<SheetState>(null);
  const [toast, setToast] = useState<{ text: string; href?: string } | null>(null);
  const [rekeyError, setRekeyError] = useState("");
  const [rekeyLogOpen, setRekeyLogOpen] = useState(false);
  const { isArmed, arm } = useArm();
  const foreground = useForeground();
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const say = useCallback((text: string, href?: string) => {
    setToast(href ? { text, href } : { text });
    if (toastTimer.current) clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast(null), href ? 10_000 : 4_000);
  }, []);
  useEffect(
    () => () => {
      if (toastTimer.current) clearTimeout(toastTimer.current);
    },
    [],
  );

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.growbotStatus());
      setError("");
    } catch (e) {
      setError(message(e));
    }
  }, []);

  const loadJob = useCallback(async () => {
    try {
      const j = await api.growbotFlashJob();
      setJob(j);
      return j;
    } catch {
      // A missed poll is retried on the next tick; the job runs on the box regardless.
      return null;
    }
  }, []);

  useEffect(() => {
    if (!foreground) return;
    void loadStatus();
    void loadJob();
    const id = setInterval(() => void loadStatus(), STATUS_POLL_MS);
    return () => clearInterval(id);
  }, [foreground, loadStatus, loadJob]);

  // Polled every second only while something is flashing; a finished job re-reads the robots,
  // since a flash adds or rekeys one.
  const running = job?.state === "running";
  useEffect(() => {
    if (!running || !foreground) return;
    const id = setInterval(() => {
      void loadJob().then((j) => {
        if (j && j.state !== "running") void loadStatus();
      });
    }, JOB_POLL_MS);
    return () => clearInterval(id);
  }, [running, foreground, loadJob, loadStatus]);

  useEffect(() => {
    if (!openAdd) return;
    setSheet({ kind: "add", target: "new" });
    onOpenAddConsumed?.();
  }, [openAdd, onOpenAddConsumed]);

  async function startFlash(body: GrowbotFlashRequest): Promise<GrowbotFlashJob | null> {
    const out = await api.startGrowbotFlash(body);
    if (out.job && typeof out.job === "object") {
      setJob(out.job);
      return out.job;
    }
    return loadJob();
  }

  /** Mint a one-time link and open it in a new tab. The tab is opened FIRST, inside the tap,
   *  because a window opened after an await is a popup the phone blocks. */
  function launch(code: string) {
    let tab: Window | null = null;
    try {
      tab = window.open("", "_blank");
    } catch {
      tab = null;
    }
    const robot = status?.robots.find((r) => r.code === code);
    api
      .growbotTicket(code)
      .then(({ url }) => {
        if (tab && !tab.closed) {
          tab.location.href = url;
          const away = robot && !robot.connected;
          say(
            away
              ? `Opened GrowBot. ${robot.name || code} isn't connected right now — its legs move once it is on and reaches the box.`
              : "Opened GrowBot in a new tab. The link works once and is spent.",
          );
        } else {
          say("The phone blocked the new tab — tap here to open GrowBot.", url);
        }
      })
      .catch((e) => {
        tab?.close();
        say(`GrowBot didn't open — ${message(e)}`);
      });
  }

  function openRobot(code: string) {
    if (!certNoted()) {
      setSheet({ kind: "cert", code });
      return;
    }
    setSheet(null);
    launch(code);
  }

  async function rekey(r: GrowbotRobot) {
    if (!arm(`rot:${r.code}`)) return;
    setRekeyError("");
    setRekeyLogOpen(false);
    setSheet({ kind: "rekey", code: r.code });
    try {
      await startFlash({ mode: "rekey", target: r.code });
    } catch (e) {
      setRekeyError(message(e));
    }
  }

  async function revoke(r: GrowbotRobot) {
    if (!arm(`rev:${r.code}`)) return;
    try {
      const out = await api.revokeGrowbotRobot(r.code);
      setStatus((s) =>
        s ? { ...s, robots: s.robots.map((x) => (x.code === out.code ? out : x)) } : s,
      );
      say(
        `${r.name || r.code}'s key is revoked. It disconnects now and can't come back until it is reflashed.`,
      );
    } catch (e) {
      say(`The key wasn't revoked — ${message(e)}`);
    }
  }

  const up = status?.service !== "down";
  const robots = status?.robots ?? [];
  const sheetRobot =
    sheet && sheet.kind !== "add" ? robots.find((r) => r.code === sheet.code) : undefined;

  function watch() {
    if (!job) return;
    if (job.mode === "rekey" && job.code) setSheet({ kind: "rekey", code: job.code });
    else setSheet({ kind: "add", target: "new" });
  }

  return (
    <>
      {status?.service === "down" && (
        <output className="gb-banner">
          growbot is down — no robot can connect. Ops → Services has its log and Restart.
        </output>
      )}
      <main className="screen-body gb-screen">
        {error && (
          <p className="gb-msg bad" role="alert">
            {error}
          </p>
        )}
        {status === null && !error && <p className="gb-hint dim">Loading robots…</p>}
        {robots.map((r) => {
          const s = robotState(r, up);
          const name = r.name || r.code;
          const revoked = !!r.revoked_at;
          return (
            <section key={r.code} className="gb-card" aria-label={name}>
              <div className="gb-card-head">
                <h3>{name}</h3>
                <span className="gb-state">
                  <span className={`gb-dot ${s.level}`} aria-hidden="true" />
                  <span className={`gb-${s.level}-t`}>{s.text}</span>
                </span>
              </div>
              {revoked ? (
                <>
                  <p className="gb-msg bad">
                    <strong>Key revoked.</strong> It can't reach the box until it is reflashed over
                    USB, which gives it a new key.
                  </p>
                  <button
                    type="button"
                    className="gb-btn primary block"
                    onClick={() => setSheet({ kind: "add", target: r.code })}
                  >
                    Reflash {name}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="gb-btn primary block"
                  onClick={() => openRobot(r.code)}
                >
                  Open GrowBot
                </button>
              )}
              <dl className="gb-facts">
                <dt>Pairing code</dt>
                <dd className="gb-mono">{r.code}</dd>
                <dt>Device key</dt>
                <dd>
                  {revoked ? (
                    <span className="gb-bad-t">revoked</span>
                  ) : (
                    `made ${day(keyMadeAt(r))}, only on this robot`
                  )}
                </dd>
              </dl>
              {!revoked && (
                <div className="gb-acts">
                  <button
                    type="button"
                    className={`gb-btn${isArmed(`rot:${r.code}`) ? " armed" : ""}`}
                    onClick={() => void rekey(r)}
                    disabled={running}
                  >
                    {isArmed(`rot:${r.code}`) ? "Tap again for a new key" : "New key"}
                  </button>
                  <button
                    type="button"
                    className={`gb-btn danger${isArmed(`rev:${r.code}`) ? " armed" : ""}`}
                    onClick={() => void revoke(r)}
                  >
                    {isArmed(`rev:${r.code}`) ? "Tap again to revoke" : "Revoke key"}
                  </button>
                </div>
              )}
            </section>
          );
        })}
        {status !== null && robots.length === 0 && (
          <section className="gb-card">
            <h3>No robots yet</h3>
            <p className="gb-hint">Plug the robot's Pico into the box's USB, then add it.</p>
          </section>
        )}
        {robots.length > 0 && !certNoted() && (
          <p className="gb-hint dim">
            The first time you open GrowBot on this phone, it explains the box's certificate.
          </p>
        )}
      </main>
      <div className="gb-foot">
        {running ? (
          <button type="button" className="gb-btn primary block" onClick={watch}>
            Flashing — tap to watch
          </button>
        ) : (
          <button
            type="button"
            className="gb-btn primary block"
            onClick={() => setSheet({ kind: "add", target: "new" })}
          >
            Add a robot
          </button>
        )}
      </div>

      {sheet?.kind === "add" && (
        <GrowbotAddSheet
          robots={robots}
          initialTarget={sheet.target}
          job={job}
          onStart={startFlash}
          onOpen={openRobot}
          onClose={() => setSheet(null)}
        />
      )}

      {sheet?.kind === "rekey" && (
        <Sheet title={`New key — ${sheetRobot?.name || sheet.code}`} onClose={() => setSheet(null)}>
          <p className="gb-hint">
            The box makes a new key and writes it into {sheetRobot?.name || sheet.code} over USB, so
            it has to be on the box's USB now. The old key stops working the moment this finishes.
          </p>
          {rekeyError && (
            <p className="gb-msg bad" role="alert">
              {rekeyError}
            </p>
          )}
          {job && job.mode === "rekey" && job.code === sheet.code && !rekeyError && (
            <>
              <GrowbotPhases job={job} />
              <GrowbotFail job={job} />
              {job.state === "ok" && (
                <p className="gb-msg ok">
                  Done — {sheetRobot?.name || sheet.code} is back with its new key.
                </p>
              )}
              <button
                type="button"
                className="gb-linkbtn"
                onClick={() => setRekeyLogOpen((o) => !o)}
              >
                {rekeyLogOpen || job.state === "fail" ? "Hide the log" : "Show the log"}
              </button>
              {(rekeyLogOpen || job.state === "fail") && <GrowbotLog lines={job.log} />}
            </>
          )}
          <div className="gb-sheet-foot">
            {running && !rekeyError ? (
              <button type="button" className="gb-btn block" disabled>
                Writing the new key…
              </button>
            ) : (
              <button type="button" className="gb-btn block" onClick={() => setSheet(null)}>
                Done
              </button>
            )}
          </div>
        </Sheet>
      )}

      {sheet?.kind === "cert" && (
        <Sheet title="First time on this phone" onClose={() => setSheet(null)}>
          <p className="gb-hint">
            GrowBot opens on the box's own address,{" "}
            <span className="gb-mono">jbrain.local:8444</span>, with the box's certificate. A phone
            that hasn't been told to trust the box shows a warning page first.
          </p>
          <p className="gb-hint">
            Install the box's certificate once and the warning is gone — for GrowBot and every other
            page on the box. The phone needs it for the camera and microphone.
          </p>
          <p className="gb-hint dim">
            After downloading: on iPhone, open Settings → Profile Downloaded, then General → About →
            Certificate Trust Settings. On Android, Settings → Security → Install a certificate → CA
            certificate.
          </p>
          <div className="gb-sheet-foot">
            <a className="gb-btn block" href={CERT_URL} download="jbrain-box-root.crt">
              Download the box's certificate
            </a>
            <button
              type="button"
              className="gb-btn primary block"
              onClick={() => {
                noteCert();
                const code = sheet.code;
                setSheet(null);
                launch(code);
              }}
            >
              Open GrowBot
            </button>
          </div>
        </Sheet>
      )}

      {toast && (
        <output className="gb-toast">
          {toast.href ? (
            <a href={toast.href} target="_blank" rel="noreferrer">
              {toast.text}
            </a>
          ) : (
            toast.text
          )}
        </output>
      )}
    </>
  );
}
