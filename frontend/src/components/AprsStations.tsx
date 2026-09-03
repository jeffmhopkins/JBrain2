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

import { Fragment, type ReactElement, useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api/client";
import {
  type AprsRoster,
  type AprsStationDetail,
  type AprsStationPacket,
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
import { AprsSymbol } from "./aprsIcons";
import {
  ChatIcon,
  GaugeIcon,
  GraphIcon,
  ListIcon,
  MessageIcon,
  RadioIcon,
  ThingIcon,
} from "./icons";

export function AprsStations({ tick, owner }: { tick: number; owner: string | null }) {
  const [window_, setWindow] = useState<WindowId>("1d");
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [station, setStation] = useState<string | null>(null);
  const [roster, setRoster] = useState<AprsRoster | null>(null);
  const [detail, setDetail] = useState<AprsStationDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A poll in flight when the owner taps a chip, a range or a station can land after the
  // request that replaced it and paint a state nobody asked for: one station's packets
  // under another's callsign, or a pressed Weather chip above the unfiltered roster. The
  // same guard the screen above this already uses for exactly the same reason.
  const seq = useRef(0);

  const load = useCallback(async () => {
    const mine = ++seq.current;
    try {
      if (station) {
        const next = await api.getAprsStation(station, window_, kinds);
        if (mine !== seq.current) return;
        setDetail(next);
      } else {
        const next = await api.getAprsStations(window_, kinds, owner);
        if (mine !== seq.current) return;
        setRoster(next);
      }
      setError(null);
    } catch (err) {
      if (mine !== seq.current) return;
      setError(err instanceof ApiError ? err.message : "Couldn't read the stations.");
    }
  }, [station, window_, kinds, owner]);

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
    setError(null);
  }

  // Shown WHENEVER it is set, not only before the first load. Once the roster has
  // arrived, an error that replaced the view would throw away a working screen — and an
  // error that is not rendered at all leaves the list silently frozen on stale rows
  // while the health line above it still reads healthy.
  const banner = error ? (
    <p className="radio-error" role="alert">
      {error}
    </p>
  ) : null;

  if (station) {
    // The way out comes BEFORE the loading return. This screen has made the opposite
    // mistake once already — the error render used to sit after it, so a failed first
    // load spun on "Reading the log…" for ever — and here it would be worse: the only
    // "All stations" button lived inside the detail that never arrived, so a 404 or a
    // dropped connection stranded the owner on a screen with no exit.
    return (
      <>
        <button type="button" className="aprs-back" onClick={back}>
          ‹ All stations
        </button>
        {banner}
        {detail ? (
          <StationDetail
            detail={detail}
            window_={window_}
            kinds={kinds}
            owner={owner}
            onWindow={setWindow}
            onKind={toggleKind}
          />
        ) : (
          !error && <p className="radio-empty">Reading {station}…</p>
        )}
      </>
    );
  }

  if (!roster) return banner ?? <p className="radio-empty">Reading the log…</p>;
  return (
    <>
      {banner}
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
          setError(null);
        }}
      />
    </>
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
  const chips = chipsFor(roster.kind_stations, kinds);
  const mine = stations.filter((s) => isMine(s.call, owner)).length;

  return (
    <>
      <WindowTabs
        counts={roster.window_packets}
        older={roster.older}
        hasOlder={roster.has_older}
        chosen={window_}
        onWindow={onWindow}
        unit="packets"
      />
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
          {/* The list is capped. Printing a confident total it did not return would hide
              a station — including, over a long range, the owner's own. */}
          {roster.truncated && ` of ${roster.stations_total}, newest first`}
        </span>
      </div>

      {stations.length === 0 ? (
        <p className="radio-empty">
          {kinds.length > 0
            ? `No station sent ${kinds.join(" or ")} in this range. Clear the type filter or widen the range.`
            : "No stations in this range. A quiet channel and a dead antenna look the same in an empty list, so the line above shows the last decode rather than a signal bar — widen the range to see whether anything has been heard at all."}
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
}: {
  detail: AprsStationDetail;
  window_: WindowId;
  kinds: Kind[];
  owner: string | null;
  onWindow: (id: WindowId) => void;
  onKind: (kind: Kind) => void;
}) {
  const chips = chipsFor(detail.kind_packets, kinds);
  const only = Object.keys(detail.kind_packets);
  // One open at a time. Two open rows on a phone means neither is readable, and the
  // second tap becomes a way to lose the first.
  const [openPacket, setOpenPacket] = useState<string | null>(null);

  return (
    <>
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

      <WindowTabs
        counts={detail.window_packets}
        older={detail.older}
        hasOlder={detail.has_older}
        chosen={window_}
        onWindow={onWindow}
        unit="packets"
      />

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
        detail.packets.map((p) => (
          <PacketRow
            key={p.id}
            packet={p}
            // Inside a station the header already names the sender, so a callsign in
            // every title is one fact repeated down the whole list.
            scoped
            open={openPacket === p.id}
            onToggle={() => setOpenPacket(openPacket === p.id ? null : p.id)}
          />
        ))
      )}
    </>
  );
}

