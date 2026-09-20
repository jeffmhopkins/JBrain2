// Flashing a room-endpoint panel — its own surface, not a card inside Ops.
//
// It began as an Ops card and did not belong there. Ops is a dashboard you SCAN: rows of
// services, a memory graph, a status you read at a glance. This is a form you WORK, with
// a device on the end of a cable and a several-step sequence to get through, and folding
// it into a scrolling dashboard made both worse — the first screenshot on a phone had the
// status line sitting behind the buttons and a radio floating away from its own label.
//
// The panel is also the only thing here that talks to hardware the owner can hold, so it
// earns a door of its own.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type EndpointFirmware,
  type EndpointPort,
  type FlashRequest,
  api,
} from "../api/client";
import "./endpoints.css";

interface EndpointsScreenProps {
  onClose: () => void;
}

/** A numbered step, because this is a sequence and the owner is holding a board. */
function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <section className="ep-step">
      <h2>
        <span className="ep-step-n">{n}</span>
        {title}
      </h2>
      {children}
    </section>
  );
}

export function EndpointsScreen({ onClose }: EndpointsScreenProps) {
  const [ports, setPorts] = useState<EndpointPort[] | null>(null);
  const [firmware, setFirmware] = useState<EndpointFirmware | null>(null);
  const [absent, setAbsent] = useState(false);
  const [error, setError] = useState("");
  const [scanning, setScanning] = useState(false);

  const [port, setPort] = useState("");
  const [ssid, setSsid] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [erase, setErase] = useState(false);
  const [remember, setRemember] = useState(true);

  const [log, setLog] = useState<string[]>([]);
  const [flashing, setFlashing] = useState(false);

  const [watchLog, setWatchLog] = useState<string[]>([]);
  const [watching, setWatching] = useState(false);
  const [restartFirst, setRestartFirst] = useState(true);
  const watchAbort = useRef<AbortController | null>(null);

  const rescan = useCallback(async () => {
    setScanning(true);
    setError("");
    // Fetched INDEPENDENTLY of the firmware status. The port list is the primary
    // diagnostic — it is how the owner tells "not plugged in" from "the box cannot see
    // it" — and pairing the two calls meant one failing hid the other.
    try {
      const p = await api.getEndpointPorts();
      setPorts(p.ports);
      setAbsent(false);
      const panels = p.ports.filter((x) => x.is_espressif);
      // Preselect only when there is exactly one panel: picking for them when there are
      // two is how a bootloader lands on the wrong twin's unit.
      if (panels.length === 1 && panels[0]) setPort(panels[0].device);
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) setAbsent(true);
      else setError(e instanceof Error ? e.message : String(e));
      setPorts([]);
    } finally {
      setScanning(false);
    }

    try {
      setFirmware(await api.getEndpointFirmware());
    } catch {
      // Best-effort: not knowing the version must never cost the owner the port list.
      setFirmware(null);
    }
  }, []);

  useEffect(() => {
    void rescan();
  }, [rescan]);

  const flash = async () => {
    setFlashing(true);
    setLog([]);
    setError("");
    try {
      const body: FlashRequest = { port, ssid, password, erase, remember };
      if (name) body.name = name;
      for await (const line of api.flashEndpoint(body)) {
        setLog((prev) => [...prev, line]);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFlashing(false);
    }
  };

  const watch = async () => {
    watchAbort.current?.abort();
    const ctrl = new AbortController();
    watchAbort.current = ctrl;
    setWatching(true);
    setWatchLog([]);
    setError("");
    try {
      for await (const line of api.monitorEndpoint(
        port,
        { seconds: 180, reset: restartFirst },
        ctrl.signal,
      )) {
        // Capped in the UI as well as the sidecar: a panel in a boot loop produces lines
        // faster than anyone reads them, and the newest ones are the ones that matter.
        setWatchLog((prev) => (prev.length > 800 ? [...prev.slice(-600), line] : [...prev, line]));
      }
    } catch (e) {
      if (!ctrl.signal.aborted) setError(e instanceof Error ? e.message : String(e));
    } finally {
      setWatching(false);
    }
  };

  const stopWatching = () => {
    watchAbort.current?.abort();
    setWatching(false);
  };

  useEffect(() => () => watchAbort.current?.abort(), []);

  const panels = (ports ?? []).filter((p) => p.is_espressif);
  const ready = Boolean(port && ssid) && !flashing;
  const done = log.length > 0 && log[log.length - 1] === "OK";
  const failed = log.some((l) => l.startsWith("FAILED"));

  return (
    <div className="ep-wrap">
      <header className="ep-bar">
        <button type="button" onClick={onClose}>
          Back
        </button>
        <h1>Room endpoints</h1>
      </header>

      {absent ? (
        <p className="ep-empty">
          No panel flasher on this box — <code>JBRAIN_ENDPOINT_URL</code> is empty.
        </p>
      ) : (
        <div className="ep-body">
          <Step n={1} title="Plug a panel into the box">
            <p className="ep-hint">
              Use a data cable, not a charge-only one. The panel's single USB-C port is the chip's
              own USB.
            </p>
            <button
              type="button"
              className="ep-primary"
              onClick={() => void rescan()}
              disabled={scanning}
            >
              {scanning ? "Scanning…" : "Rescan USB"}
            </button>

            {ports !== null && ports.length === 0 && (
              <p className="ep-warn">
                Nothing on USB. Either no panel is plugged in, or the flasher cannot see it.
              </p>
            )}

            {ports !== null && ports.length > 0 && (
              <ul className="ep-ports">
                {ports.map((p) => (
                  <li key={p.device}>
                    <label className={port === p.device ? "sel" : ""}>
                      <input
                        type="radio"
                        name="ep-port"
                        checked={port === p.device}
                        onChange={() => setPort(p.device)}
                      />
                      <span className="ep-port-text">
                        <code>{p.device}</code>
                        <em>{p.label}</em>
                      </span>
                      {p.is_espressif && <span className="ep-badge">panel</span>}
                    </label>
                  </li>
                ))}
              </ul>
            )}
            {panels.length > 1 && (
              <p className="ep-hint">Two panels are connected — pick the one to flash.</p>
            )}
          </Step>

          <Step n={2} title="Wi-Fi for this panel">
            <p className="ep-hint">
              2.4 GHz only — this radio has no 5 GHz. Written into the panel at flash time, along
              with its own device key and this box's certificate.
            </p>
            <label className="ep-field">
              Network name
              <input value={ssid} onChange={(e) => setSsid(e.target.value)} autoCapitalize="none" />
            </label>
            <label className="ep-field">
              Password
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            <label className="ep-check">
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
              />
              <span>
                Remember this network on the box
                <em>
                  So a panel can be re-flashed later without a phone and this password retyped —
                  including remotely, from the owner debug console. The box stores it; untick if you
                  would rather type it every time.
                </em>
              </span>
            </label>

            <label className="ep-field">
              Which panel (optional)
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. left"
              />
            </label>
          </Step>

          <Step n={3} title="Flash">
            <p className="ep-hint">
              {firmware
                ? `Firmware ${firmware.version}, already on the box — nothing to fetch or download.`
                : "This box has no firmware built into its checkout. Run Ops → Update, then rescan."}
            </p>

            <label className="ep-check">
              <input type="checkbox" checked={erase} onChange={(e) => setErase(e.target.checked)} />
              <span>
                Erase first (recovery)
                <em>
                  For a panel that will not boot: hold BOOT while plugging it in, then flash with
                  this ticked.
                </em>
              </span>
            </label>

            <button
              type="button"
              className="ep-primary"
              onClick={() => void flash()}
              disabled={!ready}
            >
              {flashing ? "Flashing…" : erase ? "Erase and flash" : "Flash panel"}
            </button>
            {!port && <p className="ep-hint">Pick a port above first.</p>}
            {port && !ssid && (
              <p className="ep-hint">A panel with no network can never be updated again.</p>
            )}

            {done && <p className="ep-ok">Done. The panel reboots into the new firmware.</p>}
            {failed && <p className="ep-warn">Flash failed — the log below says where.</p>}
            {log.length > 0 && <pre className="ep-log">{log.join("\n")}</pre>}
          </Step>

          <Step n={4} title="Watch the panel">
            <p className="ep-hint">
              The panel's own console, over the same USB. Everything else here shows what the box
              saw; this is the only view from the panel's side — Wi-Fi, TLS, and whether it decides
              to take an update.
            </p>

            <label className="ep-check">
              <input
                type="checkbox"
                checked={restartFirst}
                onChange={(e) => setRestartFirst(e.target.checked)}
                disabled={watching}
              />
              <span>
                Restart it first
                <em>
                  A panel only checks for firmware every 15 minutes. Restarting makes the whole boot
                  — network, box, update decision — happen in the first few seconds.
                </em>
              </span>
            </label>

            <button
              type="button"
              className="ep-primary"
              onClick={() => (watching ? stopWatching() : void watch())}
              disabled={!port || flashing}
            >
              {watching ? "Stop watching" : "Watch console"}
            </button>
            {!port && <p className="ep-hint">Pick a port above first.</p>}

            {watchLog.length > 0 && <pre className="ep-log">{watchLog.join("\n")}</pre>}
          </Step>
        </div>
      )}

      {error && (
        <p className="ep-warn" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
