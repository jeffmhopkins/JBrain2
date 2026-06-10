// Pure display helpers for the property-graph surfaces (analysis tab +
// entity pages). The temper from docs/DESIGN.md applies: keep predicate
// paths, soften the meta (confidence as "96%", lowercase-calm chips).

import type { FactOut } from "../api/client";

/** `predicate[.qualifier]` — the graph address, minus the subject node. */
export function edgePath(predicate: string, qualifier: string | null): string {
  return qualifier ? `${predicate}.${qualifier}` : predicate;
}

/** Render value_json into the edge's value; falls back to the statement. */
export function factValue(fact: FactOut): string {
  const v = fact.value_json;
  if (typeof v === "string") return v;
  if (typeof v === "number") return String(v);
  if (v !== null && typeof v === "object") {
    const o = v as Record<string, unknown>;
    if (typeof o.systolic === "number" && typeof o.diastolic === "number") {
      return `${o.systolic}/${o.diastolic}${typeof o.unit === "string" ? ` ${o.unit}` : ""}`;
    }
    if (o.value !== undefined) {
      return `${String(o.value)}${typeof o.unit === "string" ? ` ${o.unit}` : ""}`;
    }
  }
  return fact.statement;
}

export function fmtConfidence(confidence: number): string {
  return `${Math.round(confidence * 100)}%`;
}

/** Precision-aware date rendering: month-precision reads "Sep 2026", not a day. */
export function fmtTemporal(iso: string | null, precision: string): string {
  if (iso === null) return "—";
  const d = new Date(iso);
  if (precision === "year" || precision === "era") return String(d.getFullYear());
  if (precision === "month") {
    return d.toLocaleDateString(undefined, { month: "short", year: "numeric" });
  }
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

/** A fact's validity span for timeline rails: "Mar 2023 → Jun 2026". */
export function factSpan(fact: FactOut): string {
  const from = fmtTemporal(fact.valid_from, fact.temporal_precision);
  if (fact.valid_to === null) return from;
  return `${from} → ${fmtTemporal(fact.valid_to, fact.temporal_precision)}`;
}
