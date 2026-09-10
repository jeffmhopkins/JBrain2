// The tuner controls — the binding spec at docs/mocks/sdr-tuner/a-tuner-sheet.html.
//
// Mounted by the omnibox radio sheet (inside the shared <Sheet>) and by the Radios tab,
// as the `listen` job's surface. The sheet wrapper that used to live here went with the
// omnibox's move to a per-radio sheet (docs/mocks/omnibox-radios/d-radio-then-task.html):
// the omnibox opens on a RADIO now, and what is drawn depends on the job it is holding.
//
// Content order is binding: readout + tune steppers, mode, signal, transport, actions.
// Release is a first-class action because it is what hands this session's radio back — and
// what makes the omnibox icon disappear, since the icon IS the lease.

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { api } from "../api/client";
import { mhz } from "../mhz";
import {
  AUDIO_LATE_S,
  isSdrPlaying,
  sdrAudioLag,
  subscribeSdrAudio,
  toggleSdrAudio,
} from "../sdrAudio";
import { type BandSection, loadBands } from "../sdrBands";
import { bandwidthAdjustable, bandwidthLabel, bandwidthSpoken } from "../sdrBandwidth";
import {
  sdrCaptions,
  startSdrCaptions,
  stopSdrCaptions,
  subscribeSdrCaptions,
} from "../sdrCaptions";
import { channelIndex, channelLabel, namedByFrequency, planAt, stepChannel } from "../sdrChannels";
import { type SdrListening, liveRecording, useSdrSession } from "../sdrSession";
import { startSdrSpectrum, stopSdrSpectrum } from "../sdrSpectrum";
import { formatSize } from "../sdrTrim";
import { whyNotTunable } from "../sdrTunable";
import { confidenceColor } from "./AudioTranscript";
import { SdrTape } from "./SdrTape";
import { SdrTuningView } from "./SdrTuningView";
import { PauseIcon, PlayIcon, RecordIcon } from "./icons";

// Every demodulator the back end has, in the order a dial usually offers them —
// widest first, then the two sidebands. `nfm` is NOT a sixth button: the sidecar maps
// it onto `fm` and gives it the same IF rate, deviation, filter and passband
// (`deploy/sdr/demod.py`, `listen.py` MODES), so a button for it would be a second way
// to ask for the mode already on the row.
//
// LSB was missing here while the back end has always had it, and a band that selects
// it — the 40 m and 80 m ham sections do — put the radio in a mode with no button to
// leave it by, showing `LSB` under the readout above a row of four it was not one of.
const MODES = ["wbfm", "fm", "am", "usb", "lsb"] as const;

// The tuning step is the owner's to pick, because no single value fits the bands this
// radio covers: broadcast FM channels are 200 kHz apart, so stepping a fixed 25 kHz
// meant eight taps per station, while the VHF/UHF voice bands need 12.5 or 25 to land
// on a channel at all. The values are the real channel spacings in use — 9 kHz is
// AM/MW outside the Americas, 10 kHz inside them, 200 kHz the US FM raster.
//
// The bottom three are not channel spacings but SSB tuning, and the reason they exist
// is that 1 kHz — the old floor — is far too coarse on the HF voice bands. SSB has no
// carrier to land on: the dial IS the audio pitch, so being 1 kHz off does not put you
// beside the signal, it moves the voice a whole kilohertz and makes it unintelligible.
// A 3 kHz passband holds a hundred distinct 10 Hz positions, and zero-beating one is
// what the fine steps are for.
const STEPS_HZ = [
  10, 100, 500, 1_000, 5_000, 9_000, 10_000, 12_500, 25_000, 50_000, 100_000, 200_000,
];
// Opening on a step that suits the mode makes the common case need no choice at all,
// the same reasoning that puts an 88-108 MHz request on wbfm without being asked.
// SSB opens on 100 Hz: fine enough to zero-beat a voice, coarse enough that crossing a
// 3 kHz channel is a handful of taps rather than three hundred.
// An explicit pick always wins over this.
const DEFAULT_STEP_HZ: Record<string, number> = {
  wbfm: 100_000,
  am: 10_000,
  usb: 100,
  lsb: 100,
};
const FALLBACK_STEP_HZ = 25_000;

