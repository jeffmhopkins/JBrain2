// The drive-mode read-aloud player for a deep-research report (owner-chosen variant C2,
// docs/mocks/research-drive-play/compare-c.html). The report is meant to be played in the
// car, so every control is a large, distinct target: an amber round play/pause, steel
// square "Prev story" / "Next story" buttons as tall as it, and a progress bar split into
// one segment per story (tap a segment to jump there). Shared by the chat card and the
// Research Library's report screen.

import { type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { reportSections } from "./reportSections";
import type { ReadAloudProgress } from "./useReadAloud";

/** The read-aloud capability a report player needs: the keyed play/pause every turn uses,
 * plus story-by-story playback and its progress. */
export interface ReportReadAloud {
  playing: string | null;
  onToggle: (key: string, markdown: string) => void;
  playSections: (key: string, sections: string[], start: number, resume?: boolean) => void;
  progress: ReadAloudProgress | null;
}

// Within this long of a story's start, "Prev story" goes to the previous story; past it,
// it restarts the current one — the usual media-player convention.
const RESTART_AFTER_MS = 4000;

const fmtMin = (seconds: number): string => `~${Math.max(1, Math.ceil(seconds / 60))} min`;

export function ReportPlayer({
  playKey,
  reportMd,
  readAloud,
}: {
  playKey: string;
  reportMd: string;
  readAloud: ReportReadAloud;
}): ReactNode {
  const sections = useMemo(() => reportSections(reportMd), [reportMd]);
  const mds = useMemo(() => sections.map((s) => s.md), [sections]);
  const isPlaying = readAloud.playing === playKey;
  const pos = readAloud.progress?.key === playKey ? readAloud.progress : null;
  const last = sections.length - 1;
  const cur = Math.min(pos?.section ?? 0, Math.max(0, last));
  const finished = !isPlaying && pos !== null && pos.section >= last && pos.fraction >= 1;
  // The player stays open while paused mid-report; Stop (or the end of the brief) folds it
  // back to the single Play button. A key that's playing always shows the player, so a
  // remounted card picks its run back up; a paused run left behind starts folded.
  const [stopped, setStopped] = useState(!isPlaying);
  const open = isPlaying || (pos !== null && !finished && !stopped);

  // When the current story began speaking — re-stamped on every story change (so `cur` is
  // the trigger even though the body doesn't read it) and on a fresh play, but not on a
  // resume, so "Prev story" after resuming mid-story still restarts it.
  const storyStart = useRef(0);
  // biome-ignore lint/correctness/useExhaustiveDependencies: cur is the re-stamp trigger
  useEffect(() => {
    storyStart.current = Date.now();
  }, [cur]);

  if (!sections.length) return null;

  const total = sections.reduce((a, s) => a + s.seconds, 0);
  const play = (from: number): void => {
    setStopped(false);
    storyStart.current = Date.now();
    readAloud.playSections(playKey, mds, Math.max(0, Math.min(last, from)));
  };
  const resume = (): void => readAloud.playSections(playKey, mds, cur, true);
  const pause = (): void => readAloud.onToggle(playKey, "");

  if (!open) {
    return (
      <button type="button" className="rp-idle" onClick={() => play(0)}>
        <PlayIcon />
        <span>Play brief · {fmtMin(total)}</span>
      </button>
    );
  }

  const fraction = pos?.fraction ?? 0;
  const left =
    sections.slice(cur + 1).reduce((a, s) => a + s.seconds, 0) +
    (1 - fraction) * (sections[cur]?.seconds ?? 0);
  const title = sections[cur]?.title || "Intro";

  return (
    <section className="rp" aria-label="Report player">
      <div className="rp-now">
        Story {cur + 1} of {sections.length}
        {isPlaying ? "" : " · paused"}
      </div>
      <div className="rp-title">{title}</div>
      <div className="rp-segs">
        {sections.map((s, k) => (
          <button
            type="button"
            // biome-ignore lint/suspicious/noArrayIndexKey: stories are positional
            key={k}
            className={`rp-seg${k === cur ? " cur" : ""}`}
            style={{ flexGrow: s.seconds }}
            onClick={() => play(k)}
            aria-label={`Play story ${k + 1}: ${s.title || "Intro"}`}
          >
            <i style={{ width: `${(k < cur ? 1 : k === cur ? fraction : 0) * 100}%` }} />
          </button>
        ))}
      </div>
      <div className="rp-meta">
        <span>{fmtMin(left)} left</span>
        <span>{fmtMin(total)}</span>
      </div>
      <div className="rp-ctrls">
        <button
          type="button"
          className="rp-skip"
          onClick={() => {
            const restart = isPlaying && Date.now() - storyStart.current > RESTART_AFTER_MS;
            play(restart || cur === 0 ? cur : cur - 1);
          }}
        >
          <PrevIcon />
          <span>Prev story</span>
        </button>
        <button
          type="button"
          className="rp-pp"
          onClick={() => (isPlaying ? pause() : resume())}
          aria-label={isPlaying ? "Pause" : "Play"}
        >
          {isPlaying ? <PauseIcon /> : <PlayIcon />}
        </button>
        <button
          type="button"
          className="rp-skip"
          disabled={cur >= last}
          onClick={() => play(cur + 1)}
        >
          <NextIcon />
          <span>Next story</span>
        </button>
      </div>
      <button
        type="button"
        className="rp-stop"
        onClick={() => {
          if (isPlaying) pause();
          setStopped(true);
        }}
      >
        Stop
      </button>
    </section>
  );
}

function PlayIcon(): ReactNode {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M8 5v14l11-7z" />
    </svg>
  );
}
function PauseIcon(): ReactNode {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <rect x="6" y="5" width="4" height="14" rx="1" />
      <rect x="14" y="5" width="4" height="14" rx="1" />
    </svg>
  );
}
function PrevIcon(): ReactNode {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M11 6 3 12l8 6zM21 6l-8 6 8 6z" />
    </svg>
  );
}
function NextIcon(): ReactNode {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="m3 6 8 6-8 6zM13 6l8 6-8 6z" />
    </svg>
  );
}
