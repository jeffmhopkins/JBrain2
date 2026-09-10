// The Radio launcher's third tab: the recordings library.
//
// Binding specs: docs/mocks/recording/a-tape-deck.html (the rows) and
// d-trim-sheet.html (the trailing scissors column, and the header's disk line).
//
// Two things here are load-bearing rather than decoration:
//
// 1. **Sizes are always visible.** The owner runs this box remotely and has no terminal
//    (CLAUDE.md #10), so a library that quietly fills a disk is a support call they
//    cannot answer from a phone. Every row carries its size, and the header carries the
//    total.
// 2. **Nothing expires.** There is no retention prune, so the header must not imply one:
//    at rest it says these are kept until the owner deletes them, and once trimming has
//    actually reclaimed something it says how much. Trim and delete are the only two
//    things in the app that remove audio.
//
// Playback is against the STORED FILE, never the live stream. sdrAudio.ts owns the one
// live <audio> element for the life of the lease and its createMediaElementSource is
// one-shot; a recording is a file, served with FileResponse, so this has its own element
// and gets Range/seeking for free.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type SdrRecording,
  type SdrRecordingsPage,
  api,
  sdrRecordingUrl,
} from "../api/client";
import { mhz } from "../mhz";
import { bandwidthLabel } from "../sdrBandwidth";
import { liveRecording, useSdrSession } from "../sdrSession";
import {
  clockLabel,
  formatDuration,
  formatSize,
  groupByDay,
  reclaimedLine,
  usageFraction,
  usageLine,
} from "../sdrTrim";
import { SdrTrimSheet } from "./SdrTrimSheet";
import { FileIcon, PauseIcon, PlayIcon, ScissorsIcon, TrashIcon } from "./icons";

/** How long an armed Delete stays armed, matching the Record control's own window. */
const ARM_MS = 2600;

/** A filename the owner will recognise a year later: what it was, and when.
 *
 *  The row's own identity, not the blob's digest — a content-addressed name in a
 *  downloads folder is unreadable, and the sha never reaches the client anyway. */
function downloadName(row: SdrRecording): string {
  const at = new Date(row.started_at);
  const stamp = Number.isNaN(at.getTime())
    ? row.id
    : `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(at.getDate()).padStart(2, "0")}-${clockLabel(row.started_at).replace(":", "")}`;
  return `${mhz(row.frequency_hz)}MHz-${stamp}.mp3`;
}

