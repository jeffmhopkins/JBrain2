import { Fragment, useState } from "react";
import { edgePath, valueLabel } from "../../analysis/format";
import { type TraceStage, confidenceBadge, traceLog } from "../payload";
import type { ReviewBlock } from "./types";

/** The optional process-trace dropdown (docs/mocks/review-process-trace-mockups
 * "Direction A"): a timeline of the three pipeline stages — tap a stage to
 * expand it — with a "show console" toggle that swaps the timeline for the dense
 * raw log in the same spot, copyable in one tap for troubleshooting. */
function ProcessTrace({
  stages,
  factLine,
  verdictLine,
}: {
  stages: TraceStage[];
  factLine: string;
  verdictLine: string;
}) {
  const [open, setOpen] = useState(false);
  const [showConsole, setShowConsole] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [copied, setCopied] = useState(false);

  function copy() {
    void navigator.clipboard?.writeText(traceLog(stages, factLine, verdictLine));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }
  function toggleStage(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }

  return (
    <div className="rtrace">
      <button
        type="button"
        className="rtrace-toggle"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="rtrace-lead">
          how this was decided — <b>{stages.length} stages</b>
        </span>
        <span className="rtrace-chev" aria-hidden="true">
          ›
        </span>
      </button>

      {open && (
        <div className="rtrace-panel">
          <div className="rtrace-actions">
            <button type="button" className="rtrace-mode" onClick={() => setShowConsole((c) => !c)}>
              {showConsole ? "▤ show timeline" : "‹/› show console"}
            </button>
          </div>

          {showConsole ? (
            <div className="rtrace-console">
              <div className="rtrace-console-head">
                <span>raw trace · paste anywhere</span>
                <button
                  type="button"
                  className={`rtrace-copy${copied ? " done" : ""}`}
                  onClick={copy}
                >
                  {copied ? "copied ✓" : "copy"}
                </button>
              </div>
              <div className="rtrace-log">
                {stages.map((s) => (
                  <div key={s.key} className={`rtrace-block stage-${s.key}`}>
                    <div className="rtrace-cstage">
                      <span className="rtrace-dot" />
                      {s.name.toUpperCase()}
                      <span className="rtrace-cver">{s.version}</span>
                    </div>
                    {s.rows.map(([k, v]) => (
                      <div key={k} className="rtrace-cline">
                        <span className="rtrace-k">{k}</span> ={" "}
                        <span className="rtrace-v">{v}</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <ol className="rtrace-timeline">
              {stages.map((s) => {
                const isOpen = expanded.has(s.key);
                return (
                  <li key={s.key} className={`rtrace-node stage-${s.key}${isOpen ? " open" : ""}`}>
                    <span className="rtrace-bullet" aria-hidden="true" />
                    <button
                      type="button"
                      className="rtrace-head"
                      aria-expanded={isOpen}
                      onClick={() => toggleStage(s.key)}
                    >
                      <span className="rtrace-stage">{s.name}</span>
                      <span className="rtrace-ver">{s.version}</span>
                    </button>
                    <p className="rtrace-summary">{s.summary}</p>
                    {isOpen && (
                      <dl className="rtrace-kv">
                        {s.rows.map(([k, v]) => (
                          <Fragment key={k}>
                            <dt>{k}</dt>
                            <dd>{v}</dd>
                          </Fragment>
                        ))}
                      </dl>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </div>
      )}
    </div>
  );
}

/** The verbose extraction → integration → arbiter trace. Self-gates when the
 * payload carries none. */
export const Trace: ReviewBlock = ({ ctx }) => {
  const { parsed } = ctx;
  if (parsed.trace === null) return null;
  const conf = confidenceBadge(parsed.confidence);
  return (
    <ProcessTrace
      stages={parsed.trace}
      factLine={`${edgePath(parsed.predicate ?? "", parsed.qualifier)} → ${valueLabel(
        parsed.valueJson,
        parsed.statement ?? "",
      )}`}
      verdictLine={conf?.text ?? "held for review"}
    />
  );
};
