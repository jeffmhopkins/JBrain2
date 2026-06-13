import { type ReactNode, useEffect, useMemo, useState } from "react";
import type { AppointmentRef } from "../agent/types";
import { type AppointmentOut, api } from "../api/client";
import { Sheet } from "../components/Sheet";

// The owner's read-only calendar over the appointments projection: Day / Week /
// Month / Tasks (docs/mocks/appointments-calendar-views.html). Appointments are
// derived from notes (#7), so this surface never writes — changes go through the
// agent. Recurrence shows a ↻ but isn't expanded into instances here (the ICS
// feed delegates expansion to the subscriber's calendar app).

type View = "day" | "week" | "month" | "tasks";
const VIEWS: View[] = ["day", "week", "month", "tasks"];
const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MON = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
const DAY_START = 7;
const DAY_END = 20;
const PXH = 48;

interface Ev {
  id: string;
  title: string;
  domain: string;
  start: Date;
  end: Date | null;
  allDay: boolean;
  status: string;
  location: string | null;
  recurring: boolean;
  rrule: string | null;
  attendees: string[];
  sourceNoteId: string | null;
}

function toEv(a: AppointmentOut): Ev {
  return {
    id: a.id,
    title: a.title,
    domain: a.domain,
    start: new Date(a.start),
    end: a.end ? new Date(a.end) : null,
    allDay: a.all_day,
    status: a.status,
    location: a.location,
    recurring: a.recurring,
    rrule: a.rrule,
    attendees: a.attendees,
    sourceNoteId: a.source_note_id,
  };
}

const sameDay = (a: Date, b: Date) =>
  a.getFullYear() === b.getFullYear() &&
  a.getMonth() === b.getMonth() &&
  a.getDate() === b.getDate();
const mins = (d: Date) => d.getHours() * 60 + d.getMinutes();
const startOfWeek = (d: Date) => {
  const x = new Date(d);
  x.setDate(x.getDate() - x.getDay());
  x.setHours(0, 0, 0, 0);
  return x;
};
function fmtT(d: Date): string {
  let h = d.getHours();
  const m = d.getMinutes();
  const ap = h < 12 ? "AM" : "PM";
  h = h % 12 || 12;
  return `${h}${m ? `:${String(m).padStart(2, "0")}` : ""} ${ap}`;
}
// Local-zone parts for the reschedule <input>s (the value attributes want the
// owner's wall-clock date/time, not the UTC the ISO string would give).
const pad2 = (n: number) => String(n).padStart(2, "0");
const isoDate = (d: Date) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const isoTime = (d: Date) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const domClass = (e: Ev) =>
  `dom-${["general", "health", "finance", "location"].includes(e.domain) ? e.domain : "general"}`;
// Total accessors — the project's noUncheckedIndexedAccess types a fixed-array
// lookup as possibly-undefined even when the index is in range by construction.
const monFull = (i: number) => MON[i] ?? "";
const mon3 = (i: number) => monFull(i).slice(0, 3);
const dow = (i: number) => DOW[i] ?? "";
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** "in 3 days" / "tomorrow" / "in 2 hr" / "now" / "yesterday" / "3 days ago". */
function relTime(start: Date, now: Date): string {
  const ms = +start - +now;
  const past = ms < 0;
  const abs = Math.abs(ms);
  const min = Math.round(abs / 60000);
  const hr = Math.round(abs / 3600000);
  const day = Math.round(abs / 86400000);
  let q: string;
  if (min < 1) return "now";
  if (min < 60) q = `${min} min`;
  else if (hr < 24) q = `${hr} hr`;
  else if (day === 1) return past ? "yesterday" : "tomorrow";
  else if (day < 14) q = `${day} days`;
  else q = `${Math.round(day / 7)} wk`;
  return past ? `${q} ago` : `in ${q}`;
}

/** "1 hr" / "45 min" / "1 hr 30 min" — null when there's no end. */
function durationText(start: Date, end: Date | null): string | null {
  if (!end) return null;
  const min = Math.round((+end - +start) / 60000);
  if (min <= 0) return null;
  const h = Math.floor(min / 60);
  const m = min % 60;
  if (h === 0) return `${m} min`;
  return m === 0 ? `${h} hr` : `${h} hr ${m} min`;
}

