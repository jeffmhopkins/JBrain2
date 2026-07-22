// The deep_research live-progress timeline + its deepest-research backgrounded wrapper.
// Extracted from FullBrainSurface so the tool-view registry can render the `deepest_run`
// view (DeepestRunCard) without a FullBrainSurface ↔ registry import cycle; FullBrainSurface
// re-exports both for its inline use (and the existing tests).
//
// The timeline is a vertical rail: the eight canonical stages stack down a steel spine, the
// active one opens a slot that hosts its live detail line, the sub-agent fan it spawned, and
// (Write / Revise) the report streaming into a pane — so the owner watches the orchestration
// in one scannable column that never wraps.
import { type ReactNode, useEffect, useRef } from "react";
import { Markdown } from "./markdown";
import type { ToolActivity } from "./transcript";

const DR_PHASES = [
  "Plan",
  "Research",
  "Cross-check",
  "Coverage",
  "Gap-fill",
  "Write",
  "Critique",
  "Revise",
] as const;

export function DeepResearchProgress({
  tool,
  fan,
}: {
  tool: ToolActivity;
  /** The turn's live sub-agent fan (`<SubagentFan>`, unchanged). It mounts in the ACTIVE
   * stage's slot so the roster + budget read as part of the stage that spawned them,
   * rather than a loose block below the whole checklist. */
  fan?: ReactNode;
}): ReactNode {
  const p = tool.progress;
  const step = p?.step ?? 0; // 1-based; 0 before the first phase event lands
  const preview = p?.preview ?? "";
  // The stage whose slot the detail/fan/report hang under: the live ordinal, or Plan while
  // we wait for the first phase event (so a fan that spawned pre-phase still has a home).
  const active = step > 0 ? step : 1;
  // Follow the report as it streams into the pane, unless the reader scrolled up in it.
  const paneRef = useRef<HTMLDivElement | null>(null);
  const stick = useRef(true);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `preview` is the scroll trigger
  useEffect(() => {
    const el = paneRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [preview]);
  function onScroll(): void {
    const el = paneRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 32;
  }
  return (
    <output className="fb-drp" aria-live="polite">
      <ol className="fb-drp-steps">
        {DR_PHASES.map((name, i) => {
          const ord = i + 1;
          // A vertical timeline: everything before the live step reads done, the live step
          // opens its slot, the rest wait — one scannable column that never wraps.
          const state = ord < step ? "done" : ord === active ? "active" : "todo";
          const isActive = state === "active";
          return (
            <li key={name} className={`fb-drp-step ${state}`}>
              <span className="fb-drp-dot" aria-hidden="true">
                {state === "done" ? "✓" : ""}
              </span>
              <span className="fb-drp-name">{name}</span>
              {/* The active stage's slot: its live detail, the sub-agent fan it spawned, and
                  (Write / Revise) the report streaming in — all indented under the stage. */}
              {isActive && (p?.label || fan || preview) && (
                <div className="fb-drp-panel">
                  {p?.label && <div className="fb-drp-active">{p.label}</div>}
                  {fan}
                  {preview && (
                    <div className="fb-drp-report" ref={paneRef} onScroll={onScroll}>
                      <Markdown text={preview} harmonyCitations />
                    </div>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ol>
    </output>
  );
}

/** A background deepest-research run in the chat (DEEPEST_RESEARCH_TOOL_PLAN.md, R8;
 * GUI gate: variant A). Deliberately thin — it IS the `deep_research` timeline card,
 * backgrounded: an amber "deepest" identity + a coarse per-round meta line (a deepest run
 * is background, so it advances per checkpoint tick, not per streamed token), wrapped
 * around the unchanged `DeepResearchProgress` timeline + its `SubagentFan`. Maximum reuse
 * of the deep_research surface, per the chosen mock. */
export function DeepestRunCard({
  run,
  tool,
  fan,
}: {
  run: {
    round: number;
    sources: number;
    coverageLabel: string;
    elapsedLabel: string;
    status: "running" | "done" | "failed";
  };
  tool: ToolActivity;
  fan?: ReactNode;
}): ReactNode {
  const running = run.status === "running";
  return (
    <div className="fb-deepest">
      <div className="fb-deepest-head">
        <span className="fb-deepest-badge">
          <svg viewBox="0 0 24 24" aria-hidden="true" width="12" height="12">
            <path d="M12 3v18M5 8l7-5 7 5M5 16l7 5 7-5" />
          </svg>
          Deepest research
        </span>
        <span className="fb-deepest-sub">{running ? "running in the background" : run.status}</span>
      </div>
      {running && (
        <div className="fb-deepest-meta" aria-live="polite">
          round {run.round} · {run.sources} sources · {run.coverageLabel} · {run.elapsedLabel} · you
          can leave — I'll post the report here
        </div>
      )}
      <DeepResearchProgress tool={tool} fan={fan} />
    </div>
  );
}
