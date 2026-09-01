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
import type { SdrListening } from "../sdrSession";
import { Sheet } from "./Sheet";

const MODES = ["wbfm", "fm", "am", "usb"] as const;
// 25 kHz is the channel spacing that suits the VHF/UHF voice bands this is mostly
// pointed at; broadcast FM sits on 200 kHz, so a station is eight taps away. Worth
// revisiting once the band-plan table exists and can supply a per-band step.
const STEP_HZ = 25_000;

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
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // One <audio> element pointed at the proxied live stream. Same-origin and
  // session-authed: the sidecar sits on an internal network the browser cannot reach.
  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    el.src = "/api/sdr/audio";
    // play() returns a Promise in modern browsers but `undefined` in older ones
    // (and in jsdom), so it cannot be chained blind. Autoplay refusal is normal
    // and not worth surfacing — the transport control is right there.
    const started: unknown = el.play();
    if (started instanceof Promise) started.catch(() => {});
    return () => {
      el.pause();
      el.removeAttribute("src");
      el.load(); // drop the connection rather than leave it streaming in the background
    };
  }, []);

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
        (listening.frequency_hz + direction * STEP_HZ) / 1_000_000,
        undefined,
        listening.session_id,
      ),
    );

  const bars = Math.min(6, Math.round(listening.peak * 12));

  return (
    <Sheet title="Tuned station" onClose={onClose}>
      <p className="sdr-note">
        The radio has one tuner, so this session holds it until you release it.
      </p>

      <div className="sdr-readout">
        <div className="sdr-freq">
          {mhz(listening.frequency_hz)}
          <span className="sdr-unit">MHz</span>
        </div>
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
          <span className="sdr-stepsize">{STEP_HZ / 1000} kHz</span>
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

      {/* biome-ignore lint/a11y/useMediaCaption: live radio has no caption track */}
      <audio ref={audioRef} className="sdr-audio" controls preload="none" />

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
