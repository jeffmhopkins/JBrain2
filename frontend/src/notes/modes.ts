// Omnibox mode model. The segmented row carries either the main trio
// (Entry / Research / Full Brain) or the entry sub-types
// (Entry / Medical / Financial); tapping Entry while active toggles rows.
// Pure data + a pure transition function so the morph is unit-testable.

export type Mode = "entry" | "research" | "fullbrain" | "medical" | "financial";
export type SegRow = "main" | "sub";

export const ROWS: Record<SegRow, readonly Mode[]> = {
  main: ["entry", "research", "fullbrain"],
  sub: ["entry", "medical", "financial"],
};

export interface SegState {
  row: SegRow;
  mode: Mode;
}

export function tapSegment(state: SegState, tapped: Mode): SegState {
  if (tapped === "entry" && state.mode === "entry") {
    return { row: state.row === "main" ? "sub" : "main", mode: "entry" };
  }
  return { row: state.row, mode: tapped };
}

export interface ModeMeta {
  label: string;
  /** CSS custom property names from tokens.css — never raw colors. */
  color: string;
  tint: string;
  placeholder: string;
  footer: string;
  tool: "clip" | "bolt";
  /** Backend domain code for capture modes; null = Phase 4 conversation modes. */
  domain: "general" | "health" | "finance" | null;
  dest: { path: string; options: readonly string[] } | null;
}

export const MODES: Record<Mode, ModeMeta> = {
  entry: {
    label: "Entry",
    color: "var(--green)",
    tint: "var(--green-tint)",
    placeholder: "Write an entry…",
    footer: "Saved to your wiki · no AI.",
    tool: "clip",
    domain: "general",
    dest: null,
  },
  research: {
    label: "Research",
    color: "var(--amber)",
    tint: "var(--amber-tint)",
    placeholder: "Ask your brain… (read-only)",
    footer: "Read-only — nothing gets written.",
    tool: "bolt",
    domain: null,
    dest: null,
  },
  fullbrain: {
    label: "Full Brain",
    color: "var(--steel)",
    tint: "var(--steel-tint)",
    placeholder: "Talk it out — full tool access…",
    footer: "Full tool access.",
    tool: "clip",
    domain: null,
    dest: null,
  },
  medical: {
    label: "Medical",
    color: "var(--rose)",
    tint: "var(--rose-tint)",
    placeholder: "Log a lab, note, procedure…",
    footer: "Files to notes/medical/ · PDFs staged.",
    tool: "clip",
    domain: "health",
    dest: { path: "notes/medical/", options: ["Records", "Labs", "Medications", "Appointments"] },
  },
  financial: {
    label: "Financial",
    color: "var(--violet)",
    tint: "var(--violet-tint)",
    placeholder: "Log a statement, receipt, transaction…",
    footer: "Files to notes/financial/.",
    tool: "clip",
    domain: "finance",
    dest: { path: "notes/financial/", options: ["Receipts", "Statements", "Accounts"] },
  },
};

/** Stream dot + destination-label colors by backend domain code. */
export const DOMAIN_COLOR: Record<string, string> = {
  general: "var(--green)",
  health: "var(--rose)",
  finance: "var(--violet)",
  location: "var(--steel)",
};

export const DOMAIN_LABEL: Record<string, string> = {
  health: "Medical",
  finance: "Financial",
};