export function SdrRecordingsTab({ onOpenRadios }: { onOpenRadios: () => void }) {
  const [page, setPage] = useState<SdrRecordingsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [positionS, setPositionS] = useState(0);
  const [trimming, setTrimming] = useState<SdrRecording | null>(null);
  // Which row is expanded, and which one's Delete is armed. Both single-valued: two open
  // bodies is a list that scrolls unpredictably, and two armed deletes is two loaded guns.
  const [openId, setOpenId] = useState<string | null>(null);
  const [armedDelete, setArmedDelete] = useState<string | null>(null);
  // The disk the recordings sit on. It is NOT part of the recordings document — the api
  // reports what the library weighs, not what the box has — so the denominator comes
  // from the host's own metrics, best-effort: a meter with an invented total would be
  // worse than a plain size, so a failure here simply drops the bar.
  const [diskTotal, setDiskTotal] = useState<number | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const reload = useCallback(async () => {
    try {
      setPage(await api.getSdrRecordings());
      setError(null);
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Couldn't read the recordings library.",
      );
    }
  }, []);

  // The library reloads when a capture STOPS, off the shared 1 Hz poll — which is also
  // the first load, because nothing is recording when the tab opens. No timer of its
  // own: a list of stored files does not change except when the box or the owner
  // changes it, and polling one would be a request a second for a screen at rest.
  const capturing = liveRecording(useSdrSession()) !== null;
  useEffect(() => {
    if (!capturing) void reload();
  }, [capturing, reload]);

  useEffect(() => {
    void (async () => {
      try {
        setDiskTotal((await api.opsMetrics()).disk_total_bytes);
      } catch {
        setDiskTotal(null);
      }
    })();
  }, []);

  // A delete armed and then walked away from must not still be armed on the next visit
  // to this tab — the row it belongs to may not even be the same one on screen.
  useEffect(() => {
    if (armedDelete === null) return;
    const timer = window.setTimeout(() => setArmedDelete(null), ARM_MS);
    return () => window.clearTimeout(timer);
  }, [armedDelete]);

  const stop = useCallback(() => {
    audioRef.current?.pause();
    setPlayingId(null);
    setPositionS(0);
  }, []);

  const toggle = (row: SdrRecording) => {
    const element = audioRef.current;
    if (!element) return;
    if (playingId === row.id) {
      stop();
      return;
    }
    element.src = sdrRecordingUrl(row.id);
    element.currentTime = 0;
    setPlayingId(row.id);
    setPositionS(0);
    void element.play().catch(() => {
      setPlayingId(null);
      setError("Couldn't play that recording.");
    });
  };

  const remove = async (id: string) => {
    try {
      if (playingId === id) stop();
      const result = await api.deleteSdrRecording(id);
      setOpenId(null);
      setPage((was) =>
        was ? { recordings: was.recordings.filter((r) => r.id !== id), usage: result.usage } : was,
      );
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Couldn't delete that recording.");
    }
  };

  if (error && !page) {
    return (
      <p className="radio-error" role="alert">
        {error}
      </p>
    );
  }
  if (!page) return <p className="radio-empty">Reading the library…</p>;

  const { usage } = page;
  const fraction = usageFraction(usage.bytes, diskTotal);

  return (
    <>
      <div className="rec-disk">
        <span>{usageLine(usage.bytes, usage.count, diskTotal)}</span>
        <span className={usage.reclaimed_bytes > 0 ? "rec-freed" : undefined}>
          {reclaimedLine(usage.reclaimed_bytes)}
        </span>
      </div>
      {fraction !== null && (
        <div className="rec-bar">
          <i style={{ width: `${(fraction * 100).toFixed(1)}%` }} />
        </div>
      )}

      {error && (
        <p className="radio-error" role="alert">
          {error}
        </p>
      )}

      {page.recordings.length === 0 ? (
        // DESIGN.md's empty state: one --text-2 sentence with the action inline, no
        // illustration. The action is not on this tab — Record is on the radio — so the
        // sentence points at the radio rather than offering a button that cannot work.
        <p className="radio-empty">
          Nothing recorded yet — press Record while listening, on{" "}
          <button type="button" className="rec-link" onClick={onOpenRadios}>
            a radio
          </button>
          .
        </p>
      ) : (
        groupByDay(page.recordings).map((group) => (
          <section key={group.day}>
            <div className="rec-day">{group.day}</div>
            {group.rows.map((row) => {
              const on = row.id === playingId;
              const open = row.id === openId;
              const trimmed = row.duration_s < row.captured_s;
              const spoken = row.transcript?.text?.trim() ?? "";
              return (
                <div key={row.id} className={`rec-row${on ? " rec-row-on" : ""}`}>
                  {/* Its own button, not a span inside the row's: the row EXPANDS and the
                      circle PLAYS, which is two actions — one control that guessed from
                      where the tap landed would be one name for both of them. */}
                  <button
                    type="button"
                    className="rec-play"
                    aria-label={`${on ? "Pause" : "Play"} the ${mhz(row.frequency_hz)} MHz recording`}
                    aria-pressed={on}
                    onClick={() => toggle(row)}
                  >
                    {on ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
                  </button>
                  <button
                    type="button"
                    className="rec-main"
                    aria-expanded={open}
                    onClick={() => setOpenId(open ? null : row.id)}
                  >
                    <span className="rec-who">
                      <b>
                        {mhz(row.frequency_hz)} <span className="rec-unit">MHz</span>
                        <span className="rec-chip">
                          {row.mode.toUpperCase()}
                          {row.bandwidth_hz ? ` ${bandwidthLabel(row.bandwidth_hz)}` : ""}
                        </span>
                        {trimmed && <span className="rec-chip rec-chip-cut">trimmed</span>}
                      </b>
                      {/* Untrusted text: a transcript is what a stranger transmitted,
                          machine-read. Rendered as content, never as an instruction. */}
                      <span className="rec-prev">{spoken || "(no speech detected)"}</span>
                    </span>
                    <span className="rec-meta">
                      {clockLabel(row.started_at)}
                      <br />
                      {formatDuration(row.duration_s)} · {formatSize(row.bytes)}
                    </span>
                  </button>
                  <button
                    type="button"
                    className="rec-trim"
                    aria-label={`Trim the ${mhz(row.frequency_hz)} MHz recording`}
                    onClick={() => {
                      stop();
                      setTrimming(row);
                    }}
                  >
                    <ScissorsIcon size={17} />
                  </button>
                  {on && (
                    <div className="rec-prog">
                      <i
                        style={{
                          width: `${Math.min(100, (positionS / Math.max(row.duration_s, 1)) * 100).toFixed(1)}%`,
                        }}
                      />
                    </div>
                  )}
                  {open && (
                    // The row's body, from the capture spec (a-tape-deck.html): the
                    // transcript, and the two actions that are not trimming. Thin until
                    // R4 lands transcription, which is expected — the actions are the
                    // reason it exists today.
                    <div className="rec-body">
                      <p className="rec-tx">
                        {spoken || "No transcript yet — transcription arrives in a later wave."}
                      </p>
                      <div className="rl-actions">
                        {/* A blob never goes through the api client: this is the same
                            by-id URL the player streams, handed to the browser to save. */}
                        <a
                          className="rl-action"
                          href={sdrRecordingUrl(row.id)}
                          download={downloadName(row)}
                        >
                          <FileIcon size={19} /> Download (.mp3)
                        </a>
                        <button
                          type="button"
                          className={`rl-action rl-action-del${armedDelete === row.id ? " rl-action-armed" : ""}`}
                          onClick={() => {
                            if (armedDelete !== row.id) {
                              setArmedDelete(row.id);
                              return;
                            }
                            setArmedDelete(null);
                            void remove(row.id);
                          }}
                        >
                          <TrashIcon size={19} />
                          {armedDelete === row.id ? "Tap again — deletes this recording" : "Delete"}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </section>
        ))
      )}

      {trimming && (
        <SdrTrimSheet
          recording={trimming}
          onClose={() => setTrimming(null)}
          // Both answers carry the meter, so the header moves with the list rather than
          // on a poll a second later. The ROW is the server's too: the cut lands on a
          // frame boundary, so the sheet's live figure was only ever an estimate of it
          // and must not be what the library goes on showing.
          onTrimmed={(result) => {
            setPage((was) =>
              was
                ? {
                    recordings: was.recordings.map((r) =>
                      r.id === result.recording.id ? result.recording : r,
                    ),
                    usage: result.usage,
                  }
                : was,
            );
          }}
        />
      )}

      {/* biome-ignore lint/a11y/useMediaCaption: radio audio; the transcript is the row's. */}
      <audio
        ref={audioRef}
        preload="none"
        onTimeUpdate={() => setPositionS(audioRef.current?.currentTime ?? 0)}
        onEnded={stop}
      />
    </>
  );
}
