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
import { isSdrPlaying, subscribeSdrAudio, toggleSdrAudio } from "../sdrAudio";
import type { SdrListening } from "../sdrSession";
import { SdrTape } from "./SdrTape";
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

  // Reflect what the audio element is actually doing. The sheet does not own it —
  // it is parked in <body> for the life of the lease — so the transport reads the
  // element's state rather than any state of its own (sdrAudio.ts).
  const [playing, setPlaying] = useState(() => isSdrPlaying());
  useEffect(() => {
    setPlaying(isSdrPlaying());
    return subscribeSdrAudio(setPlaying);
  }, []);

  // The tap that opened the field asked for the keypad, so put the caret in it.
  // Keyed on `editing`, NOT on the draft text: re-selecting on every keystroke fights
  // the soft keyboard. Done in an effect rather than with autoFocus, which fires
  // before layout and which the a11y lint rightly refuses.
  const editing = draft !== null;
  useEffect(() => {
    if (editing) freqInput.current?.select();
  }, [editing]);

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
            {/* Explicit commit and cancel. Committing on blur meant that anything
                which stole focus — a re-render, the soft keyboard closing — read as
                the field disappearing on its own, so the edit ends only when the
                owner says it does. */}
            <button
              type="button"
              className="sdr-freq-ok"
              aria-label="Tune to this frequency"
              disabled={busy}
              onClick={commitDraft}
            >
              Go
            </button>
            <button
              type="button"
              className="sdr-freq-cancel"
              aria-label="Cancel"
              onClick={() => setDraft(null)}
            >
              ✕
            </button>
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

      {/* Live radio is playing or it is not: there is no timeline to scrub, which is
          why the native transport sat at 0:00 / 0:00. Pausing drops the connection and
          resuming rejoins the broadcast as it is now, rather than replaying a backlog. */}
      {/* Layout B, the instrument face: the tape IS the panel and the one reading
          worth keeping — how long this session has held the tuner — is inset on it,
          in the quiet band the waveform rarely reaches. The signal meter that used to
          sit above is gone: it measured demodulated AUDIO level, not reception
          strength, so it read high on an empty FM channel's hiss, and the tape shows
          that same quantity far better. */}
      <div className="sdr-face">
        <SdrTape />
        <span className="sdr-face-elapsed">{elapsed(listening.elapsed_s)}</span>
      </div>
      <div className="sdr-transport">
        <button
          type="button"
          className="sdr-play"
          aria-label={playing ? "Pause" : "Play"}
          aria-pressed={playing}
          onClick={toggleSdrAudio}
        >
          {playing ? "❚❚" : "▶"}
        </button>
        <span className={`sdr-livedot${playing ? " sdr-livedot-on" : ""}`} aria-hidden="true" />
        <span className={`sdr-livetag${playing ? " sdr-livetag-on" : ""}`}>
          {playing ? "LIVE" : "PAUSED"}
        </span>
      </div>

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