/** Glyphs for the packets that carry NO symbol of their own — telemetry, a message, a
 *  status line, a plain beacon. Without one the list gets a ragged left edge; with the
 *  same tint as a real symbol it would claim to be something the station chose. House
 *  icons, so the difference from a drawn APRS symbol is visible at a glance. */
const KIND_ICONS: Record<string, (p: { size?: number }) => ReactElement> = {
  Telemetry: GraphIcon,
  Message: MessageIcon,
  Status: ChatIcon,
  Capabilities: ListIcon,
  "AX.25 beacon": RadioIcon,
  Weather: GaugeIcon,
};

/** One heard packet, as a sentence with the bytes one tap below.
 *
 * Binding spec: `docs/mocks/aprs/i-packet-readable.html`, shape D. The inversion it
 * settles is that the MEANING goes on the row and the frame goes one tap away — on this
 * channel the resting list was `` `m3jq6F>/`On D-Star ``, `T#110,190,088` and
 * `@031030z2837.27N/08049.42W_338/000g000`, three lines of which none could be read.
 *
 * THREE RULES, each of them a thing the first cut got wrong:
 *
 * 1. **Two voices, told apart by typeface.** The sentence is the app's, in the system
 *    font; the station's own comment is quoted verbatim in monospace. Typography carries
 *    the trust boundary so no badge has to — and where the only content IS the station's
 *    text, a status or a beacon, the app says nothing rather than reciting a stranger's
 *    sentence in its own voice.
 * 2. **The icon is not restated as text.** When the whole reading is the symbol's name
 *    the glyph already says it, and its accessible label says it to a screen reader. The
 *    name stays in the detail, where the reader is asking what the packet contains.
 *  3. **The title is the type**, and the callsign only where it is not already known.
 *
 * Every string here came off the air from an anonymous transmitter. React escapes it, the
 * frame is never linkified, and none of it is ever presented as the app's own claim. */
