// The Full Brain surface, rendered inline in the home page body: the streamed
// transcript with the two lateral panels the mock specifies — Sessions slides in
// from the left, Proposals from the right (docs/mocks/assistant-lateral-swipe.html).
// The horizontal swipe that shuttles those panels lives on the omnibox (the
// composer the home screen provides), so a drag across the transcript never
// hijacks reading or text selection; the top-bar buttons open the panels by tap.
// The composer is the omnibox, not here — this surface only reads `fb` and
// renders. An answer that used tools carries an inline "Worked" disclosure (tap
// to expand in place); each step is itself a pulldown showing its arguments,
// result, and raw payload (docs/research/brain-tooluse-ux).

import { type ReactNode, useEffect, useRef, useState } from "react";
import { DOMAIN_COLOR } from "../notes/modes";
import { ProposalTree } from "./ProposalTree";
import { ProposalsPanel } from "./ProposalsPanel";
import { SessionsPanel } from "./SessionsPanel";
import { Markdown, type MdFlag, stripModelCitations } from "./markdown";
import { type AgentStatus, agentStatus } from "./status";
import { type SourceRef, type ToolStep, toolStep } from "./toolSummary";
import type { ToolActivity, TranscriptMessage } from "./transcript";
import type { ProposalRef } from "./types";
import type { FullBrain } from "./useFullBrain";
import { usePacedText } from "./usePacedText";
import { ToolView } from "./views/registry";

// A tool call can finish in a blink; pin its label for at least this long so the
// "what it's doing" status is actually readable. A new tool inside the window
// swaps the label and re-arms the hold (see AgentStatusLine).
const TOOL_HOLD_MS = 1000;

interface Props {
  fb: FullBrain;
  /** Open a source note by id (from a Worked-block card). */
  onOpenNote?: ((noteId: string) => void) | undefined;
  /** Open an entity page by id (from a response entity chip). */
  onOpenEntity?: ((entityId: string) => void) | undefined;
  /** Fired after a Proposal enacts — the home stream refreshes so a note the
   * enactment created shows without waiting for the poll. */
  onProposalEnacted?: (() => void) | undefined;
}

export function FullBrainSurface({
  fb,
  onOpenNote,
  onOpenEntity,
  onProposalEnacted,
}: Props): ReactNode {
  const chatRef = useRef<HTMLElement>(null);
  const { panel, setPanel } = fb;

  // Keep the newest turn in view as text streams and tools land — each event
  // hands us a fresh `messages` array, so this re-runs through the whole stream.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run per transcript change; the effect reads the DOM.
  useEffect(() => {
    const el = chatRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [fb.messages]);

  // The session's name lives in the top bar (HomeScreen owns it); the panels are
  // a swipe away on the omnibox — right for Sessions, left for Proposals.
  return (
    <div className="fb-shell">
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
          agentOptions={fb.agentOptions}
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
          <ProposalTree
            proposalId={fb.openProposal}
            onClose={() => fb.setOpenProposal(null)}
            onEnacted={onProposalEnacted}
          />
        )}
      </aside>
    </div>
  );
}

// The B-direction status line (docs/mocks/assistant-ai-status-*.html): a quiet
// pulsing dot and a label that shimmers steel while the agent is live, then
// settles; a clean finish auto-hides after a beat, errors stay put. A tool's
// label is held for TOOL_HOLD_MS so a fast call doesn't flash past unread.
export function AgentStatusLine({ status }: { status: AgentStatus | null }): ReactNode {
  // What's actually on screen. It tracks `status` except that a "tool" label is
  // pinned: when the tool finishes inside the window we keep showing it, falling
  // through to the live status only once the hold elapses. A new tool inside the
  // window swaps the label and re-arms the hold.
  const [shown, setShown] = useState<AgentStatus | null>(status);
  const holdUntil = useRef(0);
  const heldTool = useRef<string | null>(null);

  useEffect(() => {
    const now = Date.now();
    if (status?.kind === "tool") {
      // Identify the tool by its rendered label; a different one re-arms the hold.
      const key = `${status.label}|${status.emphasis ?? ""}`;
      if (key !== heldTool.current) {
        heldTool.current = key;
        holdUntil.current = now + TOOL_HOLD_MS;
        setShown(status);
      }
      return;
    }
    // Not a tool: if the last tool is still inside its window, keep it up and
    // switch to the current status only once the window closes.
    if (heldTool.current !== null && now < holdUntil.current) {
      const t = setTimeout(() => {
        heldTool.current = null;
        setShown(status);
      }, holdUntil.current - now);
      return () => clearTimeout(t);
    }
    heldTool.current = null;
    setShown(status);
  }, [status]);

  const [doneHidden, setDoneHidden] = useState(false);
  // Reset on any kind change; a clean finish hides itself after a beat. Keying
  // on `kind` keeps the timer from re-arming when it fires (kind is unchanged).
  const kind = shown?.kind;
  useEffect(() => {
    setDoneHidden(false);
    if (kind !== "done") return;
    const t = setTimeout(() => setDoneHidden(true), 2600);
    return () => clearTimeout(t);
  }, [kind]);

  if (!shown || (shown.kind === "done" && doneHidden)) return null;
  const live = shown.kind === "thinking" || shown.kind === "tool" || shown.kind === "answering";
  const cls = live ? "live" : shown.kind === "error" ? "err" : "done";

  return (
    <output className={`fb-status ${cls}`}>
      <span className="fb-status-mark" aria-hidden="true" />
      <span className="fb-status-lab">
        {shown.label}
        {shown.emphasis ? <span className="tool"> {shown.emphasis}</span> : null}
        {live ? "…" : ""}
      </span>
    </output>
  );
}