const RRULE_DAYS: Record<string, string> = {
  MO: "Mon",
  TU: "Tue",
  WE: "Wed",
  TH: "Thu",
  FR: "Fri",
  SA: "Sat",
  SU: "Sun",
};
/** Humanize an iCal RRULE to "repeats weekly" / "repeats every Tue". */
function humanRrule(rrule: string | null): string {
  if (!rrule) return "repeats";
  const parts: Record<string, string> = {};
  for (const p of rrule.split(";")) {
    const [k, v] = p.split("=");
    if (k && v) parts[k.toUpperCase()] = v;
  }
  const freq = (parts.FREQ ?? "").toLowerCase();
  if (freq === "weekly" && parts.BYDAY) {
    const days = parts.BYDAY.split(",")
      .map((d) => RRULE_DAYS[d] ?? "")
      .filter(Boolean);
    if (days.length) return `repeats every ${days.join(", ")}`;
  }
  const label: Record<string, string> = {
    daily: "repeats daily",
    weekly: "repeats weekly",
    monthly: "repeats monthly",
    yearly: "repeats yearly",
  };
  return label[freq] ?? "repeats";
}
const Repeat = () => (
  <svg className="cal-rep" viewBox="0 0 24 24" aria-hidden="true">
    <path d="M4 9a7 7 0 0 1 12-3l3 3M20 15a7 7 0 0 1-12 3l-3-3" />
  </svg>
);
function flag(e: Ev): ReactNode {
  if (e.status === "tentative") return <span className="cal-flag">tentative</span>;
  if (e.status === "cancelled") return <span className="cal-flag cancel">cancelled</span>;
  return null;
}

export function CalendarScreen({
  onOpenNote,
  onCompose,
}: {
  onOpenNote: (noteId: string) => void;
  onCompose: (text: string, appt: AppointmentRef) => void;
}) {
  const [events, setEvents] = useState<Ev[] | null>(null);
  const [view, setView] = useState<View>("month");
  const [cur, setCur] = useState<Date>(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d;
  });
  const [open, setOpen] = useState<Ev | null>(null);

  useEffect(() => {
    let stale = false;
    api
      .appointments()
      .then((rows) => {
        if (!stale) setEvents(rows.map(toEv));
      })
      .catch(() => {
        if (!stale) setEvents([]);
      });
    return () => {
      stale = true;
    };
  }, []);

  const onDay = useMemo(
    () => (d: Date) =>
      (events ?? []).filter((e) => sameDay(e.start, d)).sort((a, b) => +a.start - +b.start),
    [events],
  );

  function shift(n: number) {
    const d = new Date(cur);
    if (view === "month") d.setMonth(d.getMonth() + n);
    else if (view === "week") d.setDate(d.getDate() + 7 * n);
    else d.setDate(d.getDate() + n);
    setCur(d);
  }

  const title =
    view === "month"
      ? `${monFull(cur.getMonth())} ${cur.getFullYear()}`
      : view === "tasks"
        ? "Agenda"
        : view === "week"
          ? weekTitle(cur)
          : `${dow(cur.getDay())} ${mon3(cur.getMonth())} ${cur.getDate()}`;

  return (
    <main className="screen-body calendar">
      <div className="cal-nav">
        <div className="cal-stepper">
          {view !== "tasks" && (
            <>
              <button
                type="button"
                className="cal-step"
                aria-label="Previous"
                onClick={() => shift(-1)}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M15 6l-6 6 6 6" />
                </svg>
              </button>
              <h2 className="cal-title">{title}</h2>
              <button type="button" className="cal-step" aria-label="Next" onClick={() => shift(1)}>
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M9 6l6 6-6 6" />
                </svg>
              </button>
            </>
          )}
          {view === "tasks" && <h2 className="cal-title">{title}</h2>}
        </div>
        <button type="button" className="cal-today" onClick={() => setCur(startOfToday())}>
          Today
        </button>
      </div>

      <div className="cal-seg" role="tablist" aria-label="Calendar view">
        {VIEWS.map((v) => (
          <button
            key={v}
            type="button"
            role="tab"
            aria-selected={view === v}
            className={view === v ? "on" : ""}
            onClick={() => setView(v)}
          >
            {cap(v)}
          </button>
        ))}
      </div>

      <div className="cal-view">
        {events === null ? (
          <div className="cal-empty">Loading…</div>
        ) : view === "day" ? (
          <DayView day={cur} list={onDay(cur)} onOpen={setOpen} />
        ) : view === "week" ? (
          <WeekView
            anchor={cur}
            onDay={onDay}
            onPick={(d) => {
              setCur(d);
              setView("day");
            }}
            onOpen={setOpen}
          />
        ) : view === "month" ? (
          <MonthView cur={cur} onDay={onDay} onPick={setCur} onOpen={setOpen} />
        ) : (
          <TasksView events={events} onOpen={setOpen} />
        )}
      </div>

      {open && (
        <EventSheet
          ev={open}
          onClose={() => setOpen(null)}
          onOpenNote={onOpenNote}
          onCompose={onCompose}
        />
      )}
    </main>
  );
}

