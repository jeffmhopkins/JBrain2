// The tuned-station sheet — the binding spec at docs/mocks/sdr-tuner/a-tuner-sheet.html.
//
// Composes the shared <Sheet> rather than inventing a modal: Sheet.tsx's own header
// calls bespoke modals a design-doc violation, and composing it inherits all five of
// its dismiss paths (scrim, Escape, swipe-down, the grab handle, platform Back).
//
// Content order is binding: readout + tune steppers, mode, signal, transport, actions.
// Release is a first-class action because it is what hands the single tuner back — and
// what makes the omnibox icon disappear, since the icon IS the lease.

import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { attachSdrAudio } from "../sdrAudio";
import type { SdrListening } from "../sdrSession";
import { Sheet } from "./Sheet";

const MODES = ["wbfm", "fm", "am", "usb"] as const;

// The tuning step is the owner's to pick, because no single value fits the bands this
// radio covers: broadcast FM channels are 200 kHz apart, so stepping a fixed 25 kHz
// meant eight taps per station, while the VHF/UHF voice bands need 12.5 or 25 to land
// on a channel at all. The values are the real channel spacings in use — 9 kHz is
// AM/MW outside the Americas, 10 kHz inside them, 200 kHz the US FM raster.
const STEPS_HZ = [1_000, 5_000, 9_000, 10_000, 12_500, 25_000, 50_000, 100_000, 200_000];
// Opening on a step that suits the mode makes the common case need no choice at all,
// the same reasoning that puts an 88-108 MHz request on wbfm without being asked.
// An explicit pick always wins over this.
const DEFAULT_STEP_HZ: Record<string, number> = { wbfm: 100_000, am: 10_000 };
const FALLBACK_STEP_HZ = 25_000;

function stepLabel(hz: number): string {
  return hz >= 1_000_000 ? `${hz / 1_000_000} MHz` : `${hz / 1000} kHz`;
}

// The R820T2's real range, mirrored from the api and the sidecar so a typo is caught
// under the owner's thumb rather than as a round trip that comes back an error.
const MIN_MHZ = 24;
const MAX_MHZ = 1766;

interface Props {
  listening: SdrListening;
  onClose: () => void;
}

function mhz(hz: number): string {
  return (hz / 1_000_000).toFixed(3);
}

