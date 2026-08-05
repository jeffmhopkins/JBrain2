// Rename bottom sheet for the Research Library's Reports tab — the owner overrides the
// LLM-generated (or an earlier manual) heading from the row's ⋯ action sheet. A single
// pre-filled text input; Enter or "Save" commits, Escape or Cancel closes. Composes the
// shared Sheet, mirroring MoveReportSheet.

import { useState } from "react";
import { Sheet } from "./Sheet";

const MAX_TITLE = 120;

interface RenameReportSheetProps {
  currentTitle: string;
  /** Commit the trimmed, non-empty new title. */
  onRename: (title: string) => void;
  onClose: () => void;
}

export function RenameReportSheet({ currentTitle, onRename, onClose }: RenameReportSheetProps) {
  const [title, setTitle] = useState(currentTitle);

  function submit() {
    const trimmed = title.trim();
    if (trimmed) onRename(trimmed);
  }

  return (
    <Sheet title="Rename report" onClose={onClose}>
      <div className="movetask-new">
        <input
          className="movetask-input"
          value={title}
          maxLength={MAX_TITLE}
          placeholder="Report title"
          aria-label="Report title"
          // biome-ignore lint/a11y/noAutofocus: the sheet opened to rename — focus belongs on the input.
          autoFocus
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            if (e.key === "Escape") onClose();
          }}
        />
        <button
          type="button"
          className="movetask-create"
          disabled={title.trim().length === 0}
          onClick={submit}
        >
          Save
        </button>
      </div>
      <button type="button" className="movetask-cancel" onClick={onClose}>
        Cancel
      </button>
    </Sheet>
  );
}