function startOfToday(): Date {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return d;
}
function weekTitle(cur: Date): string {
  const s = startOfWeek(cur);
  const e = new Date(s);
  e.setDate(s.getDate() + 6);
  const m = mon3(s.getMonth());
  return e.getMonth() === s.getMonth()
    ? `${m} ${s.getDate()} – ${e.getDate()}`
    : `${m} ${s.getDate()} – ${mon3(e.getMonth())} ${e.getDate()}`;
}

function DayView({
  day,
  list,
  onOpen,
}: { day: Date; list: Ev[]; onOpen: (e: Ev) => void }): ReactNode {
  const allday = list.filter((e) => e.allDay);
  const timed = list.filter((e) => !e.allDay);
  const now = new Date();
  const hours = [];
  for (let h = DAY_START; h <= DAY_END; h++) hours.push(h);
  return (
    <>
      {allday.length > 0 && (
        <div className="cal-allday">
          {allday.map((e) => (
            <button
              key={e.id}
              type="button"
              className={`cal-adchip ${domClass(e)}`}
              onClick={() => onOpen(e)}
            >
              {e.recurring && "↻ "}
              {e.title}
            </button>
          ))}
        </div>
      )}
      <div className="cal-day">
        {hours.map((h) => (
          <div key={h} className="cal-hour">
            <span className="cal-hl">{(h % 12 || 12) + (h < 12 ? "am" : "pm")}</span>
          </div>
        ))}
        {timed.map((e) => {
          const top = ((mins(e.start) - DAY_START * 60) / 60) * PXH;
          const h = Math.max(e.end ? ((mins(e.end) - mins(e.start)) / 60) * PXH : 34, 34);
          return (
            <button
              key={e.id}
              type="button"
              className={`cal-ev ${domClass(e)}${e.status === "cancelled" ? " cancelled" : ""}`}
              style={{ top: `${Math.max(top, 0)}px`, height: `${h - 4}px` }}
              onClick={() => onOpen(e)}
            >
              <span className="cal-et">
                {e.recurring && <Repeat />}
                {e.title}
                {flag(e)}
              </span>
              <span className="cal-ew">
                {fmtT(e.start)}
                {e.end && `–${fmtT(e.end)}`}
              </span>
            </button>
          );
        })}
        {sameDay(day, now) && now.getHours() >= DAY_START && now.getHours() <= DAY_END && (
          <div
            className="cal-now"
            style={{ top: `${((mins(now) - DAY_START * 60) / 60) * PXH}px` }}
          />
        )}
      </div>
      {list.length === 0 && <div className="cal-empty">Nothing scheduled.</div>}
    </>
  );
}

function WeekView({
  anchor,
  onDay,
  onPick,
  onOpen,
}: {
  anchor: Date;
  onDay: (d: Date) => Ev[];
  onPick: (d: Date) => void;
  onOpen: (e: Ev) => void;
}): ReactNode {
  const s = startOfWeek(anchor);
  const days = [...Array(7)].map((_, i) => {
    const d = new Date(s);
    d.setDate(s.getDate() + i);
    return d;
  });
  const now = new Date();
  const H = 340;
  const span = (DAY_END - DAY_START) * 60;
  return (
    <>
      <div className="cal-week-head">
        {days.map((d) => (
          <button
            key={+d}
            type="button"
            className={`cal-wd${sameDay(d, now) ? " today" : ""}${sameDay(d, anchor) ? " sel" : ""}`}
            onClick={() => onPick(d)}
          >
            <span className="cal-dn">{dow(d.getDay()).charAt(0)}</span>
            <span className="cal-num">{d.getDate()}</span>
          </button>
        ))}
      </div>
      <div className="cal-week-grid">
        {days.map((d) => (
          <div key={+d} className="cal-wcol">
            {onDay(d)
              .filter((e) => !e.allDay)
              .map((e) => {
                const top = ((mins(e.start) - DAY_START * 60) / span) * H;
                const ht = Math.max(e.end ? ((mins(e.end) - mins(e.start)) / span) * H : 15, 15);
                return (
                  <button
                    key={e.id}
                    type="button"
                    className={`cal-wev ${domClass(e)}${e.status === "cancelled" ? " cancelled" : ""}`}
                    style={{ top: `${Math.max(top, 0)}px`, height: `${ht}px` }}
                    onClick={() => onOpen(e)}
                  >
                    <span className="cal-wt">{e.title.split(" ")[0]}</span>
                  </button>
                );
              })}
          </div>
        ))}
      </div>
    </>
  );
}

