// The Full Brain surface, rendered inline in the home page body: the streamed
// transcript with the two lateral panels the mock specifies — Sessions slides in
// from the left, Proposals from the right (docs/mocks/assistant-lateral-swipe.html).
// A horizontal swipe is the in-context shortcut (right→Sessions, left→Proposals,
// the opposite swipe sends the open panel back out); the header buttons do the
// same for anyone who'd rather tap. The composer is the omnibox, not here — this
// surface only reads `fb` and renders. An answer that used tools carries an inline
// "Worked" disclosure (tap to expand in place); each step is itself a pulldown
// showing its arguments, result, and raw payload (docs/research/brain-tooluse-ux).

import { type ReactNode, type TouchEvent, useEffect, useRef, useState } from "react";
import { DOMAIN_COLOR } from "../notes/modes";
import { ProposalTree } from "./ProposalTree";
import { ProposalsPanel } from "./ProposalsPanel";
import { SessionsPanel } from "./SessionsPanel";
import { Markdown } from "./markdown";
import { type AgentStatus, agentStatus } from "./status";
import { type SourceRef, type ToolStep, toolStep } from "./toolSummary";
import type { ToolActivity, TranscriptMessage } from "./transcript";
import type { ProposalRef } from "./types";
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
    // Text fields opt out so typing/selection isn't hijacked; taps on the Worked
    // disclosure and its step rows fall through (a tap never travels OPEN_PX), so
    // the horizontal swipe keeps its single meaning — the lateral panels.
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
          activeId={fb.active?.id ?? null}
          onOpen={fb.open}
          onCreate={fb.create}
          onClose={() => setPanel("none")}
          onRename={fb.rename}
          onDelete={fb.remove}
          onArchive={fb.archive}
          onUnarchive={fb.unarchive}
          onRescope={fb.rescope}
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
  // While the turn is still streaming, hold the whole bubble until the answer
  // text begins — tool calls alone shouldn't pop an empty Worked block ahead of
  // any prose. The status line above the omnibox carries "what it's doing" (the
  // live tool) until the typed answer lands; then the bubble appears with the
  // prose and its Worked disclosure together.
  if (message.streaming && !message.text) {
    return null;
  }
  // A settled turn with nothing to show (no text, no tools, no views) renders
  // nothing — the status line above the composer carries any residual state.
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
  // Entities the turn resolved, deduped. Those whose name appears in the answer
  // are linkified inline (Markdown). The rest aren't chipped under the prose —
  // they stay reachable as tappable links inside the Worked step that surfaced
  // them, so an oblique reference ("your wife") never spawns a loose pill.
  const entities = [
    ...new Map(
      message.tools.flatMap((t) => t.entities ?? []).map((e) => [e.entity_id, e]),
    ).values(),
  ];

  // A proposal the turn staged — surfaced in the answer itself (not buried in the
  // Worked drop-down) so reviewing it is a single tap on the response.
  const staged = message.tools.find((t) => t.proposal)?.proposal;

  // The answer side: the prose, any tool-result views, and the proposal affordance.
  const answer = (
    <>
      {message.text && (
        <Markdown text={message.text} onCite={onCite} entities={entities} onEntity={onOpenEntity} />
      )}
      {message.views.map((v, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
        <ToolView key={i} payload={v} />
      ))}
      {staged && <ProposalChip proposal={staged} onOpen={onOpenProposal} />}
    </>
  );

  // No tools → a plain bubble. With tools, the answer carries an inline "Worked"
  // disclosure that expands in place to the tool steps (docs/research/
  // brain-tooluse-ux/A-disclosure-patterns.md). Both share the one bubble.
  return (
    <div className="bubble ai">
      {answer}
      {message.tools.length > 0 && (
        <Worked tools={message.tools} onOpenNote={onOpenNote} onOpenEntity={onOpenEntity} />
      )}
    </div>
  );
}

