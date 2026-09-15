import type { SyncStatus } from "../notes/useNotes";
import { TopBarVitals } from "./TopBarVitals";
import { ChevronLeftIcon, NoteIcon, RadioIcon } from "./icons";

interface TopBarProps {
  /** Sub-screen title; omitted on home, where the wordmark (or session) shows. */
  title?: string;
  onBack?: () => void;
  syncStatus: SyncStatus;
  /** Opens the vitals detail surface. Absent on screens that have no route to it. */
  onOpenVitals?: (() => void) | undefined;
  /** On home, the active Full Brain session: its name takes the wordmark's slot
   *  so the conversation doesn't spend a second row on a title, and a tap reopens
   *  the Sessions list. Absent in the other home modes, where the wordmark shows. */
  session?: { title: string; onOpen: () => void } | undefined;
  /** On Entry's open note conversation: the note the conversation is ABOUT, one tap away.
   *  A control in the readout cluster for the same reason the radio icon is one — it
   *  exists only while that note is open, so it grows the row rather than shoving the
   *  chart sideways, and the left slot is spent on the back arrow out to the notes list. */
  note?: { onOpen: () => void } | undefined;
  /** A radio this session is holding, if any: present ONLY while the lease is held,
   *  because the icon IS the lease — its presence and the radio being held are one
   *  fact rather than two that can disagree. Undefined the rest of the time. */
  radio?: { onOpen: () => void } | undefined;
}

export function TopBar({
  title,
  onBack,
  syncStatus,
  session,
  note,
  onOpenVitals,
  radio,
}: TopBarProps) {
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
        {/* Beside the vitals rather than in the composer's icon row, where it sat next
            to Attach. Attach is a control you reach for; this is a READOUT of what the
            box is doing, which is what the rest of this cluster already is.
            Outermost, because it comes and goes: a slot that appears and disappears
            between the wordmark and the chart would shove the chart sideways every time
            a lease starts or ends. On the edge it only ever grows the row. */}
        {note && (
          <button
            type="button"
            className="icon-btn note-btn"
            aria-label="Open the note"
            onClick={note.onOpen}
          >
            <NoteIcon size={22} />
          </button>
        )}
        <TopBarVitals syncStatus={syncStatus} onOpen={onOpenVitals} />
        {radio && (
          <button
            type="button"
            className="icon-btn sdr-btn"
            aria-label="Tuned radio"
            onClick={radio.onOpen}
          >
            <RadioIcon size={22} />
          </button>
        )}
      </div>
    </header>
  );
}
