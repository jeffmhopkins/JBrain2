// The bottom-docked composer (docs/DESIGN.md "The omnibox home").
// Compact by default — two lines to type — and it grows with the text; the
// segmented row morphs between the main trio and the entry sub-types, and
// Medical/Financial expose an in-box destination row.

import {
  type CSSProperties,
  type ReactNode,
  type TouchEvent,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { attachmentKind } from "../agent/attachmentKind";
import type { AppointmentRef } from "../agent/types";
import type { ContextUsage } from "../agent/useFullBrain";
import { MODES, type Mode, ROWS, type SegState, tapSegment } from "../notes/modes";
import type { SendInput } from "../notes/useNotes";
import {
  BotIcon,
  ClipIcon,
  FileIcon,
  FinancialIcon,
  ImageIcon,
  MedicalIcon,
  PlusIcon,
  SearchIcon,
  SendIcon,
  StopIcon,
} from "./icons";

const MODE_ICON: Record<Mode, (p: { size?: number }) => ReactNode> = {
  entry: PlusIcon,
  research: SearchIcon,
  fullbrain: BotIcon,
  medical: MedicalIcon,
  financial: FinancialIcon,
};

const SWIPE_UP_PX = 48;
const PANEL_PX = 56; // horizontal travel that commits a Full Brain lateral panel

interface OmniboxProps {
  /** Mode state is lifted so the home stream can scope itself to the mode. */
  seg: SegState;
  onSegChange: (seg: SegState) => void;
  /** Non-null = the box is PATCHing an existing note instead of capturing. */
  onSend: (input: SendInput) => void;
  /** Full Brain routes the typed body to the live transcript; Research toasts.
   * Staged files ride along (chat attachments). Resolves true once the send is
   * under way so the box can clear them; false (e.g. an upload failed) keeps them
   * staged for a retry. A void/undefined return is treated as success. */
  onConversation: (body: string, files: File[]) => undefined | Promise<boolean>;
  /** A turn is streaming — the send button becomes a Stop button (and another send
   * is blocked). */
  busy?: boolean;
  /** Abort the in-flight turn. When present and `busy`, the send button turns into a
   * Stop button that calls this; absent outside the conversation surface. */
  onStop?: (() => void) | undefined;
  /** Live context-window fill for the open chat — rendered as a compact meter in the
   * composer foot. Null/absent hides it (capture modes, or before the first turn). */
  contextUsage?: ContextUsage | null;
  onOpenLauncher: () => void;
  /** Per-segment label overrides. The active research-mode tab reads "Teacher"
   * while a Teacher session is open, otherwise the mode's own label stands. */
  labels?: Partial<Record<Mode, string>> | undefined;
  /** Text to seed the composer with (e.g. a calendar "reschedule" handoff). */
  draft?: string;
  onConsumeDraft?: () => void;
  /** A calendar handoff's appointment, shown as a removable pill in the attach
   * area; its id rides the next Full Brain send so the agent resolves it. */
  apptRef?: AppointmentRef | null;
  onClearApptRef?: () => void;
  /** Full Brain only: a committed horizontal swipe across the box shuttles the
   * lateral panels (`dx` is the signed travel — right opens Sessions, left opens
   * Proposals; the opposite swipe sends the open one back). Absent elsewhere, so
   * the gesture is inert outside the conversation surface. */
  onLateralSwipe?: ((dx: number) => void) | undefined;
  /** Whether the attach paperclip is offered. Capture modes always allow it (note
   * attachments). A conversation mode allows it only when the agent's model is
   * vision-capable; with vision off the paperclip is simply hidden. Defaults to true. */
  attachEnabled?: boolean;
}

export function Omnibox({
  seg,
  onSegChange,
  onSend,
  onConversation,
  busy = false,
  onStop,
  contextUsage,
  onOpenLauncher,
  labels,
  draft,
  onConsumeDraft,
  apptRef,
  onClearApptRef,
  onLateralSwipe,
  attachEnabled = true,
}: OmniboxProps) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  // Remember the chosen destination per mode so flipping modes keeps it.
  const [destinations, setDestinations] = useState<Partial<Record<Mode, string>>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const touchStartY = useRef<number | null>(null);
  const touchStartX = useRef<number | null>(null);

  // A handoff (e.g. the calendar's "reschedule") seeds the composer once, then
  // clears so a re-render can't re-seed; the owner reviews and sends themselves.
  useEffect(() => {
    if (draft) {
      setText(draft);
      onConsumeDraft?.();
      inputRef.current?.focus();
    }
  }, [draft, onConsumeDraft]);

  // Grow the box with the text: collapse to the CSS min (two lines), then take
  // the content height. `text` and `seg.mode` are deliberate triggers — the
  // content height is read off the DOM, which only reflects them after a render,
  // so they don't appear in the closure for the linter to credit.
  // biome-ignore lint/correctness/useExhaustiveDependencies: re-measure on text/mode change
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight}px`;
  }, [text, seg.mode]);

  // Entering edit mode loads the note body; leaving it restores blank capture.
  const meta = MODES[seg.mode];
  const destination = meta.dest ? (destinations[seg.mode] ?? meta.dest.options[0] ?? null) : null;

  function send() {
    if (busy) return;
    const body = text.trim();
    if (meta.domain === null) {
      // Research / Full Brain hand off to the conversation surface, staged files
      // riding along as chat attachments. A files-only turn is allowed (caption
      // optional). Clear the composer only once the send is confirmed under way —
      // an upload failure keeps BOTH the text and the files staged for a retry.
      if (body === "" && files.length === 0) return;
      const staged = files;
      const result = onConversation(body, staged);
      if (result instanceof Promise) {
        void result.then((ok) => {
          if (ok) {
            setText("");
            setFiles((cur) => cur.filter((f) => !staged.includes(f)));
          }
        });
      } else {
        setText("");
        setFiles([]);
      }
      return;
    }
    if (body === "") return;
    onSend({ domain: meta.domain, destination, body, files });
    setText("");
    setFiles([]);
  }

  function stageFiles(list: FileList | null) {
    if (list) setFiles((prev) => [...prev, ...Array.from(list)]);
  }

  // Both omnibox gestures live on the segment row only (Entry / Research / Full
  // Brain), so a swipe never fights typing, scrolling, or text selection in the
  // composer below: swipe up opens the card launcher; a horizontal swipe (Full
  // Brain only) shuttles the lateral panels.
  function onTouchStart(event: TouchEvent) {
    const touch = event.touches[0];
    if (touch === undefined) {
      touchStartX.current = null;
      touchStartY.current = null;
      return;
    }
    touchStartX.current = touch.clientX;
    touchStartY.current = touch.clientY;
  }

  function onTouchMove(event: TouchEvent) {
    const startY = touchStartY.current;
    const touch = event.touches[0];
    if (startY === null || touch === undefined) return;
    const dy = startY - touch.clientY;
    const dx = Math.abs((touchStartX.current ?? touch.clientX) - touch.clientX);
    // Clearly vertical and upward — don't fire on horizontal panel swipes.
    if (dy > SWIPE_UP_PX && dy > dx * 2) {
      touchStartY.current = null;
      touchStartX.current = null;
      onOpenLauncher();
    }
  }

  function onTouchEnd(event: TouchEvent) {
    const startX = touchStartX.current;
    touchStartX.current = null;
    touchStartY.current = null;
    const touch = event.changedTouches[0];
    if (startX === null || touch === undefined || onLateralSwipe === undefined) return;
    const dx = touch.clientX - startX;
    // A committed, clearly-horizontal drag shuttles the panels; a tap or a short
    // travel leaves them be (so typing and segment taps are never hijacked).
    if (Math.abs(dx) >= PANEL_PX) onLateralSwipe(dx);
  }

  const boxStyle = { "--mode": meta.color, "--mode-tint": meta.tint } as CSSProperties;
  const ModeIcon = MODE_ICON[seg.mode];

  return (
    <div className="dock">
      <div className="omnibox" style={boxStyle}>
        <div
          className="seg-row"
          role="tablist"
          onTouchStart={onTouchStart}
          onTouchMove={onTouchMove}
          onTouchEnd={onTouchEnd}
        >
          {ROWS[seg.row].map((mode) => {
            const m = MODES[mode];
            const active = mode === seg.mode;
            const Ic = MODE_ICON[mode];
            return (
              <button
                key={mode}
                type="button"
                role="tab"
                aria-selected={active}
                className={`seg${active ? " seg-on" : ""}`}
                style={
                  active
                    ? ({ "--mode": m.color, "--mode-tint": m.tint } as CSSProperties)
                    : undefined
                }
                onClick={() => onSegChange(tapSegment(seg, mode))}
              >
                <span className="seg-ic">
                  <Ic size={18} />
                </span>
                {labels?.[mode] ?? m.label}
              </button>
            );
          })}
        </div>

        {meta.dest && (
          <div className="dest-row">
            <span className="dest-ic">
              <ModeIcon size={18} />
            </span>
            <span className="dest-path">{meta.dest.path}</span>
            <select
              aria-label="Destination"
              value={destination ?? ""}
              onChange={(e) => setDestinations((prev) => ({ ...prev, [seg.mode]: e.target.value }))}
            >
              {meta.dest.options.map((opt) => (
                <option key={opt}>{opt}</option>
              ))}
            </select>
            {/* Custom destinations land with the wiki tree; placeholder for parity with the approved mock. */}
            <button type="button" className="dest-new" disabled>
              + New
            </button>
          </div>
        )}

        <textarea
          ref={inputRef}
          className="composer-input"
          placeholder={meta.placeholder}
          value={text}
          onChange={(e) => setText(e.target.value)}
          aria-label="Composer"
        />

        {apptRef && (
          <div className="staged-files">
            <button
              type="button"
              className="chip chip-staged chip-appt"
              onClick={onClearApptRef}
              aria-label={`Remove appointment ${apptRef.title}`}
            >
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="M8 2v4M16 2v4M3 9h18M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" />
              </svg>
              {apptRef.title} ×
            </button>
          </div>
        )}

        {files.length > 0 && (
          <div className="staged-files">
            {files.map((file, index) => {
              const kind = attachmentKind(file.type);
              const FileGlyph = kind === "img" ? ImageIcon : FileIcon;
              return (
                <button
                  key={`${file.name}-${index}`}
                  type="button"
                  className={`chip chip-staged att-${kind}`}
                  onClick={() => setFiles((prev) => prev.filter((_, i) => i !== index))}
                  aria-label={`Remove ${file.name}`}
                >
                  <FileGlyph size={12} /> {file.name} ×
                </button>
              );
            })}
          </div>
        )}

        {/* Vision off on a conversation mode just hides the paperclip — no stand-in
            line (capture modes always keep their attach). */}
        <div className="composer-foot">
          {contextUsage && <ContextMeter usage={contextUsage} />}
          <div className="foot-icons">
            {attachEnabled && (
              <button
                type="button"
                className="icon-btn"
                aria-label="Attach files"
                onClick={() => fileInputRef.current?.click()}
              >
                <ClipIcon size={24} />
              </button>
            )}
            {busy && onStop ? (
              // While a turn streams the send button becomes Stop — one tap aborts
              // the run (the partial answer above stays put, settled as "Stopped").
              <button
                type="button"
                className="icon-btn stop-btn"
                aria-label="Stop generating"
                onClick={onStop}
              >
                <StopIcon size={24} />
              </button>
            ) : (
              <button
                type="button"
                className="icon-btn send-btn"
                aria-label="Send"
                onClick={send}
                disabled={
                  busy || (text.trim() === "" && !(meta.domain === null && files.length > 0))
                }
              >
                <SendIcon size={24} />
              </button>
            )}
          </div>
        </div>

        <input
          ref={fileInputRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            stageFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </div>
    </div>
  );
}

// A terse token count for the context meter: "850", "12.3k", "256k".
function fmtTokens(n: number): string {
  if (n < 1000) return String(n);
  const k = n / 1000;
  return `${k < 10 ? k.toFixed(1) : Math.round(k)}k`;
}

// The live context-usage meter in the composer foot: a thin fill bar plus
// "used / window" and a percentage, so the owner can see how full the model's
// context is getting (mainly the local models' 32k window). It tints toward the
// warning hue as the window fills — calm until it actually matters.
function ContextMeter({ usage }: { usage: ContextUsage }): ReactNode {
  const frac = usage.window > 0 ? Math.min(usage.used / usage.window, 1) : 0;
  const pct = Math.round(frac * 100);
  // The carried-forward floor, clamped under the peak so the solid segment never
  // overruns the transient one it sits on (window guards a zero/garbage window).
  const baseFrac = usage.window > 0 ? Math.min(usage.base / usage.window, frac) : 0;
  const basePct = Math.round(baseFrac * 100);
  const level = frac >= 0.9 ? "high" : frac >= 0.7 ? "mid" : "";
  const transient = Math.max(usage.used - usage.base, 0);
  return (
    <output
      className={`ctx-meter${level ? ` ctx-${level}` : ""}`}
      aria-label={`Context used: ${usage.used} of ${usage.window} tokens (${pct}%) — ${usage.base} carried, ${transient} this turn`}
      title={`${usage.base.toLocaleString()} carried + ${transient.toLocaleString()} this turn / ${usage.window.toLocaleString()} tokens`}
    >
      {/* Two stacked fills: the lighter peak reaches `pct`, the solid base layers over
          its left to `basePct`. The base is painted last so it sits on top. */}
      <span className="ctx-bar" aria-hidden="true">
        <span className="ctx-fill ctx-fill-peak" style={{ width: `${pct}%` }} />
        <span className="ctx-fill ctx-fill-base" style={{ width: `${basePct}%` }} />
      </span>
      <span className="ctx-text">
        {fmtTokens(usage.used)}/{fmtTokens(usage.window)} · {pct}%
      </span>
    </output>
  );
}
