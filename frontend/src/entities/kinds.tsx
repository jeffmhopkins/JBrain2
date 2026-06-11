// Entity-type icons + per-type accent (docs/DESIGN.md "Entity-type accents").
// Entity.kind is free text (schema.org-guided), so we normalize to a small set
// of canonical types and fall back to Thing for anything unrecognized. The disc
// is tinted by type; the surrounding row's dot still carries the domain.

import type { CSSProperties, ReactElement } from "react";
import {
  AnimalIcon,
  ConditionIcon,
  CreativeWorkIcon,
  DrugIcon,
  EventIcon,
  type IconProps,
  OrgIcon,
  PersonIcon,
  PlaceIcon,
  ProcedureIcon,
  ProductIcon,
  ThingIcon,
} from "../components/icons";

export type EntityTypeKey =
  | "Person"
  | "Organization"
  | "Place"
  | "Event"
  | "Product"
  | "Animal"
  | "CreativeWork"
  | "MedicalCondition"
  | "MedicalProcedure"
  | "Drug"
  | "Thing";

/** Per-type accent as a token reference — no raw hex outside tokens.css. */
export const ENTITY_TYPE_COLOR: Record<EntityTypeKey, string> = {
  Person: "var(--steel)",
  Organization: "var(--violet)",
  Place: "var(--green)",
  Event: "var(--amber)",
  Product: "var(--periwinkle)",
  Animal: "var(--sage)",
  CreativeWork: "var(--rose)",
  MedicalCondition: "var(--terracotta)",
  MedicalProcedure: "var(--teal)",
  Drug: "var(--orchid)",
  Thing: "var(--slate)",
};

const GLYPH: Record<EntityTypeKey, (p: IconProps) => ReactElement> = {
  Person: PersonIcon,
  Organization: OrgIcon,
  Place: PlaceIcon,
  Event: EventIcon,
  Product: ProductIcon,
  Animal: AnimalIcon,
  CreativeWork: CreativeWorkIcon,
  MedicalCondition: ConditionIcon,
  MedicalProcedure: ProcedureIcon,
  Drug: DrugIcon,
  Thing: ThingIcon,
};

// Fold the schema.org canonicals plus the synonyms the extractor tends to
// emit (snake_case, plurals, domain shorthand) onto a canonical key. Match is
// on the alphanumeric-only, lowercased form, so "MedicalCondition",
// "medical_condition", and "Medical Condition" all land together.
const ALIASES: Record<string, EntityTypeKey> = {
  person: "Person",
  people: "Person",
  individual: "Person",
  human: "Person",
  patient: "Person",
  organization: "Organization",
  organisation: "Organization",
  org: "Organization",
  company: "Organization",
  institution: "Organization",
  group: "Organization",
  team: "Organization",
  clinic: "Organization",
  hospital: "Organization",
  place: "Place",
  location: "Place",
  gpe: "Place",
  city: "Place",
  region: "Place",
  country: "Place",
  address: "Place",
  venue: "Place",
  event: "Event",
  occasion: "Event",
  meeting: "Event",
  appointment: "Event",
  product: "Product",
  vehicle: "Product",
  device: "Product",
  gadget: "Product",
  technology: "Product",
  animal: "Animal",
  pet: "Animal",
  dog: "Animal",
  cat: "Animal",
  species: "Animal",
  creature: "Animal",
  creativework: "CreativeWork",
  book: "CreativeWork",
  article: "CreativeWork",
  film: "CreativeWork",
  movie: "CreativeWork",
  song: "CreativeWork",
  album: "CreativeWork",
  paper: "CreativeWork",
  document: "CreativeWork",
  medicalcondition: "MedicalCondition",
  condition: "MedicalCondition",
  diagnosis: "MedicalCondition",
  disease: "MedicalCondition",
  symptom: "MedicalCondition",
  illness: "MedicalCondition",
  medicalprocedure: "MedicalProcedure",
  procedure: "MedicalProcedure",
  surgery: "MedicalProcedure",
  operation: "MedicalProcedure",
  test: "MedicalProcedure",
  scan: "MedicalProcedure",
  lab: "MedicalProcedure",
  drug: "Drug",
  medication: "Drug",
  medicine: "Drug",
  supplement: "Drug",
  thing: "Thing",
  object: "Thing",
  other: "Thing",
  unknown: "Thing",
  misc: "Thing",
};

/** Map a free-text kind onto a canonical type; Thing is the safe fallback. */
export function resolveEntityKind(kind: string): EntityTypeKey {
  return ALIASES[kind.toLowerCase().replace(/[^a-z0-9]/g, "")] ?? "Thing";
}

interface EntityTypeIconProps {
  kind: string;
  /** Disc diameter in px; the glyph scales to ~56% of it. */
  size?: number;
}

/** Type-tinted disc with the type's glyph, for entity rows and the hub. */
export function EntityTypeIcon({ kind, size = 32 }: EntityTypeIconProps) {
  const key = resolveEntityKind(kind);
  const Glyph = GLYPH[key];
  // --etype drives both the disc tint (color-mix in CSS) and the glyph color.
  const style = { "--etype": ENTITY_TYPE_COLOR[key], width: size, height: size } as CSSProperties;
  return (
    <span className="etype-disc" data-entity-kind={key} style={style}>
      <Glyph size={Math.round(size * 0.56)} />
    </span>
  );
}
