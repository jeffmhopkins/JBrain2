// The agent-model sheet (long-press a conversation tab in the omnibox): pick the
// model this conversation's agent runs on, for THIS conversation only, plus — for a
// reasoning route — how hard it thinks. Lists the on-box models currently loaded (the
// quick, no-cold-load picks) plus an "Automatic" row that clears back to the resolved
// default. The model and the reasoning level are INDEPENDENT per-conversation picks:
// either can be set without the other, so the owner can dial reasoning on the default
// route (Automatic) without also pinning a model. Both ride every turn of the open chat
// (useFullBrain's per-session overrides) and clear on reload — they never change the
// global task routing in Settings.

import { useEffect, useState } from "react";
import type { ModelPick } from "../agent/useFullBrain";
import type { ReasoningEffort } from "../api/client";
import { api } from "../api/client";
import { Sheet } from "./Sheet";

interface AgentModelSheetProps {
  /** The open chat's current model pick, or null when it runs on the default route. */
  model: ModelPick | null;
  /** The open chat's current reasoning override, or null when it runs at the route's
   * own effort (shown as the "(default)"-marked pill). */
  effort: ReasoningEffort | null;
  /** Apply a model pick (or null to clear back to Automatic); the sheet closes after. */
  onChooseModel: (pick: ModelPick | null) => void;
  /** Apply a reasoning level (or null to clear back to the route's default). The sheet
   * stays open — model-then-level and level-then-model both land in one visit. */
  onChooseEffort: (effort: ReasoningEffort | null) => void;
  onClose: () => void;
}

interface Row {
  id: string;
  label: string;
  /** False for the current pick when it's no longer resident (unloaded since chosen). */
  loaded: boolean;
  /** Honors a reasoning level — surfaces the effort control when any listed model does. */
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
// Only a last-resort fallback: the route's real effective effort (from the settings
// snapshot) is preferred, so the marked pill reflects what a turn ACTUALLY runs at.
const FALLBACK_DEFAULT: ReasoningEffort = "medium";

export function AgentModelSheet({
  model,
  effort,
  onChooseModel,
  onChooseEffort,
  onClose,
}: AgentModelSheetProps) {
  // null = still loading; [] = loaded but nothing resident.
  const [rows, setRows] = useState<Row[] | null>(null);
  // agent.turn's effective effort from Settings — what a default-route turn ACTUALLY
  // runs at (a stored override, else the task's bucket, else the global default), marked
  // "(default)" so the owner sees what "leave it alone" means and it matches the truth.
  const [defaultLevel, setDefaultLevel] = useState<ReasoningEffort>(FALLBACK_DEFAULT);
  // Whether the default route (agent.turn) is itself reasoning-capable — the effort
  // control is meaningful on Automatic only then. The backend reports null here for a
  // non-reasoning route.
  const [routeReasons, setRouteReasons] = useState(false);

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
        setRouteReasons(turn?.reasoning_effort != null);
        setDefaultLevel(turn?.reasoning_effort ?? s.reasoning_default ?? FALLBACK_DEFAULT);
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
    model && !list.some((r) => r.id === model.id)
      ? [...list, { id: model.id, label: model.label, loaded: false, reasons: false }]
      : list;
  // The level control earns its place when it can ever apply: the default route reasons
  // (so it works on Automatic), or a listed/loaded model does.
  const showEfforts = routeReasons || withSelected.some((r) => r.reasons);

  function pickModel(next: ModelPick | null) {
    onChooseModel(next);
    onClose();
  }

  // Pills set the level WITHOUT closing (so model-then-level and level-then-model both
  // land in one visit), and independently of the model pick — so a default-route
  // conversation can carry a reasoning level with no model change. Tapping the
  // "(default)"-marked pill clears the override (null), so the route's own effort keeps
  // applying (and keeps tracking Settings).
  function armEffort(level: ReasoningEffort) {
    onChooseEffort(level === defaultLevel ? null : level);
  }

  return (
    <Sheet title="Conversation model" onClose={onClose}>
      <p className="model-sheet-note">
        Pick the model this conversation runs on. It applies to this conversation only.
      </p>
      <div className="domain-rows" aria-label="Agent model">
        <button
          type="button"
          aria-pressed={model === null}
          className={`domain-row${model === null ? " domain-row-on" : ""}`}
          onClick={() => pickModel(null)}
        >
          <span className="model-row-name">Automatic</span>
          <span className="model-row-meta">default route</span>
          {model === null && (
            <span className="model-row-check" aria-hidden="true">
              ✓
            </span>
          )}
        </button>
        {withSelected.map((row) => {
          const on = model?.id === row.id;
          return (
            <button
              key={row.id}
              type="button"
              aria-pressed={on}
              className={`domain-row${on ? " domain-row-on" : ""}`}
              onClick={() => pickModel({ id: row.id, label: row.label })}
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
            How hard a reasoning model thinks for this conversation. Applies whether or not a model
            is pinned; models without a reasoning control ignore it.
          </p>
        </>
      )}
      {rows !== null && withSelected.length === 0 && !showEfforts && (
        <p className="model-sheet-empty">
          No models loaded. Load one from Settings to run this conversation on it.
        </p>
      )}
    </Sheet>
  );
}
