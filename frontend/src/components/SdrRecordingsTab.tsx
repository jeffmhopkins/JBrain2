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
//
// **Not every row is a file.** Long-pressing Record swaps it to captions, which keeps the
// transcript of the same reception and no audio at all — so a captions row has no blob,
// no size and no waveform, and the api answers its audio and trim routes with a sentence
// saying so. Everything here that assumes an MP3 is gated on `isSdrClip`: the play
// control, the size in the meta, the scissors and the download. What a captions row
// offers instead is its transcript, which IS the artifact, rendered by the same
// `TranscriptBody` the note and tool-result viewers use.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type SdrClip,
  type SdrRecording,
  type SdrRecordingsPage,
  api,
  isSdrClip,
  sdrRecordingUrl,
} from "../api/client";
import { mhz } from "../mhz";
import { bandwidthLabel } from "../sdrBandwidth";
import { liveRecording, onSdrRecordingSaved, useSdrSession } from "../sdrSession";
import {
  clockLabel,
  formatDuration,
  formatSize,
  groupByDay,
  reclaimedLine,
  usageFraction,
  usageLine,
} from "../sdrTrim";
import { TranscriptBody, transcriptWords } from "./AudioTranscript";
import { SdrTrimSheet } from "./SdrTrimSheet";
import { ClipIcon, FileIcon, PauseIcon, PlayIcon, ScissorsIcon, TrashIcon } from "./icons";

/** How long an armed Delete stays armed, matching the Record control's own window. */
const ARM_MS = 2600;

/** A filename the owner will recognise a year later: what it was, and when.
 *
 *  The row's own identity, not the blob's digest — a content-addressed name in a
 *  downloads folder is unreadable, and the sha never reaches the client anyway. */
function downloadName(row: SdrClip): string {
  const at = new Date(row.started_at);
  const stamp = Number.isNaN(at.getTime())
    ? row.id
    : `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(at.getDate()).padStart(2, "0")}-${clockLabel(row.started_at).replace(":", "")}`;
  return `${mhz(row.frequency_hz)}MHz-${stamp}.mp3`;
}

/** Fold a saved-but-not-yet-listed recording into a list that answered without it.
 *
 *  The stop route returns the row it wrote, while the LIST is read on a poll that flips
 *  the moment the stream ends — a whole waveform computation before the insert. So the
 *  list can be right about everything except the one clip the owner is looking for, and
 *  this is what puts it on screen anyway. Clears the held row as soon as a list carries
 *  it: from then on the server's copy is the one that gets trimmed, deleted and drawn.
 *
 *  `usage` moves with the row for the same reason the api ships the meter WITH the rows:
 *  a header counting one fewer recording than the list shows is a disagreement the owner
 *  cannot resolve from a phone. */
function withPendingSave(
  page: SdrRecordingsPage,
  pending: { current: SdrRecording | null },
): SdrRecordingsPage {
  const row = pending.current;
  if (!row) return page;
  if (page.recordings.some((r) => r.id === row.id)) {
    pending.current = null;
    return page;
  }
  return {
    recordings: [row, ...page.recordings],
    usage: {
      ...page.usage,
      // Only a clip moves the disk meter, mirroring the api's own `FILTER (WHERE kind =
      // 'audio')`: a captions row took no space, and pricing one at the bitrate it does
      // not have would make the header report bytes nothing on the box is holding. The
      // COUNT moves for both, because it counts the library the rows below add up to.
      bytes: page.usage.bytes + (row.bytes ?? 0),
      count: page.usage.count + 1,
    },
  };
}

