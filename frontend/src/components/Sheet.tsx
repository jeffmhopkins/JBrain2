// The shared bottom sheet (docs/reference/DESIGN.md "Modal system"): scrim, body-scroll
// lock, Escape + swipe-down + scrim-tap dismiss, drag handle, one title.
// Every bottom-sheet flow composes this shell — bespoke modals are a
// design-doc violation. It also self-registers in the back-layer stack so the
// platform Back gesture closes the sheet (not the screen beneath it), matching
// swipe-down; see backLayers.ts.

import { type ReactNode, type TouchEvent, useEffect, useRef } from "react";
import { useBackLayer } from "../backLayers";

const SWIPE_DOWN_PX = 96;

interface SheetProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
}

export function Sheet({ title, onClose, children }: SheetProps) {
  const panelRef = useRef<HTMLDialogElement>(null);
  const touchStartY = useRef<number | null>(null);

  // Back gesture pops this sheet before the screen under it (App reads the stack).
  useBackLayer(onClose);

  useEffect(() => {
    panelRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [onClose]);

  function onTouchStart(event: TouchEvent) {
    touchStartY.current = event.touches[0]?.clientY ?? null;
  }

  function onTouchMove(event: TouchEvent) {
    const startY = touchStartY.current;
    const y = event.touches[0]?.clientY;
    if (startY !== null && y !== undefined && y - startY > SWIPE_DOWN_PX) {
      touchStartY.current = null;
      onClose();
    }
  }

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: scrim tap is a pointer enhancement; Escape is the keyboard path.
    <div
      className="sheet-scrim"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <dialog
        className="sheet"
        open
        aria-modal="true"
        aria-label={title}
        ref={panelRef}
        tabIndex={-1}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
      >
        <button type="button" className="sheet-grab" onClick={onClose} aria-label="Close">
          <span className="sheet-handle" aria-hidden="true" />
        </button>
        <h2 className="sheet-title">{title}</h2>
        {children}
      </dialog>
    </div>
  );
}
