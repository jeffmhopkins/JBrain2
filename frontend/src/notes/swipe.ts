// Swipe-left action rail on note bubbles: pure drag state so the
// translate-follow / snap behavior is unit-testable without touch events.
// The bubble follows the finger horizontally once the gesture is clearly
// horizontal; vertical movement wins (axis "v") so list scrolling is never
// hijacked. Release snaps fully open (rail width) or fully closed.

export const RAIL_WIDTH = 192;
const AXIS_LOCK_PX = 8;

export type DragAxis = "none" | "h" | "v";

export interface Drag {
  startX: number;
  startY: number;
  /** Offset at gesture start: 0 closed, -RAIL_WIDTH open. */
  base: number;
  axis: DragAxis;
  /** Current translateX for the bubble, clamped to [-width, 0]. */
  offset: number;
  /** The open rail's width. A rail whose button count or text-size scaling differs from
   * the note rail's passes its own, so the bubble opens exactly as far as the rail is wide. */
  width: number;
}

export function beginDrag(x: number, y: number, open: boolean, width = RAIL_WIDTH): Drag {
  const base = open ? -width : 0;
  return { startX: x, startY: y, base, axis: "none", offset: base, width };
}

export function moveDrag(drag: Drag, x: number, y: number): Drag {
  const dx = x - drag.startX;
  const dy = y - drag.startY;
  let axis = drag.axis;
  if (axis === "none" && (Math.abs(dx) > AXIS_LOCK_PX || Math.abs(dy) > AXIS_LOCK_PX)) {
    // Ties go vertical: scrolling wins over the rail.
    axis = Math.abs(dx) > Math.abs(dy) ? "h" : "v";
  }
  const offset = axis === "h" ? Math.min(0, Math.max(-drag.width, drag.base + dx)) : drag.base;
  return { ...drag, axis, offset };
}

/** Snap decision at release: true = rail open. */
export function endDrag(drag: Drag): boolean {
  if (drag.axis !== "h") return drag.base === -drag.width;
  return drag.offset < -drag.width / 2;
}
