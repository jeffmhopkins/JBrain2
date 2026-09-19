// G: where a number came from (docs/mocks/code-run/g-cited-floating.html).
//
// The gate settled on D's marker with F's popover, because their weaknesses were exactly
// complementary: D was findable but pushed the page down, F floated but was invisible until
// guessed at. G is both — a visible `ƒn` on the number, and a panel that floats over the
// transcript instead of reflowing it.
//
// It opens COMPACT (the expression and its answer) and expands in place, capped at 46% of
// the frame with its body scrolling. That is the answer to the one flaw recorded against F:
// a popover is a poor place for eight lines of code, so small content stays a popover and
// large content stops being one — the expanded body renders the SAME `code_run` component
// the Worked step does, which is what D2 bought.

import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CalcTarget } from "./markdown";
import { ToolView } from "./views/registry";

/** Where the panel sits, in viewport coordinates, plus which side of the marker it opened
 * on — the tail points back at the number so the link between them survives the float. */
interface Placement {
  left: number;
  top: number;
  above: boolean;
}

const MARGIN = 8;
const WIDTH = 296;

function place(anchor: DOMRect, height: number): Placement {
  // Clamped to the viewport on both axes: a popover that opens off-screen is a popover the
  // owner cannot read, and the marker can sit anywhere in a paragraph.
  const left = Math.min(
    Math.max(MARGIN, anchor.left + anchor.width / 2 - WIDTH / 2),
    Math.max(MARGIN, window.innerWidth - WIDTH - MARGIN),
  );
  const below = anchor.bottom + 8;
  const above = below + height > window.innerHeight - MARGIN;
  return { left, top: above ? Math.max(MARGIN, anchor.top - 8 - height) : below, above };
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
  const [open, setOpen] = useState(false);
  const [placement, setPlacement] = useState<Placement | null>(null);
  const panel = useRef<HTMLDivElement>(null);

  // Measured after layout, and again when it expands: the panel's height decides whether it
  // opens above or below, so guessing would flip it to the wrong side on a tall snippet.
  useLayoutEffect(() => {
    const el = panel.current;
    if (el) setPlacement(place(anchor, el.offsetHeight));
  }, [anchor, open]);

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

  const data = target.payload.data;
  const expression = typeof data.code === "string" ? data.code : "";

  return (
    <>
      {/* Tap-anywhere-to-close, transparent: the popover is a glance, not a mode. */}
      <button type="button" className="fb-calc-scrim" aria-label="close" onClick={onClose} />
      <div
        ref={panel}
        className={`fb-calc-pop${placement?.above ? " above" : ""}${open ? " open" : ""}`}
        style={
          placement
            ? { left: `${placement.left}px`, top: `${placement.top}px`, width: `${WIDTH}px` }
            : { left: "-9999px", top: "0", width: `${WIDTH}px` }
        }
        role="dialog"
        aria-label="how this number was worked out"
      >
        <div className="fb-calc-head">
          <span className="fb-calc-expr" title={expression}>
            {expression}
          </span>
          <span className="fb-calc-ans">{target.brief}</span>
        </div>
        {open ? (
          <div className="fb-calc-body">
            <ToolView payload={target.payload} />
          </div>
        ) : (
          <button type="button" className="fb-calc-more" onClick={() => setOpen(true)}>
            show the working
          </button>
        )}
      </div>
    </>
  );
}