function MonthView({
  cur,
  onDay,
  onPick,
  onOpen,
}: {
  cur: Date;
  onDay: (d: Date) => Ev[];
  onPick: (d: Date) => void;
  onOpen: (e: Ev) => void;
}): ReactNode {
  const now = new Date();
  const first = new Date(cur.getFullYear(), cur.getMonth(), 1);
  const start = new Date(first);
  start.setDate(1 - first.getDay());
  const cells = [...Array(42)].map((_, i) => {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    return d;
  });
  const selected = onDay(cur);
  return (
    <>
      <div className="cal-m-dow">
        {["S", "M", "T", "W", "T", "F", "S"].map((c, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: fixed weekday header
          <span key={i}>{c}</span>
        ))}
      </div>
      <div className="cal-m-grid">
        {cells.map((d) => {
          const evs = onDay(d);
          const domains = [...new Set(evs.map((e) => e.domain))].slice(0, 4);
          const out = d.getMonth() !== cur.getMonth();
          return (
            <button
              key={+d}
              type="button"
              className={`cal-mc${out ? " out" : ""}${sameDay(d, now) ? " today" : ""}${sameDay(d, cur) ? " sel" : ""}`}
              onClick={() => onPick(d)}
            >
              <span className="cal-mn">{d.getDate()}</span>
              <span className="cal-mdots">
                {domains.map((dm) => (
                  <i key={dm} className={`dom-${dm}`} />
                ))}
              </span>
            </button>
          );
        })}
      </div>
      <div className="cal-m-agenda">
        <div className="cal-ad">
          {dow(cur.getDay())} {mon3(cur.getMonth())} {cur.getDate()}
          {sameDay(cur, now) && " · Today"}
        </div>
        {selected.length === 0 ? (
          <div className="cal-empty cal-empty-sm">Nothing scheduled.</div>
        ) : (
          selected.map((e) => <TaskRow key={e.id} ev={e} onOpen={onOpen} />)
        )}
      </div>
    </>
  );
}

function TasksView({ events, onOpen }: { events: Ev[]; onOpen: (e: Ev) => void }): ReactNode {
  const now = new Date();
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  const upcoming = events
    .filter((e) => e.start >= startOfToday())
    .sort((a, b) => +a.start - +b.start);
  const groups: { day: Date; items: Ev[] }[] = [];
  for (const e of upcoming) {
    const last = groups[groups.length - 1];
    if (last && sameDay(last.day, e.start)) last.items.push(e);
    else groups.push({ day: e.start, items: [e] });
  }
  if (groups.length === 0) return <div className="cal-empty">No upcoming appointments.</div>;
  return (
    <div className="cal-tasks">
      {groups.map((g) => {
        const rel = sameDay(g.day, now) ? "Today" : sameDay(g.day, tomorrow) ? "Tomorrow" : "";
        return (
          <div key={+g.day} className="cal-tgroup">
            <div className="cal-th">
              {dow(g.day.getDay())} {mon3(g.day.getMonth())} {g.day.getDate()}
              {rel && <span className="cal-rel">{rel}</span>}
            </div>
            {g.items.map((e) => (
              <TaskRow key={e.id} ev={e} onOpen={onOpen} />
            ))}
          </div>
        );
      })}
    </div>
  );
}

