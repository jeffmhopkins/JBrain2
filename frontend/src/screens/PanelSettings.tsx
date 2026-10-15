// The desk panel's settings, on its card in jpanel → Panels, under Alarms
// (docs/mocks/jpanel-c6/PwaPanelSettings.dc.html, approved by the owner 2026-10-13).
//
// THE PANEL IS THE SOURCE OF TRUTH. Every value here is what the panel last reported on its
// pickup; a change made here is a request queued on the box, which the panel takes on its next
// pickup and reports back. So a changed row says "Waiting for the panel" until that report, and
// only then "On the panel" — and when the panel and this screen change the same setting, the
// panel's last report wins (owner, 2026-10-13): this screen simply shows what it reported.

import { type ReactElement, useCallback, useEffect, useRef, useState } from "react";

import {
  ApiError,
  type PanelSettingKey,
  type PanelSettingValues,
  type PanelSettingsOut,
  api,
} from "../api/client";
import { hm } from "./PanelAlarms";

/** While a change waits, ask again this often: the panel picks up every 3 s. */
const WAITING_POLL_MS = 5_000;
/** Otherwise often enough that "reported 40 s ago" and the offline state stay true. */
const IDLE_POLL_MS = 30_000;
/** A slider sends once it has been still this long, not at every step of a drag. */
const SLIDE_SETTLE_MS = 500;
/** How long a row that landed keeps saying "On the panel". */
const LANDED_MS = 6_000;

const COLOURS = ["#B23A3A", "#A47A3E", "#6E7178"];

