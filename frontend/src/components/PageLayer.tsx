// A pushed page inside a card screen: its own back bar over the screen beneath, climbed by
// back, a down-swipe at scroll-top, or the platform Back gesture (it registers in the shared
// back-layer stack, like a Sheet). Settings and Ops open their category tiles into one of these
// (DESIGN.md "Navigation: the card launcher").

import { type ReactNode, type TouchEvent, useEffect, useRef } from "react";
import { useBackLayer } from "../backLayers";
import { ChevronLeftIcon } from "./icons";

// Matches the subscreen's own down-swipe threshold in App.
const SWIPE_DOWN_PX = 56;

export function PageLayer({
  title,
  onBack,
  bodyClassName,
  children,
}: {
  title: string;
  onBack: () => void;
  bodyClassName?: string;
  children: ReactNode;
}) {
  useBackLayer(onBack);
  const body = useRef<HTMLDivElement>(null);
  const back = useRef<HTMLButtonElement>(null);
  const start = useRef<{ x: number; y: number } | null>(null);
  useEffect(() => {
    back.current?.focus({ preventScroll: true });
  }, []);
  // The card screen's own swipe-down would close the whole card; a page climbs one level.
  function onTouchStart(e: TouchEvent) {
    e.stopPropagation();
    const t = e.touches[0];
    const atTop = (body.current?.scrollTop ?? 0) <= 4;
    start.current = atTop && t ? { x: t.clientX, y: t.clientY } : null;
  }
  function onTouchMove(e: TouchEvent) {
    e.stopPropagation();
    const s = start.current;
    const t = e.touches[0];
    if (!s || !t) return;
    const dy = t.clientY - s.y;
    if (dy > SWIPE_DOWN_PX && dy > Math.abs(t.clientX - s.x) * 2) {
      start.current = null;
      onBack();
    }
  }
  return (
    <div className="subscreen" onTouchStart={onTouchStart} onTouchMove={onTouchMove}>
      <header className="top-bar">
        <button type="button" className="back-btn" onClick={onBack} aria-label="Back" ref={back}>
          <ChevronLeftIcon size={22} />
          <span className="screen-title">{title}</span>
        </button>
      </header>
      <div className={`screen-body${bodyClassName ? ` ${bodyClassName}` : ""}`} ref={body}>
        {children}
      </div>
    </div>
  );
}
