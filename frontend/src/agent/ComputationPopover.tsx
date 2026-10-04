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
//
// What closes it is exactly three things: a tap OUTSIDE the panel (the scrim), Escape, and
// the close control. It used to close on ANY scroll as well — a capture-phase listener on
// window — and a scroll event's capture reaches window from every element, the panel's own
// scrolling body included. So the gesture the 46vh cap exists for (scroll a long program)
// shut the panel on its first pixel, and a tap inside that nudged the body did the same: on
// the box it read as "touching the popover closes it".

import { type ReactNode, useEffect, useLayoutEffect, useRef, useState } from "react";
import { XIcon } from "../components/icons";
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

/** What the panel points at: a fixed rect, the marker element, or a function that finds the
 * marker afresh (the surface re-queries it by number inside its bubble, so a re-render that
 * replaces the node — a verdict landing, the paced reveal settling — cannot strand it). */
export type CalcAnchor = DOMRect | Element | (() => Element | null);

function resolve(anchor: CalcAnchor): Element | DOMRect | null {
  if (typeof anchor === "function") return anchor();
  return anchor;
}

/** The marker's rect NOW, else the last rect it had: a marker momentarily out of the page
 * keeps the panel where it was rather than collapsing it to the corner. */
function rectOf(anchor: CalcAnchor, last: DOMRect | null): DOMRect {
  const at = resolve(anchor);
  if (at instanceof Element) {
    return at.isConnected ? at.getBoundingClientRect() : (last ?? at.getBoundingClientRect());
  }
  return at ?? last ?? ({ left: 0, top: 0, bottom: 0, right: 0, width: 0, height: 0 } as DOMRect);
}

export function ComputationPopover({
  target,
  anchor,
  onClose,
}: {
  target: CalcTarget;
  anchor: CalcAnchor;
  onClose: () => void;
}): ReactNode {
  const [placement, setPlacement] = useState<Placement | null>(null);
  const panel = useRef<HTMLDialogElement>(null);
  const closeBtn = useRef<HTMLButtonElement>(null);
  const lastRect = useRef<DOMRect | null>(null);

  // Re-placed whenever the panel's own SIZE changes, and whenever something OUTSIDE it
  // scrolls. The height is what decides which side of the marker it opens on, and an
  // observer covers every way the body can grow (a long error, a wrapped line, a rotated
  // phone) rather than only the ones thought of here. The scrim catches touch, so the
  // transcript only moves under the panel programmatically (the stream's follow-to-bottom)
  // or by wheel/keyboard — and then the panel follows its marker instead of closing. A
  // scroll fires per frame at most but in bursts, so placement is rAF-throttled. Its OWN
  // scroll is ignored: that is the owner reading the working.
  useLayoutEffect(() => {
    const el = panel.current;
    if (!el) return;
    let frame = 0;
    const measure = () => {
      const rect = rectOf(anchor, lastRect.current);
      lastRect.current = rect;
      setPlacement(place(rect, el.offsetHeight));
    };
    const onScroll = (e: Event) => {
      if (e.target instanceof Node && el.contains(e.target)) return;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        measure();
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      ro.disconnect();
      window.removeEventListener("scroll", onScroll, true);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [anchor]);

  // Focus lands on the close control when the panel opens and goes back to the marker when
  // it closes, so a keyboard or screen-reader user is neither left behind on the marker nor
  // dropped at the top of the page. `preventScroll`, because a focus that scrolls would move
  // the transcript the panel is pointing into.
  // biome-ignore lint/correctness/useExhaustiveDependencies: open/close only — the anchor is read at close time.
  useEffect(() => {
    closeBtn.current?.focus({ preventScroll: true });
    return () => {
      const at = resolve(anchor);
      if (at instanceof HTMLElement && at.isConnected) at.focus({ preventScroll: true });
    };
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
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
        <div className="fb-calc-head">
          <button
            ref={closeBtn}
            type="button"
            className="fb-calc-x"
            aria-label="close the working"
            onClick={onClose}
          >
            <XIcon size={14} />
          </button>
        </div>
        <div className="fb-calc-body">
          <ToolView payload={target.payload} />
        </div>
      </dialog>
    </>
  );
}
