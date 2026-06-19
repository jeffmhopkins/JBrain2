// L7a — the inline place-digest panel above the Map tab (binding mock:
// docs/mocks/location-l7/option-c.html, "Option C — week timeline track").
//
// A COMPUTE-ON-READ rollup of recent place activity rendered as a collapsible
// panel: a per-day horizontal place-track (home teal, other places steel, a dashed
// amber "no signal" gap), a headline summary, a nightly⇄weekly toggle defaulting to
// WEEKLY, a "computed just now ↻" recompute affordance, a first/last-seen line, and
// an owner-only footnote. NAMES + TIMES ONLY — there is no coordinate anywhere here
// (so the panel needs no basemap). The teal accent is --location.

import { useCallback, useEffect, useState } from "react";
import { type DayTrack, type LocationDigest, api } from "../api/client";

type Period = "week" | "night";

type State = { phase: "loading" } | { phase: "error" } | { phase: "done"; digest: LocationDigest };

export interface DigestDeps {
  loadDigest: (period: Period) => Promise<LocationDigest>;
}

export function LocationDigestPanel({ deps }: { deps?: DigestDeps | undefined }) {
  const load = deps?.loadDigest ?? api.locationDigest;
  const [open, setOpen] = useState(true);
  const [period, setPeriod] = useState<Period>("week"); // weekly default (binding)
  const [state, setState] = useState<State>({ phase: "loading" });
  const [spinning, setSpinning] = useState(false);

  const refresh = useCallback(
    async (p: Period): Promise<void> => {
      setState({ phase: "loading" });
      try {
        setState({ phase: "done", digest: await load(p) });
      } catch {
        setState({ phase: "error" });
      }
    },
    [load],
  );

  useEffect(() => {
    let stale = false;
    load(period)
      .then((digest) => {
        if (!stale) setState({ phase: "done", digest });
      })
      .catch(() => {
        if (!stale) setState({ phase: "error" });
      });
    return () => {
      stale = true;
    };
  }, [load, period]);

  function recompute(): void {
    // Compute-on-read: the ↻ spins briefly and re-fetches (no stored feed).
    setSpinning(true);
    void refresh(period).finally(() => setSpinning(false));
  }

  const summary = state.phase === "done" ? summaryLine(state.digest) : "";

  return (
    <section className={`loc-digest${open ? "" : " loc-digest-collapsed"}`}>
      <button
        type="button"
        className="loc-digest-head"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="loc-digest-icon" aria-hidden="true">
          ▤
        </span>
        <span className="loc-digest-title">
          <span className="loc-digest-t">Your week in places</span>
          {summary && <span className="loc-digest-s">{summary}</span>}
        </span>
        <span className="loc-digest-chev" aria-hidden="true">
          ⌄
        </span>
      </button>

      {open && (
        <div className="loc-digest-body">
          <div className="loc-digest-ctl">
            <div className="loc-digest-toggle" role="tablist" aria-label="Digest period">
              {(["week", "night"] as Period[]).map((p) => (
                <button
                  key={p}
                  type="button"
                  role="tab"
                  aria-selected={period === p}
                  className={period === p ? "on" : ""}
                  onClick={() => setPeriod(p)}
                >
                  {p === "week" ? "This week" : "Last night"}
                </button>
              ))}
            </div>
            <button
              type="button"
              className={`loc-digest-recompute${spinning ? " spin" : ""}`}
              onClick={recompute}
            >
              <span aria-hidden="true">↻</span> {spinning ? "computing…" : "computed just now"}
            </button>
          </div>

          {state.phase === "loading" && <p className="analysis-quiet">computing digest…</p>}
          {state.phase === "error" && (
            <p className="analysis-quiet">couldn't compute the digest — check the connection.</p>
          )}
          {state.phase === "done" && <DigestBody digest={state.digest} />}

          <div className="loc-digest-owner">
            <span aria-hidden="true">⛉</span> owner-only · computed on read · place names + times
            only
          </div>
        </div>
      )}
    </section>
  );
}

