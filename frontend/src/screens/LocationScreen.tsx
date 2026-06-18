// The owner's location surface — Devices / Timeline / Map tabs
// (docs/mocks/location-chosen-combined.html). Owner-eyes only: the phones write
// fixes via OwnTracks; this reads the slice back and manages per-device keys.
//
// Wave 5b ships the Devices tab (provision / rotate / revoke, with last-seen /
// battery / connection / fix count); Timeline and Map land in later waves and
// show a placeholder until then. The location domain stays on --steel.

import { useEffect, useState } from "react";
import { type DeviceSummary, type ProvisionedDevice, api } from "../api/client";
import { Sheet } from "../components/Sheet";

type Tab = "devices" | "timeline" | "map";

const TAB_LABEL: Record<Tab, string> = {
  devices: "Devices",
  timeline: "Timeline",
  map: "Map",
};

export interface LocationDeps {
  listDevices: () => Promise<DeviceSummary[]>;
  provisionDevice: (label: string) => Promise<ProvisionedDevice>;
  rotateDevice: (id: string) => Promise<string>;
  revokeDevice: (id: string) => Promise<void>;
}

interface LocationScreenProps {
  /** Injectable for tests; defaults to the live API client. */
  deps?: LocationDeps;
}

export function LocationScreen({ deps }: LocationScreenProps) {
  // Devices is the functional landing tab in 5b; Timeline becomes the default
  // once its feed ships (per the chosen mock).
  const [tab, setTab] = useState<Tab>("devices");

  return (
    <main className="screen-body location-screen">
      <div className="seg-row" role="tablist" aria-label="Location views">
        {(Object.keys(TAB_LABEL) as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            className={`seg${tab === t ? " seg-on" : ""}`}
            onClick={() => setTab(t)}
          >
            {TAB_LABEL[t]}
          </button>
        ))}
      </div>

      {tab === "devices" && <DevicesTab deps={deps} />}
      {tab === "timeline" && <ComingSoon what="The timeline feed" />}
      {tab === "map" && <ComingSoon what="The map" />}
    </main>
  );
}

function ComingSoon({ what }: { what: string }) {
  return <p className="analysis-quiet loc-soon">{what} arrives in a later wave.</p>;
}

// --- Devices tab ----------------------------------------------------------

type State =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "done"; devices: DeviceSummary[] };

// A flow that reveals a freshly minted key exactly once (provision or rotate).
type KeyReveal = { label: string; key: string };

