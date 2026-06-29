// Small shared glyphs used by more than one agent surface. BrainGlyph lives here (not
// in FullBrainSurface) so the sub-agent fan can render the same "Thinking" mark as the
// main answer's activity line without importing back into FullBrainSurface (a cycle).

import type { ReactNode } from "react";

// The "Thinking" disclosure mark — the main answer's activity line and each sub-agent
// child's trace use it so the two reasoning surfaces read identically.
export function BrainGlyph({ className }: { className?: string }): ReactNode {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      aria-hidden="true"
    >
      <path d="M9 3a4 4 0 0 0-3.9 5 3.5 3.5 0 0 0 .4 6.5V17a2 2 0 0 0 2 2h1" />
      <path d="M15 3a4 4 0 0 1 3.9 5 3.5 3.5 0 0 1-.4 6.5V17a2 2 0 0 1-2 2h-1" />
      <path d="M12 4v16" />
    </svg>
  );
}
