// The owner's location surface — Phones / Timeline / Map tabs. Owner-eyes only:
// the family's phones write fixes via the JBrain360 app; this reads the slice
// back and manages the paired phones.
//
// The Phones tab (docs/mocks/phone-management/b-swipe-rail.html) is a swipe-rail
// list with an Active / Revoked filter: swipe a phone left for re-pair · rename ·
// revoke · delete. "Re-pair" rolls a fresh pairing code (rotating the phone's key
// when it redeems) — the only way to rotate a paired phone's credential. The
// location domain stays on --steel.

import { QRCodeSVG } from "qrcode.react";
import { type TouchEvent, useEffect, useRef, useState } from "react";
import {
  type DeviceSummary,
  type LocationDigest,
  type LocationFix,
  type PairingCode,
  type PlaceGeofence,
  type TimelineEntry,
  api,
} from "../api/client";
import { Sheet } from "../components/Sheet";
import { PencilIcon, RefreshIcon, TrashIcon, XIcon } from "../components/icons";
import { type Drag, RAIL_WIDTH, beginDrag, endDrag, moveDrag } from "../notes/swipe";
import { LocationDigestPanel } from "./LocationDigestPanel";
import { LocationMapTab, type PlaceNoteInput } from "./LocationMapTab";
import { travelingSpeedMph } from "./speed";

type Tab = "devices" | "timeline" | "map";

const TAB_LABEL: Record<Tab, string> = {
  devices: "Phones",
  timeline: "Timeline",
  map: "Map",
};

export interface LocationDeps {
  listDevices: () => Promise<DeviceSummary[]>;
  mintPairingCode: (label: string, monitoring?: number, deviceId?: string) => Promise<PairingCode>;
  renameDevice: (id: string, label: string) => Promise<void>;
  revokeDevice: (id: string) => Promise<void>;
  deleteDevice: (id: string) => Promise<void>;
  listTimeline: () => Promise<TimelineEntry[]>;
  listPlaces: () => Promise<PlaceGeofence[]>;
  listFixes: (subjectId: string, since: string, until: string) => Promise<LocationFix[]>;
  filePlaceNote: (place: PlaceNoteInput) => Promise<void>;
  reverseGeocode: (lat: number, lon: number) => Promise<string | null>;
  loadDigest: (period: "week" | "night") => Promise<LocationDigest>;
}

interface LocationScreenProps {
  /** Injectable for tests; defaults to the live API client. */
  deps?: LocationDeps;
}

export function LocationScreen({ deps }: LocationScreenProps) {
  // The map is the landing tab — it's the surface the owner reaches for most;
  // Devices/Timeline are a tap away.
  const [tab, setTab] = useState<Tab>("map");

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
      {tab === "map" && (
        <>
          {/* L7a: the place digest sits inline ABOVE the map (no extra tab). */}
          <LocationDigestPanel deps={deps ? { loadDigest: deps.loadDigest } : undefined} />
          <LocationMapTab deps={deps} />
        </>
      )}
    </main>
  );
}

// --- Phones tab -----------------------------------------------------------

type State =
  | { phase: "loading" }
  | { phase: "error" }
  | { phase: "done"; devices: DeviceSummary[] };

type DeviceFilter = "active" | "revoked";