function DevicesTab({ deps }: { deps: LocationDeps | undefined }) {
  const list = deps?.listDevices ?? api.listLocationDevices;
  const provision = deps?.provisionDevice ?? api.provisionDevice;
  const rotate = deps?.rotateDevice ?? api.rotateDevice;
  const revoke = deps?.revokeDevice ?? api.revokeDevice;

  const [state, setState] = useState<State>({ phase: "loading" });
  const [adding, setAdding] = useState(false);
  const [reveal, setReveal] = useState<KeyReveal | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState<DeviceSummary | null>(null);

  async function refresh(): Promise<void> {
    try {
      setState({ phase: "done", devices: await list() });
    } catch {
      setState({ phase: "error" });
    }
  }

  useEffect(() => {
    let stale = false;
    list()
      .then((devices) => {
        if (!stale) setState({ phase: "done", devices });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [list]);

  async function doRotate(d: DeviceSummary): Promise<void> {
    const key = await rotate(d.id);
    setReveal({ label: d.label, key });
    await refresh();
  }

  async function doRevoke(d: DeviceSummary): Promise<void> {
    await revoke(d.id);
    setConfirmRevoke(null);
    await refresh();
  }

  return (
    <>
      <button type="button" className="list-new" onClick={() => setAdding(true)}>
        ＋ Add device
      </button>

      {state.phase === "loading" && <p className="analysis-quiet">loading devices…</p>}
      {state.phase === "error" && (
        <p className="analysis-quiet">couldn't load devices — check the connection.</p>
      )}
      {state.phase === "done" && state.devices.length === 0 && (
        <p className="analysis-quiet">no devices yet — add one to start tracking a phone.</p>
      )}

      {state.phase === "done" && state.devices.length > 0 && (
        <div className="loc-card-list">
          {state.devices.map((d) => (
            <DeviceCard
              key={d.id}
              device={d}
              onRotate={() => void doRotate(d)}
              onRevoke={() => setConfirmRevoke(d)}
            />
          ))}
        </div>
      )}

      {adding && (
        <AddDeviceSheet
          provision={provision}
          onClose={() => setAdding(false)}
          onProvisioned={(r) => {
            setAdding(false);
            setReveal(r);
            void refresh();
          }}
        />
      )}

      {reveal && <KeyRevealSheet reveal={reveal} onClose={() => setReveal(null)} />}

      {confirmRevoke && (
        <Sheet title={`Revoke ${confirmRevoke.label}?`} onClose={() => setConfirmRevoke(null)}>
          <p className="loc-sheet-note">
            Its key stops working immediately — the phone can no longer post fixes. Stored history
            is kept. You can add it back later with a new key.
          </p>
          <div className="loc-sheet-actions">
            <button type="button" className="ghost" onClick={() => setConfirmRevoke(null)}>
              Cancel
            </button>
            <button
              type="button"
              className="btn-destructive"
              onClick={() => void doRevoke(confirmRevoke)}
            >
              Revoke
            </button>
          </div>
        </Sheet>
      )}
    </>
  );
}

function DeviceCard({
  device,
  onRotate,
  onRevoke,
}: {
  device: DeviceSummary;
  onRotate: () => void;
  onRevoke: () => void;
}) {
  return (
    <div className={`loc-card${device.revoked ? " loc-card-revoked" : ""}`}>
      <div className="loc-card-head">
        <span className="loc-card-name">{device.label}</span>
        {device.revoked && <span className="loc-badge">revoked</span>}
      </div>
      <div className="loc-card-meta">{deviceStatus(device)}</div>
      <div className="loc-card-actions">
        {!device.revoked && (
          <button type="button" className="ghost" onClick={onRotate}>
            Rotate key
          </button>
        )}
        {!device.revoked && (
          <button type="button" className="ghost loc-danger" onClick={onRevoke}>
            Revoke
          </button>
        )}
      </div>
    </div>
  );
}

function AddDeviceSheet({
  provision,
  onClose,
  onProvisioned,
}: {
  provision: (label: string) => Promise<ProvisionedDevice>;
  onClose: () => void;
  onProvisioned: (reveal: KeyReveal) => void;
}) {
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  async function submit(): Promise<void> {
    const l = label.trim();
    if (!l || busy) return;
    setBusy(true);
    setFailed(false);
    try {
      const provisioned = await provision(l);
      onProvisioned({ label: l, key: provisioned.key });
    } catch {
      setFailed(true);
      setBusy(false);
    }
  }

  return (
    <Sheet title="Add device" onClose={onClose}>
      <input
        // biome-ignore lint/a11y/noAutofocus: a deliberately-summoned sheet form
        autoFocus
        aria-label="Device name"
        placeholder="device name (e.g. Jeff's phone)…"
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void submit();
        }}
      />
      {failed && <p className="loc-sheet-error">couldn't add the device — try again.</p>}
      <div className="loc-sheet-actions">
        <button type="button" className="ghost" onClick={onClose}>
          Cancel
        </button>
        <button
          type="button"
          className="primary"
          disabled={!label.trim() || busy}
          onClick={() => void submit()}
        >
          {busy ? "Adding…" : "Add device"}
        </button>
      </div>
    </Sheet>
  );
}

function KeyRevealSheet({ reveal, onClose }: { reveal: KeyReveal; onClose: () => void }) {
  const url = `${window.location.origin}/api/owntracks`;
  return (
    <Sheet title={`Key for ${reveal.label}`} onClose={onClose}>
      <p className="loc-sheet-warn">
        Shown once. Copy it into OwnTracks now — it can't be retrieved again. If you lose it, rotate
        the key.
      </p>
      <KeyLine label="Key" value={reveal.key} />
      <p className="loc-sheet-note">Configure OwnTracks in HTTP mode:</p>
      <KeyLine label="URL" value={url} />
      <KeyLine label="Username" value="any (ignored)" />
      <KeyLine label="Password" value={reveal.key} />
      <div className="loc-sheet-actions">
        <button type="button" className="primary" onClick={onClose}>
          Done
        </button>
      </div>
    </Sheet>
  );
}

function KeyLine({ label, value }: { label: string; value: string }) {
  return (
    <div className="loc-keyline">
      <span className="loc-keyline-label">{label}</span>
      <code className="loc-keyline-value">{value}</code>
    </div>
  );
}

// --- formatting -----------------------------------------------------------

function deviceStatus(d: DeviceSummary): string {
  if (d.last_seen === null) {
    return d.revoked ? "revoked — never reported" : "no fixes yet";
  }
  const parts = [`last seen ${relativeTime(d.last_seen)}`];
  if (d.battery_pct !== null) parts.push(`${d.battery_pct}% battery`);
  if (d.connection) parts.push(d.connection);
  parts.push(`${d.fix_count.toLocaleString()} ${d.fix_count === 1 ? "fix" : "fixes"}`);
  return parts.join(" · ");
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown";
  const delta = Date.now() - then;
  if (delta < MINUTE) return "just now";
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)}m ago`;
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h ago`;
  if (delta < 7 * DAY) return `${Math.floor(delta / DAY)}d ago`;
  return new Date(iso).toLocaleDateString();
}

// `relativeTime` is exported for direct unit testing of the time buckets.
export { relativeTime };
