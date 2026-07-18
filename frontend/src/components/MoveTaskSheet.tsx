// Move-to-group bottom sheet (Tasks, GUI Direction B — docs/mocks/task-grouping/
// b-chips-move-sheet.html). Filing a task into a bucket is a deliberate menu pick,
// never a drag across the screen: the owner's groups as rows (the current one
// ticked), an "Ungrouped" escape, and a "New group…" row that files into a
// bucket it creates in the same tap. Composes the shared Sheet.

import { useState } from "react";
import type { TaskGroup } from "../api/client";
import { Sheet } from "./Sheet";
import { CheckIcon, PlusIcon } from "./icons";

interface MoveTaskSheetProps {
  taskName: string;
  currentGroupId: string | null;
  groups: TaskGroup[];
  /** Move into an existing bucket (null = Ungrouped). */
  onMove: (groupId: string | null) => void;
  /** Create a bucket by this name and move the task into it. */
  onCreateAndMove: (name: string) => void;
  onClose: () => void;
}

export function MoveTaskSheet({
  taskName,
  currentGroupId,
  groups,
  onMove,
  onCreateAndMove,
  onClose,
}: MoveTaskSheetProps) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");

  function submitNew() {
    const trimmed = name.trim();
    if (trimmed) onCreateAndMove(trimmed);
  }

  return (
    <Sheet title="Move task" onClose={onClose}>
      <p className="movetask-lead">
        Move <b>{taskName}</b> to…
      </p>
      <div className="movetask-rows" aria-label="Destination group">
        {groups.map((g) => (
          <button
            key={g.id}
            type="button"
            aria-pressed={g.id === currentGroupId}
            className={`movetask-row${g.id === currentGroupId ? " movetask-row-on" : ""}`}
            onClick={() => onMove(g.id)}
          >
            <span className="movetask-dot" aria-hidden="true" />
            <span className="movetask-name">{g.name}</span>
            {g.id === currentGroupId && <CheckIcon size={16} />}
          </button>
        ))}
        <button
          type="button"
          aria-pressed={currentGroupId === null}
          className={`movetask-row${currentGroupId === null ? " movetask-row-on" : ""}`}
          onClick={() => onMove(null)}
        >
          <span className="movetask-dot idle" aria-hidden="true" />
          <span className="movetask-name">Ungrouped</span>
          {currentGroupId === null && <CheckIcon size={16} />}
        </button>

        {creating ? (
          <div className="movetask-new">
            <input
              className="movetask-input"
              value={name}
              placeholder="New group name"
              aria-label="New group name"
              // biome-ignore lint/a11y/noAutofocus: the row morphed into an input on the user's tap — focus belongs here.
              autoFocus
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submitNew();
                if (e.key === "Escape") setCreating(false);
              }}
            />
            <button
              type="button"
              className="movetask-create"
              disabled={name.trim().length === 0}
              onClick={submitNew}
            >
              Create
            </button>
          </div>
        ) : (
          <button
            type="button"
            className="movetask-row movetask-new-row"
            onClick={() => setCreating(true)}
          >
            <PlusIcon size={16} />
            <span className="movetask-name">New group…</span>
          </button>
        )}
      </div>
      <button type="button" className="movetask-cancel" onClick={onClose}>
        Cancel
      </button>
    </Sheet>
  );
}
