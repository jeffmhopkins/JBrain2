// The tool-view component registry: a fixed map from a `view` name to a
// first-party React component, and <ToolView> which renders the named component
// from a ViewPayload — or NOTHING if the name is unknown. This is invariant #1/#9
// (DESIGN.md "Agent tool views"): model output never authors markup; it only
// selects a registered component and fills its data-only slots. Adding a
// component is a deliberate change here, like adding a tool.

import type { ReactNode } from "react";
import type { CitationRef, ViewPayload } from "../types";

export interface ViewProps {
  data: Record<string, unknown>;
  refs: CitationRef[];
}

// Tone/flag is an enum, never a color (DESIGN.md): the component maps it to a
// class so the theme owns the palette.
type Tone = "neutral" | "good" | "warn" | "bad";
const TONES = new Set<Tone>(["neutral", "good", "warn", "bad"]);
function toneOf(value: unknown): Tone {
  return typeof value === "string" && TONES.has(value as Tone) ? (value as Tone) : "neutral";
}

function asStrings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

/** A simple labelled figure: `{label, value, unit?, tone?}`. */
function StatBlock({ data }: ViewProps): ReactNode {
  const unit = typeof data.unit === "string" ? data.unit : "";
  return (
    <div className={`tv-stat tone-${toneOf(data.tone)}`}>
      <div className="tv-stat-value">
        {String(data.value ?? "")}
        {unit && <span className="tv-stat-unit">{unit}</span>}
      </div>
      <div className="tv-stat-label">{String(data.label ?? "")}</div>
    </div>
  );
}

/** A read-only grid: `{columns: string[], rows: string[][]}`. */
function DataTable({ data }: ViewProps): ReactNode {
  const columns = asStrings(data.columns);
  const rows = Array.isArray(data.rows) ? data.rows : [];
  return (
    <table className="tv-table">
      {columns.length > 0 && (
        <thead>
          <tr>
            {columns.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
      )}
      <tbody>
        {rows.map((row, r) => (
          // Rows are positional data with no stable id; index is the only key.
          // biome-ignore lint/suspicious/noArrayIndexKey: positional table rows
          <tr key={r}>
            {asStrings(row).map((cell, c) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: positional cells
              <td key={c}>{cell}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

interface ChecklistItem {
  id: string;
  body: string;
  checked: boolean;
}

function asItems(value: unknown): ChecklistItem[] {
  if (!Array.isArray(value)) return [];
  return value.map((it) => {
    const o = (it ?? {}) as Record<string, unknown>;
    return { id: String(o.id ?? ""), body: String(o.body ?? ""), checked: Boolean(o.checked) };
  });
}

/** A read-only checklist: `{title, items: [{id, body, checked}]}` — the owner's
 * list, rendered as full-bleed rows with checked state (DESIGN.md "Lists"). */
function ListCard({ data }: ViewProps): ReactNode {
  const items = asItems(data.items);
  return (
    <div className="tv-list">
      <div className="tv-list-head">{String(data.title ?? "List")}</div>
      <ul className="tv-list-items">
        {items.map((it, i) => (
          <li
            // Item ids are stable; the index only backs the rare empty-id row.
            key={it.id || i}
            className={`tv-list-row${it.checked ? " checked" : ""}`}
          >
            <span className="tv-list-box" aria-hidden="true" />
            <span className="tv-list-body">{it.body}</span>
          </li>
        ))}
        {items.length === 0 && <li className="tv-list-empty">empty</li>}
      </ul>
    </div>
  );
}

function refKey(ref: CitationRef): string {
  if (ref.kind === "fact") return `fact:${ref.fact_id}`;
  if (ref.kind === "entity") return `entity:${ref.entity_id}`;
  return `note:${ref.note_id}`;
}

/** Pointer-not-copy citation chips from the payload's refs (hover-cards later). */
function CitationCard({ data, refs }: ViewProps): ReactNode {
  return (
    <div className="tv-citations">
      {typeof data.title === "string" && <div className="tv-citations-title">{data.title}</div>}
      <div className="tv-citation-chips">
        {refs.map((ref) => (
          <span key={refKey(ref)} className={`tv-cite kind-${ref.kind}`}>
            {ref.label}
          </span>
        ))}
      </div>
    </div>
  );
}

const REGISTRY: Record<string, (props: ViewProps) => ReactNode> = {
  stat_block: StatBlock,
  data_table: DataTable,
  citation_card: CitationCard,
  list_card: ListCard,
};

export function isKnownView(name: string): boolean {
  return name in REGISTRY;
}

/** Render a tool-result view from its payload, or nothing if the named component
 * is not registered (an unknown `view` is rejected, never rendered). */
export function ToolView({ payload }: { payload: ViewPayload }): ReactNode {
  const Component = REGISTRY[payload.view];
  if (!Component) return null;
  return (
    <div className={`tool-view surface-${payload.surface}`}>
      <Component data={payload.data} refs={payload.refs} />
    </div>
  );
}
