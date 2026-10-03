import type { ReactNode } from "react";
import { useState } from "react";

// The Ops screen's one disclosure shell (DESIGN.md "Ops screen"), shared with cards that
// live in their own files (the Local engine card).

/** `headerRight` shows in the header whether open or closed (group counts);
 * `summaryCollapsed` shows only while collapsed (the System recap). The body
 * is mounted only when open, so collapsed groups never fetch their logs.
 * Pass `open` + `onToggle` to control it — a card that opens itself on a state
 * change (rather than only on mount) needs to, or closing a confirm would remount
 * and collapse it. */
export function OpsCard({
  title,
  defaultOpen = false,
  headerRight,
  summaryCollapsed,
  bodyClassName,
  open: controlledOpen,
  onToggle,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  headerRight?: ReactNode;
  summaryCollapsed?: ReactNode;
  bodyClassName?: string;
  open?: boolean;
  onToggle?: (open: boolean) => void;
  children: ReactNode;
}) {
  const [ownOpen, setOwnOpen] = useState(defaultOpen);
  const open = controlledOpen ?? ownOpen;
  const setOpen = (next: boolean) => (onToggle ? onToggle(next) : setOwnOpen(next));
  return (
    <section className="ops-card">
      <button
        type="button"
        className={`ops-card-head${open ? " open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className="ops-card-title">{title}</span>
        <span className="ops-card-right">
          {!open && summaryCollapsed}
          {headerRight}
        </span>
        <span className="ops-card-caret">›</span>
      </button>
      {open && (
        <div className={`ops-card-body${bodyClassName ? ` ${bodyClassName}` : ""}`}>{children}</div>
      )}
    </section>
  );
}
