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

import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, chatAttachmentUrl, faviconUrl } from "../api/client";
import { FileIcon, ImageIcon } from "../components/icons";
import { DOMAIN_COLOR } from "../notes/modes";
import { INLINE_KINDS, InlineProposal } from "./InlineProposal";
import { ProposalTree } from "./ProposalTree";
import { ProposalsPanel } from "./ProposalsPanel";
import { SessionsPanel } from "./SessionsPanel";
import { SubagentFan } from "./SubagentFan";
import { attachmentKind } from "./attachmentKind";
import { BrainGlyph } from "./glyphs";
import { type CiteTarget, Markdown, type MdFlag, stripModelCitations } from "./markdown";
import { type AgentStatus, agentStatus } from "./status";
import { type SourceRef, type ToolStep, toolStep } from "./toolSummary";
import type { ToolActivity, TranscriptMessage } from "./transcript";
import type { ChatAttachment, EntityRef, ProposalRef, WebSource } from "./types";
import type { FullBrain } from "./useFullBrain";
import { usePacedText } from "./usePacedText";
import { ToolView } from "./views/registry";

// A tool call can finish in a blink; pin its label for at least this long so the
// "what it's doing" status is actually readable. A new tool inside the window
// swaps the label and re-arms the hold (see AgentStatusLine).
const TOOL_HOLD_MS = 1000;

// A running tool shows how long it's been going ("5m 23s") — a slow step (a
// sub-agent fan, a web fetch) reads as working, not frozen. Compact: seconds
// alone under a minute, m+s under an hour, h+m beyond.
function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000);
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

// Read-aloud control state for one settled answer: whether it's speaking, whether
// auto-play is armed (the control's third state), and the tap / long-press handlers.
interface AudioControl {
  playing: boolean;
  autoPlay: boolean;
  onToggle: () => void;
  onToggleAuto: () => void;
}

// How long a press must hold before it counts as a long-press (arm/disarm auto-play).
const LONG_PRESS_MS = 500;

interface Props {
  fb: FullBrain;
  /** Open a source note by id (from a Worked-block card). */
  onOpenNote?: ((noteId: string) => void) | undefined;
  /** Open an entity page by id (from a response entity chip). */
  onOpenEntity?: ((entityId: string) => void) | undefined;
  /** Fired after a Proposal enacts — the home stream refreshes so a note the
   * enactment created shows without waiting for the poll. */
  onProposalEnacted?: (() => void) | undefined;
  /** Read-aloud (piper) is enabled: each settled answer gets a three-state play
   * control beside its copy button, and the copy button drops its label to save room.
   * The control also shows on a still-streaming turn while auto-play is speaking it, so
   * a long turn can be paused before it settles. `playing` is the key of the turn
   * speaking now (null = silent); `autoPlay` is the armed auto-play mode (the control's
   * third state). `onToggle` plays a turn by key (or pauses it if it's the one playing);
   * `onToggleAuto` (long-press) flips auto-play. Absent = no play control (read-aloud
   * off / unavailable). */
  readAloud?:
    | {
        playing: string | null;
        autoPlay: boolean;
        onToggle: (key: string, markdown: string) => void;
        onToggleAuto: () => void;
      }
    | undefined;
}

