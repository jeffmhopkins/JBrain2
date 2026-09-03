// Who is out there, rather than what scrolled past (F3; binding spec
// docs/mocks/aprs/e-stations.html).
//
// The flat packet feed this replaces was unreadable for a reason that is not obvious
// until you see real traffic: on the owner's own capture, 6 callsigns appeared in the
// AX.25 source while 16 stations were actually transmitting, because three quarters of
// the channel was one IGate relaying internet traffic and a relayed frame's source
// names the relay. A feed shows that as one machine shouting; a roster keyed on the
// true sender shows it as sixteen stations, and the owner never has to learn what a
// third-party frame is.
//
// FILTERING IS THE SERVER'S JOB. The range and the chips are query parameters, not a
// client that downloads the log and narrows it here — a year of this channel is ~1.2M
// rows to render sixteen lines.
//
// EVERY STRING RENDERED HERE CAME OFF THE AIR from anyone with a transmitter, callsigns
// included. React escapes it, each row states how it reached us, and none of it is ever
// put in front of a model as an instruction (the plan's two trust tiers).

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import {
  type AprsRoster,
  type AprsStationDetail,
  type Kind,
  WINDOWS,
  type WindowId,
  ago,
  arrival,
  chipsFor,
  isMine,
  pinMine,
  shownLabel,
} from "../aprsStations";

export function AprsStations({ tick, owner }: { tick: number; owner: string | null }) {
  const [window_, setWindow] = useState<WindowId>("1d");
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [station, setStation] = useState<string | null>(null);
  const [roster, setRoster] = useState<AprsRoster | null>(null);
  const [detail, setDetail] = useState<AprsStationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      if (station) {
        setDetail(await api.getAprsStation(station, window_, kinds));
      } else {
        setRoster(await api.getAprsStations(window_, kinds));
      }
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't read the stations.");
    }
  }, [station, window_, kinds]);

  // `tick` is the parent's poll counter, and depending on it IS the refresh — the
  // roster has to keep up with a live channel without owning a second timer that could
  // drift out of step with the health line above it.
  // biome-ignore lint/correctness/useExhaustiveDependencies: tick is the poll signal
  useEffect(() => {
    void load();
  }, [load, tick]);

  function toggleKind(kind: Kind) {
    setKinds((current) =>
      current.includes(kind) ? current.filter((k) => k !== kind) : [...current, kind],
    );
  }

  // Leaving a station keeps the range but DROPS the chips. Inside a station they mean
  // "this station's weather"; back at the roster the same chips mean "stations that
  // send weather at all" — carrying a selection across that change silently rewrites
  // what the owner asked for.
  function back() {
    setStation(null);
    setKinds([]);
    setDetail(null);
  }

  if (error && !roster && !detail) {
    return (
      <p className="radio-error" role="alert">
        {error}
      </p>
    );
  }

  if (station) {
    if (!detail) return <p className="radio-empty">Reading {station}…</p>;
    return (
      <StationDetail
        detail={detail}
        window_={window_}
        kinds={kinds}
        owner={owner}
        onWindow={setWindow}
        onKind={toggleKind}
        onBack={back}
      />
    );
  }

  if (!roster) return <p className="radio-empty">Reading the log…</p>;
  return (
    <Roster
      roster={roster}
      window_={window_}
      kinds={kinds}
      owner={owner}
      onWindow={setWindow}
      onKind={toggleKind}
      onOpen={(call) => {
        setStation(call);
        setDetail(null);
      }}
    />
  );
}

function Roster({
  roster,
  window_,
  kinds,
  owner,
  onWindow,
  onKind,
  onOpen,
}: {
  roster: AprsRoster;
  window_: WindowId;
  kinds: Kind[];
  owner: string | null;
  onWindow: (id: WindowId) => void;
  onKind: (kind: Kind) => void;
  onOpen: (call: string) => void;
}) {
  const stations = pinMine(roster.stations, owner);
  const chips = chipsFor(roster.kind_stations);
  const mine = stations.filter((s) => isMine(s.call, owner)).length;

  return (
    <>
      <WindowTabs counts={roster.window_packets} chosen={window_} onWindow={onWindow} unit="pkt" />
      {chips.length > 0 && (
        // At the ROOT these narrow the roster to stations that send that kind at all —
        // "show me who is putting out weather" is a question about stations. So the
        // counts are stations, and they are computed over the range UNFILTERED by the
        // selection, or the row would rearrange itself as you used it.
        <div className="aprs-chips">
          {chips.map(({ kind, count }) => (
            <button
              type="button"
              key={kind}
              className="aprs-chip"
              aria-pressed={kinds.includes(kind)}
              onClick={() => onKind(kind)}
            >
              {kind}
              <span className="aprs-chip-n">{count}</span>
            </button>
          ))}
        </div>
      )}

      {roster.unclassified > 0 && (
        // A roster that is still filling in has to SAY so. Quietly listing fewer
        // stations is the same failure as a dead receiver looking like a quiet channel,
        // which is the confusion this whole surface exists to prevent.
        <output className="aprs-hint">
          {roster.unclassified} packet{roster.unclassified === 1 ? "" : "s"} not sorted yet — this
          list is still filling in.
        </output>
      )}

      <div className="aprs-sec">
        {kinds.length > 0 ? `Stations sending ${kinds.join(" or ")}` : "Stations heard"}
        <span className="aprs-count">
          {shownLabel(stations.length, roster.stations_total, kinds.length > 0)}
        </span>
      </div>

      {stations.length === 0 ? (
        <p className="radio-empty">
          {kinds.length > 0
            ? `No station sent ${kinds.join(" or ")} in this range. Clear the type filter or widen the range.`
            : "No stations in this range. Widen it, or turn APRS logging on to start hearing the channel."}
        </p>
      ) : (
        stations.map((s) => (
          <button
            type="button"
            key={s.call}
            className={`aprs-station${isMine(s.call, owner) ? " mine" : ""}`}
            onClick={() => onOpen(s.call)}
          >
            <span className="aprs-st-main">
              <span className="aprs-st-call">{s.call}</span>
              <span className="aprs-st-sub">
                {arrival(s)} · {s.kinds.join(", ")}
              </span>
            </span>
            <span className="aprs-st-right">
              <span className="aprs-st-ago">{ago(s.last_heard_at)} ago</span>
              <span className="aprs-st-n">{s.packets} pkt</span>
            </span>
          </button>
        ))
      )}

      {mine > 0 && (
        // The trap this shape removes: the owner's own traffic often arrives WRAPPED,
        // because an IGate relays a message to RF only once the addressee has been heard
        // nearby. Filed by the relay it reads as somebody else's noise; filed by the
        // true sender it is simply him, however it got here.
        <p className="aprs-hint">
          Your stations are pinned to the top. They stay listed however they reached the box — an
          IGate relays your mail onto RF, so your own traffic often arrives as third-party.
        </p>
      )}
    </>
  );
}

