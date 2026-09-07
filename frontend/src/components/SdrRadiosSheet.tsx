// The omnibox radio sheet: which radio, and what it is doing.
//
// Binding spec: `docs/mocks/omnibox-radios/d-radio-then-task.html` **shape D**, chosen
// 2026-09-07 over a swipe pager, a plain radio switcher, and every radio stacked at
// once. Pick the radio; a Doing row shows and changes what it is on; that job's live
// surface follows.
//
// **What it replaces.** The icon opened the tuner sheet with `sdr.listening` and
// nothing else, and that sheet has no notion of purpose — so a radio running APRS or a
// spectrum got a tuner drawn for something producing no audio, and a second radio had
// nowhere to appear at all. `sdr.sessions` has carried every held radio since the lease
// became per-radio; nothing was reading it.
//
// **It is a CONTROL, not a window**, and that is the whole reason this shape won: the
// job row here is the same `RadioJob` the Radios tab uses, so a radio can be moved from
// a spectrum to a station without leaving the composer — and the confirm that guards
// stopping a running job comes with it rather than being reimplemented.

import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import { jobLabel, jobOf, sessionOn } from "../sdrJobs";
import { type SdrRadio, type SdrRadios, labelFor } from "../sdrRadios";
import { useSdrSession } from "../sdrSession";
import { RadioJob } from "./SdrRadiosTab";
import { Sheet } from "./Sheet";

interface Props {
  /** Which radio to open on — the one the omnibox icon was reflecting. */
  openOn: string | null;
  /** Leave for the APRS log (the Radio screen), which is where its health lives. */
  onOpenAprs: () => void;
  onClose: () => void;
}

/** Every radio worth a tab: one the box can see, or one it is holding anyway. */
export function tabbable(radios: SdrRadios): SdrRadio[] {
  return radios.radios.filter((radio) => radio.attached || radio.role !== "general");
}

export function SdrRadiosSheet({ openOn, onOpenAprs, onClose }: Props) {
  const sdr = useSdrSession();
  // The roster is fetched here rather than passed in: home has never had one, and the
  // alternative — threading it down from `App` — would put a poll behind the whole home
  // screen for a sheet that is usually closed. What each radio is DOING still comes from
  // the shared session store, so the tabs and the job surface cannot disagree.
  const [radios, setRadios] = useState<SdrRadios | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [at, setAt] = useState<string | null>(openOn);

  const refresh = useCallback(async () => {
    try {
      setRadios(await api.getSdrRadios());
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn't read the radios.");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const shown = radios ? tabbable(radios) : [];
  // The tapped radio, or the first that has a tab. DERIVED rather than corrected in
  // state: the omnibox names the radio its icon was reflecting, and by the time the
  // roster arrives that dongle may be unplugged — a serial with no radio behind it must
  // fall through to one that exists, not leave the sheet empty over a box that is
  // plainly holding a radio.
  const here = shown.find((radio) => radio.serial === at) ?? shown[0] ?? null;

  return (
    <Sheet title="Radios" onClose={onClose}>
      {shown.length > 1 && (
        // One tab per radio, each naming what it is doing. With a single radio the row
        // is nothing but chrome, so it is not drawn — the cost D was chosen with is
        // paid only where there is a choice to make.
        <div className="radio-tabs">
          {shown.map((radio) => {
            const session = sessionOn(sdr, radio.serial);
            const job = session ? jobOf(session) : "idle";
            return (
              // Pressed buttons, not a `tablist`: the mock's shape, and honest ARIA —
              // a real tablist owes arrow-key navigation and a labelled tabpanel, and
              // half of one reads worse to a screen reader than a plain toggle.
              <button
                key={radio.serial}
                type="button"
                className="radio-tab"
                aria-pressed={radio.serial === here?.serial}
                onClick={() => setAt(radio.serial)}
              >
                <span className={`radio-dot job-${job}`} aria-hidden="true" />
                <span className="radio-tab-name">{labelFor(radio)}</span>
                <span className="radio-tab-job">{jobLabel(job)}</span>
              </button>
            );
          })}
        </div>
      )}
      {error && (
        <p className="radio-error" role="alert">
          {error}
        </p>
      )}
      {radios && here && (
        <RadioJob
          radio={here}
          radios={radios}
          // No APRS log is fetched for the omnibox: the health line belongs to the
          // screen that owns the log, and `AprsJob` still offers the way there.
          log={null}
          onChanged={() => void refresh()}
          onOpenAprs={onOpenAprs}
        />
      )}
      {radios && !here && <p className="radio-hint">No radio on this box.</p>}
      {!radios && !error && <p className="radio-empty">Looking for radios…</p>}
    </Sheet>
  );
}
