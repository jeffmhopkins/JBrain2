// The spectrum job's surface: pick a band, watch it.
//
// One of the four things a radio can be doing (docs/mocks/sdr-launcher/shapes.html
// shape A), so it takes the radio it belongs to and the session holding it rather than
// resolving either itself. That is what lets the roster show a waterfall per radio
// without two of them fighting over which session is "the" spectrum.
//
// The picture is drawn from a session that OUTLIVES this surface. Leaving the radio's
// detail stops the stream and leaves the radio held, the same way audio outlives the
// tuner sheet — so coming back is instant, and releasing is a thing the owner does on
// purpose.

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import { mhz } from "../mhz";
import {
  type BandSection,
  type SdrBands,
  type SpectrumRange,
  dutyNote,
  loadBands,
  sectionAt,
} from "../sdrBands";
import type { SdrListening } from "../sdrSession";
import { startSdrSpectrum, stopSdrSpectrum } from "../sdrSpectrum";
import { SdrBandSheet } from "./SdrBandSheet";
import { SdrWaterfall } from "./SdrWaterfall";

export function SdrSpectrumJob({
  serial,
  /** The spectrum session on THIS radio, or null when it is not watching yet. */
  session,
  onChanged,
}: {
  serial: string;
  session: SdrListening | null;
  onChanged: () => void;
}) {
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

  // The stream follows the SESSION, not the surface: opening it with nothing to draw
  // would hold a socket against a 409, and leaving it open after the owner navigates
  // away would keep a radio's rows flowing through the api for nothing.
  //
  // `GET /api/sdr/spectrum` serves THE spectrum session, which is safe only because
  // there can be one: `sdrJobs.jobAllowed` disables Spectrum on a second radio while a
  // first is watching, naming the one that has it. If that ever stops being true the
  // route needs a serial — the picture would otherwise be of the other radio's band,
  // and every row would draw correctly at the wrong frequencies.
  const live = session !== null;
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
        // owner changed band.
        if (session) await api.sdrSpectrumTune(range, session.session_id);
        else await api.sdrSpectrumStart(range, serial);
        setError(null);
        onChanged();
      } catch (err) {
        // The 409 names the job holding the radio; the 400 explains a band that cannot
        // be drawn. Both are sentences meant for the owner (CLAUDE.md #10).
        setError(err instanceof ApiError ? err.message : "Couldn't start the spectrum.");
      } finally {
        setBusy(false);
      }
    },
    [session, serial, onChanged],
  );

  const section =
    bands && session?.sweep
      ? sectionAt(bands.sections, session.sweep.start_hz, session.sweep.stop_hz)
      : null;

  return (
    <>
      {error && (
        <p className="radio-error" role="alert">
          {error}
        </p>
      )}

      <BandButton section={section} session={session} onOpen={() => setSheet(true)} />

      {session ? (
        <>
          <SdrWaterfall />
          {section && dutyNote(section) && (
            // Said on the picture, not in a help page: a span wide enough to need
            // several retunes watches any one frequency for a fraction of each second,
            // and a burst can fall between visits. A waterfall that hid that would look
            // exactly like one that could not miss anything.
            <p className="radio-hint">{dutyNote(section)}</p>
          )}
        </>
      ) : (
        <p className="radio-hint">
          Pick a band and the box draws it, one row a second, for as long as this radio is watching.
          Shortwave can be listened to but not drawn — the sweep tool cannot use the direct-sampling
          path.
        </p>
      )}

      {sheet && (
        <SdrBandSheet
          purpose="spectrum"
          onPick={(range) => void point(range)}
          onClose={() => setSheet(false)}
        />
      )}
      {busy && <p className="radio-hint">Working…</p>}
    </>
  );
}

function BandButton({
  section,
  session,
  onOpen,
}: {
  section: BandSection | null;
  session: SdrListening | null;
  onOpen: () => void;
}) {
  const sweep = session?.sweep;
  return (
    <button type="button" className="band" onClick={onOpen}>
      <span className="bband">
        <span className="bt">
          {section
            ? `${section.band} · ${section.name}`
            : sweep
              ? "Hand-entered range"
              : "Choose a band"}
        </span>
        <span className="bd">
          {sweep
            ? `${mhz(sweep.start_hz)}–${mhz(sweep.stop_hz)} MHz · ${khz(sweep.bin_hz)} kHz bins`
            : "Nothing is being watched on this radio."}
        </span>
      </span>
      <span className="bcaret">›</span>
    </button>
  );
}

function khz(hz: number): string {
  const at = hz / 1000;
  return at >= 10 ? at.toFixed(0) : at.toFixed(1);
}
