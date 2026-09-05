// The tuned-station sheet — the binding spec at docs/mocks/sdr-tuner/a-tuner-sheet.html.
//
// Composes the shared <Sheet> rather than inventing a modal: Sheet.tsx's own header
// calls bespoke modals a design-doc violation, and composing it inherits all five of
// its dismiss paths (scrim, Escape, swipe-down, the grab handle, platform Back).
//
// Content order is binding: readout + tune steppers, mode, signal, transport, actions.
// Release is a first-class action because it is what hands this session's radio back — and
// what makes the omnibox icon disappear, since the icon IS the lease.

import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { mhz } from "../mhz";
import { isSdrPlaying, subscribeSdrAudio, toggleSdrAudio } from "../sdrAudio";
import {
  sdrCaptions,
  startSdrCaptions,
  stopSdrCaptions,
  subscribeSdrCaptions,
} from "../sdrCaptions";
import type { SdrListening } from "../sdrSession";
import { confidenceColor } from "./AudioTranscript";
import { SdrTape } from "./SdrTape";
import { Sheet } from "./Sheet";
import { PauseIcon, PlayIcon } from "./icons";

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

// What the RADIO reaches, mirrored from the api and the sidecar so a typo is caught
// under the owner's thumb rather than as a round trip that comes back an error.
//
// 0.1, not 24. This said 24 — the R820T2 TUNER's floor — and refused with "This radio
// tunes 24-1766 MHz" for everything below it, while `rtl_fm -E direct2` has been
// listening down to 100 kHz the whole time and every route behind it bounds on
// `TUNABLE_MIN_MHZ`. A duplicated tuner floor refusing what the box can do is the exact
// bug class `jbrain/sdr/tuner.py` exists to end, reappearing one layer up.
const MIN_MHZ = 0.1;
const MAX_MHZ = 1766;
// ...and the hole that lowering the floor opened. Below 24 MHz the R820T2 is powered
// down and the RTL2832U's ADC is fed straight from the antenna at 28.8 MHz, so the
// honest range down there stops at half of that. 14.4-24 MHz is the SECOND Nyquist
// zone: ask for 18.1 and the radio hands back 10.7, mirrored, while reporting a healthy
// session at the frequency you typed. Refused rather than warned, because there is
// nothing in the audio to tell the owner they are somewhere else.
const NYQUIST_MHZ = 14.4;
const TUNER_MIN_MHZ = 24;
const ADC_RATE_MHZ = 28.8;

/** Why the radio cannot honestly be tuned there, or null.
 *
 *  Mirrored from `listen.aliased_refusal`, which is where it is enforced — this is the
 *  reading that catches it under the owner's thumb rather than as a round trip. Both
 *  the steppers and the typed field ask it: stepping up from 14.35 MHz walks into the
 *  same zone as typing 18.1, and a guard on only one of them is a guard on neither. */
function whyNotTunable(mhzValue: number): string | null {
  if (mhzValue < MIN_MHZ || mhzValue > MAX_MHZ) {
    return `This radio tunes ${MIN_MHZ}-${MAX_MHZ} MHz.`;
  }
  if (mhzValue > NYQUIST_MHZ && mhzValue < TUNER_MIN_MHZ) {
    const image = (ADC_RATE_MHZ - mhzValue).toFixed(3);
    return (
      `Nothing between ${NYQUIST_MHZ} and ${TUNER_MIN_MHZ} MHz: down here the radio ` +
      `bypasses its tuner and samples at ${ADC_RATE_MHZ} MHz, so you would hear ` +
      `${image} MHz instead.`
    );
  }
  return null;
}

interface Props {
  listening: SdrListening;
  onClose: () => void;
}

interface ControlsProps {
  listening: SdrListening;
  /** Called after Release succeeds. The sheet dismisses itself; the Radio screen's
   * Tuner tab has nothing to dismiss and simply falls back to its idle state. */
  onReleased: () => void;
}

function elapsed(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

export function SdrTunerSheet({ listening, onClose }: Props) {
  return (
    <Sheet title="Tuned station" onClose={onClose}>
      <SdrTunerControls listening={listening} onReleased={onClose} />
    </Sheet>
  );
}

/** The transport itself — readout, steppers, mode, signal, play/pause, captions,
 * Release. Extracted from the sheet so the Radio screen mounts the real controls
 * instead of a second, read-only rendering of the same lease: the tab used to show
 * frequency and mode as text while the only way to actually drive the radio was the
 * composer's icon. One implementation, two mounts. */
export function SdrTunerControls({ listening, onReleased }: ControlsProps) {
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

  // Captions hold a whisper model resident on the box's GPU next to the chat model,
  // so they are opt-in and stop with the sheet rather than running unattended.
  const [captions, setCaptions] = useState(() => sdrCaptions());
  useEffect(() => {
    setCaptions(sdrCaptions());
    return subscribeSdrCaptions(setCaptions);
  }, []);
  useEffect(() => () => stopSdrCaptions(), []);

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

  const step = (direction: number) => {
    const target = (listening.frequency_hz + direction * stepHz) / 1_000_000;
    // Checked here too, not only in the typed field: a 100 kHz step held down from
    // 14.35 MHz walks into the aliasing zone one tap at a time, and the owner would
    // have no reason to suspect the frequency stopped meaning what it says.
    const refusal = whyNotTunable(target);
    if (refusal !== null) {
      setError(refusal);
      return;
    }
    return act(() => api.sdrTune(target, undefined, listening.session_id));
  };

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
    const refusal = whyNotTunable(value);
    if (refusal !== null) {
      setError(refusal);
      return;
    }
    if (value === listening.frequency_hz / 1_000_000) return;
    void act(() => api.sdrTune(value, undefined, listening.session_id));
  };

  return (
    <>
      <p className="sdr-note">This session holds its radio until you release it.</p>

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
        {captions.on && (
          // Burned in over the waveform like a subtitle over a picture, so captions
          // cost no height. The plate is half-transparent deliberately: the tape stays
          // readable THROUGH the words, which is the point of putting them here.
          <p className="sdr-caption">
            <span>
              {captions.error
                ? captions.error
                : captions.latest
                  ? captions.latest.words.length > 0
                    ? captions.latest.words.map((word, i) => (
                        // Same rose-amber-green scale as the transcript viewer, from
                        // the same exported function — narrowband voice degrades in a
                        // patterned way, and numbers are both the least certain and
                        // usually the payload.
                        <span
                          key={`${word.text}-${i}`}
                          style={{ color: confidenceColor(word.confidence) }}
                        >
                          {word.text}{" "}
                        </span>
                      ))
                    : captions.latest.text
                  : "Listening…"}
            </span>
          </p>
        )}
      </div>
      <div className="sdr-transport">
        <button
          type="button"
          className="sdr-play"
          aria-label={playing ? "Pause" : "Play"}
          aria-pressed={playing}
          onClick={toggleSdrAudio}
        >
          {playing ? <PauseIcon size={20} /> : <PlayIcon size={20} />}
        </button>
        <span className={`sdr-livedot${playing ? " sdr-livedot-on" : ""}`} aria-hidden="true" />
        <span className={`sdr-livetag${playing ? " sdr-livetag-on" : ""}`}>
          {playing ? "LIVE" : "PAUSED"}
        </span>
        <button
          type="button"
          className="sdr-cc"
          aria-pressed={captions.on}
          aria-label="Live captions"
          onClick={() => (captions.on ? stopSdrCaptions() : startSdrCaptions())}
        >
          CC
        </button>
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
              onReleased();
            })
          }
        >
          Release
        </button>
      </div>
    </>
  );
}