function DigestBody({ digest }: { digest: LocationDigest }) {
  const anyData = digest.days.some((d) => d.has_data);
  if (!anyData) {
    return (
      <p className="analysis-quiet">
        no place activity in this {digest.period === "night" ? "night" : "week"} yet — crossings
        appear here once a device enters or leaves a saved place.
      </p>
    );
  }
  return (
    <>
      <div className="loc-digest-legend">
        <span className="lg-home">
          <i /> Home
        </span>
        <span className="lg-place">
          <i /> place
        </span>
        <span className="lg-out">
          <i /> out / other
        </span>
        <span className="lg-gap">
          <i /> no signal
        </span>
      </div>
      <div className="loc-digest-week">
        {digest.days.map((d) => (
          <DayRow key={d.day} day={d} />
        ))}
      </div>
      <SeenLine digest={digest} />
    </>
  );
}

function DayRow({ day }: { day: DayTrack }) {
  return (
    <div className={`loc-digest-drow${isToday(day.day) ? " today" : ""}`}>
      <span className="loc-digest-dl">{dayLabel(day.day)}</span>
      <div className="loc-digest-track" aria-label={trackLabel(day)}>
        {day.segments.map((s, i) => (
          <span
            key={`${day.day}-${i}`}
            className={`loc-digest-seg ${segClass(s.place_name)}`}
            style={{ width: `${Math.max(0, s.width) * 100}%` }}
            title={s.place_name ?? "no signal"}
          />
        ))}
      </div>
    </div>
  );
}

function SeenLine({ digest }: { digest: LocationDigest }) {
  const trip = digest.longest_trip;
  const newest = digest.seen[digest.seen.length - 1];
  if (!trip && !newest) return null;
  return (
    <p className="loc-digest-seen">
      {trip && (
        <>
          Longest trip <b>{dayLabel(trip.day)}</b> — {durationText(trip.seconds)} at{" "}
          <b>{trip.place_name}</b>.{" "}
        </>
      )}
      {newest && (
        <>
          Most recent: <b>{newest.place_name}</b> (last seen {timeText(newest.last_seen)}).
        </>
      )}
    </p>
  );
}

// --- formatting (names + times only) ---------------------------------------

/** A teal "home" bar, a steel "place" bar, a dashed-amber "no signal" gap. Every
 * non-home named place reads steel; only the saved "Home" place is teal. */
function segClass(placeName: string | null): string {
  if (placeName === null) return "gap";
  if (placeName.toLowerCase() === "home") return "home";
  return "place";
}

function summaryLine(d: LocationDigest): string {
  const parts: string[] = [];
  parts.push(
    `${d.nights_home}/${d.nights_total} ${d.period === "night" ? "night home" : "nights home"}`,
  );
  if (d.places_visited > 0) {
    parts.push(`${d.places_visited} ${d.places_visited === 1 ? "place" : "places"} visited`);
  }
  if (d.longest_trip) parts.push(`longest trip ${dayLabel(d.longest_trip.day)}`);
  return parts.join(" · ");
}

function trackLabel(day: DayTrack): string {
  if (!day.has_data) return `${dayLabel(day.day)}: no signal`;
  const names = day.segments.map((s) => s.place_name).filter((n): n is string => n !== null);
  return `${dayLabel(day.day)}: ${[...new Set(names)].join(", ") || "out"}`;
}

function isToday(iso: string): boolean {
  const d = new Date(`${iso}T00:00:00`);
  const now = new Date();
  return (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  );
}

function dayLabel(iso: string): string {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString([], { weekday: "short" });
}

function timeText(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const wd = d.toLocaleDateString([], { weekday: "short" });
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return `${wd} ${t}`;
}

function durationText(seconds: number): string {
  const mins = Math.round(seconds / 60);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}

export { segClass, summaryLine, durationText };