function stepLabel(hz: number): string {
  if (hz >= 1_000_000) return `${hz / 1_000_000} MHz`;
  // Sub-kilohertz steps stay in Hz: "0.1 kHz" is a step size nobody says out loud, and
  // the leading zero is exactly the digit that gets misread on a dial.
  return hz >= 1_000 ? `${hz / 1000} kHz` : `${hz} Hz`;
}

interface ControlsProps {
  listening: SdrListening;
  /** Called after Release succeeds. Both mounts simply fall back to their idle state:
   * the radio the sheet is on stops being held, and its job surface follows. */
  onReleased: () => void;
}

function elapsed(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** LIVE, or LIVE and how far behind — see the transport row. */
export function liveTag(behindS: number | null): string {
  if (behindS === null || behindS < AUDIO_LATE_S) return "LIVE";
  // Whole seconds: a tenth of a second of playback delay is not a thing anyone can act
  // on, and the reading is only ever a rough one — the browser's buffer, not a clock.
  return `LIVE −${Math.round(behindS)}s`;
}

/** The transport itself — readout, steppers, mode, signal, play/pause, captions,
 * Release. Extracted from the sheet so the Radio screen mounts the real controls
 * instead of a second, read-only rendering of the same lease: the tab used to show
 * frequency and mode as text while the only way to actually drive the radio was the
 * composer's icon. One implementation, two mounts. */
/** Rows of the tuning strip to ask for when the sheet opens. The strip's waterfall mode
 *  draws a history like the band picture does, and it was blank on arrival for the same
 *  reason (`SdrSpectrumJob.BACKFILL_ROWS`). */
const BACKFILL_ROWS = 120;

/** How long an armed Record stays armed. From the tuner's binding spec, and the same
 *  2.6 s the delete confirmations elsewhere use. */
const ARM_MS = 2600;

export function SdrTunerControls({ listening, onReleased }: ControlsProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Record is arm-then-confirm (docs/mocks/sdr-tuner/a-tuner-sheet.html: tap, "Tap
  // again", a 2.6 s window). The arming is the only piece of this that is local state —
  // it is ceremony in front of the tap, not a claim about the box. WHETHER a capture is
  // running, how long it has been running and how big it is all come off the 1 Hz status
  // poll, so the button can never go on counting through a capture the box has dropped.
  const [armed, setArmed] = useState(false);
  // Null until the owner picks one, so the mode's default can keep applying as they
  // switch bands; an explicit choice then sticks for the rest of the session.
  const [pickedStep, setPickedStep] = useState<number | null>(null);
  const [stepOpen, setStepOpen] = useState(false);
  const [bwOpen, setBwOpen] = useState(false);
  const stepHz = pickedStep ?? DEFAULT_STEP_HZ[listening.mode] ?? FALLBACK_STEP_HZ;

  // The capture in flight, off the SHARED 1 Hz poll rather than a timer of this
  // component's own — the same store the omnibox and the Radios tab read, so the
  // elapsed time on this button and the row that lands in the library cannot disagree.
  const sdr = useSdrSession();
  const recording = liveRecording(sdr);
  // The armed state disarms itself, so a tap the owner walked away from cannot start a
  // recording minutes later. Keyed on `armed` alone: re-arming restarts the window,
  // which is what a second deliberate tap on an already-armed button means.
  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), ARM_MS);
    return () => window.clearTimeout(timer);
  }, [armed]);

  // The band table, for the one question this control asks of it: does a complete
  // channel plan cover where the radio is? Best-effort — a table that fails to load
  // leaves the dial exactly as it was, counting kilohertz, rather than disabling it.
  const [sections, setSections] = useState<BandSection[]>([]);
  useEffect(() => {
    let live = true;
    loadBands()
      .then((bands) => live && setSections(bands.sections))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);
  // Per-INSTANCE, because the controls are mounted twice at once — the omnibox sheet
  // and the Radios tab behind it — and the shared stream counts its holders. A constant
  // string would let both mounts register as one, so the first to close would take the
  // socket with it and the survivor would freeze; that is the bug this counts against.
  const holderId = useId();
  const plan = planAt(sections, listening.frequency_hz);
  const here = plan ? channelIndex(plan, listening.frequency_hz) : -1;
  // A callback ref rather than an effect: the row exists only while the list is open,
  // and this fires when it mounts. `block: "center"` because the point is to see the
  // channels either side of the one the radio is on.
  //
  // useCallback is what makes that true, and it is not a micro-optimisation. React
  // re-invokes a callback ref whenever its IDENTITY changes — detaching with null and
  // re-attaching — so an inline arrow re-ran this on every render. The tuner re-renders
  // about once a second off the live poll, which yanked the FM dial back to the tuned
  // channel every second while the owner was trying to scroll the other 99.
  const scrollHere = useCallback(
    (el: HTMLButtonElement | null) => el?.scrollIntoView({ block: "center" }),
    [],
  );
  // The owner's explicit pick always wins: asking for a step size on a channelised band
  // is asking to tune between the channels, which is a real thing to want on CB.
  const counting = plan !== null && pickedStep === null;
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

  // The tuning view's rows come off the same stream the waterfall uses, and only the
  // I/Q engine publishes them — `rtl_fm` demodulates in a subprocess we cannot see
  // into, so on that engine there is nothing to draw and the strip is not mounted at
  // all. Opening the stream anyway would hold a socket against a 409.
  const drawing = listening.engine === "iq";
  useEffect(() => {
    if (!drawing) return;
    // The CHANNEL only: this sheet draws the tuning strip, and the band picture the
    // same session can now produce would cost ~11% of a core with nothing rendering it.
    // NAMED, because the sidecar prefers a spectrum session when nobody says: with the
    // other dongle sweeping, this asked for the channel strip and was handed the sweep.
    startSdrSpectrum(
      { view: "channel", serial: listening.serial ?? null, backfill: BACKFILL_ROWS },
      holderId,
    );
    return () => stopSdrSpectrum(holderId);
  }, [drawing, listening.serial, holderId]);

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

  // The filter width, and the rungs this mode offers. Both come from the SESSION rather
  // than a table here: the ladder is the demodulator's (deploy/sdr/demod.py), and a copy
  // in the PWA would go on offering widths a redeployed box had stopped accepting.
  const ladder = listening.bandwidths_hz ?? [];
  const width = listening.bandwidth_hz ?? 0;
  const adjustable = bandwidthAdjustable(listening);

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

  /** Send a new filter width, and nothing else.
   *
   *  The frequency goes back unchanged and NO mode is named: a session keeps its width
   *  across a retune, but naming a mode resets it to that mode's default, so sending one
   *  here would silently undo the very change being made. */
  const setWidth = (hz: number) =>
    act(() => api.sdrTune(listening.frequency_hz / 1_000_000, undefined, listening.session_id, hz));

  const tune = (mhzValue: number) => {
    // Checked here too, not only in the typed field: a 100 kHz step held down from
    // 14.35 MHz walks into the aliasing zone one tap at a time, and the owner would
    // have no reason to suspect the frequency stopped meaning what it says.
    const refusal = whyNotTunable(mhzValue);
    if (refusal !== null) {
      setError(refusal);
      return;
    }
    return act(() => api.sdrTune(mhzValue, undefined, listening.session_id));
  };

  /** ± by CHANNEL where a plan covers the radio, by step size everywhere else.
   *
   *  The end of a plan is a real stop rather than a fallback into kilohertz: leaning on
   *  + at CB 40 must not walk out of the band it says it is in. */
  const step = (direction: number) => {
    if (counting && plan) {
      const next = stepChannel(plan, listening.frequency_hz, direction);
      if (!next) {
        setError(`That is the ${direction > 0 ? "top" : "bottom"} of ${plan.name}.`);
        return;
      }
      return tune(next.hz / 1_000_000);
    }
    return tune((listening.frequency_hz + direction * stepHz) / 1_000_000);
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
            className={`sdr-stepsize${counting ? " sdr-stepsize-ch" : ""}`}
            aria-label={
              counting && plan
                ? `${channelLabel(plan, listening.frequency_hz)} of ${plan.name}. Tap for the channel list.`
                : `Tuning step, ${stepLabel(stepHz)}. Tap to change.`
            }
            aria-expanded={stepOpen}
            onClick={() => setStepOpen((open) => !open)}
          >
            {counting && plan ? channelLabel(plan, listening.frequency_hz) : stepLabel(stepHz)}
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
        {stepOpen && counting && plan && (
          <div className="sdr-chans" aria-label={`${plan.name} channels`}>
            {plan.channels.map((channel, at) => (
              <button
                key={channel.hz}
                type="button"
                // The AM dial is 118 channels, so without this an owner listening on
                // 1010 kHz opens a list that starts at 530 and has to find their own
                // channel in it.
                ref={at === here ? scrollHere : null}
                aria-pressed={at === here}
                aria-label={`${channel.name}, ${mhz(channel.hz)} MHz${channel.note ? `, ${channel.note}` : ""}`}
                className={`sdr-chan${at === here ? " sdr-chan-on" : ""}`}
                disabled={busy}
                onClick={() => {
                  setStepOpen(false);
                  void tune(channel.hz / 1_000_000);
                }}
              >
                <b>{channel.name}</b>
                {!namedByFrequency(channel) && <em>{mhz(channel.hz)}</em>}
              </button>
            ))}
            {/* The way out of counting, and it has to be inside the list that started
                it: on CB the interesting thing is sometimes BETWEEN two channels. */}
            <button
              type="button"
              className="sdr-chan sdr-chan-free"
              onClick={() => {
                setPickedStep(stepHz);
                setStepOpen(true);
              }}
            >
              <b>kHz</b>
              <em>step</em>
            </button>
          </div>
        )}
        {stepOpen && !counting && (
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
            {plan && (
              // Back to counting. Only offered where a plan covers the radio, so it is
              // never a control that does nothing.
              <button
                type="button"
                className="sdr-stepopt"
                onClick={() => {
                  setPickedStep(null);
                  setStepOpen(false);
                }}
              >
                Channels
              </button>
            )}
          </div>
        )}
      </div>

      {/* A fieldset with its legend, not a div wearing role="group": the grouping is
          real, so the element that means it is the one to use. */}
      {/* A fieldset with its legend, not a div wearing role="group": the grouping is
          real, so the element that means it is the one to use.

          **The mode button carries its width** (docs/mocks/bandwidth, shape D). Tapping
          a mode you are not on switches mode; tapping the one you ARE on opens its
          filter ladder. That keeps a second row off a sheet already holding a
          waterfall, a readout, a transport and captions — and the draggable handles on
          the tuning view are what stop the second level being undiscoverable. */}
      <fieldset
        className="seg-set"
        aria-label={adjustable ? "Demodulation mode and bandwidth" : "Demodulation mode"}
      >
        <legend className="sdr-label">{adjustable ? "Mode & bandwidth" : "Mode"}</legend>
        <div className="seg-row sdr-modes">
          {MODES.map((mode) => {
            const on = mode === listening.mode;
            return (
              <button
                key={mode}
                type="button"
                className={`seg${on ? " seg-on" : ""}`}
                aria-pressed={on}
                aria-haspopup={on && adjustable ? "dialog" : undefined}
                aria-expanded={on && adjustable ? bwOpen : undefined}
                disabled={busy}
                onClick={() => {
                  if (on) {
                    if (adjustable) setBwOpen((was) => !was);
                    return;
                  }
                  setBwOpen(false);
                  // No width sent: the sidecar resets to the new mode's default, because
                  // the ladders differ per mode and carrying a width across would refuse
                  // an ordinary mode press (deploy/sdr/listen.py `Session.tune`).
                  void act(() =>
                    api.sdrTune(listening.frequency_hz / 1_000_000, mode, listening.session_id),
                  );
                }}
              >
                {mode.toUpperCase()}
                {on && width > 0 && <em className="sdr-bw">{bandwidthLabel(width)}</em>}
              </button>
            );
          })}
        </div>
        {bwOpen && adjustable && (
          <div className="sdr-steps" aria-label={`${listening.mode.toUpperCase()} bandwidth`}>
            {ladder.map((hz) => (
              <button
                key={hz}
                type="button"
                aria-pressed={hz === width}
                className={`sdr-stepopt${hz === width ? " sdr-stepopt-on" : ""}`}
                disabled={busy}
                onClick={() => {
                  setBwOpen(false);
                  void setWidth(hz);
                }}
              >
                {bandwidthSpoken(hz)}
              </button>
            ))}
          </div>
        )}
      </fieldset>

      {listening.engine !== undefined && listening.engine !== "iq" && (
        // Said rather than left blank, because the owner has no terminal (CLAUDE.md
        // #10): a strip that is simply missing looks like a build without the feature,
        // and this is the one state where knowing WHICH engine is running is the whole
        // diagnosis. The sidecar falls back on its own so the audio keeps working.
        <p className="radio-hint">
          No tuning view on this radio: the box could not open it for its own samples and fell back
          to rtl_fm, which demodulates out of reach. The sound is unaffected.
        </p>
      )}
      {drawing && (
        <SdrTuningView
          frequencyHz={listening.frequency_hz}
          onTune={(hz) =>
            void act(() => api.sdrTune(hz / 1_000_000, undefined, listening.session_id))
          }
          demodMode={listening.mode}
          bandwidthHz={width}
          minHz={listening.bandwidth_min_hz ?? 0}
          maxHz={listening.bandwidth_max_hz ?? 0}
          viewSpanHz={listening.view_span_hz ?? 0}
          viewSpans={listening.view_spans_hz ?? []}
          // Its own call, not `act`: a zoom is a crop on the box rather than a retune,
          // so it must not put the whole sheet in the busy state a rebuild deserves.
          onViewSpan={(hz) => void act(() => api.sdrViewSpan(hz, listening.session_id))}
          // Absent where there is nothing to choose, which is what makes the handles
          // disappear on wide FM rather than appear and refuse.
          onBandwidth={adjustable ? (hz) => void setWidth(hz) : undefined}
        />
      )}

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
          {/* LIVE was printed whatever the delay, including through the eight seconds
              the element used to run behind the air — a label that named the quantity
              and did not measure it. It now says how far behind when it is behind, and
              re-reads on the session poll's own second. */}
          {playing ? liveTag(sdrAudioLag()) : "PAUSED"}
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
        <button
          type="button"
          className={`sdr-act sdr-act-record${armed ? " armed" : ""}`}
          aria-pressed={recording !== null}
          aria-label={
            recording
              ? "Stop recording"
              : armed
                ? "Tap again to start recording"
                : "Record what you are hearing"
          }
          disabled={busy}
          onClick={() => {
            if (recording) {
              // No confirmation on the way OUT: stopping destroys nothing, and the clip
              // it lands is the thing the owner asked for.
              setArmed(false);
              void act(() => api.sdrRecord(false));
              return;
            }
            if (!armed) {
              setArmed(true);
              return;
            }
            setArmed(false);
            void act(() => api.sdrRecord(true));
          }}
        >
          <RecordIcon size={16} />
          {recording ? (
            // Elapsed and running size, both read off the poll. The size is what argues
            // for stopping — the owner runs this box remotely and cannot go and look at
            // the disk (CLAUDE.md #10).
            <>
              <span className="sdr-rec-el">{elapsed(recording.seconds)}</span>
              <small>{formatSize(recording.bytes)}</small>
            </>
          ) : armed ? (
            "Tap again"
          ) : (
            "Record"
          )}
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