export function SdrRecordingsTab({ onOpenRadios }: { onOpenRadios: () => void }) {
  const [page, setPage] = useState<SdrRecordingsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [positionS, setPositionS] = useState(0);
  const [trimming, setTrimming] = useState<SdrClip | null>(null);
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

  // A recording that has been saved but is not in the list yet. The recorder reports no
  // capture from the moment the STREAM ends — before the waveform is computed and the
  // row inserted — so a reload triggered by that poll can honestly answer without the
  // clip the owner just made, and a slow one can land after a good list and undo it. The
  // row is held here and folded into whatever any list says until one carries it, which
  // is what makes the new clip appear and stay. Cleared when the list has it, and when
  // the owner deletes it.
  const pendingSave = useRef<SdrRecording | null>(null);

  const reload = useCallback(async () => {
    try {
      const fresh = await api.getSdrRecordings();
      setPage(withPendingSave(fresh, pendingSave));
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
  const wasCapturing = useRef(false);
  useEffect(() => {
    if (capturing) {
      wasCapturing.current = true;
      return;
    }
    const justStopped = wasCapturing.current;
    wasCapturing.current = false;
    void reload();
    if (!justStopped) return;
    // A capture that ended without this PWA stopping it — the box ran out of disk, or a
    // second device pressed Stop — lands no `saved` answer here to hold on to, and it is
    // the only way this effect sees a capture end at all: the Record control is on
    // another tab of the launcher, so a stop made HERE arrives as a fresh mount instead.
    // The poll flips the moment the STREAM ends, a waveform computation before the row
    // is inserted, so the read above can be early with nothing to correct it. Two
    // follow-ups are what make the clip turn up on its own.
    const timers = [1200, 5000].map((ms) => window.setTimeout(() => void reload(), ms));
    return () => {
      for (const timer of timers) window.clearTimeout(timer);
    };
  }, [capturing, reload]);

  // ...and again on the stop's own answer, which is the signal that is true by
  // construction: the row it carries has been written. The poll is what makes the
  // library feel live; this is what makes it correct.
  useEffect(
    () =>
      onSdrRecordingSaved((row) => {
        pendingSave.current = row;
        void reload();
      }),
    [reload],
  );

  useEffect(() => {
    void (async () => {
      try {
        setDiskTotal((await api.opsMetrics()).disk_total_bytes);
      } catch {
        setDiskTotal(null);
      }
    })();
  }, []);

  // The LIST carries no transcript — only `has_transcript` — because a captions recording
  // may hold four hours of speech (~200 000 characters, `recorder.py`) and five hundred of
  // those would be a library nobody could load. So the row asks for its own when it opens,
  // exactly as the trim sheet asks for the waveform the list leaves out. Best-effort: a
  // failure leaves the body empty rather than putting an error banner over a list that
  // works, and `transcript !== undefined` is the guard — `null` means the box HAS no
  // transcript and must not be asked again.
  useEffect(() => {
    if (openId === null) return;
    const row = page?.recordings.find((r) => r.id === openId);
    if (!row || row.transcript !== undefined || row.has_transcript !== true) return;
    let live = true;
    void api.getSdrRecording(openId).then(
      (full) => {
        if (!live) return;
        setPage((was) =>
          was === null
            ? was
            : {
                ...was,
                recordings: was.recordings.map((r) =>
                  r.id === full.id ? { ...r, transcript: full.transcript ?? null } : r,
                ),
              },
        );
      },
      () => {},
    );
    return () => {
      live = false;
    };
  }, [openId, page]);

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

  const toggle = (row: SdrClip) => {
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
      // Otherwise the next reload folds it straight back in: a held row outliving the
      // recording it describes is a delete that appears not to have worked.
      if (pendingSave.current?.id === id) pendingSave.current = null;
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
              // The one question the whole row hangs on. A captions row has no file, so
              // it has nothing to play, nothing to trim, no size and nothing to download.
              const clip = isSdrClip(row) ? row : null;
              const on = row.id === playingId;
              const open = row.id === openId;
              const trimmed = row.duration_s < row.captured_s;
              const spoken = row.transcript?.text?.trim() ?? "";
              // The list says WHETHER there is a transcript, never what it says. So an
              // empty `spoken` on a row that has one means "not fetched yet" — saying
              // "no speech detected" there would be the library asserting silence it
              // never read.
              const unread = row.transcript === undefined && row.has_transcript === true;
              return (
                <div
                  key={row.id}
                  className={`rec-row${on ? " rec-row-on" : ""}${clip ? "" : " rec-row-cc"}`}
                >
                  {/* Its own button, not a span inside the row's: the row EXPANDS and the
                      circle PLAYS, which is two actions — one control that guessed from
                      where the tap landed would be one name for both of them.
                      Absent on a captions row rather than disabled: a dead play control
                      is a promise the box then refuses, and the api's own answer for that
                      URL is "there is no clip to play". */}
                  {clip && (
                    <button
                      type="button"
                      className="rec-play"
                      aria-label={`${on ? "Pause" : "Play"} the ${mhz(row.frequency_hz)} MHz recording`}
                      aria-pressed={on}
                      onClick={() => toggle(clip)}
                    >
                      {on ? <PauseIcon size={14} /> : <PlayIcon size={14} />}
                    </button>
                  )}
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
                        {/* What kind of row this is, said on the row rather than inferred
                            from the absence of a play control. */}
                        {!clip && <span className="rec-chip rec-chip-cc">CC</span>}
                        {trimmed && <span className="rec-chip rec-chip-cut">trimmed</span>}
                      </b>
                      {/* Untrusted text: a transcript is what a stranger transmitted,
                          machine-read. Rendered as content, never as an instruction. */}
                      <span className="rec-prev">
                        {spoken || (unread ? "" : "(no speech detected)")}
                      </span>
                    </span>
                    <span className="rec-meta">
                      {clockLabel(row.started_at)}
                      <br />
                      {/* The duration is the wall clock the capture ran either way, so it
                          is true for both kinds. The SIZE is the half only a clip has —
                          omitted rather than shown as 0 kB, which would read as a
                          recording that came out empty. */}
                      {formatDuration(row.duration_s)}
                      {clip && ` · ${formatSize(clip.bytes)}`}
                    </span>
                  </button>
                  {/* Trim is MP3-frame surgery end to end — `ffmpeg -c copy` on the box,
                      72 ms frames in the sheet — so the column is simply not there on a
                      row with no frames. The api refuses it with a sentence too. */}
                  {clip && (
                    <button
                      type="button"
                      className="rec-trim"
                      aria-label={`Trim the ${mhz(row.frequency_hz)} MHz recording`}
                      onClick={() => {
                        stop();
                        setTrimming(clip);
                      }}
                    >
                      <ScissorsIcon size={17} />
                    </button>
                  )}
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
                    // transcript, and the two actions that are not trimming.
                    <div className="rec-body">
                      {clip ? (
                        <p className="rec-tx">
                          {spoken ||
                            (unread
                              ? "Reading the transcript…"
                              : "No transcript — an audio recording is not transcribed after the " +
                                "fact. Long-press Record to keep the captions instead.")}
                        </p>
                      ) : (
                        // On a captions row the transcript IS the recording, so it gets
                        // the real viewer rather than the two-line preview: the same
                        // `TranscriptBody` a note's audio attachment and jerv's transcribe
                        // result use, tinting each word by how sure whisper was. Narrowband
                        // voice degrades in a patterned way and the numbers are both the
                        // least certain and usually the payload, which is exactly what the
                        // gradient shows. No `onSeek`: there is no clip to seek in.
                        <TranscriptBody
                          words={transcriptWords(row.transcript?.words)}
                          currentIdx={-1}
                          text={
                            spoken ||
                            (unread
                              ? "Reading the transcript…"
                              : "Nothing was said while this was recording.")
                          }
                        />
                      )}
                      <div className="rl-actions">
                        {clip ? (
                          // A blob never goes through the api client: this is the same
                          // by-id URL the player streams, handed to the browser to save.
                          <a
                            className="rl-action"
                            href={sdrRecordingUrl(clip.id)}
                            download={downloadName(clip)}
                          >
                            <FileIcon size={19} /> Download (.mp3)
                          </a>
                        ) : (
                          // Copy, not Download: there is no file to hand the browser, and
                          // minting one here would be this surface inventing an artifact
                          // the box does not have. Copy is how text leaves every other
                          // screen in this app (`.rl-action` + ClipIcon, ResearchScreen).
                          <button
                            type="button"
                            className="rl-action"
                            disabled={!spoken}
                            onClick={() => {
                              void navigator.clipboard?.writeText(spoken).catch(() => {
                                // A clipboard the browser refuses is not worth a dialog:
                                // the words are on screen and can be selected.
                              });
                            }}
                          >
                            <ClipIcon size={19} /> Copy transcript
                          </button>
                        )}
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
            // A row still held as pending is held as the SERVER last described it, or a
            // reload landing after this would fold the untrimmed original back in.
            if (pendingSave.current?.id === result.recording.id) {
              pendingSave.current = result.recording;
            }
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
