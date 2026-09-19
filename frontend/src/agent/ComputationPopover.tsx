// G: where a number came from (docs/mocks/code-run/g-cited-floating.html).
//
// The gate settled on D's marker with F's popover, because their weaknesses were exactly
// complementary: D was findable but pushed the page down, F floated but was invisible until
// guessed at. G is both — a visible `ƒn` on the number, and a panel that floats over the
// transcript instead of reflowing it.
//
// It opens on the WORKING, not on a summary of it. The collapsed head that used to come
// first (the expression on one line, its answer on the right, and a "show the working"
// link under them) is gone: the panel was already open, so the first thing in it restated
// the expression and the result that the body then labelled properly underneath — the same
// duplication the `code_run` STEP had against its own prose. One tap on the marker is the
// only tap; the body is capped at 46% of the frame and scrolls, so a long program is still
// a glance rather than a mode.

import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CalcTarget } from "./markdown";
import { ToolView } from "./views/registry";

/** Where the panel sits, in viewport coordinates, plus which side of the marker it opened
 * on — the tail points back at the number so the link between them survives the float. */
interface Placement {
  left: number;
  top: number;
  width: number;
  above: boolean;
}

const MARGIN = 8;
const MAX_WIDTH = 320;

function place(anchor: DOMRect, height: number): Placement {
  // A cap, not a width: on a narrow phone a fixed 320 would hang off the frame, and this
  // panel now always carries the full working rather than a one-line head.
  const width = Math.min(MAX_WIDTH, window.innerWidth - MARGIN * 2);
  // Clamped to the viewport on both axes: a popover that opens off-screen is a popover the
  // owner cannot read, and the marker can sit anywhere in a paragraph.
  const left = Math.min(
    Math.max(MARGIN, anchor.left + anchor.width / 2 - width / 2),
    Math.max(MARGIN, window.innerWidth - width - MARGIN),
  );
  const below = anchor.bottom + 8;
  const above = below + height > window.innerHeight - MARGIN;
  return { left, top: above ? Math.max(MARGIN, anchor.top - 8 - height) : below, width, above };
}

export function ComputationPopover({
  target,
  anchor,
  onClose,
}: {
  target: CalcTarget;
  /** The marker's rect at the moment it was tapped. */
  anchor: DOMRect;
  onClose: () => void;
}): ReactNode {
  const [placement, setPlacement] = useState<Placement | null>(null);
  const panel = useRef<HTMLDialogElement>(null);

  // Re-placed whenever the panel's own SIZE changes. The height is what decides which side
  // of the marker it opens on, and an observer covers every way the body can grow (a long
  // error, a wrapped line, a rotated phone) rather than only the ones thought of here.
  useLayoutEffect(() => {
    const el = panel.current;
    if (!el) return;
    const measure = () => setPlacement(place(anchor, el.offsetHeight));
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [anchor]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    // Any scroll of the transcript takes the marker out from under the panel, and a panel
    // pointing at nothing is worse than no panel.
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onClose, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onClose, true);
    };
  }, [onClose]);

  return (
    <>
      {/* Tap-anywhere-to-close, transparent: the popover is a glance, not a mode. */}
      <button type="button" className="fb-calc-scrim" aria-label="close" onClick={onClose} />
      {/* A real <dialog>, not a div wearing its role: non-modal (`open`, never showModal),
          because the scrim above already handles dismissal and a modal would trap focus in
          what is meant to be a glance. */}
      <dialog
        ref={panel}
        open
        className={`fb-calc-pop${placement?.above ? " above" : ""}`}
        style={
          placement
            ? {
                left: `${placement.left}px`,
                top: `${placement.top}px`,
                width: `${placement.width}px`,
              }
            : { left: "-9999px", top: "0", width: `${MAX_WIDTH}px` }
        }
        aria-label="how this number was worked out"
      >
        <div className="fb-calc-body">
          <ToolView payload={target.payload} />
        </div>
      </dialog>
    </>
  );
}
