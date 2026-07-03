import { useEffect, useState } from "react";
import { type LlmUsage, type UsageTotals, api } from "../api/client";

/** Compact token count: `41k`, `1.2M`. Shared with the Runs surface, which
 * shows per-run token spend in the same register. */
export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

/** `41k in · 12k out · ~$0.08`; cost omitted when the price table has no
 * entry for the model — tokens only, never a guessed price. */
function usageLine(totals: UsageTotals): string {
  const parts = [`${fmtTokens(totals.input_tokens)} in`, `${fmtTokens(totals.output_tokens)} out`];
  if (totals.cost_usd !== null) parts.push(`~$${totals.cost_usd.toFixed(2)}`);
  return parts.join(" · ");
}

/** AI usage (docs/reference/ANALYSIS.md "Token accounting"): live token totals from the
 * adapter's llm_usage rows, priced at query time. Lives on the LLM Settings
 * screen as a collapsible drawer next to the model controls (moved off Ops in
 * the B3 redesign — spend belongs with the model config that drives it). The
 * card self-fetches and fails quietly: usage is telemetry, never blocking. */
export function AiUsageCard() {
  const [usage, setUsage] = useState<LlmUsage | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let stale = false;
    api
      .llmUsage()
      .then((u) => {
        // Telemetry fails quietly: a missing/malformed payload is treated as
        // "no data yet", never an exception that takes the screen down with it.
        if (!stale && u?.today && u.month) setUsage(u);
      })
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, []);

  const summary = usage ? usageLine(usage.month) : "";
  return (
    <section className="llm-local">
      <button
        type="button"
        className="llm-local-toggle"
        aria-expanded={open}
        aria-label={`AI usage${usage ? ` — this month ${summary}` : ""}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="llm-local-title">AI usage</span>
        <span className="llm-local-summary">{summary}</span>
        <span className={`llm-exp-caret${open ? " llm-exp-open" : ""}`} aria-hidden="true">
          ›
        </span>
      </button>

      {open && (
        <div className="llm-local-body">
          {usage === null ? (
            <p className="llm-local-hint">no usage data yet.</p>
          ) : (
            <div className="llm-usage-rows">
              <div className="usage-row">
                <span className="usage-label">today</span>
                <span className="usage-value">{usageLine(usage.today)}</span>
              </div>
              <div className="usage-row">
                <span className="usage-label">this month</span>
                <span className="usage-value">{usageLine(usage.month)}</span>
              </div>
              {usage.by_task.length > 0 && (
                <div className="usage-tasks">
                  {usage.by_task.map((task) => (
                    <div key={task.task} className="usage-row usage-task-row">
                      <span className="usage-label">{task.task}</span>
                      <span className="usage-value">{usageLine(task)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
