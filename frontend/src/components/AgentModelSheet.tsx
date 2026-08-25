// The agent-model sheet (long-press a conversation tab in the omnibox): pick the
// model this conversation's agent runs on, for THIS conversation only. Lists the
// on-box models currently loaded (the quick, no-cold-load picks) plus an
// "Automatic" row that clears back to the resolved default, and — for reasoning
// models — how hard the pick thinks (its per-conversation reasoning level). The
// choice rides every turn of the open chat (useFullBrain's per-session override)
// and clears on reload — it never changes the global task routing in Settings.

import { useEffect, useState } from "react";
import type { ModelPick } from "../agent/useFullBrain";
import type { ReasoningEffort } from "../api/client";
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
  /** Honors a reasoning level — only these rows carry the armed level onto the pick. */
  reasons: boolean;
}

const EFFORTS: ReasoningEffort[] = ["none", "low", "medium", "high"];
const EFFORT_LABEL: Record<ReasoningEffort, string> = {
  none: "None",
  low: "Low",
  medium: "Medium",
  high: "High",
};
// What a reasoning model runs at when the route carries no effort at all — the
// model's own built-in default (the router's medium-bucket contract for agent.turn).
const FALLBACK_DEFAULT: ReasoningEffort = "medium";

export function AgentModelSheet({ selected, onChoose, onClose }: AgentModelSheetProps) {
  // null = still loading; [] = loaded but nothing resident.
  const [rows, setRows] = useState<Row[] | null>(null);
  // The armed reasoning level (null = the route's default, shown as the
  // "(default)"-marked pill). Seeded from the current pick; a row tap carries it onto
  // the pick, and a pill tap re-applies it live when a reasoning pick is already
  // active — so either order works in one visit.
  const [effort, setEffort] = useState<ReasoningEffort | null>(selected?.effort ?? null);
  // The level a turn runs at with no override — agent.turn's effective effort from
  // Settings (the stored effort rides onto a picked model too), marked "(default)"
  // on its pill so the owner sees what "leave it alone" means.
  const [defaultLevel, setDefaultLevel] = useState<ReasoningEffort>(FALLBACK_DEFAULT);

  useEffect(() => {
    let stale = false;
    api
      .getLlmSettings()
      .then((s) => {
        if (stale) return;
        setRows(
          s.local_models
            .filter((m) => m.loaded)
            .map((m) => ({
              id: m.id,
              label: m.label,
              loaded: true,
              reasons: m.supports_reasoning,
            })),
        );
        const turn = s.tasks?.find((t) => t.id === "agent.turn");
        setDefaultLevel(turn?.reasoning_effort ?? FALLBACK_DEFAULT);
      })
      .catch(() => {
        if (!stale) setRows([]);
      });
    return () => {
      stale = true;
    };
  }, []);

  // Keep the current pick visible even if it's no longer resident (unloaded since it
  // was chosen), so the owner still sees — and can clear — the active choice. Its
  // catalog capability is gone with the row, so a carried level is the evidence it
  // reasons (keeps the control alive for the pick it's already applied to).
  const list: Row[] = rows ?? [];
  const withSelected =
    selected && !list.some((r) => r.id === selected.id)
      ? [
          ...list,
          { id: selected.id, label: selected.label, loaded: false, reasons: !!selected.effort },
        ]
      : list;
  const selectedRow = selected ? withSelected.find((r) => r.id === selected.id) : undefined;
  // The level control earns its place only when it can ever apply.
  const showEfforts = withSelected.some((r) => r.reasons);

  function pick(next: ModelPick | null) {
    onChoose(next);
    onClose();
  }

  function pickRow(row: Row) {
    pick({ id: row.id, label: row.label, ...(row.reasons && effort ? { effort } : {}) });
  }

  // Pills arm the level without closing (so model-then-level and level-then-model both
  // land in one visit); with a reasoning pick already active the change applies live.
  // Tapping the "(default)"-marked pill arms null — no override on the wire, so the
  // route's own effort keeps applying (and keeps tracking Settings).
  function armEffort(level: ReasoningEffort) {
    const next = level === defaultLevel ? null : level;
    setEffort(next);
    if (selected && selectedRow?.reasons) {
      onChoose({ id: selected.id, label: selected.label, ...(next ? { effort: next } : {}) });
    }
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
              onClick={() => pickRow(row)}
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
      {showEfforts && (
        <>
          <p className="model-sheet-subhead">Reasoning</p>
          <div className="seg-row model-effort-row" aria-label="Reasoning level">
            {EFFORTS.map((e) => {
              const on = (effort ?? defaultLevel) === e;
              return (
                <button
                  key={e}
                  type="button"
                  className={`seg${on ? " seg-on" : ""}`}
                  aria-pressed={on}
                  onClick={() => armEffort(e)}
                >
                  <span>{EFFORT_LABEL[e]}</span>
                  {e === defaultLevel && <span className="model-effort-default">(default)</span>}
                </button>
              );
            })}
          </div>
          <p className="model-sheet-note model-effort-note">
            How hard a reasoning model thinks for this conversation. Rides the model pick; models
            without a reasoning control ignore it.
          </p>
        </>
      )}
      {rows !== null && withSelected.length === 0 && (
        <p className="model-sheet-empty">
          No models loaded. Load one from Settings to run this conversation on it.
        </p>
      )}
    </Sheet>
  );
}
