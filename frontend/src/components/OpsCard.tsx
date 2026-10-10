import type { ReactNode } from "react";
import { useState } from "react";

// The Ops Services page's disclosure shell (DESIGN.md "Ops screen"): one per service group.

/** `headerRight` shows in the header whether open or closed (group counts and state). The
 * body is mounted only when open, so collapsed groups never fetch their logs. */
export function OpsCard({
  title,
  headerRight,
  bodyClassName,
  children,
}: {
  title: string;
  headerRight?: ReactNode;
  bodyClassName?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <section className="ops-card">
      <button
        type="button"
        className={`ops-card-head${open ? " open" : ""}`}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className="ops-card-title">{title}</span>
        <span className="ops-card-right">{headerRight}</span>
        <span className="ops-card-caret">›</span>
      </button>
      {open && (
        <div className={`ops-card-body${bodyClassName ? ` ${bodyClassName}` : ""}`}>{children}</div>
      )}
    </section>
  );
}
