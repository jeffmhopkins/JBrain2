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
  /** Current translateX for the bubble, clamped to [-RAIL_WIDTH, 0]. */
  offset: number;
}

export function beginDrag(x: number, y: number, open: boolean): Drag {
  const base = open ? -RAIL_WIDTH : 0;
  return { startX: x, startY: y, base, axis: "none", offset: base };
}

export function moveDrag(drag: Drag, x: number, y: number): Drag {
  const dx = x - drag.startX;
  const dy = y - drag.startY;
  let axis = drag.axis;
  if (axis === "none" && (Math.abs(dx) > AXIS_LOCK_PX || Math.abs(dy) > AXIS_LOCK_PX)) {
    // Ties go vertical: scrolling wins over the rail.
    axis = Math.abs(dx) > Math.abs(dy) ? "h" : "v";
  }
  const offset = axis === "h" ? Math.min(0, Math.max(-RAIL_WIDTH, drag.base + dx)) : drag.base;
  return { ...drag, axis, offset };
}

/** Snap decision at release: true = rail open. */
export function endDrag(drag: Drag): boolean {
  if (drag.axis !== "h") return drag.base === -RAIL_WIDTH;
  return drag.offset < -RAIL_WIDTH / 2;
}
