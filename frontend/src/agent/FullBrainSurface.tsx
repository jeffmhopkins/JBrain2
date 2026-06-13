// The Full Brain surface, rendered inline in the home page body: the streamed
// transcript with the two lateral panels the mock specifies — Sessions slides in
// from the left, Proposals from the right (docs/mocks/assistant-lateral-swipe.html).
// A horizontal swipe is the in-context shortcut (right→Sessions, left→Proposals,
// the opposite swipe sends the open panel back out); the header buttons do the
// same for anyone who'd rather tap. The composer is the omnibox, not here — this
// surface only reads `fb` and renders.

import {
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  type TouchEvent,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { DOMAIN_COLOR } from "../notes/modes";
import { ProposalTree } from "./ProposalTree";
import { ProposalsPanel } from "./ProposalsPanel";
import { SessionsPanel } from "./SessionsPanel";
import { Markdown, unlinkedEntities } from "./markdown";
import { type AgentStatus, agentStatus } from "./status";
import { type SourceRef, toolStep } from "./toolSummary";
import type { ToolActivity, TranscriptMessage } from "./transcript";
import type { FullBrain } from "./useFullBrain";
import { ToolView } from "./views/registry";

const OPEN_PX = 56; // horizontal travel that commits a panel open or closed

interface Props {
  fb: FullBrain;
  /** Open a source note by id (from a Worked-block card). */
  onOpenNote?: ((noteId: string) => void) | undefined;
  /** Open an entity page by id (from a response entity chip). */
  onOpenEntity?: ((entityId: string) => void) | undefined;
}

export function FullBrainSurface({ fb, onOpenNote, onOpenEntity }: Props): ReactNode {
  const drag = useRef<{ x: number; axis: "?" | "h" | "v" } | null>(null);
  const chatRef = useRef<HTMLElement>(null);
  const { panel, setPanel } = fb;

  // Keep the newest turn in view as text streams and tools land — each event
  // hands us a fresh `messages` array, so this re-runs through the whole stream.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run per transcript change; the effect reads the DOM.
  useEffect(() => {
    const el = chatRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [fb.messages]);

  function onTouchStart(e: TouchEvent): void {
    const target = e.target as HTMLElement;
    // Text fields opt out so typing/selection isn't hijacked, and a flip card
    // owns its own horizontal swipe (answer ⇄ tool use); taps on buttons fall
    // through (a tap never travels OPEN_PX).
    if (target.closest("textarea, input, select, .fb-flip")) {
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

  // The session's name lives in the top bar (HomeScreen owns it); the panels are
  // a swipe away — right for Sessions, left for Proposals.
  return (
    <div
      className="fb-shell"
      onTouchStart={onTouchStart}
      onTouchMove={onTouchMove}
      onTouchEnd={onTouchEnd}
    >
      <div className="fullbrain">
        {fb.active ? (
          <main className="fb-chat" aria-label="Conversation" ref={chatRef}>
            {fb.messages.map((m, i) => (
              <Bubble
                // Transcript is append-only; the positional key is stable.
                // biome-ignore lint/suspicious/noArrayIndexKey: append-only transcript
                key={i}
                message={m}
                onOpenNote={onOpenNote}
                onOpenEntity={onOpenEntity}
                onOpenProposal={(id) => {
                  fb.setOpenProposal(id);
                  fb.setPanel("proposals");
                }}
              />
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
          onRename={fb.rename}
          onDelete={fb.remove}
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

function Bubble({
  message,
  onOpenNote,
  onOpenProposal,
  onOpenEntity,
}: {
  message: TranscriptMessage;
  onOpenNote?: ((noteId: string) => void) | undefined;
  onOpenProposal?: ((proposalId: string) => void) | undefined;
  onOpenEntity?: ((entityId: string) => void) | undefined;
}): ReactNode {
  if (message.role === "user") {
    return <div className="bubble me">{message.text}</div>;
  }
  // A turn that's still thinking (no text, no tools, no views yet) shows nothing
  // here — the status line above the composer carries that state instead.
  if (!message.text && message.tools.length === 0 && message.views.length === 0) {
    return null;
  }
  // `[^n]` in the answer maps to the n-th source the turn surfaced (flattened
  // across this turn's tools, in order) — tap opens that note.
  const flatSources = message.tools.flatMap((t) => t.sources ?? []);
  const onCite = onOpenNote
    ? (n: number) => {
        const src = flatSources[n - 1];
        if (src) onOpenNote(src.noteId);
      }
    : undefined;
  // Entities the turn resolved (find_entity), deduped. Those whose name appears
  // in the answer are linkified inline (Markdown); only the rest fall back to
  // chips, so a surfaced entity is never left without a tap target.
  const entities = [
    ...new Map(
      message.tools.flatMap((t) => t.entities ?? []).map((e) => [e.entity_id, e]),
    ).values(),
  ];
  // Hold the fallback chips until the turn settles: mid-stream the name often
  // isn't typed yet, so a chip would flash and then vanish as the inline link
  // takes over. Once streaming ends, chip whatever the prose never named.
  const looseEntities = message.streaming ? [] : unlinkedEntities(message.text, entities);

  // The answer side: the prose, any fallback entity chips, and tool-result views.
  const answer = (
    <>
      {message.text && (
        <Markdown text={message.text} onCite={onCite} entities={entities} onEntity={onOpenEntity} />
      )}
      {looseEntities.length > 0 && (
        <div className="fb-entities">
          {looseEntities.map((e) => (
            <button
              key={e.entity_id}
              type="button"
              className="entity-chip"
              onClick={() => onOpenEntity?.(e.entity_id)}
            >
              <span
                className="ent-dot"
                style={{ background: DOMAIN_COLOR[e.domain] ?? "var(--text-3)" }}
              />
              {e.label}
            </button>
          ))}
        </div>
      )}
      {message.views.map((v, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
        <ToolView key={i} payload={v} />
      ))}
    </>
  );

  // No tools → a plain bubble. With tools, the bubble is a flip card: the answer
  // faces front, swipe left to turn it to the tool use (docs/mocks/
  // assistant-flip-tooluse.html).
  if (message.tools.length === 0) {
    return <div className="bubble ai">{answer}</div>;
  }
  return (
    <FlipBubble tools={message.tools} onOpenNote={onOpenNote} onOpenProposal={onOpenProposal}>
      {answer}
    </FlipBubble>
  );
}

const FLIP_PX = 44; // horizontal travel that commits a flip

// A two-faced assistant bubble: the answer up front, the tool use on the back.
// A horizontal swipe (or a tap on the corner cue) turns it; the box stays a
// fixed width pinned to the left so it flips in place, and the height eases to
// whichever face shows. The back is sized to its own content (top/left/right,
// no bottom) so a tall tool run is never clipped.
function FlipBubble({
  tools,
  onOpenNote,
  onOpenProposal,
  children,
}: {
  tools: ToolActivity[];
  onOpenNote?: ((noteId: string) => void) | undefined;
  onOpenProposal?: ((proposalId: string) => void) | undefined;
  children: ReactNode;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const flip = useRef<HTMLDivElement>(null);
  const front = useRef<HTMLDivElement>(null);
  const back = useRef<HTMLDivElement>(null);
  // Becomes a real drag only once the gesture proves horizontal — until then a
  // press falls through so inner cards/chips stay tappable.
  const drag = useRef<{ x: number; y: number; active: boolean } | null>(null);

  const steps = tools.map(toolStep);
  const sourceCount = steps.reduce((n, s) => n + s.sources.length, 0);
  const parts = [`${steps.length} step${steps.length === 1 ? "" : "s"}`];
  if (sourceCount) parts.push(`${sourceCount} source${sourceCount === 1 ? "" : "s"}`);
  const staged = tools.find((t) => t.proposal)?.proposal;

  function fit(animate: boolean): void {
    const w = wrap.current;
    const face = (open ? back.current : front.current)?.offsetHeight;
    if (!w || face == null) return;
    w.style.transition = animate ? "height 0.28s cubic-bezier(0.2,0.7,0.3,1)" : "none";
    w.style.height = `${face}px`;
  }
  // Apply the flip + fit the height from `open`. Runs every render so streaming
  // text re-fits the front; height only animates on an actual turn, and the
  // hidden face is inert so it's out of the tab order / a11y tree.
  const prevOpen = useRef(open);
  useLayoutEffect(() => {
    if (flip.current) {
      flip.current.style.transition = "";
      flip.current.style.transform = `rotateY(${open ? 180 : 0}deg)`;
    }
    fit(prevOpen.current !== open);
    prevOpen.current = open;
    if (front.current) front.current.inert = open;
    if (back.current) back.current.inert = !open;
  });

  function onPointerDown(e: ReactPointerEvent): void {
    drag.current = { x: e.clientX, y: e.clientY, active: false };
  }
  function onPointerMove(e: ReactPointerEvent): void {
    const d = drag.current;
    if (!d || !flip.current) return;
    const dx = e.clientX - d.x;
    if (!d.active) {
      // Claim the gesture only when it's clearly horizontal; otherwise let the
      // vertical scroll have it.
      if (Math.abs(dx) < 8 || Math.abs(dx) <= Math.abs(e.clientY - d.y)) return;
      d.active = true;
      flip.current.style.transition = "none";
      const w = wrap.current;
      const h = Math.max(front.current?.offsetHeight ?? 0, back.current?.offsetHeight ?? 0);
      if (w) {
        w.style.transition = "none";
        w.style.height = `${h}px`;
      }
      flip.current.setPointerCapture(e.pointerId);
    }
    const deg = Math.max(0, Math.min(180, (open ? 180 : 0) + (d.x - e.clientX) * 0.55));
    flip.current.style.transform = `rotateY(${deg}deg)`;
  }
  function onPointerUp(e: ReactPointerEvent): void {
    const d = drag.current;
    drag.current = null;
    if (!d || !d.active) return; // a tap fell through to the inner control
    const dx = d.x - e.clientX; // a left swipe is positive
    setOpen(open ? !(dx < -FLIP_PX) : dx > FLIP_PX);
  }

  return (
    <div className="fb-flipwrap" ref={wrap}>
      {/* Pointer-only swipe surface; the keyboard path is the cue buttons below. */}
      <div
        className="fb-flip"
        ref={flip}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => {
          drag.current = null;
        }}
      >
        <div className="bubble ai fb-face fb-front" ref={front}>
          {children}
          <button type="button" className="fb-cue" onClick={() => setOpen(true)}>
            <ChevronGlyph className="fb-cue-ic back" />
            {steps.length} tool{steps.length === 1 ? "" : "s"}
          </button>
        </div>
        <div className="bubble ai fb-face fb-back" ref={back}>
          <div className="fb-back-head">
            <GearGlyph />
            <span>Worked</span>
            <span className="tw-meta">· {parts.join(" · ")}</span>
          </div>
          {staged && (
            <button
              type="button"
              className="proposal-chip"
              onClick={() => onOpenProposal?.(staged.proposal_id)}
            >
              <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
                <rect x="9" y="3" width="6" height="4" rx="1" />
                <path d="m9 14 2 2 4-4" />
              </svg>
              Review proposal
              <ChevronGlyph className="tw-chev" />
            </button>
          )}
          <div className="toolwork-detail">
            {steps.map((s) => (
              <div key={s.id}>
                <div className="toolwork-step">
                  <StepGlyph name={s.name} />
                  <span>{s.label}</span>
                  {s.name === "search" && (
                    <span className="tw-count">
                      {s.sources.length} result{s.sources.length === 1 ? "" : "s"}
                    </span>
                  )}
                </div>
                {s.sources.length > 0 && (
                  <div className="toolwork-srcs">
                    {s.sources.map((src) => (
                      <SourceCard key={src.noteId} src={src} onOpen={onOpenNote} />
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
          <button type="button" className="fb-cue" onClick={() => setOpen(false)}>
            answer
            <ChevronGlyph className="fb-cue-ic" />
          </button>
        </div>
      </div>
    </div>
  );
}

function SourceCard({
  src,
  onOpen,
}: {
  src: SourceRef;
  onOpen?: ((noteId: string) => void) | undefined;
}): ReactNode {
  const dot = (
    <span className="tw-dot" style={{ background: DOMAIN_COLOR[src.domain] ?? "var(--text-3)" }} />
  );
  if (onOpen) {
    return (
      <button type="button" className="toolwork-card" onClick={() => onOpen(src.noteId)}>
        {dot}
        <span className="tw-text">{src.text}</span>
        <ChevronGlyph className="tw-chev" />
      </button>
    );
  }
  return (
    <div className="toolwork-card">
      {dot}
      <span className="tw-text">{src.text}</span>
    </div>
  );
}

function GearGlyph(): ReactNode {
  return (
    <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1" />
    </svg>
  );
}

function ChevronGlyph({ className }: { className: string }): ReactNode {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      <path d="m9 6 6 6-6 6" />
    </svg>
  );
}

function StepGlyph({ name }: { name: string }): ReactNode {
  if (name === "search") {
    return (
      <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="11" cy="11" r="7" />
        <path d="m20 20-3.5-3.5" />
      </svg>
    );
  }
  if (name === "read_note" || name === "read_entity") {
    return (
      <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
        <path d="M14 3v5h5M9 13h6M9 17h6" />
      </svg>
    );
  }
  return <span className="tw-ic tw-bullet" aria-hidden="true" />;
}