// The reason shown when a ⚠ flag is tapped — short and plain, the owner's words
// not the verifier's. Reflexion's grounding check means exactly this.
const FLAG_REASON = "Not in your notes — I couldn't ground this in a source.";

// Build the inline flags for a turn from its reflexion verdict: one amber ⚠ per
// ungrounded answer sentence, each carrying its reason. A passing or absent
// verdict yields none, so the bubble is byte-for-byte unchanged (Option 1 is
// purely additive). The id is the claim's index so it's stable across renders.
function mdFlags(message: TranscriptMessage): MdFlag[] {
  const v = message.verdict;
  if (!v || v.passed) return [];
  return v.ungroundedClaims.map((claim, i) => ({ id: `ug-${i}`, claim, reason: FLAG_REASON }));
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
  // Which ungrounded-claim flag's reason note is open (one at a time). Declared
  // before the early returns so the hook order is stable across renders.
  const [openFlag, setOpenFlag] = useState<string | null>(null);
  // Pace the *displayed* prose: a steady typewriter reveal while the turn streams,
  // snapping to the full text once it settles. Only the Markdown text is paced —
  // sources, entities, and flags below still read the full `message.text`, so they
  // resolve correctly the moment the turn finishes.
  const shownText = usePacedText(message.text, message.streaming);
  if (message.role === "user") {
    return <div className="bubble me">{message.text}</div>;
  }
  // While the turn is still streaming, hold the whole bubble until the answer
  // text begins — tool calls alone shouldn't pop an empty Worked block ahead of
  // any prose. EXCEPT a reasoning model: show the bubble as soon as thinking
  // streams, so the "Thinking…" disclosure is live. The status line above the
  // omnibox still carries "what it's doing" until the typed answer lands.
  if (message.streaming && !message.text && !message.reasoning) {
    return null;
  }
  // A settled turn with nothing to show (no text, tools, views, or reasoning)
  // renders nothing — the status line above the composer carries residual state.
  if (
    !message.text &&
    message.tools.length === 0 &&
    message.views.length === 0 &&
    !message.reasoning
  ) {
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

  // Reflexion flagged this turn (Loop 1): map each ungrounded answer sentence to an
  // amber ⚠ flag anchored after it, tappable for the reason. A passing/absent
  // verdict makes no flags, so the bubble renders exactly as before.
  const flags = mdFlags(message);

  // The answer side: the prose, any tool-result views, and the proposal affordance.
  const answer = (
    <>
      {message.text && (
        <Markdown
          text={shownText}
          onCite={onCite}
          entities={entities}
          onEntity={onOpenEntity}
          flags={flags}
          onFlag={(id) => setOpenFlag((cur) => (cur === id ? null : id))}
          openFlag={openFlag}
          streaming={message.streaming}
        />
      )}
      {message.views.map((v, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
        <ToolView key={i} payload={v} />
      ))}
      {staged && <ProposalChip proposal={staged} onOpen={onOpenProposal} />}
    </>
  );

  // A turn answered from the model's own knowledge with no retrieval carries a calm
  // neutral provenance chip. The backend guarantees this never co-occurs with an
  // amber flag (zero-retrieval ⇒ this; retrieval ⇒ maybe a verdict), so guard on the
  // verdict too and the bubble renders at most one of the two.
  const generalKnowledge = message.generalKnowledge === true && flags.length === 0;

  // The answer leads the bubble; the model's reasoning trace and tool steps share a
  // single disclosure line at the foot ("Thinking · Worked"), each expanding in place
  // (docs/research/brain-tooluse-ux/A-disclosure-patterns.md). While thinking — before
  // any answer — the bubble is just that line with the trace open and auto-following.
  // A settled answer also gets a copy affordance pinned to the right of that line, so
  // the foot strip shows on every finished turn even with no reasoning or tools.
  const settledAnswer = !message.streaming && message.text.trim() !== "";
  return (
    <div className="bubble ai">
      {answer}
      {generalKnowledge && <GeneralKnowledgeNote />}
      {(message.reasoning || message.tools.length > 0 || settledAnswer) && (
        <ActivityLine
          reasoning={message.reasoning}
          thinking={message.thinking}
          hasAnswer={message.text !== ""}
          tools={message.tools}
          copyText={settledAnswer ? stripModelCitations(message.text) : ""}
          onOpenNote={onOpenNote}
          onOpenEntity={onOpenEntity}
        />
      )}
    </div>
  );
}

// The one disclosure line at the foot of an assistant bubble: the model's reasoning
// trace ("Thinking") and its tool steps ("Worked") as two segments on a single row,
// each expanding its own body in place (the violet/steel registers from DESIGN.md).
// While the model is still thinking the trace auto-opens, a pulse marks it live, and
// the trace auto-follows the newest text; the moment the answer's first token lands it
// collapses to "Thought for Ns" (the duration measured here, so the reducer stays
// pure) and stays a tap away. The "Worked" segment appears as soon as a tool runs —
// on the same line — so a turn that thinks AND uses tools reads as one foot strip.
function ActivityLine({
  reasoning,
  thinking,
  hasAnswer,
  tools,
  copyText,
  onOpenNote,
  onOpenEntity,
}: {
  reasoning: string;
  thinking: boolean;
  hasAnswer: boolean;
  tools: ToolActivity[];
  /** The settled answer text to copy; "" while streaming or empty (no copy button). */
  copyText: string;
  onOpenNote?: ((noteId: string) => void) | undefined;
  onOpenEntity?: ((entityId: string) => void) | undefined;
}): ReactNode {
  // The trace and the steps are one disclosure with two segments: at most one body
  // is open, and tapping a segment switches the view to it (tapping the open one
  // closes it). A live thinking phase opens the trace; the answer's arrival collapses
  // it unless the owner has since switched to "Worked".
  const [open, setOpen] = useState<"think" | "work" | null>(thinking ? "think" : null);
  const startRef = useRef<number | null>(null);
  const [ms, setMs] = useState<number | null>(null);
  const traceRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (thinking) {
      if (startRef.current === null) startRef.current = performance.now();
      setOpen("think");
    } else {
      // The thinking phase ended — record its duration once, and collapse the trace
      // (but leave a Worked view the owner opened mid-stream in place).
      if (startRef.current !== null && ms === null) setMs(performance.now() - startRef.current);
      setOpen((cur) => (cur === "think" ? null : cur));
    }
  }, [thinking, ms]);

  // Follow the newest reasoning while it streams, so a long trace stays readable
  // without the owner chasing the scrollbar (only while live and open). `reasoning`
  // is the intentional trigger — each new slice re-runs the scroll-to-bottom, even
  // though the body reads the ref's height, not the string itself.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reasoning drives the re-scroll
  useEffect(() => {
    if (thinking && open === "think" && traceRef.current) {
      traceRef.current.scrollTop = traceRef.current.scrollHeight;
    }
  }, [reasoning, thinking, open]);

  const hasReasoning = reasoning !== "";
  const steps = tools.map(toolStep);
  const sourceCount = steps.reduce((n, s) => n + s.sources.length, 0);
  const failCount = steps.filter((s) => s.ok === false).length;
  const label = thinking
    ? "Thinking…"
    : ms !== null
      ? `Thought for ${Math.max(1, Math.round(ms / 1000))}s`
      : "Thought";
  // No top border (and flush to the top) while the line leads a still-thinking
  // bubble with no answer above it yet.
  const bare = thinking && !hasAnswer;

  return (
    // The line and its disclosure share one foot strip — a single flex child of the
    // bubble, so a closed body adds no gap and the line stays tight to the card foot.
    // The two segments are a segmented control over ONE panel: selecting a chip swaps
    // the panel's content (reasoning ⇄ steps), selecting the open chip closes it. With
    // a single body the open height and bottom spacing are identical for either view.
    <div className={`fb-act-foot${bare ? " bare" : ""}`}>
      <div className="fb-activity">
        {hasReasoning && (
          <button
            type="button"
            className={`fb-act-chip fb-act-think${open === "think" ? " on" : ""}`}
            aria-expanded={open === "think"}
            onClick={() => setOpen((v) => (v === "think" ? null : "think"))}
          >
            <BrainGlyph className="fb-act-ic" />
            <span className="fb-act-lab">
              {thinking && <span className="fb-act-pulse" aria-hidden="true" />}
              {label}
            </span>
          </button>
        )}
        {tools.length > 0 && (
          <button
            type="button"
            className={`fb-act-chip fb-act-work${open === "work" ? " on" : ""}`}
            aria-expanded={open === "work"}
            onClick={() => setOpen((v) => (v === "work" ? null : "work"))}
          >
            <GearGlyph />
            <span className="fb-act-lab">Worked</span>
            <span className="fb-act-count">
              {" · "}
              {steps.length} step{steps.length === 1 ? "" : "s"}
              {sourceCount > 0 && ` · ${sourceCount} source${sourceCount === 1 ? "" : "s"}`}
              {failCount > 0 && <span className="fb-worked-fail"> · {failCount} failed</span>}
            </span>
          </button>
        )}
        {copyText && <CopyButton text={copyText} />}
      </div>
      {(hasReasoning || tools.length > 0) && (
        <div className={`fb-act-body${open ? " open" : ""}`}>
          <div className="fb-act-inner">
            {hasReasoning && (
              <div className={`fb-act-view${open === "think" ? " show" : ""}`}>
                <div className="fb-thinking-trace" ref={traceRef}>
                  {reasoning}
                </div>
              </div>
            )}
            {tools.length > 0 && (
              <div className={`fb-act-view${open === "work" ? " show" : ""}`}>
                <div className="fb-steps">
                  {steps.map((s) => (
                    <StepRow
                      key={s.id}
                      step={s}
                      onOpenNote={onOpenNote}
                      onOpenEntity={onOpenEntity}
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// Copy the answer to the clipboard, pinned to the right of the activity line: a glyph
// plus a "Copy" label that briefly swaps to a green check + "Copied" then resets — the
// same confirmation pattern as the review trace.
function CopyButton({ text }: { text: string }): ReactNode {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => clearTimeout(timer.current ?? undefined), []);
  return (
    <button
      type="button"
      className={`fb-act-copy${copied ? " done" : ""}`}
      aria-label={copied ? "Copied" : "Copy response"}
      onClick={() => {
        void navigator.clipboard?.writeText(text);
        setCopied(true);
        clearTimeout(timer.current ?? undefined);
        timer.current = setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? <CheckGlyph className="fb-act-ic" /> : <CopyGlyph className="fb-act-ic" />}
      <span className="fb-act-copy-lab">{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}

// A check glyph for the copy button's brief confirmation state.
function CheckGlyph({ className }: { className?: string }): ReactNode {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <path d="M5 13l4 4L19 7" />
    </svg>
  );
}

// A clipboard glyph for the copy affordance (two offset rounded rectangles).
function CopyGlyph({ className }: { className?: string }): ReactNode {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      aria-hidden="true"
    >
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" />
    </svg>
  );
}

// A small "brain" glyph for the thinking disclosure (icons.tsx has none).
function BrainGlyph({ className }: { className?: string }): ReactNode {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      aria-hidden="true"
    >
      <path d="M9 3a4 4 0 0 0-3.9 5 3.5 3.5 0 0 0 .4 6.5V17a2 2 0 0 0 2 2h1" />
      <path d="M15 3a4 4 0 0 1 3.9 5 3.5 3.5 0 0 1-.4 6.5V17a2 2 0 0 1-2 2h-1" />
      <path d="M12 4v16" />
    </svg>
  );
}

// A calm, neutral footer note under an answer the agent gave from its own general
// knowledge (no retrieval). Deliberately NOT the amber "unverified" flag (DESIGN.md:
// warning=amber, info=steel) — a quiet ⓘ glyph + a muted one-liner, not an alarm.
function GeneralKnowledgeNote(): ReactNode {
  return (
    <p className="fb-genknow" role="note">
      <svg className="fb-genknow-ic" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 11v5" />
        <path d="M12 8h.01" />
      </svg>
      From general knowledge — not your notes
    </p>
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

// The one argument worth showing on a web-tool's collapsed row: the url it
// fetched, the query it searched. Other tools carry their detail in the
// expanded step (or a source card), so the row stays a clean label.
function inlineArg(step: ToolStep): string | undefined {
  const args = step.args;
  if (!args) return undefined;
  const key = step.name === "web_fetch" ? "url" : step.name === "web_search" ? "query" : undefined;
  if (!key) return undefined;
  const v = args[key];
  return typeof v === "string" && v.trim() ? v.trim() : undefined;
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
  // The web tools carry their target inline on the row — the fetched url, the
  // searched query — so the call reads at a glance without expanding it. It
  // truncates with an ellipsis rather than wrapping (no overflow on a phone).
  const inline = inlineArg(step);

  return (
    <div className={`fb-step${isErr ? " err" : ""}${open ? " open" : ""}`}>
      <button
        type="button"
        className={`fb-step-row${inline ? " has-arg" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <StepGlyph name={step.name} />
        <span className="fb-step-lab">{step.label}</span>
        {inline && (
          <span className="fb-step-arg" title={inline}>
            {inline}
          </span>
        )}
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
  if (name.includes("list")) {
    return (
      <svg className="tw-ic" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01" />
      </svg>
    );
  }
  return <span className="tw-ic tw-bullet" aria-hidden="true" />;
}