function DevicesTab({ deps }: { deps: LocationDeps | undefined }) {
  const list = deps?.listDevices ?? api.listLocationDevices;
  const mintCode = deps?.mintPairingCode ?? api.mintPairingCode;
  const rename = deps?.renameDevice ?? api.renameDevice;
  const revoke = deps?.revokeDevice ?? api.revokeDevice;
  const remove = deps?.deleteDevice ?? api.deleteDevice;

  const [state, setState] = useState<State>({ phase: "loading" });
  const [filter, setFilter] = useState<DeviceFilter>("active");
  const [pairing, setPairing] = useState(false);
  const [pairCode, setPairCode] = useState<PairingCode | null>(null);
  // Only one row's swipe rail is open at a time.
  const [openRailId, setOpenRailId] = useState<string | null>(null);

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

  // Re-pair / restore: mint a code BOUND to this phone — redeeming it rotates the
  // phone's key in place. This is the one credential-rotation path for a phone.
  async function doRepair(d: DeviceSummary): Promise<void> {
    setOpenRailId(null);
    setPairCode(await mintCode(d.label, 1, d.id));
    await refresh();
  }
  async function doRename(id: string, label: string): Promise<void> {
    await rename(id, label);
    await refresh();
  }
  async function doRevoke(id: string): Promise<void> {
    await revoke(id);
    await refresh();
  }
  async function doDelete(id: string): Promise<void> {
    await remove(id);
    await refresh();
  }

  const devices = state.phase === "done" ? state.devices : [];
  const active = devices.filter((d) => !d.revoked);
  const revoked = devices.filter((d) => d.revoked);
  const shown = filter === "active" ? active : revoked;

  return (
    <>
      <button type="button" className="list-new" onClick={() => setPairing(true)}>
        ＋ Pair a phone
      </button>

      {state.phase === "loading" && <p className="analysis-quiet">loading phones…</p>}
      {state.phase === "error" && (
        <p className="analysis-quiet">couldn't load phones — check the connection.</p>
      )}

      {state.phase === "done" && devices.length === 0 && (
        <p className="analysis-quiet">no phones yet — pair one to start tracking.</p>
      )}

      {state.phase === "done" && devices.length > 0 && (
        <>
          <div className="loc-filter" role="tablist" aria-label="Phone status">
            <button
              type="button"
              role="tab"
              aria-selected={filter === "active"}
              className={`loc-filter-chip${filter === "active" ? " on" : ""}`}
              onClick={() => {
                setFilter("active");
                setOpenRailId(null);
              }}
            >
              Active <span className="loc-filter-count">{active.length}</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={filter === "revoked"}
              className={`loc-filter-chip${filter === "revoked" ? " on" : ""}`}
              onClick={() => {
                setFilter("revoked");
                setOpenRailId(null);
              }}
            >
              Revoked <span className="loc-filter-count">{revoked.length}</span>
            </button>
          </div>

          <p className="loc-swipe-hint">Swipe a phone left for actions.</p>

          {shown.length === 0 ? (
            <p className="analysis-quiet">
              {filter === "active" ? "no active phones." : "no revoked phones."}
            </p>
          ) : (
            <div className="loc-phone-list">
              {shown.map((d) => (
                <PhoneRow
                  key={d.id}
                  device={d}
                  railOpen={openRailId === d.id}
                  onRailChange={(open) => setOpenRailId(open ? d.id : null)}
                  onRepair={() => void doRepair(d)}
                  onRename={(label) => void doRename(d.id, label)}
                  onRevoke={() => void doRevoke(d.id)}
                  onDelete={() => void doDelete(d.id)}
                />
              ))}
            </div>
          )}
        </>
      )}

      {pairing && (
        <PairPhoneSheet
          mintCode={mintCode}
          onClose={() => setPairing(false)}
          onCreated={(c) => {
            setPairing(false);
            setPairCode(c);
            void refresh();
          }}
        />
      )}

      {pairCode && <PairCodeSheet code={pairCode} onClose={() => setPairCode(null)} />}
    </>
  );
}

