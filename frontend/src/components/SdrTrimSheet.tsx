// The trim sheet — binding spec docs/mocks/recording/d-trim-sheet.html.
//
// A capture is a rough take: you pressed record before it started and stopped after it
// ended. This is where it becomes the thing worth keeping, and it is the app's first
// surface that edits stored content in place rather than only creating or deleting it —
// so it is written against DESIGN.md's "Destructive editing of stored media" rules:
//
//   1. its own sheet, entered deliberately from the row's scissors — never an inline
//      control you can brush past;
//   2. Preview is mandatory, because the original does NOT survive and there is no undo;
//   3. the confirm names the loss ("Trim & discard rest", not "Save");
//   4. the selection is DRAWN — two role="slider" handles on the waveform, draggable,
//      arrow-key operable, plus nudge buttons at the medium's own smallest honest unit
//      (one MP3 frame, 72 ms — the UI must not imply finer precision than -c copy can
//      deliver);
//   5. the saving is stated live, not implied.
//
// It is the shared <Sheet> shell, never a bespoke modal. Note Sheet.tsx:30-35 — the
// focus effect there has an empty dep array precisely so a 1 Hz repaint cannot steal the
// caret; there is no text input in here, and adding one would need that read first.

import {
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { type SdrClip, type SdrTrimResult, api, sdrRecordingUrl } from "../api/client";
import { mhz } from "../mhz";
import { bandwidthLabel } from "../sdrBandwidth";
import {
  FRAME_MS,
  FRAME_S,
  type Handle,
  type Selection,
  clampSelection,
  clockLabel,
  formatDuration,
  formatSize,
  formatStamp,
  isWholeClip,
  moveHandle,
  nudgeHandle,
  trimGain,
  waveformBars,
} from "../sdrTrim";
import { Sheet } from "./Sheet";

/** How many bars the picture draws. The stored envelope is whatever the box computed;
 *  this is what fits a phone-width sheet at 2 px a bar with a gap. */
const BARS = 116;

/** A coarse keyboard step — Shift + arrow. One second, because frame-by-frame across a
 *  seven-minute net check-in is four hundred presses. */
const COARSE_STEP_S = 1;

interface TrimSheetProps {
  /** An `SdrClip`, not any recording: every line below reads a size, a waveform or the
   *  audio route, and `ffmpeg -c copy` cuts MP3 frames. A captions row has none of those,
   *  which is why the library does not offer the scissors on one — and why the type says
   *  so here rather than this sheet learning to draw a second, fileless shape. */
  recording: SdrClip;
  onClose: () => void;
  /** What the SERVER cut, and the meter after it. The client asks in seconds; the copy
   *  lands on a frame boundary, so what comes back is the truth and replaces the row
   *  wholesale rather than being merged with the sheet's estimate. */
  onTrimmed: (result: SdrTrimResult) => void;
}

export function SdrTrimSheet({ recording, onClose, onTrimmed }: TrimSheetProps) {
  const totalS = recording.duration_s;
  const [selection, setSelection] = useState<Selection>(() =>
    clampSelection({ startS: 0, endS: totalS }, totalS),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Where the preview has reached, or null when it is not playing — which is also what
  // the Preview/Stop button reads. The frame loop asks the ELEMENT where it is on every
  // frame; this is only written when the head has moved far enough to redraw.
  const [headS, setHeadS] = useState<number | null>(null);

  const waveRef = useRef<HTMLDivElement | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const dragging = useRef<Handle | null>(null);
  // The running frame loop, and the out-point it is watching for. The out-point is
  // captured when Preview starts rather than read from state on every frame because
  // moving a handle STOPS the preview: for as long as one is running, the selection it
  // was started against is the selection.
  const frameRef = useRef<number | null>(null);
  const outRef = useRef(totalS);

  // The list deliberately omits `peaks` — 400 floats per row would dwarf a hundred-row
  // library — so the sheet asks for the one clip it is open on, once, when it opens.
  // A box older than that route, or one whose decode failed, answers without an
  // envelope; the sheet then works without a picture rather than drawing a flat line
  // and letting it read as three minutes of silence.
  const [fetched, setFetched] = useState<number[] | null>(null);
  const envelope = recording.peaks?.length ? recording.peaks : (fetched ?? []);
  const bars = waveformBars(envelope, BARS);
  const gain = trimGain(selection, totalS, recording.bytes);
  const whole = isWholeClip(selection, totalS);
  const startPct = (selection.startS / Math.max(totalS, FRAME_S)) * 100;
  const endPct = (selection.endS / Math.max(totalS, FRAME_S)) * 100;
  // What a repaint of the playhead is worth: roughly one pixel of the drawn picture,
  // which is BARS bars at about 3 px each. A frame loop asks the element where it is
  // sixty times a second; the drawing only has to keep up with the screen.
  const headStepS = totalS / (BARS * 3);

  useEffect(() => {
    if (recording.peaks?.length) return;
    let live = true;
    void api
      .getSdrRecording(recording.id)
      .then((row) => {
        if (live) setFetched(row.peaks ?? []);
      })
      .catch(() => {
        // A missing picture is not worth an error banner: the handles, the readout and
        // the confirm all still work, and the marks row already says there is no
        // waveform. Failing loudly here would block a trim the owner can still make.
        if (live) setFetched([]);
      });
    return () => {
      live = false;
    };
  }, [recording.id, recording.peaks]);

  const stopPreview = useCallback(() => {
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
    audioRef.current?.pause();
    setHeadS(null);
  }, []);

  // A sheet closed mid-preview must not leave a loop running against a torn-down
  // element. Only the frame is cancelled here: pausing an element React is about to
  // remove is pointless, and setting state on the way out is not.
  useEffect(
    () => () => {
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    },
    [],
  );

  /** Move a handle, and stop any preview first.
   *
   *  Every route into the selection goes through here — drag, arrow key, nudge button —
   *  because a preview is a claim about a PARTICULAR selection ("this is what will
   *  remain"). Left running while a handle moves, it goes on playing a selection that no
   *  longer exists, which is the same lie rule 2 exists to prevent. */
  const moveSelection = (next: (was: Selection) => Selection) => {
    stopPreview();
    setSelection(next);
  };

  /** Where on the clip a pointer is, in seconds. */
  const secondsAt = (event: ReactPointerEvent): number | null => {
    const box = waveRef.current?.getBoundingClientRect();
    if (!box || box.width <= 0) return null;
    const fraction = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
    return fraction * totalS;
  };

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    const at = secondsAt(event);
    if (at === null) return;
    // A press anywhere on the picture grabs the NEARER handle. Requiring a hit on the
    // 34px grip itself is what makes a two-handle selection fiddly on a phone; the
    // waveform is the control, and the grips are only where it says so.
    const target = (event.target as Element | null)?.closest("[data-handle]");
    const handle: Handle =
      (target?.getAttribute("data-handle") as Handle | null) ??
      (Math.abs(at - selection.startS) <= Math.abs(at - selection.endS) ? "start" : "end");
    dragging.current = handle;
    event.currentTarget.setPointerCapture(event.pointerId);
    moveSelection((was) => moveHandle(was, handle, at, totalS));
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const handle = dragging.current;
    if (!handle) return;
    const at = secondsAt(event);
    if (at === null) return;
    moveSelection((was) => moveHandle(was, handle, at, totalS));
  };

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>, handle: Handle) => {
    const direction = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
    if (direction === 0) return;
    event.preventDefault();
    const step = event.shiftKey ? COARSE_STEP_S : FRAME_S;
    moveSelection((was) => nudgeHandle(was, handle, direction * step, totalS));
  };

  /** Play ONLY the selection. Mandatory, not a nicety: the original does not survive
   *  the confirm, so the sheet has to be able to play exactly what will remain.
   *
   *  The stop is driven by a frame loop, NOT by the element's `timeupdate`. That event
   *  fires roughly four times a second, so stopping on it overruns the out-point by up
   *  to ~250 ms — a quarter-second of precisely the audio the confirm is about to
   *  destroy, played back as "what will remain". A frame loop checks the element's own
   *  clock every time the browser paints, so the stop lands within a frame or two of the
   *  handle, well inside the 72 ms frame the cut itself is quantised to. (A single
   *  timer scheduled from the remaining duration would be blind to a stall while the
   *  clip buffers: only the element knows where it really is.) */
  const preview = () => {
    const element = audioRef.current;
    if (!element) return;
    if (headS !== null) {
      stopPreview();
      return;
    }
    outRef.current = selection.endS;
    element.currentTime = selection.startS;
    setHeadS(selection.startS);

    const tick = () => {
      const at = element.currentTime;
      if (at >= outRef.current) {
        stopPreview();
        return;
      }
      // The CHECK runs every frame; the repaint does not need to. A move smaller than
      // one pixel of the drawn picture would move the playhead nowhere, and React bails
      // out of the render when the value is unchanged.
      setHeadS((was) => (was === null || Math.abs(at - was) >= headStepS ? at : was));
      frameRef.current = requestAnimationFrame(tick);
    };
    frameRef.current = requestAnimationFrame(tick);

    void element.play().catch(() => {
      // A refused autoplay or a clip the box cannot serve: say so rather than leaving
      // a Preview button that does nothing, which reads as a broken sheet.
      stopPreview();
      setError("Couldn't play this clip.");
    });
  };

  /** A backstop, not the mechanism. A hidden tab is served no animation frames while its
   *  audio plays on, so this is what ends a preview the owner backgrounded mid-play;
   *  `timeupdate` is too coarse to be the thing that places the stop. */
  const onTimeUpdate = () => {
    const element = audioRef.current;
    if (!element || headS === null) return;
    if (element.currentTime >= outRef.current) stopPreview();
  };

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "That didn't work.");
    } finally {
      setBusy(false);
    }
  };

  const commit = () =>
    run(async () => {
      stopPreview();
      onTrimmed(await api.trimSdrRecording(recording.id, selection.startS, selection.endS));
      onClose();
    });

  return (
    <Sheet title={`Trim ${mhz(recording.frequency_hz)} MHz`} onClose={onClose}>
      <p className="trim-sub">
        {recording.mode.toUpperCase()}
        {recording.bandwidth_hz ? ` ${bandwidthLabel(recording.bandwidth_hz)}` : ""} ·{" "}
        {clockLabel(recording.started_at)} · full capture {formatDuration(totalS)},{" "}
        {formatSize(recording.bytes)}
      </p>

      {/* The PICTURE is the control, not the two grips on it: a press anywhere grabs
          the nearer handle, which is what makes a two-handle selection placeable with a
          thumb. The grips are focusable and arrow-operable, and they are what a screen
          reader and a keyboard drive — this div is only the drag surface they sit on. */}
      <div
        className={`trim-wave${headS !== null ? " playing" : ""}`}
        ref={waveRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={() => {
          dragging.current = null;
        }}
        onPointerCancel={() => {
          dragging.current = null;
        }}
      >
        {bars.map((level, at) => {
          const t = ((at + 0.5) / BARS) * totalS;
          const inside = t >= selection.startS && t <= selection.endS;
          return (
            <span
              // Position IS the identity here: the bars are a fixed-length resampling of
              // one envelope, so index is stable and there is nothing else to key on.
              // biome-ignore lint/suspicious/noArrayIndexKey: see above.
              key={at}
              className={`trim-bar${inside ? " in" : ""}`}
              style={{ left: `${((at / BARS) * 100).toFixed(2)}%`, height: `${level * 96}px` }}
            />
          );
        })}
        <span className="trim-drop" style={{ left: 0, width: `${startPct.toFixed(2)}%` }} />
        <span className="trim-drop" style={{ left: `${endPct.toFixed(2)}%`, right: 0 }} />
        <span className="trim-at trim-at-in">{formatStamp(selection.startS)}</span>
        <span className="trim-at trim-at-out">{formatStamp(selection.endS)}</span>
        {(["start", "end"] as Handle[]).map((handle) => {
          const at = handle === "start" ? selection.startS : selection.endS;
          return (
            <div
              key={handle}
              className="trim-grip"
              data-handle={handle}
              role="slider"
              tabIndex={0}
              aria-label={handle === "start" ? "Start of the trim" : "End of the trim"}
              aria-valuemin={0}
              aria-valuemax={totalS}
              aria-valuenow={Number(at.toFixed(2))}
              aria-valuetext={formatStamp(at)}
              style={{ left: `${(handle === "start" ? startPct : endPct).toFixed(2)}%` }}
              onKeyDown={(event) => onKeyDown(event, handle)}
            />
          );
        })}
        {headS !== null && (
          <span
            className="trim-head"
            style={{ left: `${((headS / Math.max(totalS, FRAME_S)) * 100).toFixed(2)}%` }}
          />
        )}
      </div>

      <div className="trim-marks">
        <span>0:00</span>
        <span>{envelope.length > 0 ? "drag either handle" : "no waveform stored"}</span>
        <span>{formatDuration(totalS)}</span>
      </div>

      {/* A frame is the smallest cut `-c copy` can make. These exist because a fingertip
          on a 118px picture cannot place one, and because naming the unit is how the
          sheet declines to imply millisecond precision it does not have. */}
      <div className="trim-nudge">
        {(
          [
            ["start", -1, `−${FRAME_MS} ms in`],
            ["start", 1, `+${FRAME_MS} ms in`],
            ["end", -1, `−${FRAME_MS} ms out`],
            ["end", 1, `+${FRAME_MS} ms out`],
          ] as [Handle, number, string][]
        ).map(([handle, direction, label]) => (
          <button
            key={label}
            type="button"
            onClick={() =>
              moveSelection((was) => nudgeHandle(was, handle, direction * FRAME_S, totalS))
            }
          >
            {label}
          </button>
        ))}
      </div>

      <div className="trim-sums">
        <div>
          <span>Keeps</span>
          <b>{formatDuration(gain.keptS)}</b>
        </div>
        <div>
          <span>Size</span>
          <b>{formatSize(gain.keptBytes)}</b>
          {!whole && <b className="trim-was">{formatSize(recording.bytes)}</b>}
        </div>
      </div>

      {/* DESIGN.md rule 5: the saving is stated, not implied. The whole reason the
          feature exists is disk, and the owner cannot go and look at it. */}
      <p className="trim-gain">
        {whole ? (
          <>
            <span>Nothing trimmed yet</span>
            <b>{formatSize(recording.bytes)}</b>
          </>
        ) : (
          <>
            <span>Discards {formatDuration(gain.discardedS)} of dead air</span>
            <b>frees {formatSize(gain.freedBytes)}</b>
          </>
        )}
      </p>

      {error && (
        <p className="trim-error" role="alert">
          {error}
        </p>
      )}

      <div className="trim-acts">
        <button type="button" onClick={preview} disabled={busy}>
          {headS !== null ? "Stop" : "Preview"}
        </button>
        <button type="button" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          type="button"
          className="trim-keep"
          disabled={busy || whole}
          onClick={() => void commit()}
        >
          Trim &amp; discard rest
        </button>
      </div>

      <p className="trim-foot">
        <b>The full capture is discarded.</b> That is what frees the disk, and it is why the trim is
        confirmed rather than applied on the way past — Preview first, because there is no undo. Cut
        on an MP3 frame boundary with <code>-c copy</code>: lossless and instant, landing within{" "}
        {FRAME_MS} ms of the handle.
      </p>

      {/* Its own element, never sdrAudio.ts's: that one is the LIVE stream, parked in
          <body> for the life of the lease, and its createMediaElementSource is one-shot.
          A recording is a file. `preload="none"` because opening the sheet is not a
          request to fetch the clip — pressing Preview is. */}
      {/* biome-ignore lint/a11y/useMediaCaption: radio audio; the transcript is the row's. */}
      <audio
        ref={audioRef}
        src={sdrRecordingUrl(recording.id)}
        preload="none"
        onTimeUpdate={onTimeUpdate}
        onEnded={stopPreview}
      />
    </Sheet>
  );
}
