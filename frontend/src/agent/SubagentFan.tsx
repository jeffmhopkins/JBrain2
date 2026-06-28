// The in-chat sub-agent fan: jerv's web-sandboxed research/review/summarize children
// rendered as a bordered accordion below its answer bubble (docs/DESIGN.md "Sub-agent
// spawning surfaces", chosen layout A; docs/SUBAGENT_SPAWNING_PLAN.md Wave S3). Folded
// from the parent turn's `subagent_*` events (transcript.ts). Persona is a NEUTRAL tag;
// the only semantic colours are the glyph's steel=running / green=done / rose=failed.

import { useState } from "react";
import type { ReactNode } from "react";
import type { SubagentFan as Fan, SubagentChild } from "./transcript";

// A long fan collapses to this many rows + a "show N more" so a 16-leaf sweep doesn't
// turn the bubble into a wall (review M10).
const MAX_VISIBLE = 8;

const PERSONA_LABEL: Record<string, string> = {
  research: "research",
  review: "review",
  summarize: "summarize",
};

function childGlyph(status: SubagentChild["status"]): ReactNode {
  // aria-hidden — the status word carries the state for assistive tech.
  if (status === "done")
    return (
      <span className="fb-sa-g done" aria-hidden="true">
        ✓
      </span>
    );
  if (status === "failed")
    return (
      <span className="fb-sa-g fail" aria-hidden="true">
        ✕
      </span>
    );
  return (
    <span className="fb-sa-g run" aria-hidden="true">
      <i />
      <i />
      <i />
    </span>
  );
}

function statusWord(c: SubagentChild): string {
  if (c.status === "running") {
    const word = c.phase || "working…";
    // Live step count so a long-running child visibly moves ("researching · 4 steps").
    return c.step ? `${word} · ${c.step} step${c.step === 1 ? "" : "s"}` : word;
  }
  if (c.status === "failed") return c.stopReason === "cancelled" ? "cancelled" : "failed";
  if (c.stopReason === "budget" || c.stopReason === "tree_budget_exhausted") return "truncated";
  return "done";
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function BudgetMeter({ spent, total }: { spent: number; total: number }): ReactNode {
  const pct = Math.min(100, Math.round((spent / total) * 100));
  const cls = pct >= 99 ? "danger" : pct > 70 ? "warn" : "";
  const txt = cls === "danger" ? "budget exhausted" : `${fmtTokens(spent)} / ${fmtTokens(total)}`;
  return (
    <div
      className={`fb-sa-budget ${cls}`.trim()}
      role="meter"
      aria-label="sub-agent budget"
      aria-valuenow={pct}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <span className="fb-sa-budget-txt">{txt}</span>
      <span className="fb-sa-track" aria-hidden="true">
        <i style={{ width: `${pct}%` }} />
      </span>
    </div>
  );
}

export function SubagentFan({
  fan,
  running,
  onStop,
}: {
  fan: Fan;
  /** The parent turn is still streaming — the header shows a cascade Stop. */
  running: boolean;
  onStop?: (() => void) | undefined;
}): ReactNode {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [showAll, setShowAll] = useState(false);

  const children = fan.children;
  if (children.length === 0) return null;
  const liveCount = children.filter((c) => c.status === "running").length;
  const failed = children.filter((c) => c.status === "failed").length;
  const settled = liveCount === 0;
  const total = children.length;

  const shown = showAll ? children : children.slice(0, MAX_VISIBLE);
  const hidden = children.length - shown.length;

  function toggle(id: string): void {
    setExpanded((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // One denominator for both states (all depths), so "· N agents" while live and
  // "done · N ran" once settled never disagree.
  const count = settled
    ? `· done · ${total} ran${failed ? ` · ${failed} failed` : ""}`
    : `· ${total} agent${total === 1 ? "" : "s"}`;
  const liveLabel = settled
    ? `sub-agents done — ${total} ran${failed ? `, ${failed} failed` : ""}`
    : `${liveCount} sub-agent${liveCount === 1 ? "" : "s"} running`;

  return (
    <div className="fb-sa">
      <div className="fb-sa-head">
        <span className="fb-sa-spark" aria-hidden="true">
          ✦
        </span>
        <span className="fb-sa-h-t">{settled ? "Sub-agents" : "Researching"}</span>
        <span className="fb-sa-h-c">{count}</span>
        {running && !settled && onStop && (
          <button type="button" className="fb-sa-stop" onClick={onStop}>
            ■ Stop
          </button>
        )}
      </div>
      {fan.treeBudget > 0 && <BudgetMeter spent={fan.treeSpent} total={fan.treeBudget} />}
      {/* One persistent polite live-region for the whole fan — not N rows (avoids the
          announcement storm); it also announces the settle. The rows are silent to AT. */}
      <div aria-live="polite" className="fb-sr-only">
        {liveLabel}
      </div>
      {shown.map((c) => {
        const isFail = c.status === "failed";
        // A failed row auto-expands its error (like StepRow) — visible without a tap.
        const open = expanded.has(c.childId) || isFail;
        return (
          <div className={`fb-sa-row${c.depth >= 2 ? " sub" : ""}`} key={c.childId}>
            <button
              type="button"
              className="fb-sa-line"
              onClick={() => toggle(c.childId)}
              aria-expanded={open}
            >
              {childGlyph(c.status)}
              <span className="fb-sa-lbl">{c.label}</span>
              <span className="fb-sa-ptag">{PERSONA_LABEL[c.persona] ?? c.persona}</span>
              <span className={`fb-sa-st${isFail ? " fail" : ""}`}>{statusWord(c)}</span>
              <span className="fb-sa-car" aria-hidden="true">
                {open ? "▾" : "▸"}
              </span>
            </button>
            {/* A thin per-row bar: indeterminate sweep while running (children run
                non-streaming — no true %), solid green/rose once settled. */}
            <div className={`fb-sa-bar ${c.status}`} aria-hidden="true">
              <i />
            </div>
            {open && c.summary && (
              <div className={`fb-sa-detail${isFail ? " err" : ""}`}>{c.summary}</div>
            )}
          </div>
        );
      })}
      {hidden > 0 && (
        <button type="button" className="fb-sa-more" onClick={() => setShowAll(true)}>
          show {hidden} more
        </button>
      )}
    </div>
  );
}