function elapsed(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

export function SdrTunerSheet({ listening, onClose }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Record is arm-then-confirm per DESIGN.md's destructive-action doctrine; the
  // recording lane itself is a later wave, so the control states that plainly
  // rather than pretending to work.
  const audioSlot = useRef<HTMLDivElement | null>(null);
  // Null until the owner picks one, so the mode's default can keep applying as they
  // switch bands; an explicit choice then sticks for the rest of the session.
  const [pickedStep, setPickedStep] = useState<number | null>(null);
  const [stepOpen, setStepOpen] = useState(false);
  const stepHz = pickedStep ?? DEFAULT_STEP_HZ[listening.mode] ?? FALLBACK_STEP_HZ;
  // Stepping is for hunting around a known spot; typing is for going somewhere else
  // entirely. Null means the readout is showing, a string means it is being edited —
  // held as text so a half-typed "99." is a legal intermediate state.
  const [draft, setDraft] = useState<string | null>(null);
  const freqInput = useRef<HTMLInputElement | null>(null);

  // Borrow the shared audio element while this sheet is mounted. It is NOT created or
  // destroyed here: the stream belongs to the lease, so closing the sheet must not
  // silence the radio (sdrAudio.ts explains why it can be moved without restarting).
  useEffect(() => attachSdrAudio(audioSlot.current), []);

  // The tap that opened the field asked for the keypad, so put the caret in it —
  // done here rather than with autoFocus, which fires before the field is laid out
  // and which the a11y lint rightly refuses.
  useEffect(() => {
    if (draft !== null) freqInput.current?.select();
  }, [draft]);

  const act = async (run: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await run();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "That didn't work.");
    } finally {
      setBusy(false);
    }
  };

  const step = (direction: number) =>
    act(() =>
      api.sdrTune(
        (listening.frequency_hz + direction * stepHz) / 1_000_000,
        undefined,
        listening.session_id,
      ),
    );

  const commitDraft = () => {
    const typed = draft;
    setDraft(null);
    if (typed === null) return;
    const value = Number(typed.trim());
    // A refusal has to say the range, or the only way to find it is to guess.
    if (!typed.trim() || Number.isNaN(value)) {
      setError("That isn't a frequency. Enter it in MHz, like 99.3.");
      return;
    }
    if (value < MIN_MHZ || value > MAX_MHZ) {
      setError(`This radio tunes ${MIN_MHZ}-${MAX_MHZ} MHz.`);
      return;
    }
    if (value === listening.frequency_hz / 1_000_000) return;
    void act(() => api.sdrTune(value, undefined, listening.session_id));
  };

  const bars = Math.min(6, Math.round(listening.peak * 12));

  return (
    <Sheet title="Tuned station" onClose={onClose}>
      <p className="sdr-note">
        The radio has one tuner, so this session holds it until you release it.
      </p>

      <div className="sdr-readout">
        {draft === null ? (
          <button
            type="button"
            className="sdr-freq"
            aria-label={`Tuned to ${mhz(listening.frequency_hz)} MHz. Tap to enter a frequency.`}
            disabled={busy}
            onClick={() => setDraft(mhz(listening.frequency_hz))}
          >
            {mhz(listening.frequency_hz)}
            <span className="sdr-unit">MHz</span>
          </button>
        ) : (
          <div className="sdr-freq sdr-freq-edit">
            {/* inputMode decimal, not type=number: it raises the phone's number pad
                WITH a decimal point and no spinners, which is the whole ask. */}
            <input
              ref={freqInput}
              className="sdr-freq-input"
              type="text"
              inputMode="decimal"
              aria-label="Frequency in MHz"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onFocus={(event) => event.target.select()}
              onBlur={commitDraft}
              onKeyDown={(event) => {
                if (event.key === "Enter") commitDraft();
                // Escape abandons the edit — the sheet's own Escape handler would
                // otherwise close the whole thing over a mistyped digit.
                if (event.key === "Escape") {
                  event.stopPropagation();
                  setDraft(null);
                }
              }}
            />
            <span className="sdr-unit">MHz</span>
          </div>
        )}
        <div className="sdr-station">{listening.mode.toUpperCase()}</div>
        <div className="sdr-tuner">
          <button
            type="button"
            className="sdr-step"
            aria-label="Tune down"
            disabled={busy}
            onClick={() => void step(-1)}
          >
            −
          </button>
          <button
            type="button"
            className="sdr-stepsize"
            aria-label={`Tuning step, ${stepLabel(stepHz)}. Tap to change.`}
            aria-expanded={stepOpen}
            onClick={() => setStepOpen((open) => !open)}
          >
            {stepLabel(stepHz)}
          </button>
          <button
            type="button"
            className="sdr-step"
            aria-label="Tune up"
            disabled={busy}
            onClick={() => void step(1)}
          >
            +
          </button>
        </div>
        {stepOpen && (
          <div className="sdr-steps" aria-label="Tuning step">
            {STEPS_HZ.map((hz) => (
              <button
                key={hz}
                type="button"
                aria-pressed={hz === stepHz}
                className={`sdr-stepopt${hz === stepHz ? " sdr-stepopt-on" : ""}`}
                onClick={() => {
                  setPickedStep(hz);
                  setStepOpen(false);
                }}
              >
                {stepLabel(hz)}
              </button>
            ))}
          </div>
        )}
      </div>

      <p className="sdr-label">Mode</p>
      <div className="seg-row sdr-modes" aria-label="Demodulation mode">
        {MODES.map((mode) => (
          <button
            key={mode}
            type="button"
            className={`seg${mode === listening.mode ? " seg-on" : ""}`}
            aria-pressed={mode === listening.mode}
            disabled={busy}
            onClick={() =>
              void act(() =>
                api.sdrTune(listening.frequency_hz / 1_000_000, mode, listening.session_id),
              )
            }
          >
            {mode.toUpperCase()}
          </button>
        ))}
      </div>

      <p className="sdr-label">Signal</p>
      <div className="sdr-meter">
        <span className="sdr-bars" aria-hidden="true">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <i key={i} className={i < bars ? "on" : ""} />
          ))}
        </span>
        {/* The level, not the transcript, is what says a signal is really there. */}
        <span className="sdr-level">{Math.round(listening.peak * 100)}%</span>
        <span className="sdr-elapsed">{elapsed(listening.elapsed_s)}</span>
      </div>

      {/* The shared audio element is moved in here while the sheet is open. */}
      <div className="sdr-audio-slot" ref={audioSlot} />

      {error && <p className="sdr-error">{error}</p>}

      <div className="sdr-actions">
        <button type="button" className="sdr-act sdr-act-ghost" disabled title="Coming next">
          Record
        </button>
        <button
          type="button"
          className="sdr-act sdr-act-release"
          disabled={busy}
          onClick={() =>
            void act(async () => {
              await api.sdrStop(listening.session_id);
              onClose();
            })
          }
        >
          Release
        </button>
      </div>
    </Sheet>
  );
}
