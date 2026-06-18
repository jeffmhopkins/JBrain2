// The owner's location surface — Devices / Timeline / Map tabs
// (docs/mocks/location-chosen-combined.html). Owner-eyes only: the phones write
// fixes via OwnTracks; this reads the slice back and manages per-device keys.
//
// Wave 5b ships the Devices tab (provision / rotate / revoke, with last-seen /
// battery / connection / fix count); Timeline and Map land in later waves and
// show a placeholder until then. The location domain stays on --steel.

import { useEffect, useState } from "react";
import {
  type DeviceSummary,
  type LocationFix,
  type PlaceGeofence,
  type ProvisionedDevice,
  type TimelineEntry,
  api,
} from "../api/client";
import { Sheet } from "../components/Sheet";
import { LocationMapTab } from "./LocationMapTab";

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
  listTimeline: () => Promise<TimelineEntry[]>;
  listPlaces: () => Promise<PlaceGeofence[]>;
  listFixes: (subjectId: string, since: string, until: string) => Promise<LocationFix[]>;
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
      {tab === "timeline" && <TimelineTab deps={deps} />}
      {tab === "map" && <LocationMapTab deps={deps} />}
    </main>
  );
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

// --- Timeline tab ---------------------------------------------------------

type FeedState =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "done"; entries: TimelineEntry[]; labels: Map<string, string> };

function TimelineTab({ deps }: { deps: LocationDeps | undefined }) {
  const listTimeline = deps?.listTimeline ?? api.listLocationTimeline;
  const listDevices = deps?.listDevices ?? api.listLocationDevices;
  const [state, setState] = useState<FeedState>({ phase: "loading" });

  useEffect(() => {
    let stale = false;
    // The feed names the device (the "who"), so it joins the crossings to the
    // device list for labels — subject ids never read as sentences.
    Promise.all([listTimeline(), listDevices()])
      .then(([entries, devices]) => {
        if (stale) return;
        const labels = new Map(devices.map((d) => [d.id, d.label]));
        setState({ phase: "done", entries, labels });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [listTimeline, listDevices]);

  if (state.phase === "loading") return <p className="analysis-quiet">loading timeline…</p>;
  if (state.phase === "error") {
    return <p className="analysis-quiet">couldn't load the timeline — check the connection.</p>;
  }
  if (state.entries.length === 0) {
    return (
      <p className="analysis-quiet">
        no movement yet — crossings show here once a device enters or leaves a place.
      </p>
    );
  }

  return (
    <div className="loc-feed">
      {groupByDay(state.entries).map((g) => (
        <section key={g.day} className="loc-feed-day">
          <h3 className="loc-feed-head">{g.day}</h3>
          {g.entries.map((e, i) => (
            <div key={`${e.occurred_at}-${e.subject_id}-${i}`} className="loc-feed-row">
              <span className="loc-feed-time">{timeOfDay(e.occurred_at)}</span>
              <span className="loc-feed-text">{sentence(e, state.labels)}</span>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}

/** A crossing as a plain sentence: "Jeff's phone left Office" / "… arrived at
 * Mom's house". The verb carries the meaning — no color codes. */
function sentence(e: TimelineEntry, labels: Map<string, string>): string {
  const who = labels.get(e.subject_id) ?? "A device";
  const verb = e.transition === "enter" ? "arrived at" : "left";
  return `${who} ${verb} ${e.place_name}`;
}

function timeOfDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function groupByDay(entries: TimelineEntry[]): { day: string; entries: TimelineEntry[] }[] {
  // Entries arrive newest-first; consecutive same-day rows fold under one header.
  const groups: { day: string; entries: TimelineEntry[] }[] = [];
  for (const e of entries) {
    const day = dayLabel(e.occurred_at);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.entries.push(e);
    else groups.push({ day, entries: [e] });
  }
  return groups;
}

function dayLabel(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Unknown";
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (sameDay(d, today)) return "Today";
  if (sameDay(d, yesterday)) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" });
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
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

// Exported for direct unit testing: the time buckets and the crossing sentence.
export { relativeTime, sentence };
