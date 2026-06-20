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
import type { AppointmentRef } from "../agent/types";
import { MODES, type Mode, ROWS, type SegState, tapSegment } from "../notes/modes";
import type { SendInput } from "../notes/useNotes";
import {
  BotIcon,
  ClipIcon,
  FinancialIcon,
  MedicalIcon,
  PlusIcon,
  SearchIcon,
  SendIcon,
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
  /** Full Brain routes the typed body to the live transcript; Research toasts. */
  onConversation: (body: string) => void;
  /** A turn is streaming — block another send and dim the button. */
  busy?: boolean;
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
}

export function Omnibox({
  seg,
  onSegChange,
  onSend,
  onConversation,
  busy = false,
  onOpenLauncher,
  labels,
  draft,
  onConsumeDraft,
  apptRef,
  onClearApptRef,
  onLateralSwipe,
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
    const body = text.trim();
    if (body === "" || busy) return;
    if (meta.domain === null) {
      // Research / Full Brain hand off to the conversation surface.
      onConversation(body);
      setText("");
      return;
    }
    onSend({ domain: meta.domain, destination, body, files });
    setText("");
    setFiles([]);
  }

  function stageFiles(list: FileList | null) {
    if (list) setFiles((prev) => [...prev, ...Array.from(list)]);
  }

  // Two gestures share the box. Swipe up opens the card launcher; a horizontal
  // swipe (Full Brain only) shuttles the lateral panels. The textarea joins both
  // while it has no internal scroll (empty or short text — the common case and
  // most of the box's surface); once content overflows it owns its own vertical
  // scroll again, so we drop swipe-up there but keep the horizontal panel swipe
  // (which never conflicts with vertical scrolling). Selects stay excluded.
  function onTouchStart(event: TouchEvent) {
    const target = event.target as HTMLElement;
    if (target.closest("select") !== null) {
      touchStartX.current = null;
      touchStartY.current = null;
      return;
    }
    const touch = event.touches[0];
    if (touch === undefined) {
      touchStartX.current = null;
      touchStartY.current = null;
      return;
    }
    touchStartX.current = touch.clientX;
    const area = target.closest("textarea");
    // Null Y opts the start out of swipe-up while keeping X for the panel swipe.
    touchStartY.current =
      area !== null && area.scrollHeight > area.clientHeight ? null : touch.clientY;
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
      <div
        className="omnibox"
        style={boxStyle}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        <div className="seg-row" role="tablist">
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
            {files.map((file, index) => (
              <button
                key={`${file.name}-${index}`}
                type="button"
                className="chip chip-staged"
                onClick={() => setFiles((prev) => prev.filter((_, i) => i !== index))}
                aria-label={`Remove ${file.name}`}
              >
                <ClipIcon size={12} /> {file.name} ×
              </button>
            ))}
          </div>
        )}

        <div className="composer-foot">
          <span className="mode-dot" />
          <span className="foot-text">{meta.footer}</span>
          <div className="foot-icons">
            <button
              type="button"
              className="icon-btn"
              aria-label="Attach files"
              onClick={() => fileInputRef.current?.click()}
            >
              <ClipIcon size={24} />
            </button>
            <button
              type="button"
              className="icon-btn send-btn"
              aria-label="Send"
              onClick={send}
              disabled={text.trim() === "" || busy}
            >
              <SendIcon size={24} />
            </button>
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
