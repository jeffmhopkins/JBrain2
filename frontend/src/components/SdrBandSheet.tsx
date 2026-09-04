// The band picker: the front door to everything the radio does.
//
// Binding spec: docs/mocks/sdr-launcher/shapes.html — "the band picker is the front
// door", identical across all three shapes and therefore not part of what the owner
// chose between. 32 curated US sections, each carrying its own mode, step, channel
// spacing and sweep settings, because those settings are only correct TOGETHER
// (jbrain/sdr/bands.py says why at length).
//
// **Manual entry is the last item in the sheet, not a control on the surface.** The
// expert path reaches anywhere the radio does; it is simply not the ordinary way in,
// and putting it on the main screen would make the curated list look like a shortcut
// rather than the answer.

import { useEffect, useState } from "react";
import {
  type BandSection,
  type SdrBands,
  type SpectrumRange,
  byBand,
  dutyNote,
  loadBands,
  whyNotLive,
} from "../sdrBands";
import { Sheet } from "./Sheet";

export function SdrBandSheet({
  /** What the sheet is choosing FOR. `listen` offers every section; `spectrum` disables
   *  the ones the sweep tool cannot reach, with the reason on the row. */
  purpose,
  onPick,
  onClose,
}: {
  purpose: "listen" | "spectrum";
  onPick: (range: SpectrumRange, section: BandSection | null) => void;
  onClose: () => void;
}) {
  const [bands, setBands] = useState<SdrBands | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [manual, setManual] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void loadBands().then(
      (next) => alive && setBands(next),
      () => alive && setError("Couldn't read the band table."),
    );
    return () => {
      alive = false;
    };
  }, []);

  return (
    <Sheet title="Choose a band" onClose={onClose}>
      {error && (
        <p className="radio-error" role="alert">
          {error}
        </p>
      )}
      {!bands && !error && <p className="radio-empty">Reading the band table…</p>}
      {bands && manual === null && (
        <div className="bandlist">
          {byBand(bands.sections).map(([band, sections]) => (
            <div key={band}>
              <p className="bgroup">{band}</p>
              {sections.map((section) => (
                <BandRow
                  key={section.id}
                  section={section}
                  purpose={purpose}
                  onPick={() => onPick({ section: section.id }, section)}
                />
              ))}
            </div>
          ))}
          <button type="button" className="bitem" onClick={() => setManual("")}>
            <span className="bband">
              <span className="bt">Enter a frequency…</span>
              <span className="bd">
                Anywhere from {edge(bands.tuner_min_hz)} to {edge(bands.tuner_max_hz)} MHz. Settings
                are inherited from whichever section it lands in.
              </span>
            </span>
          </button>
        </div>
      )}
      {bands && manual !== null && (
        <ManualEntry
          bands={bands}
          purpose={purpose}
          value={manual}
          onChange={setManual}
          onPick={onPick}
          onBack={() => setManual(null)}
        />
      )}
    </Sheet>
  );
}

function BandRow({
  section,
  purpose,
  onPick,
}: {
  section: BandSection;
  purpose: "listen" | "spectrum";
  onPick: () => void;
}) {
  // Only the waterfall has bands it cannot reach. Listening works everywhere the radio
  // tunes — including the shortwave the sweep tool cannot touch — so disabling a row
  // for the tuner would take away something that works.
  const refusal = purpose === "spectrum" ? whyNotLive(section) : null;
  const duty = purpose === "spectrum" ? dutyNote(section) : null;

  return (
    <button type="button" className="bitem" onClick={onPick} disabled={refusal !== null}>
      <span className="bband">
        <span className="bt">
          {section.band} · {section.name}
          {section.direct_sampling && <span className="btag hf">HF</span>}
          {section.hops > 1 && <span className="btag slow">{section.hops} hops</span>}
        </span>
        <span className="bd">{refusal ?? duty ?? section.note}</span>
      </span>
      <span className="bhz">
        {edge(section.start_hz)}–{edge(section.stop_hz)}
      </span>
    </button>
  );
}

/** The expert path. A frequency, and a width around it — because a waterfall needs a
 *  span and a single number is not one. The width defaults to the widest single hop the
 *  radio takes, which is the most that can be watched without the picture starting to
 *  miss things (bands.py, `HOP_MAX_HZ`). */
function ManualEntry({
  bands,
  purpose,
  value,
  onChange,
  onPick,
  onBack,
}: {
  bands: SdrBands;
  purpose: "listen" | "spectrum";
  value: string;
  onChange: (next: string) => void;
  onPick: (range: SpectrumRange, section: BandSection | null) => void;
  onBack: () => void;
}) {
  const [widthMhz, setWidthMhz] = useState(2);
  const at = Number.parseFloat(value);
  const low = bands.tuner_min_hz / 1_000_000;
  const high = bands.tuner_max_hz / 1_000_000;
  const legal = Number.isFinite(at) && at >= low && at <= high;
  // Which curated section it lands in, if any. Its settings are what a manual frequency
  // inherits — the alternative is asking the owner for a mode and a step they have no
  // way to know, on a band the table already describes.
  const landed =
    bands.sections.find((s) => at * 1_000_000 >= s.start_hz && at * 1_000_000 <= s.stop_hz) ?? null;

  return (
    <div className="manual">
      <button type="button" className="manual-back" onClick={onBack}>
        ‹ Back to the list
      </button>
      <label className="sheet-field" htmlFor="manual-mhz">
        Frequency (MHz)
        <input
          id="manual-mhz"
          inputMode="decimal"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={`${low} – ${high}`}
        />
      </label>
      {purpose === "spectrum" && (
        <label className="sheet-field" htmlFor="manual-width">
          Width (MHz)
          <input
            id="manual-width"
            inputMode="decimal"
            value={widthMhz}
            onChange={(event) => setWidthMhz(Number.parseFloat(event.target.value) || 0)}
          />
        </label>
      )}
      <p className="sheet-hint">
        {!value
          ? "Anywhere the radio reaches."
          : !legal
            ? `Outside what this radio reaches (${low}–${high} MHz).`
            : landed
              ? `Lands in ${landed.band} · ${landed.name} — ${landed.mode.toUpperCase()}, steps of ${landed.step_hz / 1000} kHz.`
              : "No curated section covers this, so it keeps the mode you are already on."}
      </p>
      <button
        type="button"
        className="sheet-primary"
        disabled={!legal || (purpose === "spectrum" && widthMhz <= 0)}
        onClick={() =>
          onPick(
            purpose === "spectrum"
              ? { startMhz: at - widthMhz / 2, stopMhz: at + widthMhz / 2 }
              : { startMhz: at, stopMhz: at },
            landed,
          )
        }
      >
        Go there
      </button>
    </div>
  );
}

/** A range EDGE in a list, abbreviated on purpose — "88–108", not "88.000–108.000".
 *  Deliberately not `mhz()`: this is a label for browsing, and the full precision that
 *  matters when naming a channel is noise in a column of thirty-two of them. */
function edge(hz: number): string {
  const at = hz / 1_000_000;
  if (at >= 100) return at.toFixed(0);
  if (at >= 1) return at.toFixed(1);
  return at.toFixed(3);
}
