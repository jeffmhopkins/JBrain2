import type { SyncStatus } from "../notes/useNotes";
import { TopBarVitals } from "./TopBarVitals";
import { ChevronLeftIcon } from "./icons";

interface TopBarProps {
  /** Sub-screen title; omitted on home, where the wordmark (or session) shows. */
  title?: string;
  onBack?: () => void;
  syncStatus: SyncStatus;
  /** On home, the active Full Brain session: its name takes the wordmark's slot
   *  so the conversation doesn't spend a second row on a title, and a tap reopens
   *  the Sessions list. Absent in the other home modes, where the wordmark shows. */
  session?: { title: string; onOpen: () => void } | undefined;
}

export function TopBar({ title, onBack, syncStatus, session }: TopBarProps) {
  return (
    <header className="top-bar">
      {title ? (
        <button type="button" className="back-btn" onClick={onBack} aria-label="Back">
          <ChevronLeftIcon size={22} />
          <span className="screen-title">{title}</span>
        </button>
      ) : session ? (
        <button type="button" className="session-title" onClick={session.onOpen}>
          {session.title}
        </button>
      ) : (
        <span className="wordmark">
          JBrain<i>.</i>
        </span>
      )}
      {/* The right cluster is a readout now, not a control: the sync dot and the
          launcher bolt both gave up their slot to the vitals chart. The launcher is
          reached by swiping up on the omnibox, and a sub-screen climbs a level via
          the back chevron or the down-swipe — see docs/reference/DESIGN.md. */}
      <div className="top-bar-right">
        <TopBarVitals syncStatus={syncStatus} />
      </div>
    </header>
  );
}
