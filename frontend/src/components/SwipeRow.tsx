/** A compact row with a swipe-left action rail, the Chats panel's pattern (SessionsPanel's
 *  SessionRow) lifted out so other lists can wear it: swipe left to reveal the actions, tap
 *  the row for its main action, and only one rail open at a time (the caller holds which).
 *  A tap on an open row closes the rail instead of opening the row, so a half-meant swipe
 *  never opens anything. */
import { type ReactNode, type TouchEvent, useRef, useState } from "react";
import { getFontScale } from "../fontScale";
import { type Drag, beginDrag, endDrag, moveDrag } from "../notes/swipe";

export interface RailAction {
  key: string;
  label: string;
  icon?: ReactNode;
  /** The rail's colour: `edit` amber, `delete` rose, or any `rail-<tone>` class in CSS. */
  tone: string;
  /** Armed shows the tap-again state (useArm's), filled with the danger colour. */
  armed?: boolean;
  armedLabel?: string;
  onClick: () => void;
}

// Each action is 64px at 100% text size and scales with it, so the row opens exactly as far
// as the rail is wide.
const ACTION_PX = 64;

export function SwipeRow({
  actions,
  open,
  onOpenChange,
  onTap,
  label,
  className = "",
  children,
}: {
  actions: RailAction[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onTap: () => void;
  /** The tap target's accessible name. */
  label?: string;
  className?: string;
  children: ReactNode;
}) {
  const [drag, setDrag] = useState<Drag | null>(null);
  const dragged = useRef(false);
  const each = Math.round((ACTION_PX * getFontScale()) / 100);
  const width = each * actions.length;
  const dragging = drag !== null && drag.axis === "h";
  const offset = dragging ? drag.offset : open ? -width : 0;

  // stopPropagation so the row's swipe never reaches a screen-level swipe gesture.
  function start(e: TouchEvent): void {
    e.stopPropagation();
    dragged.current = false;
    const t = e.touches[0];
    if (t) setDrag(beginDrag(t.clientX, t.clientY, open, width));
  }
  function move(e: TouchEvent): void {
    if (drag === null) return;
    e.stopPropagation();
    const t = e.touches[0];
    if (!t) return;
    const next = moveDrag(drag, t.clientX, t.clientY);
    if (next.axis === "v") {
      setDrag(null);
      return;
    }
    setDrag(next);
  }
  function end(e: TouchEvent): void {
    if (drag === null) return;
    e.stopPropagation();
    if (drag.axis === "h") {
      dragged.current = true;
      onOpenChange(endDrag(drag));
    }
    setDrag(null);
  }
  function tap(): void {
    if (dragged.current) {
      dragged.current = false;
      return;
    }
    if (open) {
      onOpenChange(false);
      return;
    }
    onTap();
  }

  return (
    <div className={`swipe-row ${className}`.trim()}>
      {offset < 0 && (
        <div className="swipe-rail">
          {actions.map((a) => (
            <button
              key={a.key}
              type="button"
              className={`rail-btn rail-${a.tone}${a.armed ? " rail-armed" : ""}`}
              style={{ width: each }}
              onClick={a.onClick}
            >
              {a.armed ? (
                (a.armedLabel ?? "tap again")
              ) : (
                <>
                  {a.icon}
                  {a.label}
                </>
              )}
            </button>
          ))}
        </div>
      )}
      <div
        className="swipe-slide"
        style={{ transform: `translateX(${offset}px)` }}
        onTouchStart={start}
        onTouchMove={move}
        onTouchEnd={end}
      >
        <button type="button" className="swipe-tap" aria-label={label} onClick={tap}>
          {children}
        </button>
      </div>
    </div>
  );
}