function TaskRow({ ev, onOpen }: { ev: Ev; onOpen: (e: Ev) => void }): ReactNode {
  const sub = [
    ev.recurring ? "repeats" : null,
    ev.location,
    ev.attendees.length ? `with ${ev.attendees.join(", ")}` : null,
  ].filter(Boolean) as string[];
  return (
    <button
      type="button"
      className={`cal-trow ${domClass(ev)}${ev.status === "cancelled" ? " cancelled" : ""}`}
      onClick={() => onOpen(ev)}
    >
      <span className="cal-time">{ev.allDay ? "all day" : fmtT(ev.start)}</span>
      <span className="cal-rail" />
      <span className="cal-body">
        <span className="cal-tt">
          {ev.recurring && <Repeat />}
          {ev.title}
          {flag(ev)}
        </span>
        {sub.length > 0 && <span className="cal-tm">{sub.join(" · ")}</span>}
      </span>
    </button>
  );
}

// Leading icons for the meta rows (data-only; the theme owns stroke color).
const META_ICONS: Record<string, string> = {
  clock: "M12 7v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
  repeat: "M4 9a7 7 0 0 1 12-3l3 3M20 15a7 7 0 0 1-12 3l-3-3",
  pin: "M12 21s-7-5.2-7-10a7 7 0 1 1 14 0c0 4.8-7 10-7 10z M12 11a2.2 2.2 0 1 0 0-4.4 2.2 2.2 0 0 0 0 4.4",
  person: "M12 8a3.4 3.4 0 1 0 0-6.8A3.4 3.4 0 0 0 12 8 M5.5 20a6.5 6.5 0 0 1 13 0",
};
function MetaRow({ icon, children }: { icon: string; children: ReactNode }): ReactNode {
  return (
    <div className="cal-row cal-meta">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d={META_ICONS[icon] ?? ""} />
      </svg>
      <span>{children}</span>
    </div>
  );
}

