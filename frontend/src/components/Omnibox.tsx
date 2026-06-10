// The bottom-docked composer (docs/DESIGN.md "The omnibox home").
// Fixed-height box; the segmented row morphs between the main trio and the
// entry sub-types; Medical/Financial expose an in-box destination row and
// the textarea absorbs the height difference.

import { type CSSProperties, type ReactNode, type TouchEvent, useRef, useState } from "react";
import { MODES, type Mode, ROWS, type SegState, tapSegment } from "../notes/modes";
import type { SendInput } from "../notes/useNotes";
import {
  BoltIcon,
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

interface OmniboxProps {
  onSend: (input: SendInput) => void;
  onConversation: () => void;
  onOpenLauncher: () => void;
}

export function Omnibox({ onSend, onConversation, onOpenLauncher }: OmniboxProps) {
  const [seg, setSeg] = useState<SegState>({ row: "main", mode: "entry" });
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  // Remember the chosen destination per mode so flipping modes keeps it.
  const [destinations, setDestinations] = useState<Partial<Record<Mode, string>>>({});
  const fileInputRef = useRef<HTMLInputElement>(null);
  const touchStartY = useRef<number | null>(null);

  const meta = MODES[seg.mode];
  const destination = meta.dest ? (destinations[seg.mode] ?? meta.dest.options[0] ?? null) : null;

  function send() {
    const body = text.trim();
    if (body === "") return;
    if (meta.domain === null) {
      // Research / Full Brain hand off to the Phase 4 conversation surface.
      onConversation();
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

  // Swipe up anywhere on the box (except inside the textarea, which owns its
  // own touch scrolling) opens the card launcher.
  function onTouchStart(event: TouchEvent) {
    const target = event.target as HTMLElement;
    touchStartY.current =
      target.closest("textarea, select") === null ? (event.touches[0]?.clientY ?? null) : null;
  }

  function onTouchMove(event: TouchEvent) {
    const startY = touchStartY.current;
    const y = event.touches[0]?.clientY;
    if (startY !== null && y !== undefined && startY - y > SWIPE_UP_PX) {
      touchStartY.current = null;
      onOpenLauncher();
    }
  }

  const boxStyle = { "--mode": meta.color, "--mode-tint": meta.tint } as CSSProperties;
  const ModeIcon = MODE_ICON[seg.mode];
  const ToolIcon = meta.tool === "bolt" ? BoltIcon : ClipIcon;

  return (
    <div className="dock">
      <div
        className="omnibox"
        style={boxStyle}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
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
                onClick={() => setSeg((prev) => tapSegment(prev, mode))}
              >
                <span className="seg-ic">
                  <Ic size={18} />
                </span>
                {m.label}
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
          className="composer-input"
          placeholder={meta.placeholder}
          value={text}
          onChange={(e) => setText(e.target.value)}
          aria-label="Composer"
        />

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
            {meta.tool === "clip" ? (
              <button
                type="button"
                className="icon-btn"
                aria-label="Attach files"
                onClick={() => fileInputRef.current?.click()}
              >
                <ToolIcon size={24} />
              </button>
            ) : (
              <button
                type="button"
                className="icon-btn"
                aria-label="Open launcher"
                onClick={onOpenLauncher}
              >
                <ToolIcon size={24} />
              </button>
            )}
            <button
              type="button"
              className="icon-btn send-btn"
              aria-label="Send"
              onClick={send}
              disabled={text.trim() === ""}
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
