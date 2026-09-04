// The live spectrum: pick a band, watch it.
//
// **This tab is the interim home for the waterfall.** The launcher's chosen shape
// (docs/mocks/sdr-launcher/README.md, shape A) makes the RADIO the object, so a
// spectrum is one of the jobs chosen inside a radio rather than a tab of its own. That
// restructure needs the api to honour a NAMED radio — today `/sdr/spectrum` takes
// whichever general radio `roles.py` picks — so it is its own wave. Until then a tab is
// the honest place to put a working picture, and it is deliberately built out of the
// pieces shape A wants: the band sheet, the waterfall, and the session as the state.
//
// The picture is drawn from a session that OUTLIVES this tab. Closing it stops the
// stream and leaves the radio held, the same way audio outlives the tuner sheet — so
// coming back is instant, and releasing is a thing the owner does on purpose.

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import {
  type BandSection,
  type SdrBands,
  type SpectrumRange,
  dutyNote,
  loadBands,
  sectionAt,
} from "../sdrBands";
import { type SdrListening, sessionFor, useSdrSession } from "../sdrSession";
import { startSdrSpectrum, stopSdrSpectrum } from "../sdrSpectrum";
import { SdrBandSheet } from "./SdrBandSheet";
import { SdrWaterfall } from "./SdrWaterfall";

export function SdrSpectrumTab() {
  const sdr = useSdrSession();
  const watching = sessionFor(sdr, "spectrum");
  const [sheet, setSheet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bands, setBands] = useState<SdrBands | null>(null);

  useEffect(() => {
    let alive = true;
    void loadBands().then(
      (next) => alive && setBands(next),
      () => undefined, // the table is for labels; the picture works without it
    );
    return () => {
      alive = false;
    };
  }, []);

  // The stream follows the SESSION, not the tab: opening it with nothing to draw would
  // hold a socket against a 409, and leaving it open after the tab closes would keep a
  // radio's rows flowing through the api for nothing.
  const live = watching !== null;
  useEffect(() => {
    if (!live) return;
    startSdrSpectrum();
    return () => stopSdrSpectrum();
  }, [live]);

  const point = useCallback(
    async (range: SpectrumRange) => {
      setSheet(false);
      setBusy(true);
      try {
        // Moving an existing picture rather than restarting it, so the radio is never
        // released in between — that window is how a waterfall disappears because the
        // owner changed band and something else took the dongle.
        if (watching) await api.sdrSpectrumTune(range, watching.session_id);
        else await api.sdrSpectrumStart(range);
        setError(null);
      } catch (err) {
        // The 409 names the job holding the radio, and the 400 explains a band that
        // cannot be drawn. Both are sentences meant for the owner (CLAUDE.md #10).
        setError(err instanceof ApiError ? err.message : "Couldn't start the spectrum.");
      } finally {
        setBusy(false);
      }
    },
    [watching],
  );

  async function release(session: SdrListening) {
    setBusy(true);
    try {
      stopSdrSpectrum();
      await api.sdrStop(session.session_id);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't release the radio.");
    } finally {
      setBusy(false);
    }
  }

  const section =
    bands && watching?.sweep
      ? sectionAt(bands.sections, watching.sweep.start_hz, watching.sweep.stop_hz)
      : null;

  return (
    <>
      {error && (
        <p className="radio-error" role="alert">
          {error}
        </p>
      )}

      {watching ? (
        <>
          <BandButton section={section} session={watching} onOpen={() => setSheet(true)} />
          <SdrWaterfall />
          {section && dutyNote(section) && (
            // Said on the picture, not in a help page: a span wide enough to need
            // several retunes watches any one frequency for a fraction of each second,
            // and a burst can fall between visits. A waterfall that hid that would look
            // exactly like one that could not miss anything.
            <p className="radio-hint">{dutyNote(section)}</p>
          )}
          <button
            type="button"
            className="aprs-toggle aprs-toggle-on"
            disabled={busy}
            onClick={() => void release(watching)}
          >
            Stop watching
          </button>
          <p className="radio-hint">
            Holds a radio until released. Leaving this tab keeps it running — the picture comes
            straight back.
          </p>
        </>
      ) : (
        <>
          <div className="radio-tuner">
            <div className="radio-tuner-freq">—</div>
            <div className="radio-tuner-sub">
              Nothing is being watched. Pick a band and the box draws it, one row a second.
            </div>
          </div>
          <button
            type="button"
            className="aprs-toggle"
            disabled={busy}
            onClick={() => setSheet(true)}
          >
            Choose a band
          </button>
          <p className="radio-hint">
            Takes a radio for as long as it runs. Shortwave can be listened to but not drawn — the
            sweep tool cannot use the direct-sampling path.
          </p>
        </>
      )}

      {sheet && (
        <SdrBandSheet
          purpose="spectrum"
          onPick={(range) => void point(range)}
          onClose={() => setSheet(false)}
        />
      )}
    </>
  );
}

function BandButton({
  section,
  session,
  onOpen,
}: {
  section: BandSection | null;
  session: SdrListening;
  onOpen: () => void;
}) {
  const sweep = session.sweep;
  return (
    <button type="button" className="band" onClick={onOpen}>
      <span className="bband">
        <span className="bt">
          {section ? `${section.band} · ${section.name}` : "Hand-entered range"}
        </span>
        <span className="bd">
          {sweep
            ? `${mhz(sweep.start_hz)}–${mhz(sweep.stop_hz)} MHz · ${(sweep.bin_hz / 1000).toFixed(sweep.bin_hz >= 10_000 ? 0 : 1)} kHz bins`
            : "—"}
        </span>
      </span>
      <span className="bcaret">›</span>
    </button>
  );
}

function mhz(hz: number): string {
  const at = hz / 1_000_000;
  return at >= 100 ? at.toFixed(2) : at.toFixed(3);
}
