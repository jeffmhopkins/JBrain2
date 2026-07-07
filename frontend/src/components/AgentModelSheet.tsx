// The agent-model sheet (long-press a conversation tab in the omnibox): pick the
// model this conversation's agent runs on, for THIS conversation only. Lists the
// on-box models currently loaded (the quick, no-cold-load picks) plus an
// "Automatic" row that clears back to the resolved default. The choice rides every
// turn of the open chat (useFullBrain's per-session override) and clears on reload —
// it never changes the global task routing in Settings.

import { useEffect, useState } from "react";
import type { ModelPick } from "../agent/useFullBrain";
import { api } from "../api/client";
import { Sheet } from "./Sheet";

interface AgentModelSheetProps {
  /** The open chat's current pick, or null when it runs on the default route. */
  selected: ModelPick | null;
  /** Apply a pick (or null to clear back to Automatic); the sheet closes after. */
  onChoose: (pick: ModelPick | null) => void;
  onClose: () => void;
}

interface Row {
  id: string;
  label: string;
  /** False for the current pick when it's no longer resident (unloaded since chosen). */
  loaded: boolean;
}

export function AgentModelSheet({ selected, onChoose, onClose }: AgentModelSheetProps) {
  // null = still loading; [] = loaded but nothing resident.
  const [rows, setRows] = useState<Row[] | null>(null);

  useEffect(() => {
    let stale = false;
    api
      .getLlmSettings()
      .then((s) => {
        if (stale) return;
        setRows(
          s.local_models
            .filter((m) => m.loaded)
            .map((m) => ({ id: m.id, label: m.label, loaded: true })),
        );
      })
      .catch(() => {
        if (!stale) setRows([]);
      });
    return () => {
      stale = true;
    };
  }, []);

  // Keep the current pick visible even if it's no longer resident (unloaded since it
  // was chosen), so the owner still sees — and can clear — the active choice.
  const list: Row[] = rows ?? [];
  const withSelected =
    selected && !list.some((r) => r.id === selected.id)
      ? [...list, { id: selected.id, label: selected.label, loaded: false }]
      : list;

  function pick(next: ModelPick | null) {
    onChoose(next);
    onClose();
  }

  return (
    <Sheet title="Conversation model" onClose={onClose}>
      <p className="model-sheet-note">
        Pick the model this conversation runs on. It applies to this conversation only.
      </p>
      <div className="domain-rows" aria-label="Agent model">
        <button
          type="button"
          aria-pressed={selected === null}
          className={`domain-row${selected === null ? " domain-row-on" : ""}`}
          onClick={() => pick(null)}
        >
          <span className="model-row-name">Automatic</span>
          <span className="model-row-meta">default route</span>
          {selected === null && (
            <span className="model-row-check" aria-hidden="true">
              ✓
            </span>
          )}
        </button>
        {withSelected.map((row) => {
          const on = selected?.id === row.id;
          return (
            <button
              key={row.id}
              type="button"
              aria-pressed={on}
              className={`domain-row${on ? " domain-row-on" : ""}`}
              onClick={() => pick({ id: row.id, label: row.label })}
            >
              <span className="model-row-name">{row.label}</span>
              <span className="model-row-meta">{row.loaded ? "loaded" : "not loaded"}</span>
              {on && (
                <span className="model-row-check" aria-hidden="true">
                  ✓
                </span>
              )}
            </button>
          );
        })}
      </div>
      {rows !== null && withSelected.length === 0 && (
        <p className="model-sheet-empty">
          No models loaded. Load one from Settings to run this conversation on it.
        </p>
      )}
    </Sheet>
  );
}
