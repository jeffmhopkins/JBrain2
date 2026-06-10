import type { SyncStatus } from "../notes/useNotes";
import { BoltIcon, ChevronLeftIcon } from "./icons";

const SYNC_TEXT: Record<SyncStatus, string> = {
  synced: "synced",
  pending: "sync pending",
  unreachable: "server unreachable",
};

interface TopBarProps {
  /** Sub-screen title; omitted on home, where the wordmark shows instead. */
  title?: string;
  onBack?: () => void;
  syncStatus: SyncStatus;
  onBolt: () => void;
}

export function TopBar({ title, onBack, syncStatus, onBolt }: TopBarProps) {
  return (
    <header className="top-bar">
      {title ? (
        <button type="button" className="back-btn" onClick={onBack} aria-label="Back">
          <ChevronLeftIcon size={22} />
          <span className="screen-title">{title}</span>
        </button>
      ) : (
        <span className="wordmark">
          JBrain<i>.</i>
        </span>
      )}
      <div className="top-bar-right">
        <span
          className={`sync-dot sync-${syncStatus}`}
          role="status"
          aria-label={SYNC_TEXT[syncStatus]}
          title={SYNC_TEXT[syncStatus]}
        />
        <button type="button" className="icon-btn" onClick={onBolt} aria-label="Open launcher">
          <BoltIcon size={20} />
        </button>
      </div>
    </header>
  );
}