function StationDetail({
  detail,
  window_,
  kinds,
  owner,
  onWindow,
  onKind,
  onBack,
}: {
  detail: AprsStationDetail;
  window_: WindowId;
  kinds: Kind[];
  owner: string | null;
  onWindow: (id: WindowId) => void;
  onKind: (kind: Kind) => void;
  onBack: () => void;
}) {
  const chips = chipsFor(detail.kind_packets);
  const only = Object.keys(detail.kind_packets);

  return (
    <>
      <button type="button" className="aprs-back" onClick={onBack}>
        ‹ All stations
      </button>
      <div className={`aprs-st-head${isMine(detail.call, owner) ? " mine" : ""}`}>
        <span className="aprs-st-call">{detail.call}</span>
        <span className="aprs-st-sub">{arrival(detail)}</span>
      </div>
      <p className="aprs-st-sub">
        {/* All-time, not the range: it is what says whether an empty range means the
            station is quiet or means it is new. */}
        last heard {ago(detail.last_heard_at)} ago · {detail.packets_total} packet
        {detail.packets_total === 1 ? "" : "s"} in the log
      </p>

      <WindowTabs counts={detail.window_packets} chosen={window_} onWindow={onWindow} unit="pkt" />

      {chips.length > 0 ? (
        // Inside a station the chips narrow its PACKETS, so the counts are packets.
        <div className="aprs-chips">
          {chips.map(({ kind, count }) => (
            <button
              type="button"
              key={kind}
              className="aprs-chip"
              aria-pressed={kinds.includes(kind)}
              onClick={() => onKind(kind)}
            >
              {kind}
              <span className="aprs-chip-n">{count}</span>
            </button>
          ))}
        </div>
      ) : (
        // One chip is a control with nothing to choose between, and five where the
        // station only ever sends objects is four dead controls.
        <p className="aprs-hint">
          {only.length === 1
            ? `Only sends ${only[0]} — no type filter needed.`
            : "Nothing in this range to filter."}
        </p>
      )}

      <div className="aprs-sec">
        Packets<span className="aprs-count">{detail.packets.length} shown</span>
      </div>
      {detail.packets.length === 0 ? (
        <p className="radio-empty">
          Nothing from {detail.call} in this range. Widen it or clear the type filter.
        </p>
      ) : (
        detail.packets.map((p, i) => (
          <div className="aprs-row" key={`${p.heard_at}-${i}-${p.text.slice(0, 24)}`}>
            <div className="aprs-body">
              <div className="aprs-msg">
                {/* How it reached us, on every row. This is also where the untrusted
                    rule meets the eye: the badge says a stranger transmitted it. */}
                <span className={`aprs-badge b-${p.direct ? "direct" : p.gated ? "gated" : "rf"}`}>
                  {p.direct ? "direct" : p.gated ? "gated" : "rf"}
                </span>
                {p.text}
              </div>
              <div className="aprs-meta">{p.kind}</div>
            </div>
            <span className="aprs-when">{ago(p.heard_at)}</span>
          </div>
        ))
      )}
    </>
  );
}

/** The range control. Its counts are what widening WOULD reveal, so the owner can see
 *  there is nothing older before tapping "Older" and finding out. */
function WindowTabs({
  counts,
  chosen,
  onWindow,
  unit,
}: {
  counts: Record<WindowId, number>;
  chosen: WindowId;
  onWindow: (id: WindowId) => void;
  unit: string;
}) {
  return (
    <div className="aprs-windows" role="tablist" aria-label="Time range">
      {WINDOWS.map((w) => (
        <button
          type="button"
          key={w.id}
          role="tab"
          aria-selected={chosen === w.id}
          className={`aprs-window${chosen === w.id ? " on" : ""}`}
          onClick={() => onWindow(w.id)}
        >
          {w.label}
          <span className="aprs-window-n" aria-label={`${counts[w.id] ?? 0} ${unit}`}>
            {counts[w.id] ?? 0}
          </span>
        </button>
      ))}
    </div>
  );
}