// One phone as a swipe-left rail row (reuses the home-note / chats swipe paradigm
// and its rail buttons). Tapping a closed row also opens the rail, so the actions
// are reachable without the gesture. Active rows carry re-pair · rename · revoke ·
// delete; a revoked row carries only restore · delete.
function PhoneRow({
  device,
  railOpen,
  onRailChange,
  onRepair,
  onRename,
  onRevoke,
  onDelete,
}: {
  device: DeviceSummary;
  railOpen: boolean;
  onRailChange: (open: boolean) => void;
  onRepair: () => void;
  onRename: (label: string) => void;
  onRevoke: () => void;
  onDelete: () => void;
}) {
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragged = useRef(false);
  const renameRef = useRef<HTMLInputElement>(null);
  const [arming, setArming] = useState<"revoke" | "delete" | null>(null);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(device.label);

  // Closing the rail disarms any pending tap-again confirm.
  useEffect(() => {
    if (!railOpen) setArming(null);
  }, [railOpen]);
  useEffect(() => {
    if (renaming) renameRef.current?.focus();
  }, [renaming]);

  const dragging = drag !== null && drag.axis === "h";
  const offset = renaming ? 0 : dragging ? drag.offset : railOpen ? -RAIL_WIDTH : 0;

  function onTouchStart(event: TouchEvent): void {
    if (renaming) return;
    event.stopPropagation();
    dragged.current = false;
    const t = event.touches[0];
    if (t) setDrag(beginDrag(t.clientX, t.clientY, railOpen));
  }
  function onTouchMove(event: TouchEvent): void {
    if (drag === null) return;
    event.stopPropagation();
    const t = event.touches[0];
    if (!t) return;
    const next = moveDrag(drag, t.clientX, t.clientY);
    if (next.axis === "v") {
      setDrag(null);
      return;
    }
    setDrag(next);
  }
  function onTouchEnd(event: TouchEvent): void {
    if (drag === null) return;
    event.stopPropagation();
    if (drag.axis === "h") {
      dragged.current = true;
      onRailChange(endDrag(drag));
    }
    setDrag(null);
  }

  function onTap(): void {
    if (dragged.current) {
      dragged.current = false;
      return;
    }
    onRailChange(!railOpen);
  }

  function submitRename(): void {
    const label = draft.trim();
    setRenaming(false);
    onRailChange(false);
    if (label && label !== device.label) onRename(label);
  }

  return (
    <div className={`loc-phone-wrap${device.revoked ? " loc-phone-revoked" : ""}`}>
      {!renaming && offset < 0 && (
        <div className={`loc-phone-rail ${device.revoked ? "rail-2" : "rail-4"}`}>
          <button
            type="button"
            className="rail-btn rail-repair"
            onClick={() => {
              onRailChange(false);
              onRepair();
            }}
          >
            <RefreshIcon size={18} />
            {device.revoked ? "restore" : "re-pair"}
          </button>
          {!device.revoked && (
            <button
              type="button"
              className="rail-btn rail-edit"
              onClick={() => {
                setDraft(device.label);
                setRenaming(true);
              }}
            >
              <PencilIcon size={18} />
              rename
            </button>
          )}
          {!device.revoked && (
            <button
              type="button"
              className={`rail-btn rail-revoke${arming === "revoke" ? " rail-armed" : ""}`}
              onClick={() => {
                if (arming !== "revoke") {
                  setArming("revoke");
                  return;
                }
                onRailChange(false);
                onRevoke();
              }}
            >
              {arming === "revoke" ? (
                "tap again"
              ) : (
                <>
                  <XIcon size={18} />
                  revoke
                </>
              )}
            </button>
          )}
          <button
            type="button"
            className={`rail-btn rail-delete${arming === "delete" ? " rail-armed" : ""}`}
            onClick={() => {
              if (arming !== "delete") {
                setArming("delete");
                return;
              }
              onRailChange(false);
              onDelete();
            }}
          >
            {arming === "delete" ? (
              "tap again"
            ) : (
              <>
                <TrashIcon size={18} />
                delete
              </>
            )}
          </button>
        </div>
      )}
      <div
        className="loc-phone-slide"
        style={{ transform: `translateX(${offset}px)` }}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        {renaming ? (
          <input
            ref={renameRef}
            className="loc-phone-rename"
            aria-label="Phone name"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onBlur={submitRename}
            onKeyDown={(e) => {
              if (e.key === "Enter") submitRename();
              if (e.key === "Escape") {
                setRenaming(false);
                onRailChange(false);
              }
            }}
          />
        ) : (
          <button type="button" className="loc-phone-face" onClick={onTap}>
            <span className="loc-phone-name">
              {device.label}
              {device.revoked && <span className="loc-badge">revoked</span>}
            </span>
            <span className="loc-phone-meta">{deviceStatus(device)}</span>
          </button>
        )}
      </div>
    </div>
  );
}

// --- Pair a phone (JBrain360 app) -----------------------------------------

function PairPhoneSheet({
  mintCode,
  onClose,
  onCreated,
}: {
  mintCode: (label: string) => Promise<PairingCode>;
  onClose: () => void;
  onCreated: (code: PairingCode) => void;
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
      onCreated(await mintCode(l));
    } catch {
      setFailed(true);
      setBusy(false);
    }
  }

  return (
    <Sheet title="Pair a phone" onClose={onClose}>
      <p className="loc-sheet-note">
        Creates a one-time code for the JBrain360 app. Open the app on the phone and scan or paste
        it — no setup needed.
      </p>
      <input
        // biome-ignore lint/a11y/noAutofocus: a deliberately-summoned sheet form
        autoFocus
        aria-label="Phone name"
        placeholder="phone name (e.g. Jeff's phone)…"
        value={label}
        onChange={(e) => setLabel(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void submit();
        }}
      />
      {failed && <p className="loc-sheet-error">couldn't create the code — try again.</p>}
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
          {busy ? "Creating…" : "Create code"}
        </button>
      </div>
    </Sheet>
  );
}

function PairCodeSheet({ code, onClose }: { code: PairingCode; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <Sheet title="Scan to pair" onClose={onClose}>
      <p className="loc-sheet-note">
        In the JBrain360 app, scan this code — or copy it and paste. It carries the server, so the
        phone needs no setup. One-time use.
      </p>
      {/* QR must stay high-contrast black-on-white to scan in any theme. */}
      <div className="loc-qr">
        <QRCodeSVG value={code.payload} size={208} bgColor="#ffffff" fgColor="#000000" />
      </div>
      <code className="loc-pair-payload">{code.payload}</code>
      <div className="loc-sheet-actions">
        <button
          type="button"
          className="ghost"
          onClick={() => {
            void navigator.clipboard?.writeText(code.payload);
            setCopied(true);
          }}
        >
          {copied ? "Copied" : "Copy code"}
        </button>
        <button type="button" className="primary" onClick={onClose}>
          Done
        </button>
      </div>
    </Sheet>
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
  const speed = travelingSpeedMph(d.velocity_mps);
  if (speed !== null) parts.push(speed);
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