function PacketRow({
  packet,
  scoped,
  open,
  onToggle,
}: {
  packet: AprsStationPacket;
  scoped: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const symbolName = packet.fields.find(([name]) => name === "Symbol")?.[1] ?? "";
  const derived = packet.summary !== "" && packet.summary !== packet.comment;
  const said = derived ? readingOf(packet.summary, symbolName, packet.symbol) : "";
  const theirs = packet.comment || (derived ? "" : packet.text);
  const provenance = packet.direct ? "direct" : packet.gated ? "gated" : "rf";

  return (
    <div className={`aprs-packet${open ? " open" : ""}`}>
      <button type="button" className="aprs-packet-btn" aria-expanded={open} onClick={onToggle}>
        <PacketGlyph packet={packet} label={symbolName} />
        <span className="aprs-packet-body">
          <span className="aprs-packet-title">
            {!scoped && (
              <>
                <span className="aprs-call">{packet.frame.source}</span>
                <span className="aprs-dash">—</span>
              </>
            )}
            {packet.kind}
          </span>
          {said && <span className="aprs-said">{said}</span>}
          {theirs && <span className="aprs-theirs">{theirs}</span>}
          <span className="aprs-packet-meta">
            <span className={`aprs-badge b-${provenance}`}>{provenance}</span>
            {packet.relay ? `relayed by ${packet.relay}` : "heard on the air"}
            <Signal level={packet.audio_level} />
          </span>
        </span>
        <span className="aprs-when">{ago(packet.heard_at)}</span>
      </button>
      {open && (
        <div className="aprs-packet-panel">
          {packet.fields.length > 0 && (
            <dl className="aprs-fields">
              {packet.fields.map(([name, value]) => (
                <Fragment key={name}>
                  <dt>{name}</dt>
                  <dd>{value}</dd>
                </Fragment>
              ))}
            </dl>
          )}
          {packet.warnings.map((warning) => (
            <p className="aprs-warn" key={warning}>
              {warning}
            </p>
          ))}
          {/* The evidence for every sentence above it. This is also the only place the
              "gated via" claim can be checked, since the row shows the inner payload. */}
          <details className="aprs-raw">
            <summary>The frame as heard</summary>
            <pre>
              {packet.frame.source}&gt;{packet.frame.destination}
              {packet.frame.path.length > 0 ? `,${packet.frame.path.join(",")}` : ""}:{"\n"}
              {packet.text}
            </pre>
          </details>
        </div>
      )}
    </div>
  );
}

/** How strong the transmission was — and NOTHING at all when nobody measured it.
 *
 * Null is not zero. Every other fact on this row is self-declared by a stranger; this
 * one is the box's own measurement of the radio link, which is exactly why inventing it
 * would be the most misleading thing on the screen. A frame logged before the level was
 * captured simply says nothing. */
function Signal({ level }: { level: number | null }) {
  if (level === null || Number.isNaN(level)) return null;
  const label = level >= 60 ? "strong" : level >= 25 ? "ok" : "weak";
  return (
    <span className={`aprs-sig s-${label}`} title={`Audio level ${level} of 100`}>
      {label}
    </span>
  );
}

/** The reading, with the symbol's name removed when the icon already carries it. */
function readingOf(summary: string, symbolName: string, symbol: string): string {
  if (!symbolName || !symbol) return summary;
  if (summary === symbolName) return "";
  return summary.startsWith(`${symbolName}, `) ? summary.slice(symbolName.length + 2) : summary;
}

/** The station's own symbol where it has one, our inference about the packet where it
 *  does not — and they are tinted differently on purpose. An APRS symbol is a claim the
 *  station made; a kind glyph is a claim we made. They should not look alike. */
function PacketGlyph({ packet, label }: { packet: AprsStationPacket; label: string }) {
  if (packet.symbol.length === 2) {
    return (
      <span className="aprs-sym aprs-sym-own">
        <AprsSymbol
          table={packet.symbol.slice(0, 1)}
          code={packet.symbol.slice(1, 2)}
          label={label || packet.kind}
        />
      </span>
    );
  }
  const Glyph = KIND_ICONS[packet.kind] ?? ThingIcon;
  return (
    <span className="aprs-sym aprs-sym-kind" role="img" aria-label={packet.kind}>
      <Glyph size={20} />
    </span>
  );
}

/** The range control, on the house segmented control — the same `seg-tabs` the Radio
 *  tabs and the session list use, rather than a fourth answer to a settled question.
 *
 *  Its counts are what widening WOULD reveal, so the owner can see there is nothing older
 *  before tapping "Older" and finding out. `old` is the exception: counting the archive
 *  on every poll is not worth a number nobody reads precisely, so that tab shows presence
 *  until it is the range being read.
 *
 *  Plain buttons, not `role="tab"`. These are inside the APRS tab panel, and a nested
 *  tablist makes a screen reader announce a filter as a tab. `aria-pressed` says what it
 *  actually is. */
function WindowTabs({
  counts,
  older,
  hasOlder,
  chosen,
  onWindow,
  unit,
}: {
  counts: Record<string, number>;
  older: number | null;
  hasOlder: boolean;
  chosen: WindowId;
  onWindow: (id: WindowId) => void;
  unit: string;
}) {
  return (
    <div className="seg-tabs aprs-ranges" aria-label="Time range">
      {WINDOWS.map((w) => {
        const count = w.id === "old" ? (older ?? (hasOlder ? null : 0)) : (counts[w.id] ?? 0);
        return (
          <button
            type="button"
            key={w.id}
            aria-pressed={chosen === w.id}
            aria-label={count === null ? w.label : `${w.label}, ${count} ${unit}`}
            className={`seg-tab${chosen === w.id ? " on" : ""}`}
            onClick={() => onWindow(w.id)}
          >
            {w.label}
            {count !== null && <span className="seg-count">{count}</span>}
          </button>
        );
      })}
    </div>
  );
}