// The "Worked" disclosure under an answer: a labelled 44px button (honest status,
// not a hidden gesture) that expands the tool steps in place. Each step is itself
// a pulldown (StepRow). No flip, no 3D, no swipe — taps only.
function Worked({
  tools,
  onOpenNote,
  onOpenEntity,
}: {
  tools: ToolActivity[];
  onOpenNote?: ((noteId: string) => void) | undefined;
  onOpenEntity?: ((entityId: string) => void) | undefined;
}): ReactNode {
  const [open, setOpen] = useState(false);
  const bodyId = useRef(`worked-${Math.random().toString(36).slice(2)}`).current;

  const steps = tools.map(toolStep);
  const sourceCount = steps.reduce((n, s) => n + s.sources.length, 0);
  const failCount = steps.filter((s) => s.ok === false).length;
  const parts: ReactNode[] = [`${steps.length} step${steps.length === 1 ? "" : "s"}`];
  if (sourceCount) parts.push(`${sourceCount} source${sourceCount === 1 ? "" : "s"}`);

  return (
    <div className={`fb-worked${open ? " open" : ""}`}>
      <button
        type="button"
        className="fb-worked-btn"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((o) => !o)}
      >
        <GearGlyph />
        <span className="fb-worked-lab">Worked</span>
        <span className="fb-worked-meta">
          {" · "}
          {parts.map((p, i) => (
            // biome-ignore lint/suspicious/noArrayIndexKey: fixed positional meta parts
            <span key={i}>
              {i > 0 ? " · " : ""}
              {p}
            </span>
          ))}
          {failCount > 0 && <span className="fb-worked-fail"> · {failCount} failed</span>}
        </span>
        <CaretGlyph className="fb-worked-caret" />
      </button>
      <div className="fb-worked-body" id={bodyId}>
        <div className="fb-worked-inner">
          <div className="fb-steps">
            {steps.map((s) => (
              <StepRow key={s.id} step={s} onOpenNote={onOpenNote} onOpenEntity={onOpenEntity} />
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

// The "Review proposal" affordance, shown in the answer itself so acting on a
// staged change is one tap on the response (not buried in the Worked drop-down).
// DEFERRED CONCEPT: this is a navigational chip — it opens the Proposals panel.
// The richer idea (an interactive inline component that shows the proposal's
// diff, takes approve/reject in place, reflects live state, AND notifies the
// agent of the outcome so it can follow up) is a separate, larger change that
// needs a backend feedback loop; it is intentionally not built here.
function ProposalChip({
  proposal,
  onOpen,
}: {
  proposal: ProposalRef;
  onOpen?: ((proposalId: string) => void) | undefined;
}): ReactNode {
  return (
    <button type="button" className="proposal-chip" onClick={() => onOpen?.(proposal.proposal_id)}>
      <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
        <rect x="9" y="3" width="6" height="4" rx="1" />
        <path d="m9 14 2 2 4-4" />
      </svg>
      Review proposal
      <ChevronGlyph className="tw-chev" />
    </button>
  );
}

// One tool step, itself a pulldown: tap the row to reveal its arguments-in and
// result-out; a failed step opens by default with its error text. Search/read
// steps that surfaced source cards also offer a "raw result" rung for the
// verbatim backend text (docs/research/brain-tooluse-ux/B-verbose-logging.md).
function StepRow({
  step,
  onOpenNote,
  onOpenEntity,
}: {
  step: ToolStep;
  onOpenNote?: ((noteId: string) => void) | undefined;
  onOpenEntity?: ((entityId: string) => void) | undefined;
}): ReactNode {
  const isErr = step.ok === false;
  const [open, setOpen] = useState(isErr);
  // A step that comes back failed opens itself so the error is visible without a
  // tap — including when it transitions mid-stream (it mounts in-flight).
  useEffect(() => {
    if (isErr) setOpen(true);
  }, [isErr]);
  const hasSources = step.sources.length > 0;
  const hasEntities = step.entities.length > 0;
  const hasArgs = step.args != null && Object.keys(step.args).length > 0;
  const summary = step.summary?.trim();
  // The verbatim raw payload is worth a rung only when a friendly result (source
  // cards or entity links) stands in for it; otherwise the text already is the
  // summary. Entity steps especially: the raw text carries bare ids we'd rather
  // not parade, so the links are the result and the ids hide behind "raw".
  const rawText = hasSources || hasEntities ? summary : undefined;
  const mark = isErr ? "bad" : step.ok === undefined ? "live" : "";

  return (
    <div className={`fb-step${isErr ? " err" : ""}${open ? " open" : ""}`}>
      <button
        type="button"
        className="fb-step-row"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <StepGlyph name={step.name} />
        <span className="fb-step-lab">{step.label}</span>
        <span className={`fb-step-mark ${mark}`} aria-hidden="true" />
        {step.name === "search" && (
          <span className="fb-step-cnt">
            {step.sources.length} result{step.sources.length === 1 ? "" : "s"}
          </span>
        )}
        <ChevronGlyph className="fb-step-caret" />
      </button>
      <div className="fb-step-detail">
        <div className="fb-step-di">
          {hasArgs && <ArgsList args={step.args as Record<string, unknown>} />}
          {isErr ? (
            <>
              <div className="fb-res-lab">error</div>
              <div className="fb-res-txt err">{summary || "the tool returned an error"}</div>
            </>
          ) : hasSources ? (
            <>
              <div className="fb-res-lab">result</div>
              <div className="toolwork-srcs">
                {step.sources.map((src) => (
                  <SourceCard key={src.noteId} src={src} onOpen={onOpenNote} />
                ))}
              </div>
              {rawText && <RawBlock text={rawText} />}
            </>
          ) : hasEntities ? (
            <>
              <div className="fb-res-lab">result</div>
              <div className="toolwork-ents">
                {step.entities.map((e) => (
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
              {rawText && <RawBlock text={rawText} />}
            </>
          ) : step.display ? (
            <>
              <div className="fb-res-lab">result</div>
              <div className="fb-res-txt">{step.display}</div>
              {step.display !== summary && summary && <RawBlock text={summary} />}
            </>
          ) : summary ? (
            <>
              <div className="fb-res-lab">result</div>
              <div className="fb-res-txt">{summary}</div>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// A step's arguments as a flat, one-level key/value list — values are monospace
// so an id or a date stays legible; deeper structure stringifies rather than
// recursing (kept calm for a phone).
function ArgsList({ args }: { args: Record<string, unknown> }): ReactNode {
  return (
    <dl className="fb-args">
      {Object.entries(args).map(([k, v]) => (
        <div key={k} className="fb-args-row">
          <dt>{k}</dt>
          <dd>{typeof v === "string" ? v : JSON.stringify(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

// The raw result rung: the verbatim backend text in a clamped monospace inset,
// with copy and a "show all lines" grow for a long payload.
function RawBlock({ text }: { text: string }): ReactNode {
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState(false);
  const [copied, setCopied] = useState(false);
  const clean = text.replace(/<\/?mark>/g, "");
  const overflowing = clean.split("\n").length > 6;

  function copy(): void {
    void navigator.clipboard?.writeText(clean).catch(() => {});
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  }

  return (
    <div className="fb-raw-wrap">
      <button
        type="button"
        className="fb-raw-toggle"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {open ? "hide raw" : "raw result"}
      </button>
      {open && (
        <div className="fb-raw">
          <pre className={`fb-raw-pre${full ? " full" : ""}`}>{clean}</pre>
          <button type="button" className="fb-raw-copy" aria-label="copy raw result" onClick={copy}>
            {copied ? (
              <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
                <path d="m5 13 4 4L19 7" />
              </svg>
            ) : (
              <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
                <rect x="9" y="9" width="11" height="11" rx="2" />
                <path d="M5 15V5a2 2 0 0 1 2-2h10" />
              </svg>
            )}
          </button>
          {overflowing && !full && (
            <button type="button" className="fb-raw-more" onClick={() => setFull(true)}>
              show all lines
            </button>
          )}
        </div>
      )}
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

// A down-caret for the Worked disclosure — rotates 180° when the block is open.
function CaretGlyph({ className }: { className: string }): ReactNode {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      <path d="m6 9 6 6 6-6" />
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
  if (name.includes("appointment")) {
    return (
      <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
        <rect x="3" y="4" width="18" height="17" rx="2" />
        <path d="M3 9h18M8 2v4M16 2v4" />
      </svg>
    );
  }
  if (name.includes("list")) {
    return (
      <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
      </svg>
    );
  }
  return <span className="tw-ic tw-bullet" aria-hidden="true" />;
}
