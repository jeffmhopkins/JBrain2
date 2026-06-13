// Pure display helpers for the property-graph surfaces (analysis tab +
// entity pages). The temper from docs/DESIGN.md applies: keep predicate
// paths, soften the meta (confidence as "96%", lowercase-calm chips).

import type { FactOut } from "../api/client";

/** `predicate[.qualifier]` — the graph address, minus the subject node. */
export function edgePath(predicate: string, qualifier: string | null): string {
  return qualifier ? `${predicate}.${qualifier}` : predicate;
}

/** A {value, unit} quantity for display. Extraction normalizes imperial
 * lengths to inches ({value: 76, unit: "in"}) so the backend's unit-aware
 * equality works — display alone converts back: inch values ≥ 24 (i.e. body
 * heights, never small parts measurements) read as feet'inches". Storage and
 * every other unit (cm, kg, lb, …) stay verbatim. */
export function fmtQuantity(value: number, unit: string): string {
  if (unit.trim().toLowerCase() === "in" && value >= 24) {
    const feet = Math.floor(value / 12);
    return `${feet}'${value - feet * 12}"`;
  }
  return `${value} ${unit}`;
}

/** Render value_json into the edge's value; falls back to the statement. */
export function factValue(fact: FactOut): string {
  // A relationship/object edge's value IS its object node, so render that
  // entity's name — never the statement sentence it's buried in ("I have a
  // wife Celine Hopkins." → "Celine Hopkins"). EdgeValue links it to the node;
  // this keeps every other factValue caller on the same concise value, and is
  // the floor when the object resolved but EdgeValue isn't the renderer.
  if (fact.object_entity_name) return fact.object_entity_name;
  const v = fact.value_json;
  if (typeof v === "string") return v;
  if (typeof v === "number") return String(v);
  if (v !== null && typeof v === "object") {
    const o = v as Record<string, unknown>;
    if (typeof o.systolic === "number" && typeof o.diastolic === "number") {
      return `${o.systolic}/${o.diastolic}${typeof o.unit === "string" ? ` ${o.unit}` : ""}`;
    }
    if (o.value !== undefined) {
      if (typeof o.value === "number" && typeof o.unit === "string") {
        return fmtQuantity(o.value, o.unit);
      }
      return `${String(o.value)}${typeof o.unit === "string" ? ` ${o.unit}` : ""}`;
    }
    // Common single-datum shapes the extractor emits: a name ({"name": "Bella"},
    // {"name": "Jeff Hopkins"}) or a place ({"place": "Denver"}). Without this a
    // populated value_json still fell through to the whole statement sentence.
    for (const key of ["name", "place"] as const) {
      if (typeof o[key] === "string") return o[key] as string;
    }
    // A date-valued state fact (scheduledTime, startDate, …) stores its datum as
    // {start: ISO}; render the concise date/time, not the prose statement.
    if (typeof o.start === "string") {
      const precision = typeof o.precision === "string" ? o.precision : fact.temporal_precision;
      return fmtTemporal(o.start, precision);
    }
  }
  return fact.statement;
}

export function fmtConfidence(confidence: number): string {
  return `${Math.round(confidence * 100)}%`;
}

/** Precision-aware date rendering: month-precision reads "Sep 2026", not a day.
 *
 * Day/month/year/era values are CALENDAR DATES stored at UTC midnight, not
 * instants — rendering them through the browser's local zone shifts a
 * negative-offset user to the previous day ("March 19, 1986" → Mar 18). So
 * every precision except `instant` formats the stored UTC components. */
export function fmtTemporal(iso: string | null, precision: string): string {
  if (iso === null) return "—";
  const d = new Date(iso);
  const timeZone = precision === "instant" ? undefined : "UTC";
  if (precision === "year" || precision === "era") {
    return String(timeZone ? d.getUTCFullYear() : d.getFullYear());
  }
  if (precision === "month") {
    return d.toLocaleDateString(undefined, { month: "short", year: "numeric", timeZone });
  }
  if (precision === "instant") {
    // An instant is a real clock time — show it (local zone), so an appointment
    // reads "Jun 16, 2026, 2:00 PM" instead of just the date. Day/month/year
    // stay date-only below so a calendar date never shows a spurious 12:00 AM.
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  }
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone,
  });
}

/** Collapse temporal tokens that resolve to the SAME instant. The extractor
 * emits a token per date PHRASE plus one per fact temporal, so "Tuesday" and
 * "1400 on Tuesday" become two pills for one time; keep the first per resolved
 * (start, end). Unresolved phrases (null start) stay distinct by phrase. */
export function dedupeTokens<
  T extends { resolved_start: string | null; resolved_end: string | null; surface_phrase: string },
>(tokens: T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const t of tokens) {
    const key =
      t.resolved_start === null ? `p:${t.surface_phrase}` : `${t.resolved_start}|${t.resolved_end}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(t);
  }
  return out;
}

/** A fact's validity span for timeline rails: "Mar 2023 → Jun 2026". */
export function factSpan(fact: FactOut): string {
  const from = fmtTemporal(fact.valid_from, fact.temporal_precision);
  if (fact.valid_to === null) return from;
  return `${from} → ${fmtTemporal(fact.valid_to, fact.temporal_precision)}`;
}
