import { type ReactNode, useEffect, useMemo, useState } from "react";
import { type AppointmentOut, api } from "../api/client";

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
const domClass = (e: Ev) =>
  `dom-${["general", "health", "finance", "location"].includes(e.domain) ? e.domain : "general"}`;
// Total accessors — the project's noUncheckedIndexedAccess types a fixed-array
// lookup as possibly-undefined even when the index is in range by construction.
const monFull = (i: number) => MON[i] ?? "";
const mon3 = (i: number) => monFull(i).slice(0, 3);
const dow = (i: number) => DOW[i] ?? "";
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
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

export function CalendarScreen({ onOpenNote }: { onOpenNote: (noteId: string) => void }) {
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

      {open && <EventSheet ev={open} onClose={() => setOpen(null)} onOpenNote={onOpenNote} />}
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

function EventSheet({
  ev,
  onClose,
  onOpenNote,
}: { ev: Ev; onClose: () => void; onOpenNote: (noteId: string) => void }): ReactNode {
  const when = ev.allDay
    ? `All day · ${dow(ev.start.getDay())} ${mon3(ev.start.getMonth())} ${ev.start.getDate()}`
    : `${dow(ev.start.getDay())} ${mon3(ev.start.getMonth())} ${ev.start.getDate()} · ${fmtT(ev.start)}${ev.end ? `–${fmtT(ev.end)}` : ""}`;
  // Backdrop and sheet are SIBLINGS (not a button wrapping the content), so the
  // dimmer is the only click target — no nested interactive / stopPropagation.
  return (
    <div className="cal-sheet">
      <button type="button" className="cal-backdrop" aria-label="Close" onClick={onClose} />
      <div className={`cal-esheet ${domClass(ev)}`}>
        <span className="cal-grab" aria-hidden="true" />
        <h2 className={ev.status === "cancelled" ? "cancelled" : ""}>
          {ev.recurring && <Repeat />}
          {ev.title}
          {flag(ev)}
        </h2>
        <span className="cal-domtag">{ev.domain}</span>
        <div className="cal-row">{when}</div>
        {ev.recurring && <div className="cal-row">Repeats</div>}
        {ev.location && <div className="cal-row">{ev.location}</div>}
        {ev.attendees.length > 0 && <div className="cal-row">with {ev.attendees.join(", ")}</div>}
        {ev.sourceNoteId ? (
          // Projected from a note — let the owner jump to it. Closing the sheet
          // first leaves the note layer unobstructed (cf. App.openNoteFromEntity).
          <button
            type="button"
            className="cal-row cal-note cal-opennote"
            onClick={() => {
              const noteId = ev.sourceNoteId;
              onClose();
              if (noteId) onOpenNote(noteId);
            }}
          >
            Projected from your notes — open the source note.
          </button>
        ) : (
          <div className="cal-row cal-note">
            Projected from your notes — ask the agent to change it.
          </div>
        )}
      </div>
    </div>
  );
}
