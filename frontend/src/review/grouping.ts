// Group the pending review list by the entity each item is about, so triage
// reads as "everything held about Celine" instead of a flat chronological wall.
// Entity identity isn't uniform across review kinds — the payload carries it
// under different keys (entity_name, subject, name, entity_ref) and some kinds
// (collisions, merges) name no single subject at all. We read the best-available
// signal per item; anything without a subject collects in the "Other" bucket.

import type { ReviewItem } from "../api/client";
import { type EntityTypeKey, resolveEntityKind } from "../entities/kinds";

export const OTHER_GROUP_KEY = "__other__";

export interface ReviewGroup {
  /** Stable key for collapse state + React keys (normalized label, or Other). */
  key: string;
  /** Subject name as shown on the group header; "Other" for the catch-all. */
  label: string;
  /** Best-effort entity type, driving the type icon; Thing when unknown. */
  kind: EntityTypeKey;
  items: ReviewItem[];
}

/** Title-case an extractor ref slug ("celine_dubois" → "Celine Dubois", "me" →
 * "Me") so a slug-only kind reads like a name on the header. */
function titleCase(ref: string): string {
  return ref
    .split(/[\s_]+/)
    .filter((w) => w.length > 0)
    .map((w) => w[0]?.toUpperCase() + w.slice(1))
    .join(" ");
}

/** The subject this item is about as {label, kind}, or null when its payload
 * names no single entity (attribute_collision, fact_conflict, merge_proposal,
 * domain_promotion, …) — those land in the Other bucket. */
export function reviewSubject(item: ReviewItem): { label: string; kind: EntityTypeKey } | null {
  const p = item.payload;
  const str = (v: unknown): string | null =>
    typeof v === "string" && v.trim().length > 0 ? v.trim() : null;
  const kind: EntityTypeKey =
    typeof p.entity_kind === "string" ? resolveEntityKind(p.entity_kind) : "Thing";
  // entity_name (confirm_entity) and subject (new_predicate, inverse_proposal)
  // and name (ambiguous_mention) are already display names; entity_ref
  // (low_confidence_inference) is a slug, so title-case it.
  const named = str(p.entity_name) ?? str(p.subject) ?? str(p.name);
  if (named !== null) return { label: named, kind };
  const ref = str(p.entity_ref);
  if (ref !== null) return { label: titleCase(ref), kind };
  return null;
}

/** Fold items into per-entity groups, sorted by size (largest first), then
 * name; the Other bucket always sinks to the bottom. Item order within a group
 * is preserved (the backend's newest-first ordering). */
export function groupByEntity(items: ReviewItem[]): ReviewGroup[] {
  const groups = new Map<string, ReviewGroup>();
  for (const item of items) {
    const subject = reviewSubject(item);
    const key = subject === null ? OTHER_GROUP_KEY : subject.label.toLowerCase();
    const existing = groups.get(key);
    if (existing === undefined) {
      groups.set(key, {
        key,
        label: subject?.label ?? "Other",
        kind: subject?.kind ?? "Thing",
        items: [item],
      });
    } else {
      existing.items.push(item);
      // A later item may know a concrete type the first one didn't.
      if (existing.kind === "Thing" && subject !== null && subject.kind !== "Thing")
        existing.kind = subject.kind;
    }
  }
  return [...groups.values()].sort((a, b) => {
    if (a.key === OTHER_GROUP_KEY) return 1;
    if (b.key === OTHER_GROUP_KEY) return -1;
    if (b.items.length !== a.items.length) return b.items.length - a.items.length;
    return a.label.localeCompare(b.label);
  });
}