/** "40 s", "3 min", "2 h", "3 days". */
export function ago(s: number): string {
  if (s < 60) return `${Math.max(0, Math.round(s))} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 2 * 86400) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}

function minutesOf(value: string): number | null {
  const m = /^(\d{1,2}):(\d{2})/.exec(value);
  if (!m) return null;
  const h = Number(m[1]);
  const mm = Number(m[2]);
  return h < 24 && mm < 60 ? h * 60 + mm : null;
}

type Group = "clock" | "notices" | "screen";

const GROUP_KEYS: Record<Group, PanelSettingKey[]> = {
  clock: ["night_on", "night_from", "night_to", "night_color", "show_date", "tz"],
  notices: ["notif_accept", "notif_quiet", "notif_chime"],
  screen: ["brightness", "volume", "read_aloud", "auto_rotate"],
};

/** The group's one line under its title, from the values shown (the mock's words). */
export function summary(group: Group, v: PanelSettingValues, colours: string[]): string {
  if (group === "clock") {
    if (!v.night_on) return "Night clock off";
    return `Night clock ${hm(v.night_from ?? 0)}–${hm(v.night_to ?? 0)} · ${colours[v.night_color ?? 0] ?? ""}`;
  }
  if (group === "notices") {
    if (!v.notif_accept) return "Not accepting";
    return `Accepting${v.notif_quiet ? " · refused at night" : ""}${v.notif_chime ? " · chime" : " · silent"}`;
  }
  return `Brightness ${v.brightness ?? "—"}% · volume ${v.volume ?? "—"}%`;
}

/** `showNotices` opens the Notices group and brings it into view each time it changes: a notice
 *  the box turned away for a switch here (notices off, night hours) sends the owner straight to
 *  that switch. */
export function PanelSettings({
  deviceId,
  showNotices = 0,
}: { deviceId: string; showNotices?: number }) {
  const [data, setData] = useState<PanelSettingsOut | null>(null);
  const [open, setOpen] = useState<Partial<Record<Group, boolean>>>({});
  const section = useRef<HTMLElement>(null);
  useEffect(() => {
    if (!showNotices) return;
    setOpen((o) => ({ ...o, notices: true }));
    section.current?.scrollIntoView?.({ block: "start", behavior: "smooth" });
  }, [showNotices]);
  const [error, setError] = useState<string | null>(null);
  // What this screen asked for, and whether the panel has since reported it.
  const [asked, setAsked] = useState<PanelSettingValues>({});
  const [landed, setLanded] = useState<Partial<Record<PanelSettingKey, boolean>>>({});
  // A slider's value while it moves, before it is sent.
  const [drafts, setDrafts] = useState<PanelSettingValues>({});
  const timers = useRef<Partial<Record<PanelSettingKey, ReturnType<typeof setTimeout>>>>({});

  const load = useCallback(async (): Promise<void> => {
    try {
      setData(await api.panelSettings(deviceId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not read the panel's settings.");
    }
  }, [deviceId]);

  useEffect(() => {
    void load();
  }, [load]);

  const waiting = data !== null && Object.keys(data.pending).length > 0;
  useEffect(() => {
    if (data === null) return;
    const t = setInterval(() => void load(), waiting ? WAITING_POLL_MS : IDLE_POLL_MS);
    return () => clearInterval(t);
  }, [data, waiting, load]);

  // A request the panel has reported back lands; one it reported something else for is gone
  // (set at the glass meanwhile: the panel's word wins, and the row shows it).
  useEffect(() => {
    if (data === null || data.reported === null) return;
    const done: PanelSettingKey[] = [];
    for (const [k, v] of Object.entries(asked) as [PanelSettingKey, number][]) {
      if (k in data.pending) continue;
      done.push(k);
      if (data.reported[k] === v) setLanded((l) => ({ ...l, [k]: true }));
    }
    if (done.length === 0) return;
    setAsked((a) => {
      const next = { ...a };
      for (const k of done) delete next[k];
      return next;
    });
  }, [data, asked]);

  useEffect(() => {
    if (Object.keys(landed).length === 0) return;
    const t = setTimeout(() => setLanded({}), LANDED_MS);
    return () => clearTimeout(t);
  }, [landed]);

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const t of Object.values(pending)) clearTimeout(t);
    };
  }, []);

  async function send(changes: PanelSettingValues): Promise<void> {
    setError(null);
    setLanded((l) => {
      const next = { ...l };
      for (const k of Object.keys(changes) as PanelSettingKey[]) delete next[k];
      return next;
    });
    try {
      const got = await api.setPanelSettings(deviceId, changes);
      // Remembered with the answer that queued it, never before: against the last poll's data a
      // change not yet queued would read as already decided.
      setAsked((a) => ({ ...a, ...changes }));
      setData(got);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send that to the panel.");
      await load();
    }
  }

  function slide(key: PanelSettingKey, value: number): void {
    setDrafts((d) => ({ ...d, [key]: value }));
    const old = timers.current[key];
    if (old) clearTimeout(old);
    timers.current[key] = setTimeout(() => {
      setDrafts((d) => {
        const next = { ...d };
        delete next[key];
        return next;
      });
      void send({ [key]: value });
    }, SLIDE_SETTLE_MS);
  }

  if (data === null) {
    return (
      <section className="jp-settings" aria-label="Panel settings">
        <div className="jp-alarms-head">
          <span className="jp-alarms-title">Panel settings</span>
        </div>
        {error !== null && <p className="jp-unit-error">{error}</p>}
      </section>
    );
  }

  const reported = data.reported ?? {};
  const shown: PanelSettingValues = { ...reported, ...data.pending, ...drafts };
  const isWaiting = (k: PanelSettingKey) => k in data.pending;
  const since = data.reported_s ?? 0;
  const sync = !data.online
    ? {
        text: "Panel offline",
        tone: "warn",
        sub: data.supported
          ? `Showing what the panel reported ${ago(since)} ago.`
          : "The panel has not reported its settings.",
      }
    : waiting
      ? {
          text: "Sending to panel…",
          tone: "warn",
          sub: `Reported ${ago(since)} ago. A change lands within a minute.`,
        }
      : { text: "On the panel", tone: "ok", sub: `As the panel reported them ${ago(since)} ago.` };
  const lastAt = new Date(Date.now() - since * 1000);
  const lastHm = hm(lastAt.getHours() * 60 + lastAt.getMinutes());

  function state(keys: PanelSettingKey[]) {
    if (keys.some(isWaiting)) {
      return (
        <span className="jp-set-state warn">
          {data?.online ? "Waiting for the panel" : "Waiting — the panel is offline"}
        </span>
      );
    }
    if (keys.some((k) => landed[k])) return <span className="jp-set-state ok">On the panel</span>;
    return null;
  }

  function toggle(key: PanelSettingKey, label: string, sub: string, dim = false) {
    const on = (shown[key] ?? 0) !== 0;
    return (
      <div className="jp-set-row" key={key}>
        <div className={`jp-set-line${dim ? " dim" : ""}`}>
          <span className="jp-set-label">
            <span>{label}</span>
            {sub && <span className="jp-set-sub">{sub}</span>}
          </span>
          <button
            type="button"
            role="switch"
            aria-checked={on}
            aria-label={label}
            className={`jp-switch${on ? " on" : ""}`}
            disabled={dim || reported[key] === undefined}
            onClick={() => void send({ [key]: on ? 0 : 1 })}
          >
            <span className="jp-knob" />
          </button>
        </div>
        {state([key])}
      </div>
    );
  }

  function slider(key: PanelSettingKey, label: string) {
    const value = shown[key] ?? 0;
    return (
      <div className="jp-set-row" key={key}>
        <label className="jp-set-slider">
          <span className="jp-set-slider-head">
            <span>{label}</span>
            <strong>{value}%</strong>
          </span>
          <input
            type="range"
            min={10}
            max={100}
            step={5}
            value={value}
            aria-label={label}
            disabled={reported[key] === undefined}
            onChange={(e) => slide(key, Number(e.target.value))}
          />
        </label>
        {state([key])}
      </div>
    );
  }

  const night = (shown.night_on ?? 0) !== 0;
  const accept = (shown.notif_accept ?? 0) !== 0;
  const groups: { key: Group; title: string; note: string; rows: ReactElement[] }[] = [
    {
      key: "clock",
      title: "Clock",
      note: "The same hours refuse notices at night when that is on. Alarms are above.",
      rows: [
        toggle("night_on", "Night clock", "Dim digits on black at bedtime"),
        <div className="jp-set-row" key="hours">
          <div className={`jp-set-times${night ? "" : " dim"}`}>
            {(["night_from", "night_to"] as const).map((k) => (
              <label key={k}>
                {k === "night_from" ? "From" : "To"}
                <input
                  type="time"
                  className={isWaiting(k) ? "waiting" : ""}
                  value={hm(shown[k] ?? 0)}
                  disabled={reported[k] === undefined}
                  onChange={(e) => {
                    const at = minutesOf(e.target.value);
                    if (at !== null) void send({ [k]: at });
                  }}
                />
              </label>
            ))}
          </div>
          {state(["night_from", "night_to"])}
        </div>,
        <div className="jp-set-row" key="colour">
          <div className={`jp-set-colours${night ? "" : " dim"}`}>
            <span className="jp-set-sub">Night colour</span>
            <div className="jp-set-seg">
              {data.night_colors.map((name, i) => (
                <button
                  key={name}
                  type="button"
                  aria-pressed={shown.night_color === i}
                  className={shown.night_color === i ? "on" : ""}
                  style={{ color: COLOURS[i] }}
                  disabled={reported.night_color === undefined}
                  onClick={() => void send({ night_color: i })}
                >
                  {name}
                </button>
              ))}
            </div>
          </div>
          {state(["night_color"])}
        </div>,
        toggle("show_date", "Day and date under the time", ""),
        <div className="jp-set-row" key="tz">
          <label className="jp-set-line">
            <span>Time zone</span>
            <select
              value={shown.tz ?? 0}
              disabled={reported.tz === undefined}
              onChange={(e) => void send({ tz: Number(e.target.value) })}
            >
              {data.zones.map((z, i) => (
                <option key={z} value={i}>
                  {z}
                </option>
              ))}
            </select>
          </label>
          {state(["tz"])}
        </div>,
      ],
    },
    {
      key: "notices",
      title: "Notices",
      note: "A refused notice is never held: the sender is told at once.",
      rows: [
        toggle("notif_accept", "Accept notices from the box", ""),
        toggle(
          "notif_quiet",
          "Refuse during night hours",
          `${hm(shown.night_from ?? 0)}–${hm(shown.night_to ?? 0)}, the night clock’s hours`,
          !accept,
        ),
        toggle("notif_chime", "Chime when one arrives", "", !accept),
      ],
    },
    {
      key: "screen",
      title: "Screen and sound",
      note: "10% is the floor for both. Microphone gain stays in the debug console.",
      rows: [
        slider("brightness", "Brightness"),
        slider("volume", "Speaker volume"),
        toggle("read_aloud", "Read Jerv’s replies aloud", "Off: text, with Listen on each reply"),
        toggle("auto_rotate", "Auto-rotate", ""),
      ],
    },
  ];

  return (
    <section className="jp-settings" aria-label="Panel settings" ref={section}>
      <div className="jp-alarms-head">
        <span className="jp-alarms-title">Panel settings</span>
        <span className={`jp-set-sync ${sync.tone}`} aria-live="polite">
          <span className="jp-set-dot" aria-hidden="true" />
          {sync.text}
        </span>
      </div>
      <span className="jp-set-subhead">{sync.sub}</span>
      {!data.online && data.supported && (
        <p className="jp-set-offline">
          The panel has not been heard from since {lastHm}. Changes wait on the box and reach it the
          moment it is back.
        </p>
      )}
      {!data.supported ? (
        <p className="jp-unit-hint">
          This panel’s firmware does not report its settings yet. It updates itself, and they appear
          here once it has.
        </p>
      ) : (
        <div className="jp-set-groups">
          {groups.map((g) => {
            const n = GROUP_KEYS[g.key].filter(isWaiting).length;
            const isOpen = open[g.key] === true;
            return (
              <div className="jp-set-group" key={g.key}>
                <button
                  type="button"
                  className="jp-set-group-head"
                  aria-expanded={isOpen}
                  onClick={() => setOpen((o) => ({ ...o, [g.key]: !o[g.key] }))}
                >
                  <span className="jp-set-group-text">
                    <span className="jp-set-group-title">{g.title}</span>
                    <span className="jp-set-group-sum">
                      {summary(g.key, shown, data.night_colors)}
                    </span>
                  </span>
                  {n > 0 && <span className="jp-set-tag">{n} waiting</span>}
                  <span className={`jp-set-chevron${isOpen ? " open" : ""}`} aria-hidden="true">
                    ›
                  </span>
                </button>
                {isOpen && (
                  <div className="jp-set-body">
                    {g.rows}
                    <span className="jp-unit-hint">{g.note}</span>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
      {error !== null && <p className="jp-unit-error">{error}</p>}
    </section>
  );
}
