// The Full Brain surface, rendered inline in the home page body: the streamed
// transcript with the two lateral panels the mock specifies — Sessions slides in
// from the left, Proposals from the right (docs/mocks/assistant-lateral-swipe.html).
// A horizontal swipe is the in-context shortcut (right→Sessions, left→Proposals,
// the opposite swipe sends the open panel back out); the header buttons do the
// same for anyone who'd rather tap. The composer is the omnibox, not here — this
// surface only reads `fb` and renders.

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
import { ProposalTree } from "./ProposalTree";
import { ProposalsPanel } from "./ProposalsPanel";
import { SessionsPanel } from "./SessionsPanel";
import { type AgentStatus, agentStatus } from "./status";
import type { TranscriptMessage } from "./transcript";
import type { FullBrain } from "./useFullBrain";
import { ToolView } from "./views/registry";

const OPEN_PX = 56; // horizontal travel that commits a panel open or closed

export function FullBrainSurface({ fb }: { fb: FullBrain }): ReactNode {
  const drag = useRef<{ x: number; axis: "?" | "h" | "v" } | null>(null);
  const { panel, setPanel } = fb;

  function onTouchStart(e: TouchEvent): void {
    const target = e.target as HTMLElement;
    // Text fields opt out so typing/selection isn't hijacked; taps on buttons
    // fall through (a tap never travels OPEN_PX).
    if (target.closest("textarea, input, select")) {
      drag.current = null;
      return;
    }
    const t = e.touches[0];
    drag.current = t ? { x: t.clientX, axis: "?" } : null;
  }

  function onTouchMove(e: TouchEvent): void {
    const d = drag.current;
    const t = e.touches[0];
    if (!d || !t) return;
    if (d.axis === "?" && Math.abs(t.clientX - d.x) > 10) d.axis = "h";
  }

  function onTouchEnd(e: TouchEvent): void {
    const d = drag.current;
    drag.current = null;
    const t = e.changedTouches[0];
    if (!d || !t || d.axis !== "h") return;
    const dx = t.clientX - d.x;
    if (Math.abs(dx) < OPEN_PX) return;
    if (panel === "none") {
      setPanel(dx > 0 ? "sessions" : "proposals");
    } else if (panel === "sessions" && dx < 0) {
      setPanel("none"); // swipe it back out the way it came
    } else if (panel === "proposals" && dx > 0) {
      setPanel("none");
    }
  }

  // Just the session's name up top — the panels are a swipe away (right for
  // Sessions, left for Proposals). Tapping the name reopens the Sessions list.
  const title = fb.active ? fb.active.title || "Untitled session" : "Full Brain";

  return (
    <div
      className="fb-shell"
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
    >
      <div className="fullbrain">
        <button type="button" className="fb-title" onClick={() => setPanel("sessions")}>
          {title}
        </button>

        {fb.active ? (
          <main className="fb-chat" aria-label="Conversation">
            {fb.messages.map((m, i) => (
              // Transcript is append-only; positional key is stable for the turn.
              // biome-ignore lint/suspicious/noArrayIndexKey: append-only transcript
              <Bubble key={i} message={m} />
            ))}
            {fb.messages.length === 0 && (
              <p className="fb-empty">Talk it out below — full tool access.</p>
            )}
          </main>
        ) : (
          <div className="fb-empty">Choose a session to start asking about your brain.</div>
        )}

        {/* The live status sits at the surface's bottom edge, just above the
            omnibox composer — replacing the old in-bubble "…". */}
        <AgentStatusLine status={agentStatus(fb.messages)} />
      </div>

      <aside
        className={`panel left${panel === "sessions" ? " open" : ""}`}
        aria-hidden={panel !== "sessions"}
      >
        <SessionsPanel
          sessions={fb.sessions}
          onOpen={fb.open}
          onCreate={fb.create}
          onClose={() => setPanel("none")}
        />
      </aside>

      <aside
        className={`panel right${panel === "proposals" ? " open" : ""}`}
        aria-hidden={panel !== "proposals"}
      >
        {fb.openProposal === null ? (
          <ProposalsPanel
            proposals={fb.proposals}
            onOpen={(p) => fb.setOpenProposal(p.id)}
            onClose={() => setPanel("none")}
          />
        ) : (
          <ProposalTree proposalId={fb.openProposal} onClose={() => fb.setOpenProposal(null)} />
        )}
      </aside>
    </div>
  );
}

// The B-direction status line (docs/mocks/assistant-ai-status-*.html): a quiet
// pulsing dot and a label that shimmers steel while the agent is live, then
// settles; a clean finish auto-hides after a beat, errors stay put.
function AgentStatusLine({ status }: { status: AgentStatus | null }): ReactNode {
  const [doneHidden, setDoneHidden] = useState(false);
  // Reset on any kind change; a clean finish hides itself after a beat. Keying
  // on `kind` keeps the timer from re-arming when it fires (kind is unchanged).
  const kind = status?.kind;
  useEffect(() => {
    setDoneHidden(false);
    if (kind !== "done") return;
    const t = setTimeout(() => setDoneHidden(true), 2600);
    return () => clearTimeout(t);
  }, [kind]);

  if (!status || (status.kind === "done" && doneHidden)) return null;
  const live = status.kind === "thinking" || status.kind === "tool" || status.kind === "answering";
  const cls = live ? "live" : status.kind === "error" ? "err" : "done";

  return (
    <output className={`fb-status ${cls}`}>
      <span className="fb-status-mark" aria-hidden="true" />
      <span className="fb-status-lab">
        {status.label}
        {status.emphasis ? <span className="tool"> {status.emphasis}</span> : null}
        {live ? "…" : ""}
      </span>
    </output>
  );
}

function Bubble({ message }: { message: TranscriptMessage }): ReactNode {
  if (message.role === "user") {
    return <div className="bubble me">{message.text}</div>;
  }
  // A turn that's still thinking (no text, no tools, no views yet) shows nothing
  // here — the status line above the composer carries that state instead.
  if (!message.text && message.tools.length === 0 && message.views.length === 0) {
    return null;
  }
  return (
    <div className="bubble ai">
      {message.text && <span className="fb-text">{message.text}</span>}
      {message.tools.map((t) => (
        <div className="tool" key={t.id}>
          <span className={t.ok === false ? "err" : "ok"}>
            {t.ok === undefined ? "running" : t.ok ? "✓" : "✗"}
          </span>
          <span>
            {t.name}
            {t.summary ? ` · ${t.summary}` : ""}
          </span>
        </div>
      ))}
      {message.views.map((v, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
        <ToolView key={i} payload={v} />
      ))}
    </div>
  );
}
