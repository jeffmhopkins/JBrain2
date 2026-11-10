// Add a robot: the four-step sheet behind the GrowBot screen's foot button (DESIGN.md "GrowBot
// — the desk robot's screen"; binding mock docs/mocks/growbot-flash/b-growbot-screen.html).
// One primary action per step: plug it in → its Wi-Fi → flashing → paired. A flash is done
// once per robot, so it is guided rather than a form; the job itself lives on the box, so
// closing the sheet mid-flash loses nothing and the screen's foot reopens it.

import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  type GrowbotFlashJob,
  type GrowbotFlashNetwork,
  type GrowbotFlashRequest,
  type GrowbotFlashSetup,
  type GrowbotRobot,
  api,
} from "../api/client";
import { day } from "../growbot";
import { GrowbotFail, GrowbotLog, GrowbotPhases } from "./GrowbotJob";
import { Sheet } from "./Sheet";

interface Net {
  ssid: string;
  remembered: boolean;
  on: boolean;
  password?: string;
  remember?: boolean;
}

interface GrowbotAddSheetProps {
  robots: GrowbotRobot[];
  /** "new", or the pairing code of the robot to reflash. */
  initialTarget: string;
  /** The box's flash job, as the screen last polled it. */
  job: GrowbotFlashJob | null;
  /** Starts a flash and resolves with the job it became; throws the box's refusal. */
  onStart: (body: GrowbotFlashRequest) => Promise<GrowbotFlashJob | null>;
  onOpen: (code: string) => void;
  onClose: () => void;
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;
// wifi_store's own limits on the robot: a password it would refuse must not be flashed.
const PASSWORD = /^[\x20-\x7e]{8,63}$/;
const SSID_BYTES = 32;

function ssidOk(ssid: string): boolean {
  const n = new TextEncoder().encode(ssid).length;
  return n >= 1 && n <= SSID_BYTES && !ssid.includes("\0");
}

/** Why the box can't flash anything yet, in the order the owner would fix them. */
function setupRefusal(s: GrowbotFlashSetup): string | null {
  if (!s.flasher)
    return "No flasher on this box — the endpoint service isn't configured, so nothing on its USB can be flashed.";
  if (!s.lan)
    return "GrowBot needs the box's LAN site — without it there is no jbrain.local:8444 for a robot to reach.";
  if (!s.ca)
    return "The box has no certificate a robot could check, so it won't flash one that would dial in unverified. The LAN site makes it on its first start.";
  if (!s.micropython.ready)
    return `MicroPython ${s.micropython.version} isn't on the box yet — run Ops → Update, then rescan.`;
  if (!s.firmware_ready)
    return "The robot's firmware isn't on the box yet — run Ops → Update, then rescan.";
  return null;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function GrowbotAddSheet({
  robots,
  initialTarget,
  job,
  onStart,
  onOpen,
  onClose,
}: GrowbotAddSheetProps) {
  // A flash already running when the sheet opens is one the owner closed mid-way: back to it.
  const resuming = job?.state === "running" && job.mode === "flash";
  const [step, setStep] = useState(resuming ? 3 : 1);
  const [ours, setOurs] = useState<string | null>(resuming && job ? job.id : null);

  const [setup, setSetup] = useState<GrowbotFlashSetup | null>(null);
  const [setupError, setSetupError] = useState("");
  const [scanning, setScanning] = useState(false);

  const [target, setTarget] = useState(initialTarget);
  const [name, setName] = useState("");
  const [reinstall, setReinstall] = useState(false);
  const [nets, setNets] = useState<Net[] | null>(null);
  const [adding, setAdding] = useState(false);
  const [newSsid, setNewSsid] = useState("");
  const [newPass, setNewPass] = useState("");
  const [remember, setRemember] = useState(true);
  const [boxHost, setBoxHost] = useState("");

  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState("");
  const [lastBody, setLastBody] = useState<GrowbotFlashRequest | null>(null);
  const [logOpen, setLogOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  const scan = useCallback(async () => {
    setScanning(true);
    setSetupError("");
    try {
      const s = await api.growbotFlashSetup();
      setSetup(s);
      // Seeded once: a rescan for the cable must not undo the owner's network choices.
      setNets(
        (prev) =>
          prev ?? s.networks.map((n, i) => ({ ssid: n.ssid, remembered: true, on: i === 0 })),
      );
    } catch (e) {
      setSetupError(message(e));
    } finally {
      setScanning(false);
    }
  }, []);

  useEffect(() => {
    void scan();
  }, [scan]);

  const mine = job && ours === job.id ? job : null;
  useEffect(() => {
    if (step === 3 && mine?.state === "ok") setStep(4);
  }, [step, mine?.state]);

  const reflashing = robots.find((r) => r.code === target) ?? null;
  const refusal = setup ? setupRefusal(setup) : null;
  const usb = setup?.usb ?? [];
  const hasMicropythonOnly = usb.length > 0 && usb.every((u) => u.kind === "micropython");
  const chosen = (nets ?? []).filter((n) => n.on);
  const hostNeeded = setup !== null && setup.box_host === null;
  const hostOk = !hostNeeded || IPV4.test(boxHost.trim());

  const whyNot = !setup
    ? ""
    : refusal
      ? ""
      : usb.length === 0
        ? "Plug the robot into the box first."
        : target === "new" && !name.trim()
          ? "Give it a name — it's how you tell robots apart."
          : "";
  const step1Ok =
    setup !== null && !refusal && usb.length > 0 && (target !== "new" || !!name.trim());

  function body(): GrowbotFlashRequest {
    const networks: GrowbotFlashNetwork[] = chosen.map((n) =>
      n.remembered
        ? { ssid: n.ssid }
        : { ssid: n.ssid, password: n.password ?? "", remember: n.remember === true },
    );
    const b: GrowbotFlashRequest = { mode: "flash", target, networks };
    if (target === "new") b.name = name.trim();
    if (reinstall && hasMicropythonOnly) b.reinstall = true;
    if (hostNeeded) b.box_host = boxHost.trim();
    return b;
  }

  async function start(b: GrowbotFlashRequest) {
    setStarting(true);
    setStartError("");
    setLogOpen(false);
    try {
      const started = await onStart(b);
      setLastBody(b);
      setOurs(started?.id ?? null);
      setStep(3);
    } catch (e) {
      // The box's refusal is a recovery sentence (no LAN CA, nothing on USB, a bad password).
      setStartError(e instanceof ApiError || e instanceof Error ? e.message : String(e));
      if (step === 3) setStep(2);
    } finally {
      setStarting(false);
    }
  }

  function addNetwork() {
    const ssid = newSsid.trim();
    if (!ssidOk(ssid) || !PASSWORD.test(newPass)) return;
    setNets((prev) => [
      ...(prev ?? []).filter((n) => n.ssid !== ssid),
      { ssid, remembered: false, on: true, password: newPass, remember },
    ]);
    setAdding(false);
    setNewSsid("");
    setNewPass("");
    setRemember(true);
  }

  async function copyCode(code: string) {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard blocked: the code is on screen, big, to read off instead.
    }
  }

  const progress = <p className="gb-progress">Step {step} of 4</p>;

  if (step === 1) {
    return (
      <Sheet title="Plug it into the box" onClose={onClose}>
        {progress}
        <p className="gb-hint">
          The robot's Pico goes into the <strong>box's</strong> USB, with a data cable. A new one
          needs no button press.
        </p>
        {setupError && <p className="gb-msg bad">{setupError}</p>}
        {refusal && <p className="gb-msg bad">{refusal}</p>}
        {setup === null && !setupError && <p className="gb-hint dim">Looking at the box's USB…</p>}
        {setup !== null && !refusal && usb.length === 0 && (
          <p className="gb-msg warn">
            Nothing on the box's USB. Plug the robot's Pico into the box with a data cable, not a
            charge-only one, then rescan.
          </p>
        )}
        {usb.length > 0 && (
          <div className="gb-choices">
            {usb.map((u) => (
              <div key={`${u.kind}-${u.port ?? "boot"}`} className="gb-choice sel">
                <span className="gb-tx">
                  <span className="gb-mono">{u.port ?? u.label}</span>
                  <em>
                    {u.kind === "boot"
                      ? "A new Pico — MicroPython goes on first."
                      : "A Pico already running MicroPython."}
                  </em>
                </span>
                <span className="gb-tag">Pico 2 W</span>
              </div>
            ))}
          </div>
        )}
        <button
          type="button"
          className="gb-btn gb-rescan"
          onClick={() => void scan()}
          disabled={scanning}
        >
          {scanning ? "Looking…" : "Rescan USB"}
        </button>

        <fieldset className="gb-choices">
          <legend>Which robot is this</legend>
          <label className={`gb-choice${target === "new" ? " sel" : ""}`}>
            <input
              type="radio"
              name="gb-target"
              checked={target === "new"}
              onChange={() => setTarget("new")}
            />
            <span className="gb-tx">
              A new robot
              <em>It gets its own device key and pairing code.</em>
            </span>
          </label>
          {robots.map((r) => (
            <label key={r.code} className={`gb-choice${target === r.code ? " sel" : ""}`}>
              <input
                type="radio"
                name="gb-target"
                checked={target === r.code}
                onChange={() => setTarget(r.code)}
              />
              <span className="gb-tx">
                Reflash {r.name || r.code}
                <em>Same pairing code, new device key — the old key stops working.</em>
              </span>
            </label>
          ))}
        </fieldset>
        {target === "new" && (
          <label className="gb-field">
            Its name
            <input
              value={name}
              maxLength={40}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Quorra"
            />
          </label>
        )}
        {hasMicropythonOnly && (
          <label className={`gb-choice${reinstall ? " sel" : ""}`}>
            <input
              type="checkbox"
              checked={reinstall}
              onChange={(e) => setReinstall(e.target.checked)}
            />
            <span className="gb-tx">
              Reinstall MicroPython too
              <em>Only for a robot that won't start. Otherwise just its own files are replaced.</em>
            </span>
          </label>
        )}
        <div className="gb-sheet-foot">
          <button
            type="button"
            className="gb-btn primary block"
            onClick={() => setStep(2)}
            disabled={!step1Ok}
          >
            Next
          </button>
          {whyNot && <p className="gb-hint dim">{whyNot}</p>}
        </div>
      </Sheet>
    );
  }

  if (step === 2) {
    const passOk = PASSWORD.test(newPass);
    return (
      <Sheet title="Wi-Fi it should join" onClose={onClose}>
        {progress}
        <fieldset className="gb-choices">
          <legend>Networks it can join — it tries them in this order</legend>
          {(nets ?? []).map((n, i) => (
            <label key={n.ssid} className={`gb-choice${n.on ? " sel" : ""}`}>
              <input
                type="checkbox"
                checked={n.on}
                onChange={() =>
                  setNets((prev) => (prev ?? []).map((x, j) => (j === i ? { ...x, on: !x.on } : x)))
                }
              />
              <span className="gb-tx">
                {n.ssid}
                <em>
                  {n.remembered
                    ? "Remembered on the box"
                    : n.remember
                      ? "Remembered on the box after this flash"
                      : "This flash only — not remembered"}
                </em>
              </span>
            </label>
          ))}
        </fieldset>
        {adding ? (
          <div className="gb-add-net">
            <label className="gb-field">
              Network name
              <input
                value={newSsid}
                onChange={(e) => setNewSsid(e.target.value)}
                autoCapitalize="none"
              />
            </label>
            <label className="gb-field">
              Password
              <input type="password" value={newPass} onChange={(e) => setNewPass(e.target.value)} />
            </label>
            {newPass && !passOk && (
              <p className="gb-hint dim">
                A Wi-Fi password is 8 to 63 ordinary characters — the robot can't use this one.
              </p>
            )}
            <label className={`gb-choice${remember ? " sel" : ""}`}>
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
              />
              <span className="gb-tx">
                Remember it on the box
                <em>So the next robot or panel can use it without retyping.</em>
              </span>
            </label>
            <div className="gb-acts">
              <button
                type="button"
                className="gb-btn primary"
                onClick={addNetwork}
                disabled={!ssidOk(newSsid.trim()) || !passOk}
              >
                Add network
              </button>
              <button type="button" className="gb-btn" onClick={() => setAdding(false)}>
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button type="button" className="gb-btn gb-add" onClick={() => setAdding(true)}>
            Add another network
          </button>
        )}
        {chosen.length === 0 && (
          <p className="gb-msg warn">
            Pick at least one — a robot with no network can't reach the box.
          </p>
        )}
        {hostNeeded ? (
          <label className="gb-field">
            The box's address on your network
            <input
              value={boxHost}
              onChange={(e) => setBoxHost(e.target.value)}
              inputMode="decimal"
              placeholder="e.g. 192.168.1.20"
            />
          </label>
        ) : null}
        <p className="gb-hint dim">
          2.4 GHz only — the Pico has no 5 GHz radio.{" "}
          {setup?.box_host ? (
            <>
              It reaches the box at <span className="gb-mono">{setup.box_host}</span>; if the box's
              address ever changes, the robot needs reflashing over USB.
            </>
          ) : (
            "The box couldn't tell its own address, so it is written in here — if it ever changes, the robot needs reflashing over USB."
          )}
        </p>
        {startError && (
          <p className="gb-msg bad" role="alert">
            {startError}
          </p>
        )}
        <div className="gb-sheet-foot">
          <button
            type="button"
            className="gb-btn primary block"
            onClick={() => void start(body())}
            disabled={chosen.length === 0 || !hostOk || starting}
          >
            {starting ? "Starting…" : "Flash GrowBot"}
          </button>
          <button type="button" className="gb-btn block" onClick={() => setStep(1)}>
            Back
          </button>
        </div>
      </Sheet>
    );
  }

  if (step === 3) {
    const failed = mine?.state === "fail";
    const showLog = logOpen || failed;
    return (
      <Sheet title={failed ? "It didn't finish" : "Flashing"} onClose={onClose}>
        {progress}
        {!failed && <p className="gb-hint">Keep it plugged in. This takes about a minute.</p>}
        {mine ? <GrowbotPhases job={mine} /> : <p className="gb-hint dim">Starting…</p>}
        {mine && <GrowbotFail job={mine} />}
        {startError && (
          <p className="gb-msg bad" role="alert">
            {startError}
          </p>
        )}
        <button type="button" className="gb-linkbtn" onClick={() => setLogOpen((o) => !o)}>
          {showLog ? "Hide the log" : "Show the log"}
        </button>
        {showLog && mine && <GrowbotLog lines={mine.log} />}
        <div className="gb-sheet-foot">
          {failed ? (
            <>
              <button
                type="button"
                className="gb-btn primary block"
                disabled={starting || lastBody === null}
                onClick={() => {
                  if (!lastBody) return;
                  // A retry never reinstalls: whatever loaded the first time is on it now.
                  const { reinstall: _, ...again } = lastBody;
                  void start(again);
                }}
              >
                Try again
              </button>
              {mine?.fail?.code === "wrong_robot" ? (
                <button type="button" className="gb-btn block" onClick={() => setStep(1)}>
                  Back to which robot
                </button>
              ) : (
                <button type="button" className="gb-btn block" onClick={() => setStep(2)}>
                  Back to Wi-Fi
                </button>
              )}
            </>
          ) : (
            <button type="button" className="gb-btn block" disabled>
              Flashing…
            </button>
          )}
        </div>
      </Sheet>
    );
  }

  const code = mine?.code ?? reflashing?.code ?? "";
  const who = mine?.name || reflashing?.name || code;
  return (
    <Sheet title={`${who} is paired`} onClose={onClose}>
      {progress}
      <p className="gb-msg ok">Done — {who} joined Wi-Fi and reached the box.</p>
      <div className="gb-pair">
        <span className="gb-code">
          <span className="gb-code-lbl">Pairing code</span>
          {code}
        </span>
        <button type="button" className="gb-btn" onClick={() => void copyCode(code)}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p className="gb-hint dim">
        It's printed on the robot's page too. Open GrowBot below and it's already filled in.
      </p>
      <p className="gb-hint">
        Device key made {day(mine?.finished_at ?? new Date().toISOString())} at flash time. Only
        this robot has it.
      </p>
      <div className="gb-sheet-foot">
        <button type="button" className="gb-btn primary block" onClick={() => onOpen(code)}>
          Open GrowBot
        </button>
        <button type="button" className="gb-btn block" onClick={onClose}>
          Done
        </button>
      </div>
    </Sheet>
  );
}