export function FullBrainSurface({
  fb,
  onOpenNote,
  onOpenEntity,
  onProposalEnacted,
  readAloud,
}: Props): ReactNode {
  const chatRef = useRef<HTMLElement>(null);
  const { panel, setPanel } = fb;

  // Follow the stream only while the reader is already at the foot — scrolling
  // up to read back stops the view being yanked down by every new token, and
  // returning to the bottom re-arms the follow. A few px of slack absorbs
  // sub-pixel rounding and the programmatic snap below (which lands ~0 here).
  const stickRef = useRef(true);
  function onChatScroll(): void {
    const el = chatRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 64;
  }

  // A fresh session snaps to its newest turn regardless of where the last one
  // was left — opening a conversation should not strand you mid-history.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the id is a trigger, not a read.
  useEffect(() => {
    stickRef.current = true;
  }, [fb.active?.id]);

  // Keep the newest turn in view as text streams and tools land — each event
  // hands us a fresh `messages` array, so this re-runs through the whole stream.
  // Layout effect (not useEffect) so the snap reads the just-committed height and
  // fires before paint — during a fast turn the new text never flashes below the
  // fold waiting for a frame to catch up.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-run per transcript change; the effect reads the DOM.
  useLayoutEffect(() => {
    const el = chatRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [fb.messages]);

  // The status line below the chat (AgentStatusLine) appears, swaps, and hides on
  // its own timers — a held tool label, the thinking→answering flip, the clean
  // finish that fades after a beat — none of which are `messages` changes. It
  // lives outside this scroll box, so each height change silently shrinks or grows
  // the viewport; without re-pinning, the newest turn slips behind it (the status
  // sits over the last bubble instead of nudging it up). Observe the box's size and
  // re-snap whenever the reader is already at the foot, so any out-of-band reflow
  // keeps the live turn in view. Re-armed per session: the scroll box only exists
  // while a chat is open, and a fresh open mounts a new element to observe.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the id re-attaches the observer when the chat element remounts.
  useEffect(() => {
    const el = chatRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (stickRef.current) el.scrollTop = el.scrollHeight;
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [fb.active?.id]);

  // The session's name lives in the top bar (HomeScreen owns it); the panels are
  // a swipe away on the omnibox — right for Sessions, left for Proposals.
  return (
    <div className="fb-shell">
      <div className="fullbrain">
        {fb.active ? (
          <main className="fb-chat" aria-label="Conversation" ref={chatRef} onScroll={onChatScroll}>
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
                onProposalEnacted={onProposalEnacted}
                onProposalOutcome={(outcome) => fb.send(outcome, { proposalOutcome: true })}
                onDeferredComplete={(msg) => {
                  void fb.send(msg, { deferredOutcome: true });
                }}
                chatBusy={fb.busy}
                onStop={fb.stop}
                onOpenSession={fb.requestOpen}
                // The positional key doubles as the read-aloud turn key (append-only,
                // so it stays put for the turn's lifetime).
                audio={
                  readAloud
                    ? {
                        playing: readAloud.playing === String(i),
                        autoPlay: readAloud.autoPlay,
                        onToggle: () => readAloud.onToggle(String(i), m.text),
                        onToggleAuto: readAloud.onToggleAuto,
                      }
                    : undefined
                }
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
        <AgentStatusLine status={agentStatus(fb.messages, fb.active?.id)} />
      </div>

      <aside
        className={`panel left${panel === "sessions" ? " open" : ""}`}
        aria-hidden={panel !== "sessions"}
      >
        <SessionsPanel
          sessions={fb.sessions}
          agentOptions={fb.agentOptions}
          activeId={fb.active?.id ?? null}
          activeTurn={fb.activeTurn}
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
            onEnacted={() => {
              // Refresh the dependent views (the stream) AND the staged-proposals
              // list, so an enacted/minted proposal stops showing as still-staged
              // (an intake-link mints to `enacted` and must drop from the panel).
              onProposalEnacted?.();
              fb.reloadProposals();
            }}
          />
        )}
      </aside>
    </div>
  );
}

// A compact attachment chip inside a user bubble (mock B): a type-tinted icon, the
// filename (ellipsized), and a size meta. Tapping it downloads the file. The accent
// class matches the composer's staged chips so the two read identically.
function AttachmentChip({ att }: { att: ChatAttachment }): ReactNode {
  const kind = attachmentKind(att.media_type);
  const Icon = kind === "img" ? ImageIcon : FileIcon;
  return (
    <a
      className={`att-chip att-${kind}`}
      href={chatAttachmentUrl(att.id)}
      target="_blank"
      rel="noreferrer"
      title={att.filename}
    >
      <Icon size={13} />
      <span className="att-name">{att.filename}</span>
      <span className="att-meta">{prettySize(att.size_bytes)}</span>
    </a>
  );
}

// A terse human size for a chip's meta (the mock's "·3p" page hint isn't on the
// wire; the byte size is the calm stand-in).
function prettySize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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
  // Two elapsed timers run off the live status: the current phase (thinking / a
  // specific tool / answering) and the whole turn. Each keeps the key it was last
  // anchored on (`timedPhase` / `timedTurn`) and the wall-clock it started
  // (`phaseStartedAt` / `turnStartedAt`); both re-anchor in render the moment their key
  // changes (below). `now` ticks once a second to advance the displayed counts.
  const timedPhase = useRef<string | null>(null);
  const phaseStartedAt = useRef(0);
  const timedTurn = useRef<string | null>(null);
  const turnStartedAt = useRef(0);
  const [now, setNow] = useState(() => Date.now());

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

  // Tick once a second while a live phase (thinking / a tool / answering) is on
  // screen so its elapsed count advances; idle otherwise (no timer while settled).
  const ticking =
    shown?.kind === "thinking" || shown?.kind === "tool" || shown?.kind === "answering";
  useEffect(() => {
    if (!ticking) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [ticking]);

  if (!shown || (shown.kind === "done" && doneHidden)) return null;
  const live = shown.kind === "thinking" || shown.kind === "tool" || shown.kind === "answering";
  const cls = live ? "live" : shown.kind === "error" ? "err" : "done";
  // Anchor (or re-anchor) the turn timer whenever the turn changes — the moment a new
  // agent turn begins, its total restarts from zero. `turnKey` is steady across the
  // turn's phases so the total keeps climbing through them.
  const turnKey = shown.turnKey;
  if (turnKey !== undefined && turnKey !== timedTurn.current) {
    timedTurn.current = turnKey;
    // Read the clock fresh, not the (possibly stale) `now` state: after an idle gap the
    // ticker is stopped, so anchoring to `now` would make the fresh turn inherit the old
    // time and jump on the next tick. The elapsed below clamps to 0 until `now` catches up.
    turnStartedAt.current = Date.now();
  }
  // Anchor (or re-anchor) the phase timer to the live phase on screen — thinking, a
  // specific tool (keyed by its label so a new tool restarts it), or answering. The
  // turn is folded into the key so the same phase in a fresh turn still restarts.
  // Done in render so the very first frame already reads "0s"; an effect-set anchor
  // wouldn't force the extra re-render when the clock hasn't moved.
  const phaseKey = !live
    ? null
    : shown.kind === "tool"
      ? `${turnKey}|tool|${shown.label}|${shown.emphasis ?? ""}`
      : `${turnKey}|${shown.kind}`;
  if (phaseKey !== null && phaseKey !== timedPhase.current) {
    timedPhase.current = phaseKey;
    phaseStartedAt.current = Date.now();
  }
  const phaseElapsed = live ? Math.max(0, now - phaseStartedAt.current) : null;
  const turnElapsed =
    live && turnKey !== undefined ? Math.max(0, now - turnStartedAt.current) : null;
  // The parenthesised phase time is only meaningful for a tool call — how long *this*
  // step has run against the turn total. Thinking (and answering) is the turn itself,
  // so it shows a single number: the total. Show the tool's own time next to the total
  // only once the turn has outrun the current phase — before that they'd read
  // identically. Sub-second slack absorbs the one-tick lag between the two anchors.
  const showTurn =
    shown.kind === "tool" &&
    turnElapsed !== null &&
    phaseElapsed !== null &&
    turnElapsed - phaseElapsed >= 1000;

  return (
    <output className={`fb-status ${cls}`}>
      <span className="fb-status-mark" aria-hidden="true" />
      <span className="fb-status-lab">
        {shown.label}
        {shown.emphasis ? <span className="tool"> {shown.emphasis}</span> : null}
        {live ? "…" : ""}
      </span>
      {phaseElapsed !== null ? (
        // aria-hidden: a per-second announcement would spam the status region.
        <span className="fb-status-time" aria-hidden="true">
          {shown.kind === "tool"
            ? showTurn && turnElapsed !== null
              ? `(${formatElapsed(phaseElapsed)}) ${formatElapsed(turnElapsed)}`
              : formatElapsed(phaseElapsed)
            : // Thinking / answering: just the running total (falls back to the phase
              // time when there's no turn identity, e.g. a hand-built status in tests).
              formatElapsed(turnElapsed ?? phaseElapsed)}
        </span>
      ) : null}
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
  onProposalEnacted,
  onProposalOutcome,
  chatBusy,
  onOpenEntity,
  onStop,
  onOpenSession,
  onDeferredComplete,
  audio,
}: {
  message: TranscriptMessage;
  onOpenNote?: ((noteId: string) => void) | undefined;
  onOpenProposal?: ((proposalId: string) => void) | undefined;
  /** Refresh the home stream after an inline enact wrote a note. */
  onProposalEnacted?: (() => void) | undefined;
  /** Send an inline enact's server-authored outcome back to the assistant; resolves
   * TRUE when the follow-up turn actually started, FALSE when it was dropped. */
  onProposalOutcome?: ((outcome: string) => Promise<boolean>) | undefined;
  /** A turn is streaming — the inline card disables Enact so its outcome isn't dropped. */
  chatBusy?: boolean | undefined;
  onOpenEntity?: ((entityId: string) => void) | undefined;
  /** Cascade-cancel the live turn (and its sub-agent fan) — the fan header Stop. */
  onStop?: (() => void) | undefined;
  /** Open a sub-agent child's own session by id (from the fan's row). */
  onOpenSession?: ((sessionId: string) => void) | undefined;
  /** A deferred tool call's task_status card finished — send the auto-resume turn. */
  onDeferredComplete?: ((resumeMessage: string) => void) | undefined;
  /** Read-aloud control for this settled answer: `playing` = it's speaking now,
   * `autoPlay` = auto-play armed (third icon state); `onToggle` plays/pauses it,
   * `onToggleAuto` (long-press) flips auto-play. Absent = read-aloud off (no control,
   * labelled copy). */
  audio?: AudioControl | undefined;
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
    const attachments = message.attachments ?? [];
    return (
      <div className="bubble me">
        {attachments.length > 0 && (
          <div className="att-chips">
            {attachments.map((att) => (
              <AttachmentChip key={att.id} att={att} />
            ))}
          </div>
        )}
        {message.text}
      </div>
    );
  }
  // In-flight image generations with a live preview — they keep the bubble visible
  // (below) and render the sharpening preview + Stop ahead of any answer text.
  // Any in-flight image render shows the live surface — from the moment the tool is
  // called (a "preparing" placeholder during model load) through sampling and the
  // final decode, until its result lands.
  const livePreviews = message.tools.filter(
    (t) => IMAGE_TOOL_NAMES.has(t.name) && t.ok === undefined,
  );
  // In-flight non-image tools that stream a phase label (analyze_video): a live status
  // line shows the phase ("Extracting frames…", "Analyzing frame 12/30") until the
  // result lands and the tool's final view replaces it.
  const liveStatuses = message.tools.filter(
    (t) => !IMAGE_TOOL_NAMES.has(t.name) && t.ok === undefined && t.progress?.label,
  );
  // A live sub-agent fan attaches to its spawn_subagent call; the bubble must stay up
  // to host it (computed here so the streaming guard below can honour it too).
  const hasLiveFan = message.tools.some((t) => t.fan);
  // The rich live accordion shows only WHILE the turn streams. On settle it gives way
  // to the persisted `subagent_synthesis` roster card (the same surface a reopen shows),
  // so live-finished and reloaded look identical — one consistent research surface.
  const liveFanActive = hasLiveFan && message.streaming;
  // While the turn is still streaming, hold the whole bubble until the answer
  // text begins — tool calls alone shouldn't pop an empty Worked block ahead of
  // any prose. EXCEPT a reasoning model (show the live "Thinking…" disclosure), a
  // running image render (show its live preview), or a live sub-agent fan (its
  // accordion IS the surface — without this exception a non-reasoning model, which
  // streams neither answer nor reasoning during the spawn, would show nothing for the
  // whole fan run). The status line above the omnibox still carries "what it's doing".
  if (
    message.streaming &&
    !message.text &&
    !message.reasoning &&
    livePreviews.length === 0 &&
    liveStatuses.length === 0 &&
    !hasLiveFan
  ) {
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
  // `[^n]` in the answer maps to the n-th source the turn surfaced, flattened across
  // this turn's tools in order. A note source taps open its card; an entity taps open
  // the entity (a graph answer — read_entity/find_entity/relate — is citable too, not
  // just notes); a web source (jerv) renders as a favicon that opens the page (handled
  // in Markdown). Within a tool, notes precede web precede entities, so a note-only
  // turn keeps its original numbering.
  const citeTargets: CiteTarget[] = message.tools.flatMap((t) => [
    ...(t.sources ?? []).map((s): CiteTarget => ({ kind: "note", noteId: s.noteId })),
    ...(t.webSources ?? []).map((w): CiteTarget => ({ kind: "web", url: w.url, title: w.title })),
    ...(t.entities ?? []).map((e): CiteTarget => ({ kind: "entity", entityId: e.entity_id })),
  ]);
  const onCite =
    onOpenNote || onOpenEntity
      ? (n: number) => {
          const target = citeTargets[n - 1];
          if (target?.kind === "note") onOpenNote?.(target.noteId);
          else if (target?.kind === "entity") onOpenEntity?.(target.entityId);
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
  // Worked drop-down) so reviewing it is a single tap on the response. Inline-able kinds
  // render the interactive card (approve/decline/correct + one Enact that returns its
  // outcome to the assistant); the rest keep the navigational chip to the panel.
  const staged = message.tools.find((t) => t.proposal)?.proposal;
  const stagedAffordance = staged ? (
    INLINE_KINDS.has(staged.kind) ? (
      <InlineProposal
        proposalId={staged.proposal_id}
        onOutcome={(outcome) => onProposalOutcome?.(outcome) ?? Promise.resolve(false)}
        onEnacted={onProposalEnacted}
        chatBusy={chatBusy}
      />
    ) : (
      <ProposalChip proposal={staged} onOpen={onOpenProposal} />
    )
  ) : null;

  // Reflexion flagged this turn (Loop 1): map each ungrounded answer sentence to an
  // amber ⚠ flag anchored after it, tappable for the reason. A passing/absent
  // verdict makes no flags, so the bubble renders exactly as before.
  const flags = mdFlags(message);

  // Carry each image tool's last live preview to its generated_image view (1:1, in
  // call order) so the view holds it as a placeholder until the full-res image loads —
  // no blank gap on settle. Live-only: a reopened transcript has no preview to carry.
  const imagePreviews = message.tools
    .filter((t) => IMAGE_TOOL_NAMES.has(t.name))
    .map((t) => t.preview);
  let nextImagePreview = 0;
  // The live `subagent_*` fan is the surface WHILE the turn streams; the persisted
  // `subagent_synthesis` view takes over on settle (and on reopen). Suppress the view
  // only while the live fan is actually showing, so the two never stack — then let the
  // view through once the fan stands down.
  const viewsToRender = message.views
    .filter((v) => !(liveFanActive && v.view === "subagent_synthesis"))
    .map((v) => {
      if (v.view !== "generated_image") return v;
      const preview = imagePreviews[nextImagePreview++];
      return preview ? { ...v, data: { ...v.data, placeholder_data_uri: preview } } : v;
    });

  // The live sub-agent fan blocks (one per spawn step), shown only while streaming —
  // computed once so BOTH the normal and the image-split render paths show them (an
  // image+research turn used to drop the fan when it took the image-split early return).
  const fanBlocks = (liveFanActive ? message.tools.filter((t) => t.fan) : []).map(
    (t) =>
      t.fan && (
        <SubagentFan
          key={t.id}
          fan={t.fan}
          running={message.streaming}
          onStop={onStop}
          onOpen={onOpenSession}
        />
      ),
  );

  // The answer side: the prose, any tool-result views, and the proposal affordance.
  const answer = (
    <>
      {message.text && (
        <Markdown
          text={shownText}
          onCite={onCite}
          cites={citeTargets}
          entities={entities}
          onEntity={onOpenEntity}
          flags={flags}
          onFlag={(id) => setOpenFlag((cur) => (cur === id ? null : id))}
          openFlag={openFlag}
          streaming={message.streaming}
        />
      )}
      {livePreviews.map((t) => (
        <GeneratingPreview key={t.id} tool={t} />
      ))}
      {liveStatuses.map((t) =>
        t.name === "deep_research" ? (
          <DeepResearchProgress key={t.id} tool={t} />
        ) : (
          <LiveToolStatus key={t.id} tool={t} />
        ),
      )}
      {viewsToRender.map((v, i) => (
        <ToolView
          // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
          key={i}
          payload={v}
          onOpenSession={onOpenSession}
          onDeferredComplete={onDeferredComplete}
        />
      ))}
      {stagedAffordance}
    </>
  );

  // A turn answered from the model's own knowledge with no retrieval carries a calm
  // neutral provenance chip. The backend guarantees this never co-occurs with an
  // amber flag (zero-retrieval ⇒ this; retrieval ⇒ maybe a verdict), so guard on the
  // verdict too and the bubble renders at most one of the two.
  const generalKnowledge = message.generalKnowledge === true && flags.length === 0;

  // A settled spawn turn whose fan was interrupted before it produced a roster (a Stop,
  // a dropped connection, a timeout: the spawn step persisted failed with no
  // subagent_synthesis view). Without a note the bubble is just a foot strip — no answer,
  // no roster — reading as blank. Show a calm "interrupted" line so the turn is coherent
  // on reopen; the sub-agents that were minted are reachable from the chats panel.
  const interruptedSpawn =
    !message.streaming &&
    message.tools.some((t) => t.name === "spawn_subagent" && t.ok === false) &&
    !message.views.some((v) => v.view === "subagent_synthesis");

  // The answer leads the bubble; the model's reasoning trace and tool steps share a
  // single disclosure line at the foot ("Thinking · Worked"), each expanding in place
  // (docs/archive/research/brain-tooluse-ux/A-disclosure-patterns.md). While thinking — before
  // any answer — the bubble is just that line with the trace open and auto-following.
  // A settled answer also gets a copy affordance pinned to the right of that line, so
  // the foot strip shows on every finished turn even with no reasoning or tools.
  const settledAnswer = !message.streaming && message.text.trim() !== "";
  // Read-aloud engaged with a turn that is still streaming: auto-play armed, or this
  // turn already speaking (auto-play feeds it sentence-by-sentence before it settles).
  // Surface the play control now — not just on settle — so a long turn can be paused
  // mid-stream. A quiet streaming turn (auto-play off, silent) keeps its clean foot.
  const streamingAudio =
    message.streaming &&
    message.text.trim() !== "" &&
    audio !== undefined &&
    (audio.autoPlay || audio.playing);
  const activityLine =
    message.reasoning || message.tools.length > 0 || settledAnswer || streamingAudio ? (
      <ActivityLine
        reasoning={message.reasoning}
        thinking={message.thinking}
        hasAnswer={message.text !== ""}
        tools={message.tools}
        copyText={settledAnswer ? stripModelCitations(message.text) : ""}
        audio={settledAnswer || streamingAudio ? audio : undefined}
        onOpenNote={onOpenNote}
        onOpenEntity={onOpenEntity}
      />
    ) : null;

  // An image turn reads as THREE messages — preamble, image, reply — not one bubble,
  // so the picture stands as its own chat message. The split point is the prose length
  // when the image tool was called (recorded live + persisted, so reopen splits the
  // same). The paced reveal slices cleanly: the reply stays empty until the typewriter
  // passes the split, then fills in.
  const imageTool = message.tools.find((t) => IMAGE_TOOL_NAMES.has(t.name));
  const imageViews = viewsToRender.filter((v) => v.view === "generated_image");
  const splitAt = imageTool?.textOffset;
  if (splitAt !== undefined && (livePreviews.length > 0 || imageViews.length > 0)) {
    const preText = shownText.slice(0, splitAt);
    const postText = shownText.slice(splitAt);
    const otherViews = viewsToRender.filter((v) => v.view !== "generated_image");
    const hasReply =
      postText.trim() !== "" ||
      otherViews.length > 0 ||
      staged !== undefined ||
      activityLine !== null;
    return (
      <>
        {preText.trim() !== "" && (
          <div className="bubble ai">
            <Markdown
              text={preText}
              entities={entities}
              onEntity={onOpenEntity}
              streaming={message.streaming}
            />
          </div>
        )}
        {livePreviews.map((t) => (
          <div className="bubble ai bubble-media" key={`p-${t.id}`}>
            <GeneratingPreview tool={t} />
          </div>
        ))}
        {imageViews.map((v, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
          <div className="bubble ai bubble-media" key={`v-${i}`}>
            <ToolView
              payload={v}
              onOpenSession={onOpenSession}
              onDeferredComplete={onDeferredComplete}
            />
          </div>
        ))}
        {hasReply && (
          <div className="bubble ai">
            {postText.trim() !== "" && (
              <Markdown
                text={postText}
                onCite={onCite}
                cites={citeTargets}
                entities={entities}
                onEntity={onOpenEntity}
                streaming={message.streaming}
              />
            )}
            {otherViews.map((v, i) => (
              <ToolView
                // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
                key={i}
                payload={v}
                onOpenSession={onOpenSession}
                onDeferredComplete={onDeferredComplete}
              />
            ))}
            {stagedAffordance}
            {generalKnowledge && <GeneralKnowledgeNote />}
            {activityLine}
          </div>
        )}
        {fanBlocks}
      </>
    );
  }

  // A self-contained analysis card (video_analysis / task_status) is full-bleed and brings
  // its own frame, so — like an image turn — it stands as its own frameless media bubble,
  // flush in the conversation, and the turn's prose, proposal, and "Thinking / Worked" foot
  // ride SEPARATE normal bubbles around it. Keeping the foot in a real (inset) bubble is
  // what lets its thinking + tool-use disclosure render correctly; the old CSS-only flatten
  // stripped the shared bubble's chrome and orphaned the foot, colliding the strips.
  const analysisViews = viewsToRender.filter((v) => ANALYSIS_VIEWS.has(v.view));
  if (analysisViews.length > 0) {
    const otherViews = viewsToRender.filter((v) => !ANALYSIS_VIEWS.has(v.view));
    const hasReply =
      otherViews.length > 0 ||
      staged !== undefined ||
      interruptedSpawn ||
      generalKnowledge ||
      activityLine !== null;
    return (
      <>
        {message.text.trim() !== "" && (
          <div className="bubble ai">
            <Markdown
              text={shownText}
              onCite={onCite}
              cites={citeTargets}
              entities={entities}
              onEntity={onOpenEntity}
              flags={flags}
              onFlag={(id) => setOpenFlag((cur) => (cur === id ? null : id))}
              openFlag={openFlag}
              streaming={message.streaming}
            />
          </div>
        )}
        {analysisViews.map((v, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
          <div className="bubble ai bubble-media" key={`av-${i}`}>
            <ToolView
              payload={v}
              onOpenSession={onOpenSession}
              onDeferredComplete={onDeferredComplete}
            />
          </div>
        ))}
        {hasReply && (
          <div className="bubble ai">
            {otherViews.map((v, i) => (
              <ToolView
                // biome-ignore lint/suspicious/noArrayIndexKey: views append in order
                key={i}
                payload={v}
                onOpenSession={onOpenSession}
                onDeferredComplete={onDeferredComplete}
              />
            ))}
            {stagedAffordance}
            {interruptedSpawn && <InterruptedSubagentsNote />}
            {generalKnowledge && <GeneralKnowledgeNote />}
            {activityLine}
          </div>
        )}
        {fanBlocks}
      </>
    );
  }

  // The live sub-agent fan renders as its own bordered block below the answer bubble
  // (the accordion reads the parent turn's `subagent_*` events folded onto the spawn
  // call) ONLY while the turn streams (`fanBlocks`, computed above). On settle it stands
  // down and the persisted `subagent_synthesis` roster card (rendered with the answer's
  // views) takes its place — so a finished fan looks the same live as it does on reopen.
  return (
    <>
      <div className="bubble ai">
        {answer}
        {interruptedSpawn && <InterruptedSubagentsNote />}
        {generalKnowledge && <GeneralKnowledgeNote />}
        {activityLine}
      </div>
      {fanBlocks}
    </>
  );
}

// One item in the interleaved "Thinking" trace: a run of reasoning text, or a tool
// call dropped in at the point it happened (mirrors a sub-agent's ChildTrace).
type ThinkItem = { kind: "text"; text: string } | { kind: "tool"; step: ToolStep };

// Weave the flat reasoning string and the flat tools list back into one ordered trace
// using each tool's reasoning-offset (the reasoning length when it was called). Offsets
// only grow, so call order already IS trace order; a tool with no offset (an older
// persisted turn) lands at the end rather than being dropped. Pure, so it's testable
// and the render stays declarative.
function thinkingTrace(reasoning: string, tools: ToolActivity[]): ThinkItem[] {
  const placed = tools
    .map((t) => ({ t, off: Math.min(t.reasoningOffset ?? reasoning.length, reasoning.length) }))
    .sort((a, b) => a.off - b.off);
  const items: ThinkItem[] = [];
  let cursor = 0;
  for (const { t, off } of placed) {
    const at = Math.max(cursor, off);
    if (at > cursor) items.push({ kind: "text", text: reasoning.slice(cursor, at) });
    items.push({ kind: "tool", step: toolStep(t) });
    cursor = at;
  }
  if (cursor < reasoning.length) items.push({ kind: "text", text: reasoning.slice(cursor) });
  return items;
}

// A tool call shown inline inside the "Thinking" trace: a ✓/✕/· mark, the friendly
// step label, and (when it has one) the query/url/target it ran — the compact register a
// sub-agent's trace uses, so a heavy-tool-use turn reads as one flowing thought rather
// than a wall. The full args/sources/raw stay a tap away in the "Worked" segment.
function ThinkTool({ step }: { step: ToolStep }): ReactNode {
  const mark = step.ok === false ? "✕" : step.ok === undefined ? "·" : "✓";
  const cls = step.ok === false ? " bad" : step.ok === undefined ? " live" : "";
  const arg = inlineArg(step);
  return (
    <span className={`fb-think-tool${cls}`}>
      <span className="fb-think-mark" aria-hidden="true">
        {mark}
      </span>
      <span className="fb-think-name">{step.label}</span>
      {arg && (
        <span className="fb-think-arg" title={arg}>
          {arg}
        </span>
      )}
    </span>
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
  audio,
  onOpenNote,
  onOpenEntity,
}: {
  reasoning: string;
  thinking: boolean;
  hasAnswer: boolean;
  tools: ToolActivity[];
  /** The settled answer text to copy; "" while streaming or empty (no copy button). */
  copyText: string;
  /** Read-aloud control for this turn — present only when read-aloud (piper) is on.
   * Its presence also compacts the copy button to an icon to make room. */
  audio?: AudioControl | undefined;
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

  // Follow the newest line while it streams, so a long trace stays readable without
  // the owner chasing the scrollbar (only while live and open). `reasoning` and the
  // tool count are the intentional triggers — a new reasoning slice OR an interleaved
  // tool re-runs the scroll-to-bottom, even though the body reads the ref's height.
  // biome-ignore lint/correctness/useExhaustiveDependencies: reasoning + tool count drive the re-scroll
  useEffect(() => {
    if (thinking && open === "think" && traceRef.current) {
      traceRef.current.scrollTop = traceRef.current.scrollHeight;
    }
  }, [reasoning, tools.length, thinking, open]);

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
        {audio && <PlayButton audio={audio} />}
        {copyText && <CopyButton text={copyText} compact={audio !== undefined} />}
      </div>
      {(hasReasoning || tools.length > 0) && (
        <div className={`fb-act-body${open ? " open" : ""}`}>
          <div className="fb-act-inner">
            {hasReasoning && (
              <div className={`fb-act-view${open === "think" ? " show" : ""}`}>
                <div className="fb-thinking-trace" ref={traceRef}>
                  {thinkingTrace(reasoning, tools).map((it, i) =>
                    it.kind === "text" ? (
                      // biome-ignore lint/suspicious/noArrayIndexKey: trace items append in order
                      <span key={i}>{it.text}</span>
                    ) : (
                      // biome-ignore lint/suspicious/noArrayIndexKey: trace items append in order
                      <ThinkTool key={i} step={it.step} />
                    ),
                  )}
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
// same confirmation pattern as the review trace. With read-aloud on it shares the row
// with the play control, so `compact` drops the label to just the icon to save room.
function CopyButton({ text, compact }: { text: string; compact?: boolean }): ReactNode {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => clearTimeout(timer.current ?? undefined), []);
  return (
    <button
      type="button"
      className={`fb-act-copy${compact ? " compact" : ""}${copied ? " done" : ""}`}
      aria-label={copied ? "Copied" : "Copy response"}
      onClick={() => {
        void navigator.clipboard?.writeText(text);
        setCopied(true);
        clearTimeout(timer.current ?? undefined);
        timer.current = setTimeout(() => setCopied(false), 1500);
      }}
    >
      {copied ? <CheckGlyph className="fb-act-ic" /> : <CopyGlyph className="fb-act-ic" />}
      {!compact && <span className="fb-act-copy-lab">{copied ? "Copied" : "Copy"}</span>}
    </button>
  );
}

// Read-aloud control for an answer, sitting just left of the copy button (present only
// when read-aloud is enabled — on every settled answer, and on a streaming turn while
// auto-play is speaking it, so a long turn can be paused before it finalizes). Three
// states: play (tap to speak this turn), pause (it's speaking — tap to stop), and auto
// (auto-play armed, shown with a loop-marked triangle). A long-press on the control
// arms/disarms auto-play, so new turns speak themselves as they stream; a quick tap
// always plays/pauses this turn.
function PlayButton({ audio }: { audio: AudioControl }): ReactNode {
  const { playing, autoPlay, onToggle, onToggleAuto } = audio;
  // Long-press detection: a press held past LONG_PRESS_MS fires the auto-play toggle
  // and marks the gesture so the trailing click doesn't also play the turn.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const longFired = useRef(false);
  const cancel = () => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  };
  // Clear a pending long-press timer if the bubble unmounts mid-hold (refs only,
  // so the empty dep list is exhaustive).
  useEffect(() => () => clearTimeout(timer.current ?? undefined), []);
  const start = () => {
    longFired.current = false;
    cancel();
    timer.current = setTimeout(() => {
      longFired.current = true;
      onToggleAuto();
    }, LONG_PRESS_MS);
  };

  const label = playing
    ? "Pause reading aloud"
    : autoPlay
      ? "Auto-play on — long-press to turn off"
      : "Read response aloud — long-press for auto-play";
  const glyph = playing ? (
    <PauseGlyph className="fb-act-ic" />
  ) : autoPlay ? (
    <AutoPlayGlyph className="fb-act-ic" />
  ) : (
    <PlayGlyph className="fb-act-ic" />
  );
  return (
    <button
      type="button"
      className={`fb-act-play${playing ? " on" : ""}${autoPlay ? " auto" : ""}`}
      aria-label={label}
      aria-pressed={playing || autoPlay}
      onPointerDown={start}
      onPointerUp={cancel}
      onPointerLeave={cancel}
      onPointerCancel={cancel}
      onClick={() => {
        // A completed long-press already toggled auto-play — swallow the click.
        if (longFired.current) {
          longFired.current = false;
          return;
        }
        onToggle();
      }}
    >
      {glyph}
    </button>
  );
}

// A filled play triangle for the read-aloud affordance.
function PlayGlyph({ className }: { className?: string }): ReactNode {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      <path d="M7 5v14l12-7z" fill="currentColor" stroke="none" />
    </svg>
  );
}

// Two bars — the pause state the play glyph becomes while a turn is speaking.
function PauseGlyph({ className }: { className?: string }): ReactNode {
  return (
    <svg className={className} viewBox="0 0 24 24" aria-hidden="true">
      <rect x="6" y="5" width="4" height="14" rx="1" fill="currentColor" stroke="none" />
      <rect x="14" y="5" width="4" height="14" rx="1" fill="currentColor" stroke="none" />
    </svg>
  );
}

// The auto-play state: a small play triangle ringed by a repeat loop — "every turn
// speaks itself" — distinct from the bare play triangle.
function AutoPlayGlyph({ className }: { className?: string }): ReactNode {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M17 5a8 8 0 1 0 2.5 5" />
      <path d="M20 3v5h-5" />
      <path d="M10 9.5v5l4-2.5z" fill="currentColor" stroke="none" />
    </svg>
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

// A settled spawn turn whose sub-agent fan was cut before it produced a roster (a Stop,
// a dropped connection, a timeout). A calm steel ⓘ note — NOT the amber warning flag
// (DESIGN.md: warning=amber, info=steel) — so the turn reads as interrupted, not broken;
// the children that were minted are reachable from the chats panel's nested rail.
function InterruptedSubagentsNote(): ReactNode {
  return (
    <p className="fb-genknow" role="note">
      <svg className="fb-genknow-ic" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="9" />
        <path d="M12 11v5" />
        <path d="M12 8h.01" />
      </svg>
      Sub-agent run interrupted — any sub-agents that started are in the chats panel
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
// The two image-gen tools, by name — the only tools that drive a live preview
// surface (so an in-flight render shows the sharpening frame, not a Worked step).
const IMAGE_TOOL_NAMES = new Set(["generate_image", "edit_image"]);

// Self-framed, full-bleed tool-view cards that stand alone as their own media message
// (the same treatment a generated image gets): the card in a frameless bubble, and the
// turn's "Thinking / Worked" foot on a SEPARATE normal bubble below — so the disclosure
// keeps a real inset bubble to live in. video_analysis is the final analysis card;
// task_status is its deferred "analyzing…" placeholder that later swaps to it.
const ANALYSIS_VIEWS = new Set(["video_analysis", "task_status"]);

// A live status line for a multi-phase non-image tool (analyze_video): its streamed
// phase label, with a thin determinate bar while a counted phase (frame i/N) advances.
// Ephemeral — the tool's final view (the card) replaces it once the result lands.
function LiveToolStatus({ tool }: { tool: ToolActivity }): ReactNode {
  const p = tool.progress;
  const counted = p !== undefined && p.total > 0;
  const pct = counted && p ? Math.round((p.step / p.total) * 100) : 0;
  return (
    <output className="fb-toolstatus" aria-live="polite">
      <span className="fb-toolstatus-spin" aria-hidden="true" />
      <span className="fb-toolstatus-label">{p?.label ?? "Working…"}</span>
      {counted && (
        <span className="fb-toolstatus-bar" aria-hidden="true">
          <span className="fb-toolstatus-fill" style={{ width: `${pct}%` }} />
        </span>
      )}
    </output>
  );
}

// The deep_research pipeline stages, indexed by the backend `_phase` step ordinal (1-8,
// deep_research.py). A "dark" orchestration stage (Plan / Coverage / Write / Revise spawns
// no sub-agent row) used to show only a spinner; this checklist gives every stage a
// visible position + what's left, and Write/Revise ALSO stream the report itself
// (progress.preview) into a live pane, so the longest phases are watched, not blank.
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

export function DeepResearchProgress({ tool }: { tool: ToolActivity }): ReactNode {
  const p = tool.progress;
  const step = p?.step ?? 0; // 1-based; 0 before the first phase event lands
  const preview = p?.preview ?? "";
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
          // Everything before the active step reads done; the active pulses; the rest wait.
          const state = ord < step ? "done" : ord === step ? "active" : "todo";
          return (
            <li key={name} className={`fb-drp-step ${state}`}>
              <span className="fb-drp-dot" aria-hidden="true">
                {state === "done" ? "✓" : ""}
              </span>
              <span className="fb-drp-name">{name}</span>
            </li>
          );
        })}
      </ol>
      {/* The active phase's live detail (e.g. "Researching 4 angle(s)"). */}
      {p?.label && <div className="fb-drp-active">{p.label}</div>}
      {/* Write / Revise stream the report itself — render it live in a scrollable pane. */}
      {preview && (
        <div className="fb-drp-report" ref={paneRef} onScroll={onScroll}>
          <Markdown text={preview} />
        </div>
      )}
    </output>
  );
}

// aspect arg → CSS ratio, so the preview frame holds a stable size before the
// first preview frame arrives (matching the image-gen tool's three presets).
const PREVIEW_ASPECT: Record<string, string> = {
  square: "1 / 1",
  portrait: "3 / 4",
  landscape: "4 / 3",
};

// The live "image-as-progress" surface (docs/mocks/image-gen-live, Variant A): the
// preview fills the final image slot and sharpens (blur → 0) as the sampler
// advances, with a slim progress bar and a corner Stop. Replaced by the final
// generated_image view the moment the tool's result lands.
//
// It spans the whole render, not just sampling: before the first tick (the LLM is
// being unloaded and the diffusion model loaded) there's no `progress` yet, so it
// shows a "preparing…" placeholder; once the sampler reaches its last step the
// preview is done but the VAE decode hasn't returned, so it shows "finalizing…".
// Both bracket states drive an indeterminate bar; only mid-sampling shows a percent.
function GeneratingPreview({ tool }: { tool: ToolActivity }): ReactNode {
  const [stopping, setStopping] = useState(false);
  const p = tool.progress;
  const preview = p?.preview;
  const sampling = p !== undefined && p.total > 0 && p.step < p.total;
  const pct = sampling && p ? Math.round((p.step / p.total) * 100) : 0;
  // Sharpen as sampling advances; once finalizing, the held frame IS the final sample,
  // so show it crisp (blur 0) — at max blur it read as a much earlier step.
  const blur = sampling ? Math.max(0, 26 * (1 - pct / 100)) : 0;
  const aspect = PREVIEW_ASPECT[String(tool.args?.aspect ?? "square")] ?? "1 / 1";

  // Cross-fade successive frames instead of snapping: the new frame fades in over the
  // previous one (kept beneath) so the preview evolves smoothly step to step.
  const [frames, setFrames] = useState<{ prev: string | undefined; cur: string | undefined }>({
    prev: undefined,
    cur: undefined,
  });
  useEffect(() => {
    if (preview) setFrames((f) => (preview === f.cur ? f : { prev: f.cur, cur: preview }));
  }, [preview]);

  const label = stopping
    ? "stopping…"
    : p === undefined
      ? "preparing…"
      : sampling
        ? `step ${p.step} / ${p.total}`
        : "finalizing…";

  const stop = () => {
    setStopping(true);
    // Best-effort — a 409/502 just means the render finishes; the result lands either way.
    void api.interruptImageRender().catch(() => {});
  };

  return (
    <div className="fb-genprev">
      <div className="fb-genprev-frame" style={{ aspectRatio: aspect }}>
        {frames.cur ? (
          <div className="fb-genprev-stage" style={{ filter: `blur(${blur}px)` }}>
            {frames.prev && <img className="fb-genprev-img" src={frames.prev} alt="" />}
            <img
              className="fb-genprev-img fb-genprev-fade"
              key={frames.cur}
              src={frames.cur}
              alt=""
            />
          </div>
        ) : (
          <div className="fb-genprev-skeleton" />
        )}
        {!stopping && (
          <button type="button" className="fb-genprev-stop" onClick={stop}>
            <span className="fb-genprev-sq" aria-hidden="true" />
            Stop
          </button>
        )}
        <div className="fb-genprev-overlay">
          <span className="fb-genprev-step">{label}</span>
          {sampling && <span className="fb-genprev-pct">{pct}%</span>}
        </div>
        <div className="fb-genprev-bar">
          {sampling ? <i style={{ width: `${pct}%` }} /> : <i className="fb-genprev-indet" />}
        </div>
      </div>
    </div>
  );
}

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

// The one argument worth showing on a tool's collapsed row: the "what" a generic
// label ("Searched Gmail", "Searched the web") leaves implicit — the query it ran,
// the url it fetched, the name/place/subject it looked up. Keyed by the arg that
// carries that human-readable target; opaque ids (message_id, note_id, entity_id…)
// stay in the expanded step, so the row reads as a clean label + a legible target.
const INLINE_ARG_KEY: Record<string, string> = {
  search: "query",
  recall: "query",
  web_search: "query",
  web_fetch: "url",
  gmail_search: "query",
  gmail_count: "query",
  gmail_bulk_label: "query",
  gmail_sender_breakdown: "query",
  find_entity: "name",
  lookup_medication: "name",
  lookup_condition: "name",
  relate: "relationship",
  find_when_at: "place",
  time_at_place: "place",
  location_query: "place",
  where_is: "subject",
  weather: "location",
  hurricane: "location",
};

function inlineArg(step: ToolStep): string | undefined {
  const key = INLINE_ARG_KEY[step.name];
  if (!key || !step.args) return undefined;
  const v = step.args[key];
  return typeof v === "string" && v.trim() ? v.trim() : undefined;
}

// How many entity chips a step shows before the rest tuck behind a "+N more"
// toggle. A read_entity/neighborhood step surfaces every related entity as a chip
// (one per relationship edge), so a richly-connected entity turns the result into a
// wall; the cap keeps the step calm on a phone with every entity still one tap away.
const ENTITY_CHIP_CAP = 6;

// The entities a step resolved, as a tappable chip grid capped at ENTITY_CHIP_CAP —
// the overflow reveals in place on tap (the same disclosure register as "raw
// result"/"show all lines"), so the wall never leads but nothing is lost.
function EntityChips({
  entities,
  onOpenEntity,
}: {
  entities: EntityRef[];
  onOpenEntity?: ((entityId: string) => void) | undefined;
}): ReactNode {
  const [expanded, setExpanded] = useState(false);
  const overflowing = entities.length > ENTITY_CHIP_CAP;
  const shown = expanded || !overflowing ? entities : entities.slice(0, ENTITY_CHIP_CAP);
  return (
    <div className="toolwork-ents">
      {shown.map((e) => (
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
      {overflowing && (
        <button
          type="button"
          className="entity-chip entity-chip-more"
          aria-expanded={expanded}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "show less" : `+${entities.length - ENTITY_CHIP_CAP} more`}
        </button>
      )}
    </div>
  );
}

// One tool step, itself a pulldown: tap the row to reveal its arguments-in and
// result-out; a failed step opens by default with its error text. Search/read
// steps that surfaced source cards also offer a "raw result" rung for the
// verbatim backend text (docs/archive/research/brain-tooluse-ux/B-verbose-logging.md).
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
  const hasWebSources = step.webSources.length > 0;
  const hasArgs = step.args != null && Object.keys(step.args).length > 0;
  const summary = step.summary?.trim();
  // The verbatim raw payload is worth a rung only when a friendly result (source
  // cards, entity links, or web source cards) stands in for it; otherwise the text
  // already is the summary. Entity steps especially: the raw text carries bare ids
  // we'd rather not parade, so the links are the result and the ids hide behind "raw".
  const rawText = hasSources || hasEntities || hasWebSources ? summary : undefined;
  const mark = isErr ? "bad" : step.ok === undefined ? "live" : "";
  // Search/lookup tools carry their target inline on the row — the searched query,
  // the fetched url, the looked-up name — so the call reads at a glance without
  // expanding it. It truncates with an ellipsis rather than wrapping (no phone overflow).
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
        {step.name === "web_search" && hasWebSources && (
          <span className="fb-step-cnt">
            {step.webSources.length} result{step.webSources.length === 1 ? "" : "s"}
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
              <EntityChips entities={step.entities} onOpenEntity={onOpenEntity} />
              {rawText && <RawBlock text={rawText} />}
            </>
          ) : hasWebSources ? (
            <>
              <div className="fb-res-lab">sources</div>
              <div className="toolwork-srcs">
                {step.webSources.map((w) => (
                  <WebSourceCard key={w.url} src={w} />
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

// A web source in the expanded "Worked" step: a favicon + the page title, opening
// the page in a new tab. The favicon is served on-box (faviconUrl); on load failure
// it falls back to the host's initial so the card never shows a broken image.
function WebSourceCard({ src }: { src: WebSource }): ReactNode {
  const host = (() => {
    try {
      return new URL(src.url).hostname.replace(/^www\./, "");
    } catch {
      return "";
    }
  })();
  const [failed, setFailed] = useState(false);
  return (
    <a
      className="toolwork-card tw-web"
      href={src.url}
      target="_blank"
      rel="noreferrer noopener"
      title={src.url}
    >
      {host && !failed ? (
        <img
          className="tw-favicon"
          src={faviconUrl(host)}
          alt=""
          aria-hidden="true"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="tw-favicon tw-favicon-ph" aria-hidden="true">
          {host.slice(0, 1).toUpperCase() || "·"}
        </span>
      )}
      <span className="tw-text">
        <span className="tw-web-title">{src.title || host || src.url}</span>
        {host && <span className="tw-web-host">{host}</span>}
      </span>
      <ChevronGlyph className="tw-chev" />
    </a>
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
