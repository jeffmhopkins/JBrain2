// The shared center confirm (docs/reference/DESIGN.md "Modal system"): scrim, focus trap,
// body-scroll lock, Escape/back-gesture/scrim dismiss. One sentence of consequence, two
// buttons, the act on the right. Every destructive confirm composes this shell — a bespoke
// modal is a design-doc violation. Like <Sheet>, it registers in the back-layer stack so the
// platform Back gesture cancels it rather than closing the screen beneath.

import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef } from "react";
import { useBackLayer } from "../backLayers";

interface DialogProps {
  title: string;
  /** The consequence, in one sentence. */
  children: ReactNode;
  confirmLabel: string;
  /** "primary" is for a confirm whose act harms nothing (Load with nobody on). */
  tone?: "danger" | "warn" | "primary";
  /** A gate under the sentence, such as typing the world's name before a reset. */
  extra?: ReactNode;
  confirmDisabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function Dialog({
  title,
  children,
  confirmLabel,
  tone = "danger",
  extra,
  confirmDisabled = false,
  onConfirm,
  onCancel,
}: DialogProps) {
  const id = useId();
  const panelRef = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);

  useBackLayer(onCancel);

  // Cancel takes focus: Enter on a freshly opened confirm must not be the destructive act.
  // On close, focus goes back to whatever opened it, so a keyboard user isn't dropped to
  // <body>.
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    cancelRef.current?.focus();
    return () => {
      if (opener?.isConnected) opener.focus({ preventScroll: true });
    };
  }, []);

  useEffect(() => {
    // Captured and stopped: a layer still mounted underneath (the launcher beneath a card)
    // listens on window too, and the first Escape must close this, not that.
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.stopImmediatePropagation();
      onCancel();
    };
    window.addEventListener("keydown", onKey, true);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey, true);
      document.body.style.overflow = previousOverflow;
    };
  }, [onCancel]);

  function trapTab(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key !== "Tab") return;
    const buttons = panelRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input");
    if (!buttons || buttons.length === 0) return;
    const first = buttons[0];
    const last = buttons[buttons.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }

  return (
    // biome-ignore lint/a11y/useKeyWithClickEvents: scrim tap is a pointer enhancement; Escape is the keyboard path.
    <div
      className="dialog-scrim"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget) onCancel();
      }}
    >
      <dialog
        className="dialog"
        open
        aria-modal="true"
        aria-labelledby={`${id}-t`}
        aria-describedby={`${id}-b`}
        ref={panelRef}
        onKeyDown={trapTab}
      >
        <h2 className="dialog-title" id={`${id}-t`}>
          {title}
        </h2>
        <p className="dialog-body" id={`${id}-b`}>
          {children}
        </p>
        {extra}
        <div className="dialog-actions">
          <button type="button" ref={cancelRef} onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className={`dialog-confirm dialog-${tone}`}
            onClick={onConfirm}
            disabled={confirmDisabled}
          >
            {confirmLabel}
          </button>
        </div>
      </dialog>
    </div>
  );
}
