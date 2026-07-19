// The in-chat sub-agent fan: jerv's web-sandboxed research/review/summarize children
// rendered as a bordered accordion below its answer bubble (docs/reference/DESIGN.md "Sub-agent
// spawning surfaces", chosen layout A; docs/archive/SUBAGENT_SPAWNING_PLAN.md Wave S3). Folded
// from the parent turn's `subagent_*` events (transcript.ts). Persona is a NEUTRAL tag;
// the only semantic colours are the glyph's steel=running / green=done / rose=failed.

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { BrainGlyph } from "./glyphs";
import { Markdown } from "./markdown";
import type { SubagentFan as Fan, SubagentChild, SubagentTraceItem } from "./transcript";

// A friendlier label for the web tools a child runs (shown inline in its trace).
const TOOL_LABEL: Record<string, string> = {
  web_search: "search",
  web_fetch: "fetch",
  current_time: "clock",
};

// A child's live trace: its reasoning AND tool calls interleaved in ONE collapsible
// disclosure that reads like the main answer's "Thinking" (same BrainGlyph + violet
// register), default open while the child works and auto-scrolling to the newest line.
// Folding everything behind one toggle keeps a heavy-tool-use child (20+ searches) from
// turning the fan into a wall — the tools live inside the thinking, not a flat list.
function ChildTrace({
  items,
  live,
  answering,
}: {
  items: SubagentTraceItem[];
  live: boolean;
  /** The child has begun emitting its answer — thinking is done, so fold the trace and
   * let the streaming answer below take over (matches the main answer's "Thought for Ns"). */
  answering: boolean;
}): ReactNode {
  const [open, setOpen] = useState(true);
  const ref = useRef<HTMLDivElement | null>(null);
  // Auto-collapse ONCE the moment the child starts answering: thinking is done, so the
  // trace folds and the answer streams below it. A ref (not a re-trigger) so a manual
  // re-open after that sticks.
  const didFold = useRef(false);
  useEffect(() => {
    if (answering && !didFold.current) {
      didFold.current = true;
      setOpen(false);
    }
  }, [answering]);
  // Follow the newest line while it streams (only while open + live). `items` is the
  // intentional trigger — each appended chunk re-runs the scroll-to-bottom.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `items` drives the re-scroll
  useEffect(() => {
    if (open && live && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [items, open, live]);
  const toolCount = items.reduce((n, it) => n + (it.kind === "tool" ? 1 : 0), 0);
  const thinking = live && !answering;
  return (
    <div className="fb-sa-trace-wrap">
      <button
        type="button"
        className="fb-sa-trace-tog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="fb-sa-trace-car" aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
        <BrainGlyph className="fb-sa-trace-ic" />
        {thinking ? "Thinking…" : "Thought"}
        {toolCount > 0 && (
          <span className="fb-sa-trace-c">
            {" · "}
            {toolCount} tool{toolCount === 1 ? "" : "s"}
          </span>
        )}
      </button>
      {open && (
        <div className="fb-sa-trace" ref={ref}>
          {items.map((it, i) =>
            it.kind === "reasoning" ? (
              // biome-ignore lint/suspicious/noArrayIndexKey: trace items append in order
              <span key={i}>{it.text}</span>
            ) : (
              <span
                // biome-ignore lint/suspicious/noArrayIndexKey: trace items append in order
                key={i}
                className={`fb-sa-trace-tool${it.ok ? "" : " bad"}`}
              >
                <span className="fb-sa-trace-mark" aria-hidden="true">
                  {it.ok ? "✓" : "✕"}
                </span>
                <span className="fb-sa-trace-name">{TOOL_LABEL[it.name] ?? it.name}</span>
                {it.arg && (
                  <span className="fb-sa-trace-arg" title={it.arg}>
                    {it.arg}
                  </span>
                )}
              </span>
            ),
          )}
        </div>
      )}
    </div>
  );
}

// A child's final answer: the SAME rich Markdown the main answer renders (headings,
// lists, bold, `[^n]` citations), boxed into a bounded scroll area like the trace so a
// long report doesn't push the whole fan open. Auto-scrolls to the newest line while the
// answer is still streaming, exactly like ChildTrace.
function ChildAnswer({ text, live }: { text: string; live: boolean }): ReactNode {
  const ref = useRef<HTMLDivElement | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: `text` drives the re-scroll
  useEffect(() => {
    if (live && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [text, live]);
  return (
    <div className="fb-sa-sum" ref={ref}>
      <Markdown text={text} streaming={live} />
    </div>
  );
}

// A long fan collapses to this many rows + a "show N more" so a 16-leaf sweep doesn't
// turn the bubble into a wall (review M10).
const MAX_VISIBLE = 8;

const PERSONA_LABEL: Record<string, string> = {
  research: "research",
  review: "review",
  summarize: "summarize",
};

function childGlyph(status: SubagentChild["status"], queued: boolean): ReactNode {
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
  // Running and queued share the three-dot glyph, but ONLY a running child bounces — a
  // queued child (not yet started) shows the dots STATIC so it doesn't read as active.
  return (
    <span className={`fb-sa-g ${queued ? "queued" : "run"}`} aria-hidden="true">
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

// The shared sub-agent budget bar. `total` is the CHILDREN'S pool (the backend sends the
// tree budget minus the root's synthesis reserve), so the bar fills as children exhaust —
// it reads "budget exhausted" exactly when a child hits tree_budget_exhausted, not at some
// fraction with phantom headroom the children can never reach.
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

// A child's live context-window fill, the per-row twin of the composer's context meter
// (docs/archive/SUBAGENT_SPAWNING_PLAN.md): the latest model call's prompt+output over the child
// model's window, so you can watch a research child's context climb as it reads. Tints
// toward the warning hue as it fills — calm until it actually matters.
function ChildContextMeter({ used, window }: { used: number; window: number }): ReactNode {
  const frac = window > 0 ? Math.min(used / window, 1) : 0;
  const pct = Math.round(frac * 100);
  const cls = frac >= 0.9 ? "high" : frac >= 0.7 ? "mid" : "";
  return (
    <span
      className={`fb-sa-ctx${cls ? ` ${cls}` : ""}`}
      title={`context: ${used.toLocaleString()} / ${window.toLocaleString()} tokens (${pct}%)`}
    >
      <span className="fb-sa-ctx-bar" aria-hidden="true">
        <i style={{ width: `${pct}%` }} />
      </span>
      <span className="fb-sa-ctx-txt">
        {fmtTokens(used)}/{fmtTokens(window)}
      </span>
    </span>
  );
}

// A child that's minted but not yet started (serial fan): it shows as "queued" until
// its first progress event flips it to a working phase.
function isQueued(c: SubagentChild): boolean {
  return c.status === "running" && (!c.phase || c.phase === "queued") && !c.step;
}

export function SubagentFan({
  fan,
  running,
  onStop,
  onOpen,
}: {
  fan: Fan;
  /** The parent turn is still streaming — the header shows a cascade Stop. */
  running: boolean;
  onStop?: (() => void) | undefined;
  /** Open a child's own session by id (its `childId` IS the session id). */
  onOpen?: ((sessionId: string) => void) | undefined;
}): ReactNode {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [showAll, setShowAll] = useState(false);
  // Auto-collapse a child the moment it SETTLES: while it streams it auto-expands so you
  // watch it work, but a finished child folds back to a one-line row so a long fan of done
  // children isn't a wall of transcripts. Done once per child (a ref, not a re-trigger) so
  // re-opening a settled child by hand sticks. A FAILED child still shows its error via the
  // isFail auto-open in the row — this only drops it from the manual-expand set.
  const autoCollapsed = useRef<Set<string>>(new Set());
  useEffect(() => {
    const newlySettled = fan.children.filter(
      (c) => c.status !== "running" && !autoCollapsed.current.has(c.childId),
    );
    if (newlySettled.length === 0) return;
    for (const c of newlySettled) autoCollapsed.current.add(c.childId);
    setExpanded((cur) => {
      if (!newlySettled.some((c) => cur.has(c.childId))) return cur;
      const next = new Set(cur);
      for (const c of newlySettled) next.delete(c.childId);
      return next;
    });
  }, [fan.children]);

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

  // A staged (feeding-waves) fan groups its rows by wave and draws feed edges live; a
  // flat fan (no wave/feed data) renders as one ungrouped, MAX_VISIBLE-capped list.
  const staged = children.some((c) => (c.wave ?? 0) > 0 || (c.fedFrom?.length ?? 0) > 0);
  const maxWave = children.reduce((m, c) => Math.max(m, c.wave ?? 0), 0);

  function renderChild(c: SubagentChild): ReactNode {
    const isFail = c.status === "failed";
    const rowSettled = c.status !== "running";
    // The child is actively streaming — auto-expand it so you watch it work (a serial
    // local fan streams one at a time). A failed row also auto-expands its error.
    const hasTrace = Boolean(c.liveTrace && c.liveTrace.length > 0);
    // Answer tokens have begun → thinking is done (folds the trace, streams the answer).
    const answering = Boolean(c.liveText);
    const streaming = !rowSettled && !isQueued(c) && (answering || hasTrace);
    const open = expanded.has(c.childId) || isFail || streaming;
    // "Open session" is gated to a SETTLED child — a still-running one has nothing
    // persisted yet, so opening it would land on a blank conversation.
    const showOpen = Boolean(onOpen) && rowSettled;
    return (
      <div className={`fb-sa-row${c.depth >= 2 ? " sub" : ""}`} key={c.childId}>
        <button
          type="button"
          className="fb-sa-line"
          onClick={() => toggle(c.childId)}
          aria-expanded={open}
        >
          {childGlyph(c.status, isQueued(c))}
          {/* Title-forward: the label owns the row (wraps to two lines) instead of
              sharing it with a persona pill that read the same on nearly every child.
              Persona moves to the expanded detail (below). */}
          <span className="fb-sa-lbl">{c.label}</span>
          <span className={`fb-sa-st${isFail ? " fail" : ""}${isQueued(c) ? " queued" : ""}`}>
            {statusWord(c)}
          </span>
          {c.usedTokens != null && c.contextWindow != null && c.contextWindow > 0 && (
            <ChildContextMeter used={c.usedTokens} window={c.contextWindow} />
          )}
          <span className="fb-sa-car" aria-hidden="true">
            {open ? "▾" : "▸"}
          </span>
        </button>
        {/* A thin per-row bar: STATIC while queued, an indeterminate sweep once running,
            solid green/rose on settle. */}
        <div className={`fb-sa-bar ${isQueued(c) ? "queued" : c.status}`} aria-hidden="true">
          <i />
        </div>
        {/* The feed edge as text (Direction 1) — visible even while the row is collapsed. */}
        {c.fedFrom && c.fedFrom.length > 0 && (
          <div className="fb-sa-fed">← fed by {c.fedFrom.join(", ")}</div>
        )}
        {open && (
          <div className={`fb-sa-detail${isFail ? " err" : ""}`}>
            {/* Persona lives here now (off the collapsed row): a neutral tag naming what
                kind of child this is — research / review / summarize. */}
            <span className="fb-sa-ptag fb-sa-persona">
              {PERSONA_LABEL[c.persona] ?? c.persona}
            </span>
            {/* The child's session-in-miniature: its thinking + tool calls in one
                collapsible trace that folds once the answer begins, then the answer
                streaming below it. */}
            {hasTrace && c.liveTrace && (
              <ChildTrace items={c.liveTrace} live={!rowSettled} answering={answering} />
            )}
            {rowSettled
              ? c.summary && <ChildAnswer text={c.summary} live={false} />
              : c.liveText && <ChildAnswer text={c.liveText} live={true} />}
            {showOpen && onOpen && (
              <button type="button" className="fb-sa-open" onClick={() => onOpen(c.childId)}>
                Open sub-agent session →
              </button>
            )}
          </div>
        )}
      </div>
    );
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
      {staged
        ? Array.from({ length: maxWave + 1 }, (_unused, w) => {
            const wchildren = children.filter((c) => (c.wave ?? 0) === w);
            if (wchildren.length === 0) return null;
            const personas = [
              ...new Set(wchildren.map((c) => PERSONA_LABEL[c.persona] ?? c.persona)),
            ].join(", ");
            return (
              // biome-ignore lint/suspicious/noArrayIndexKey: waves render in stable order
              <div className="fb-sa-wave" key={w}>
                <div className="fb-sa-wh">
                  Wave {w + 1} · {personas}
                  {w > 0 ? ` — fed by wave ${w}` : ""}
                </div>
                {wchildren.map(renderChild)}
              </div>
            );
          })
        : shown.map(renderChild)}
      {!staged && hidden > 0 && (
        <button type="button" className="fb-sa-more" onClick={() => setShowAll(true)}>
          show {hidden} more
        </button>
      )}
    </div>
  );
}