function EventSheet({
  ev,
  onClose,
  onOpenNote,
  onCompose,
}: {
  ev: Ev;
  onClose: () => void;
  onOpenNote: (noteId: string) => void;
  onCompose: (text: string, appt: AppointmentRef) => void;
}): ReactNode {
  const [snippet, setSnippet] = useState<string | null>(null);
  const [cancelArmed, setCancelArmed] = useState(false);
  const [rescheduling, setRescheduling] = useState(false);
  const [saved, setSaved] = useState(false);

  // Pull the source note's body for an inline preview (read-only; the link
  // still opens the full note). One fetch per sheet open, stale-guarded.
  const sourceNoteId = ev.sourceNoteId;
  useEffect(() => {
    if (!sourceNoteId) return;
    let stale = false;
    api
      .getNote(sourceNoteId)
      .then((n) => {
        if (!stale) setSnippet(n.body.replace(/\s+/g, " ").trim().slice(0, 160));
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [sourceNoteId]);

  const cancelled = ev.status === "cancelled";
  const dateText = `${dow(ev.start.getDay())} ${mon3(ev.start.getMonth())} ${ev.start.getDate()}`;
  const whenText = ev.allDay ? dateText : `${dateText} · ${fmtT(ev.start)}`;
  const dur = durationText(ev.start, ev.end);

  // Every agent handoff carries the appointment id so the agent resolves THIS
  // appointment (read_appointment) instead of guessing by title, and closes the
  // sheet first so its scrim can't sit over the composer it hands to.
  function hand(text: string) {
    onClose();
    onCompose(text, { id: ev.id, title: ev.title });
  }
  function openNote() {
    onClose();
    if (sourceNoteId) onOpenNote(sourceNoteId);
  }
  function cancel() {
    if (!cancelArmed) {
      setCancelArmed(true);
      setTimeout(() => setCancelArmed(false), 2600);
      return;
    }
    hand(`Cancel my "${ev.title}" appointment on ${dateText}.`);
  }
  function ask() {
    hand(`About my "${ev.title}" appointment: `);
  }

  // "Add to calendar" fetches the single-event .ics as a blob and lets the OS
  // open it. A plain <a href> navigates, which the PWA's offline fallback
  // answers with the app shell — landing the owner back in their notes.
  async function addToCalendar() {
    try {
      const blob = await api.appointmentIcs(ev.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${ev.title || "appointment"}.ics`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setSaved(true);
      setTimeout(() => setSaved(false), 2200);
    } catch {
      // A failed download leaves the button as-is; the owner can retry.
    }
  }

  return (
    <div className="cal-sheet">
      <button type="button" className="cal-backdrop" aria-label="Close" onClick={onClose} />
      <div className={`cal-esheet ${domClass(ev)}`}>
        <span className="cal-grab" aria-hidden="true" />
        <div className="cal-esheet-hd">
          <h2 className={cancelled ? "cancelled" : ""}>
            {ev.recurring && <Repeat />}
            {ev.title}
          </h2>
          {flag(ev)}
        </div>
        <div className="cal-hero">
          <span className="cal-hero-dot" aria-hidden="true" />
          <span>
            {cancelled ? "cancelled" : <b>{relTime(ev.start, new Date())}</b>} · {ev.domain}
          </span>
        </div>

        <div className="cal-meta-rows">
          <MetaRow icon="clock">
            {ev.allDay ? `All day · ${dateText}` : whenText}
            {ev.end && !ev.allDay ? `–${fmtT(ev.end)}` : ""}
            {dur && <span className="cal-sub"> · {dur}</span>}
          </MetaRow>
          {ev.recurring && <MetaRow icon="repeat">{humanRrule(ev.rrule)}</MetaRow>}
          {ev.location && <MetaRow icon="pin">{ev.location}</MetaRow>}
          {ev.attendees.length > 0 && (
            <MetaRow icon="person">with {ev.attendees.join(", ")}</MetaRow>
          )}
        </div>

        {sourceNoteId ? (
          <>
            <div className="cal-seclabel">From your notes</div>
            <button type="button" className="cal-srccard" onClick={openNote}>
              <span className="cal-srcsnip">{snippet ? `“${snippet}…”` : "open the note"}</span>
            </button>
          </>
        ) : (
          <div className="cal-row cal-note">
            Projected from your notes — ask the agent to change it.
          </div>
        )}

        {!cancelled && (
          <div className="cal-actions">
            <button type="button" className="cal-act" onClick={() => setRescheduling(true)}>
              reschedule
            </button>
            <button type="button" className="cal-act danger" onClick={cancel}>
              {cancelArmed ? "tap again to cancel" : "cancel"}
            </button>
          </div>
        )}
        <div className="cal-actions cal-actions-2">
          {sourceNoteId && (
            <button type="button" className="cal-act ghost" onClick={openNote}>
              open note
            </button>
          )}
          <button type="button" className="cal-act ghost" onClick={ask}>
            ask the agent
          </button>
        </div>
        <div className="cal-actions cal-actions-2">
          <button type="button" className="cal-act ghost" onClick={addToCalendar}>
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M8 2v4M16 2v4M3 9h18M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" />
            </svg>
            {saved ? "saved to your device" : "add to calendar"}
          </button>
        </div>
      </div>
      {rescheduling && (
        <RescheduleSheet
          ev={ev}
          onClose={() => setRescheduling(false)}
          onSubmit={(when) => hand(`Reschedule my "${ev.title}" appointment to ${when}.`)}
        />
      )}
    </div>
  );
}

// The reschedule picker: a date (+ time, unless all-day) the owner sets, which
// hands the agent a clear "when" to stage a Proposal from — appointments stay
// derived from notes (#7), so this never writes the calendar itself.
function RescheduleSheet({
  ev,
  onClose,
  onSubmit,
}: {
  ev: Ev;
  onClose: () => void;
  onSubmit: (when: string) => void;
}): ReactNode {
  const [date, setDate] = useState(() => isoDate(ev.start));
  const [time, setTime] = useState(() => (ev.allDay ? "" : isoTime(ev.start)));

  function submit() {
    if (!date) return;
    // Build the picked moment from local date/time parts, then phrase it the way
    // the owner reads it on the calendar — unambiguous for the agent to parse.
    const [y, m, d] = date.split("-");
    const [hh, mm] = (ev.allDay || !time ? "0:0" : time).split(":");
    const picked = new Date(Number(y), Number(m) - 1, Number(d), Number(hh), Number(mm));
    const dateText = `${dow(picked.getDay())} ${mon3(picked.getMonth())} ${picked.getDate()}, ${picked.getFullYear()}`;
    onClose();
    onSubmit(ev.allDay || !time ? dateText : `${dateText} at ${fmtT(picked)}`);
  }

  return (
    <Sheet title={`Reschedule ${ev.title}`} onClose={onClose}>
      <label className="sheet-field">
        Date
        <input type="date" value={date} onChange={(e) => setDate(e.target.value)} />
      </label>
      {!ev.allDay && (
        <label className="sheet-field">
          Time
          <input type="time" value={time} onChange={(e) => setTime(e.target.value)} />
        </label>
      )}
      <button type="button" className="sheet-primary" onClick={submit} disabled={!date}>
        Reschedule
      </button>
    </Sheet>
  );
}
