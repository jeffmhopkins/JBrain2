// In-memory fixture backend for `npm run dev:mock` (VITE_MOCK=1).
// Mirrors the real API contract closely enough for UI work: idempotent
// note creation, cursor pagination, multipart attachments, always-on auth.

import type {
  AttachmentOut,
  ContainerStatus,
  EntityOut,
  FactOut,
  LlmUsage,
  NoteAnalysis,
  NoteOut,
  Principal,
  ReviewItem,
  SearchMatch,
  SearchResult,
} from "./client";

const PRINCIPAL: Principal = {
  principal_id: "mock-owner",
  kind: "owner_device",
  label: "Mock device",
};

const mockUpdate = { state: "none" as "none" | "running" | "exited", ticks: 0 };
const mockExport = { state: "none" as "none" | "running" | "exited", ticks: 0 };
const mockImport = { state: "none" as "none" | "running" | "exited", ticks: 0 };

const CONTAINERS: ContainerStatus[] = [
  {
    service: "api",
    state: "running",
    health: "healthy",
    started_at: new Date(Date.now() - 36e5 * 5).toISOString(),
    image: "jbrain/api:edge",
  },
  {
    service: "postgres",
    state: "running",
    health: "healthy",
    started_at: new Date(Date.now() - 36e5 * 30).toISOString(),
    image: "postgres:17",
  },
  {
    service: "worker",
    state: "exited",
    health: null,
    started_at: null,
    image: "jbrain/worker:edge",
  },
];

let nextId = 1;
function id(prefix: string): string {
  return `${prefix}-${nextId++}`;
}

const attachmentBlobs = new Map<string, Blob>();

function makeAttachment(filename: string, mediaType: string): AttachmentOut {
  const att = {
    id: id("att"),
    filename,
    media_type: mediaType,
    size_bytes: 24_120,
  };
  attachmentBlobs.set(att.id, new Blob([`mock contents of ${filename}`], { type: mediaType }));
  return att;
}

function daysAgo(days: number, hour: number, minute = 0): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  d.setHours(hour, minute, 0, 0);
  return d.toISOString();
}

function seedNote(
  domain: string,
  destination: string | null,
  body: string,
  createdAt: string,
  attachments: AttachmentOut[] = [],
  ingestState = "indexed",
): NoteOut {
  return {
    id: id("note"),
    client_id: id("client"),
    domain,
    destination,
    body,
    created_at: createdAt,
    tz_offset_minutes: null,
    ingest_state: ingestState,
    attachments,
    latitude: null,
    longitude: null,
    accuracy_m: null,
  };
}

// Oldest-first internally; the list endpoint serves newest-first.
const notes: NoteOut[] = [
  seedNote(
    "general",
    null,
    "Dad: grandpa worked at the mill in Ohio before the war — family history follow-up",
    daysAgo(8, 19, 12),
  ),
  seedNote(
    "finance",
    "Statements",
    "Q2 brokerage statement filed — rebalance overdue",
    daysAgo(8, 20, 40),
    [makeAttachment("brokerage-q2.pdf", "application/pdf")],
  ),
  seedNote(
    "health",
    "Medications",
    "Started vitamin D 2000 IU daily per Dr. Akin",
    daysAgo(3, 8, 5),
  ),
  seedNote("general", null, "Book rec from Sam: The Beginning of Infinity", daysAgo(3, 13, 30)),
  seedNote(
    "finance",
    "Receipts",
    "Roof repair quote — $4,200 incl. flashing. Get second opinion?",
    daysAgo(1, 10, 22),
    [makeAttachment("roof-quote.jpg", "image/jpeg")],
  ),
  seedNote("general", null, "Garage door keypad battery replaced — CR2032", daysAgo(1, 17, 48)),
  seedNote(
    "general",
    null,
    "Groceries: eggs, coffee, olive oil, that bread Mom liked",
    daysAgo(0, 8, 15),
  ),
  seedNote(
    "health",
    "Labs",
    "Annual physical — BP 118/76. Lab orders attached.\n\nDr. Akin wants a follow-up fasting panel in 3 months; book the draw early morning. Ask about the vitamin D dose then too.",
    daysAgo(0, 10, 5),
    [makeAttachment("lab-orders.pdf", "application/pdf")],
    "failed",
  ),
  seedNote(
    "general",
    null,
    "Long-form capture to exercise the 3-line clamp: the contractor said the south fence posts are rotted at the base, the gate hinge needs a longer lag bolt, and the section behind the shed should really be replaced whole rather than patched — he can quote both options next week, but materials prices change monthly so don't sit on it.",
    daysAgo(0, 11, 40),
  ),
  seedNote(
    "general",
    null,
    "Call the dentist about the crown — left side",
    daysAgo(0, 12, 10),
    [],
    "pending",
  ),
];

// The one fully-analyzed fixture note: drives the Analysis tab, the entity
// pages it links to, and several review-inbox items.
const PATEL_BODY =
  "Saw Dr. Patel this morning — BP 128/82, she wants a follow-up in three months (September). " +
  "Sarah drove me over; she's mostly moved into the new Denver place now. " +
  "Patel says keep up the morning walks.";

const patelNote = seedNote("health", "Records", PATEL_BODY, daysAgo(0, 9, 40));
notes.push(patelNote);

// ===== Phase 3 fixtures: analysis, entities, review, usage =====

// The backend serves "provider:model" here, nothing fancier.
const EXTRACTOR = "xai:grok-4.3";

function fact(over: Partial<FactOut> & Pick<FactOut, "id" | "predicate">): FactOut {
  return {
    entity_id: "ent-me",
    entity_name: "Me",
    qualifier: null,
    kind: "state",
    statement: "",
    value_json: null,
    assertion: "asserted",
    status: "active",
    pinned: false,
    confidence: 0.9,
    valid_from: patelNote.created_at,
    valid_to: null,
    reported_at: patelNote.created_at,
    temporal_precision: "day",
    source_snippet: null,
    ...over,
  };
}

const FACT_BP = fact({
  id: "fact-bp-0610",
  predicate: "blood_pressure",
  kind: "measurement",
  statement: "Blood pressure measured 128/82 mmHg at Dr. Patel's office on June 10, 2026.",
  value_json: { systolic: 128, diastolic: 82, unit: "mmHg" },
  confidence: 0.97,
  temporal_precision: "instant",
  source_snippet: "Saw Dr. Patel this morning — <mark>BP 128/82</mark>, she wants a follow-up",
});

const FACT_VISIT = fact({
  id: "fact-visit-0610",
  predicate: "medical_visit",
  kind: "event",
  statement: "Office visit with Dr. Patel on June 10, 2026.",
  value_json: "office visit — Dr. Patel",
  confidence: 0.95,
  source_snippet: "<mark>Saw Dr. Patel this morning</mark> — BP 128/82",
});

const FACT_FOLLOWUP = fact({
  id: "fact-followup-time",
  entity_id: "ent-followup",
  entity_name: "Dr. Patel follow-up",
  predicate: "scheduled_time",
  kind: "state",
  statement: "The Dr. Patel follow-up is expected in September 2026.",
  value_json: "Sep 2026",
  assertion: "expected",
  confidence: 0.93,
  valid_from: "2026-09-01T00:00:00Z",
  temporal_precision: "month",
  source_snippet: "she wants a <mark>follow-up in three months (September)</mark>",
});

const FACT_SARAH_DENVER = fact({
  id: "fact-sarah-addr-denver",
  entity_id: "ent-sarah",
  entity_name: "Sarah",
  predicate: "address",
  qualifier: "home",
  kind: "state",
  statement: "Sarah's home address is in Denver, CO as of June 2026.",
  value_json: "Denver, CO",
  status: "pending_review",
  confidence: 0.88,
  valid_from: "2026-06-01T00:00:00Z",
  temporal_precision: "month",
  source_snippet:
    "Sarah drove me over; she's mostly <mark>moved into the new Denver place</mark> now",
});

const FACT_PHYSICIAN = fact({
  id: "fact-me-physician",
  predicate: "physician",
  kind: "relationship",
  statement: "Dr. Patel is Jeff's physician.",
  value_json: "Dr. Patel",
  pinned: true,
  confidence: 0.99,
  valid_from: "2025-11-02T00:00:00Z",
  temporal_precision: "month",
  source_snippet: "<mark>Saw Dr. Patel</mark> this morning — BP 128/82",
});

const FACT_WALKS = fact({
  id: "fact-me-walks",
  predicate: "preferred_exercise",
  kind: "preference",
  statement: "Jeff keeps up morning walks, per Dr. Patel.",
  value_json: "morning walks",
  assertion: "reported",
  confidence: 0.82,
  source_snippet: "Patel says keep up the <mark>morning walks</mark>",
});

const ANALYSES: Record<string, NoteAnalysis> = {
  [patelNote.id]: {
    note_id: patelNote.id,
    title: "Dr. Patel visit — BP 128/82, follow-up in September",
    tags: ["blood-pressure", "dr-patel", "follow-up", "sarah"],
    analyzed_at: daysAgo(0, 9, 43),
    extractor: EXTRACTOR,
    facts: [FACT_BP, FACT_VISIT, FACT_FOLLOWUP, FACT_SARAH_DENVER, FACT_PHYSICIAN, FACT_WALKS],
    entities: [
      { id: "ent-me", kind: "Person", name: "Me", status: "active" },
      { id: "ent-patel", kind: "Person", name: "Dr. Patel", status: "active" },
      { id: "ent-sarah", kind: "Person", name: "Sarah", status: "active" },
      {
        id: "ent-followup",
        kind: "appointment",
        name: "Dr. Patel follow-up",
        status: "provisional",
      },
    ],
    temporal_tokens: [
      {
        id: "tok-followup",
        surface_phrase: "in three months (September)",
        kind: "point",
        resolved_start: "2026-09-01T00:00:00Z",
        resolved_end: null,
        temporal_precision: "month",
      },
      {
        id: "tok-this-morning",
        surface_phrase: "this morning",
        kind: "point",
        resolved_start: patelNote.created_at,
        resolved_end: null,
        temporal_precision: "day",
      },
    ],
  },
};

const FACT_SARAH_AUSTIN = fact({
  id: "fact-sarah-addr-austin",
  entity_id: "ent-sarah",
  entity_name: "Sarah",
  predicate: "address",
  qualifier: "home",
  kind: "state",
  statement: "Sarah's home address was in Austin, TX from March 2023 to June 2026.",
  value_json: "Austin, TX",
  status: "superseded",
  confidence: 0.94,
  valid_from: "2023-03-01T00:00:00Z",
  valid_to: "2026-06-01T00:00:00Z",
  reported_at: "2023-03-12T18:20:00Z",
  temporal_precision: "month",
  source_snippet: "helped Sarah move the last boxes into the <mark>Austin apartment</mark>",
});

const FACT_SARAH_EMPLOYER = fact({
  id: "fact-sarah-employer",
  entity_id: "ent-sarah",
  entity_name: "Sarah",
  predicate: "worksFor",
  kind: "relationship",
  statement: "Sarah works for Ridgeline Architects.",
  value_json: "Ridgeline Architects",
  confidence: 0.91,
  valid_from: "2024-01-08T00:00:00Z",
  reported_at: "2024-01-08T19:00:00Z",
  temporal_precision: "month",
  source_snippet: "Sarah started at <mark>Ridgeline Architects</mark> this week",
});

const FACT_SARAH_BDAY = fact({
  id: "fact-sarah-bday",
  entity_id: "ent-sarah",
  entity_name: "Sarah",
  predicate: "birthDate",
  kind: "attribute",
  statement: "Sarah's birthday is March 14, 1988.",
  value_json: "March 14, 1988",
  confidence: 0.96,
  valid_from: null,
  reported_at: "2025-03-14T16:00:00Z",
  temporal_precision: "day",
  source_snippet: "card in the mail for <mark>Sarah's birthday on the 14th</mark>",
});

const FACT_BP_OLD = fact({
  id: "fact-bp-0607",
  predicate: "blood_pressure",
  kind: "measurement",
  statement: "Blood pressure measured 118/76 mmHg at the annual physical.",
  value_json: { systolic: 118, diastolic: 76, unit: "mmHg" },
  confidence: 0.95,
  valid_from: daysAgo(3, 10, 5),
  reported_at: daysAgo(3, 10, 5),
  temporal_precision: "instant",
  source_snippet: "Annual physical — <mark>BP 118/76</mark>. Lab orders attached.",
});

const FACT_PATEL_SPECIALTY = fact({
  id: "fact-patel-specialty",
  entity_id: "ent-patel",
  entity_name: "Dr. Patel",
  predicate: "medicalSpecialty",
  kind: "attribute",
  statement: "Dr. Patel practices internal medicine.",
  value_json: "internal medicine",
  confidence: 0.89,
  valid_from: null,
  reported_at: "2025-11-02T15:00:00Z",
  source_snippet: "new <mark>internal medicine</mark> doc — Dr. Patel",
});

const ENTITIES: Record<string, EntityOut> = {
  "ent-me": {
    id: "ent-me",
    kind: "Person",
    canonical_name: "Me",
    status: "active",
    aliases: ["Jeff"],
    domain: "general",
    predicates: [
      {
        predicate: "blood_pressure",
        qualifier: null,
        current: FACT_BP,
        history: [FACT_BP, FACT_BP_OLD],
      },
      {
        predicate: "physician",
        qualifier: null,
        current: FACT_PHYSICIAN,
        history: [FACT_PHYSICIAN],
      },
      {
        predicate: "preferred_exercise",
        qualifier: null,
        current: FACT_WALKS,
        history: [FACT_WALKS],
      },
    ],
    inbound: [
      {
        entity_id: "ent-sarah",
        name: "Sarah",
        predicate: "sibling",
        statement: "Sarah is Jeff's sister.",
      },
    ],
    mentions: [
      {
        note_id: patelNote.id,
        snippet: "<mark>Saw Dr. Patel this morning</mark> — BP 128/82",
        created_at: patelNote.created_at,
      },
    ],
  },
  "ent-patel": {
    id: "ent-patel",
    kind: "Person",
    canonical_name: "Dr. Patel",
    status: "active",
    aliases: ["Patel"],
    domain: "health",
    predicates: [
      {
        predicate: "medicalSpecialty",
        qualifier: null,
        current: FACT_PATEL_SPECIALTY,
        history: [FACT_PATEL_SPECIALTY],
      },
    ],
    inbound: [
      {
        entity_id: "ent-me",
        name: "Me",
        predicate: "physician",
        statement: "Dr. Patel is Jeff's physician.",
      },
    ],
    mentions: [
      {
        note_id: patelNote.id,
        snippet: "Saw <mark>Dr. Patel</mark> this morning — BP 128/82",
        created_at: patelNote.created_at,
      },
    ],
  },
  "ent-sarah": {
    id: "ent-sarah",
    kind: "Person",
    canonical_name: "Sarah Hopkins",
    status: "active",
    aliases: ["Sarah", "sis"],
    domain: "general",
    predicates: [
      {
        predicate: "address",
        qualifier: "home",
        current: FACT_SARAH_DENVER,
        history: [FACT_SARAH_DENVER, FACT_SARAH_AUSTIN],
      },
      {
        predicate: "worksFor",
        qualifier: null,
        current: FACT_SARAH_EMPLOYER,
        history: [FACT_SARAH_EMPLOYER],
      },
      {
        predicate: "birthDate",
        qualifier: null,
        current: FACT_SARAH_BDAY,
        history: [FACT_SARAH_BDAY],
      },
    ],
    inbound: [
      {
        entity_id: "ent-me",
        name: "Me",
        predicate: "sibling",
        statement: "Sarah is Jeff's sister.",
      },
    ],
    mentions: [
      {
        note_id: patelNote.id,
        snippet:
          "<mark>Sarah</mark> drove me over; she's mostly moved into the new Denver place now.",
        created_at: patelNote.created_at,
      },
      {
        note_id: "note-archived-77",
        snippet: "Helped <mark>Sarah</mark> move the last boxes out of the Austin apartment.",
        created_at: "2026-03-02T17:40:00Z",
      },
    ],
  },
  "ent-followup": {
    id: "ent-followup",
    kind: "appointment",
    canonical_name: "Dr. Patel follow-up",
    status: "provisional",
    aliases: [],
    domain: "health",
    predicates: [
      {
        predicate: "scheduled_time",
        qualifier: null,
        current: FACT_FOLLOWUP,
        history: [FACT_FOLLOWUP],
      },
    ],
    inbound: [],
    mentions: [
      {
        note_id: patelNote.id,
        snippet: "she wants a <mark>follow-up in three months (September)</mark>",
        created_at: patelNote.created_at,
      },
    ],
  },
};

// Review queue: one of every kind, payloads mirroring what the backend
// writes at item creation: the row ids its resolution handlers read plus
// the display fields the card renders (summary / snippet / outcomes /
// choices / *_destructive flags). Invariant shared with the backend: every
// advertised choice action and outcome verb is exactly an action
// POST /review/{id}/resolve accepts — collisions resolve through
// accept_a/accept_b choices and advertise no footer verbs.
interface MockReviewItem extends ReviewItem {
  open: boolean;
}

const REVIEW_ITEMS: MockReviewItem[] = [
  {
    id: "rev-1",
    kind: "attribute_collision",
    domain: "general",
    created_at: daysAgo(0, 9, 45),
    open: true,
    payload: {
      fact_a: "fact-sarah-bday-1990",
      fact_b: FACT_SARAH_BDAY.id,
      predicate: "birthDate",
      note_id: patelNote.id,
      summary: "two values recorded for Sarah's birthDate",
      snippet: "card in the mail for <mark>Sarah's birthday on the 14th</mark>",
      choices: [
        { action: "accept_a", label: "May 2, 1990", detail: "previously recorded" },
        { action: "accept_b", label: "March 14, 1988", detail: "from this note" },
      ],
    },
  },
  {
    id: "rev-2",
    kind: "merge_proposal",
    domain: "general",
    created_at: daysAgo(1, 12, 0),
    open: true,
    payload: {
      entity_a: "ent-robert-chen",
      entity_b: "ent-bob",
      summary: "are “Bob” and “Robert Chen” the same person?",
      snippet:
        "Lunch with <mark>Bob</mark> — he's pitching the Donnelly account again next quarter.",
      outcomes: {
        accept:
          "bob and robert chen become one person — mentions repoint; a later split can undo it.",
        reject: "writes a permanent distinct-from edge — this pair is never proposed again.",
      },
      reject_destructive: true,
    },
  },
  {
    id: "rev-3",
    kind: "ambiguous_mention",
    domain: "general",
    created_at: daysAgo(1, 10, 30),
    open: true,
    payload: {
      name: "Sam",
      note_id: "note-archived-91",
      entity_ids: ["ent-sam-rivera", "ent-sam-okafor"],
      summary: "which Sam?",
      snippet: "<mark>Sam</mark> said the roof quote covers the flashing too.",
      // accept is not advertised: linking a pick needs layer-2/3 resolution.
      outcomes: {
        reject: "the mention stays unlinked — it can be re-proposed with more signal.",
      },
    },
  },
  {
    id: "rev-4",
    kind: "domain_promotion",
    domain: "health",
    created_at: daysAgo(2, 8, 15),
    open: true,
    payload: {
      fact_id: "fact-akin-fax",
      note_id: "note-archived-88",
      note_domain: "health",
      proposed_domain: "general",
      summary: "this faxRequest fact may belong in general, not health",
      snippet: "Asked <mark>Dr. Akin</mark>'s office to fax the form to the school nurse.",
      outcomes: {
        accept: "the fact moves to general and is pinned there — reprocessing can't pull it back.",
        reject: "the fact stays in health — the note's firewall keeps it.",
      },
    },
  },
  {
    id: "rev-5",
    kind: "fact_conflict",
    domain: "health",
    created_at: daysAgo(0, 11, 5),
    open: true,
    payload: {
      fact_a: FACT_BP.id,
      fact_b: "fact-bp-kiosk",
      predicate: "blood_pressure",
      note_id: patelNote.id,
      summary: "two blood_pressure values disagree for Me",
      snippet:
        "Pharmacy kiosk says <mark>138/92</mark> — way off this morning's 128/82 at Dr. Patel's.",
      choices: [
        { action: "accept_a", label: "128/82 mmHg", detail: "previously recorded" },
        { action: "accept_b", label: "138/92 mmHg", detail: "from this note" },
      ],
    },
  },
  // rev-6/rev-7: kinds the schema reserves but no pipeline writes yet; they
  // keep the card's rarer states (low-confidence copy, destructive accept)
  // exercised in mock mode and follow the same payload convention.
  {
    id: "rev-6",
    kind: "low_confidence",
    domain: "health",
    created_at: daysAgo(3, 8, 10),
    open: true,
    payload: {
      summary: "low-confidence extraction (41%)",
      snippet: "Started <mark>vitamin D 2000 IU daily</mark> per Dr. Akin.",
      outcomes: {
        accept: "the fact stands and gets pinned — reprocessing can't drop it.",
        reject: "the fact is retracted as a misread.",
      },
    },
  },
  {
    id: "rev-7",
    kind: "split_proposal",
    domain: "finance",
    created_at: daysAgo(4, 18, 0),
    open: true,
    payload: {
      summary: "“the Honda” may be two different cars",
      snippet: "Oil change on <mark>the Honda</mark> — 152k miles now, the other receipt said 48k.",
      entity_name: "the Honda",
      outcomes: {
        accept: "the entity splits into two vehicles — mentions re-resolve from their spans.",
        reject: "stays one car; the mileage conflict goes back to fact review.",
      },
      accept_destructive: true,
    },
  },
];

const LLM_USAGE: LlmUsage = {
  today: { input_tokens: 41_200, output_tokens: 12_400, cost_usd: 0.08 },
  month: { input_tokens: 1_240_000, output_tokens: 338_000, cost_usd: 2.41 },
  by_task: [
    { task: "note.extract", input_tokens: 982_000, output_tokens: 241_000, cost_usd: 1.83 },
    { task: "entity.disambiguate", input_tokens: 141_000, output_tokens: 52_000, cost_usd: 0.31 },
    { task: "fact.adjudicate", input_tokens: 88_400, output_tokens: 31_200, cost_usd: 0.21 },
    // A local model with no price-table entry: tokens only, never a guess.
    { task: "vision.ocr", input_tokens: 2_400_000, output_tokens: 14_000, cost_usd: null },
  ],
  days: Array.from({ length: 7 }, (_, i) => ({
    date: daysAgo(6 - i, 0).slice(0, 10),
    input_tokens: 120_000 + i * 31_000,
    output_tokens: 34_000 + i * 9_000,
    cost_usd: i === 2 ? null : 0.21 + i * 0.04,
  })),
};

function emptyAnalysis(noteId: string): NoteAnalysis {
  return {
    note_id: noteId,
    title: null,
    tags: [],
    analyzed_at: null,
    extractor: null,
    facts: [],
    entities: [],
    temporal_tokens: [],
  };
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const LATENCY_MS = 120;
const sleep = () => new Promise((resolve) => setTimeout(resolve, LATENCY_MS));

const VALID_DOMAINS = new Set(["general", "health", "finance", "location"]);

// Fake passage search over the note fixtures: substring match per term, a
// literal <mark> around the first hit (exercising the UI's mark-splitting),
// and a rotating match badge. `degraded!` anywhere in the query flips the
// keyword-only degraded banner on.
function mockSearch(params: URLSearchParams): { degraded: boolean; results: SearchResult[] } {
  const rawQ = params.get("q") ?? "";
  const domain = params.get("domain");
  const limit = Number(params.get("limit") ?? "20");
  const degraded = rawQ.includes("degraded!");
  const q = rawQ.replace(/degraded!/g, "").trim();
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);

  const matches = notes.filter((n) => {
    if (domain && n.domain !== domain) return false;
    if (terms.length === 0) return true;
    const body = n.body.toLowerCase();
    return terms.some((t) => body.includes(t));
  });

  const badges: SearchMatch[] = ["semantic", "keyword", "both"];
  const results = matches.slice(0, limit).map((n, i): SearchResult => {
    const term = terms.find((t) => n.body.toLowerCase().includes(t));
    const at = term ? n.body.toLowerCase().indexOf(term) : -1;
    const snippet =
      at >= 0 && term
        ? `${n.body.slice(Math.max(0, at - 60), at)}<mark>${n.body.slice(at, at + term.length)}</mark>${n.body.slice(at + term.length, at + term.length + 80)}`
        : n.body.slice(0, 140);
    const fromAttachment = n.attachments.length > 0 && i % 2 === 1;
    return {
      note_id: n.id,
      chunk_id: id("chunk"),
      snippet,
      match: degraded ? "keyword" : (badges[i % badges.length] ?? "keyword"),
      score: 1 - i * 0.07,
      domain: n.domain,
      destination: n.destination,
      created_at: n.created_at,
      body_preview: n.body.slice(0, 120),
      attachment_count: n.attachments.length,
      source_kind: fromAttachment ? "attachment" : "note",
      source_anchor: fromAttachment ? `${n.attachments[0]?.filename ?? "file"} · p.1` : null,
    };
  });
  return { degraded, results };
}

export const mockFetch: typeof fetch = async (input, init) => {
  await sleep();
  const url = new URL(String(input instanceof Request ? input.url : input), "http://mock");
  const path = url.pathname;
  const method = (init?.method ?? "GET").toUpperCase();

  if (path === "/api/auth/session") return new Response(null, { status: 204 });
  if (path === "/api/auth/me") return json(PRINCIPAL);

  if (path === "/api/notes" && method === "GET") {
    const limit = Number(url.searchParams.get("limit") ?? "50");
    const before = url.searchParams.get("before");
    let pool = [...notes].sort((a, b) => b.created_at.localeCompare(a.created_at));
    if (before) pool = pool.filter((n) => n.created_at < before);
    const page = pool.slice(0, limit);
    const last = page[page.length - 1];
    return json({ notes: page, next_cursor: pool.length > limit && last ? last.created_at : null });
  }

  if (path === "/api/notes" && method === "POST") {
    const body = JSON.parse(String(init?.body)) as {
      client_id: string;
      domain?: string;
      destination?: string | null;
      body: string;
      latitude?: number;
      longitude?: number;
      accuracy_m?: number;
    };
    const domain = body.domain ?? "general";
    if (!VALID_DOMAINS.has(domain)) return json({ detail: "unknown domain" }, 400);
    const existing = notes.find((n) => n.client_id === body.client_id);
    if (existing) return json(existing, 201);
    const note = seedNote(
      domain,
      body.destination ?? null,
      body.body,
      new Date().toISOString(),
      [],
      "pending",
    );
    note.client_id = body.client_id;
    note.latitude = body.latitude ?? null;
    note.longitude = body.longitude ?? null;
    note.accuracy_m = body.accuracy_m ?? null;
    notes.push(note);
    return json(note, 201);
  }

  const noteMatch = path.match(/^\/api\/notes\/([^/]+)$/);
  if (noteMatch && method === "PATCH") {
    const note = notes.find((n) => n.id === decodeURIComponent(noteMatch[1] ?? ""));
    if (!note) return json({ detail: "note not found" }, 404);
    const patch = JSON.parse(String(init?.body)) as {
      body?: string;
      domain?: string;
      destination?: string | null;
    };
    if (patch.domain !== undefined && !VALID_DOMAINS.has(patch.domain))
      return json({ detail: "unknown domain" }, 400);
    if (patch.body !== undefined) note.body = patch.body;
    if (patch.domain !== undefined) note.domain = patch.domain;
    if ("destination" in patch) note.destination = patch.destination ?? null;
    // Mirrors the real PATCH: edits re-trigger ingestion.
    note.ingest_state = "pending";
    return json(note);
  }

  if (noteMatch && method === "DELETE") {
    const index = notes.findIndex((n) => n.id === decodeURIComponent(noteMatch[1] ?? ""));
    if (index < 0) return json({ detail: "note not found" }, 404);
    notes.splice(index, 1);
    return new Response(null, { status: 204 });
  }

  if (path === "/api/search" && method === "GET") {
    return json(mockSearch(url.searchParams));
  }

  const attachMatch = path.match(/^\/api\/notes\/([^/]+)\/attachments$/);
  if (attachMatch && method === "POST") {
    const note = notes.find((n) => n.id === decodeURIComponent(attachMatch[1] ?? ""));
    if (!note) return json({ detail: "unknown note" }, 404);
    const file = init?.body instanceof FormData ? init.body.get("file") : null;
    if (!(file instanceof Blob)) return json({ detail: "missing file" }, 422);
    const filename = file instanceof File ? file.name : "upload.bin";
    const att: AttachmentOut = {
      id: id("att"),
      filename,
      media_type: file.type || "application/octet-stream",
      size_bytes: file.size,
    };
    attachmentBlobs.set(att.id, file);
    note.attachments.push(att);
    return json(att, 201);
  }

  const blobMatch = path.match(/^\/api\/attachments\/([^/]+)$/);
  if (blobMatch && method === "DELETE") {
    const attId = decodeURIComponent(blobMatch[1] ?? "");
    const owner = notes.find((n) => n.attachments.some((a) => a.id === attId));
    if (!owner) return json({ detail: "unknown attachment" }, 404);
    owner.attachments = owner.attachments.filter((a) => a.id !== attId);
    owner.ingest_state = "pending";
    attachmentBlobs.delete(attId);
    return new Response(null, { status: 204 });
  }
  if (blobMatch) {
    const blob = attachmentBlobs.get(decodeURIComponent(blobMatch[1] ?? ""));
    if (!blob) return json({ detail: "unknown attachment" }, 404);
    return new Response(blob, { status: 200, headers: { "Content-Type": blob.type } });
  }

  {
    const noteMatch = path.match(/^\/api\/notes\/([^/]+)$/);
    if (noteMatch && (!init?.method || init.method === "GET")) {
      const note = notes.find((n) => n.id === decodeURIComponent(noteMatch[1] ?? ""));
      return note ? json(note) : json({ detail: "note not found" }, 404);
    }
  }
  const analysisMatch = path.match(/^\/api\/notes\/([^/]+)\/analysis$/);
  if (analysisMatch && method === "GET") {
    const noteId = decodeURIComponent(analysisMatch[1] ?? "");
    const note = notes.find((n) => n.id === noteId);
    if (!note) return json({ detail: "note not found" }, 404);
    // Notes without an analysis fixture read as not-yet-analyzed.
    return json(ANALYSES[noteId] ?? emptyAnalysis(noteId));
  }

  const entityMatch = path.match(/^\/api\/entities\/([^/]+)$/);
  if (entityMatch && method === "GET") {
    const entity = ENTITIES[decodeURIComponent(entityMatch[1] ?? "")];
    return entity ? json(entity) : json({ detail: "entity not found" }, 404);
  }

  if (path === "/api/review" && method === "GET") {
    const status = url.searchParams.get("status") ?? "open";
    const items = REVIEW_ITEMS.filter((item) => (status === "open" ? item.open : !item.open));
    return json({ items: items.map(({ open: _open, ...item }) => item) });
  }

  const resolveMatch = path.match(/^\/api\/review\/([^/]+)\/resolve$/);
  if (resolveMatch && method === "POST") {
    const item = REVIEW_ITEMS.find((r) => r.id === decodeURIComponent(resolveMatch[1] ?? ""));
    if (!item) return json({ detail: "review item not found" }, 404);
    if (!item.open) return json({ detail: "review item is not open" }, 409);
    const body = JSON.parse(String(init?.body)) as { action: string; payload?: object };
    // Mirror the backend's contract: only the actions the payload advertises
    // (plus dismiss) resolve; anything else is a 400, the item untouched.
    const advertised = new Set(["dismiss"]);
    const choices = item.payload.choices;
    if (Array.isArray(choices)) {
      for (const choice of choices) {
        const action = (choice as { action?: unknown }).action;
        if (typeof action === "string") advertised.add(action);
      }
    }
    const outcomes = item.payload.outcomes;
    if (outcomes !== null && typeof outcomes === "object") {
      for (const verb of ["accept", "reject"]) {
        if (verb in outcomes) advertised.add(verb);
      }
    }
    if (!advertised.has(body.action)) {
      return json({ detail: `action ${body.action} is not valid for kind ${item.kind}` }, 400);
    }
    // Resolution mutates fixture state so the triage flow works end-to-end.
    item.open = false;
    item.payload = { ...item.payload, resolution: body.action };
    const { open: _open, ...out } = item;
    return json(out);
  }

  if (path === "/api/ops/llm-usage") return json(LLM_USAGE);

  if (path === "/api/ops/update" && init?.method === "POST") {
    mockUpdate.state = "running";
    mockUpdate.ticks = 0;
    return json({ updater: "jbrain-updater-mock" }, 202);
  }
  if (path === "/api/ops/update/status") {
    if (mockUpdate.state === "running" && ++mockUpdate.ticks >= 3) {
      mockUpdate.state = "exited";
    }
    return json({
      state: mockUpdate.state,
      exit_code: mockUpdate.state === "exited" ? 0 : null,
      log_tail:
        mockUpdate.state === "none"
          ? ""
          : `[update] starting\n[update] building images\n${
              mockUpdate.state === "exited" ? "[update] complete" : ""
            }`,
    });
  }
  if (path === "/api/ops/export" && init?.method === "POST") {
    mockExport.state = "running";
    mockExport.ticks = 0;
    return json({ oneshot: "jbrain-export-mock" }, 202);
  }
  if (path === "/api/ops/export/status") {
    if (mockExport.state === "running" && ++mockExport.ticks >= 3) {
      mockExport.state = "exited";
    }
    const done = mockExport.state === "exited";
    return json({
      state: mockExport.state,
      exit_code: done ? 0 : null,
      log_tail: mockExport.state === "none" ? "" : "[export] dumping database",
      filename: done ? "export-20260610-133800.jbrain.tar" : null,
    });
  }
  if (path.startsWith("/api/ops/export/file/")) {
    return new Response(new Blob(["mock export archive"]), { status: 200 });
  }
  if (path === "/api/ops/import/upload" && init?.method === "POST") {
    return json({ archive: "import-20260610-134500.jbrain.tar" }, 201);
  }
  if (path === "/api/ops/import/start" && init?.method === "POST") {
    mockImport.state = "running";
    mockImport.ticks = 0;
    return json({ oneshot: "jbrain-import-mock" }, 202);
  }
  if (path === "/api/ops/import/status") {
    if (mockImport.state === "running" && ++mockImport.ticks >= 4) {
      mockImport.state = "exited";
    }
    return json({
      state: mockImport.state,
      exit_code: mockImport.state === "exited" ? 0 : null,
      log_tail:
        mockImport.state === "none"
          ? ""
          : `[import] safety backup of current data\n[import] restoring database\n${
              mockImport.state === "exited" ? "[import] complete" : ""
            }`,
    });
  }
  if (path === "/api/ops/metrics") {
    return json({
      mem_total_bytes: 4 * 2 ** 30,
      mem_available_bytes: 1.2 * 2 ** 30,
      swap_total_bytes: 2 * 2 ** 30,
      swap_free_bytes: 1.9 * 2 ** 30,
      disk_total_bytes: 40 * 2 ** 30,
      disk_free_bytes: 24 * 2 ** 30,
      load_1m: 0.42,
      load_5m: 0.31,
      load_15m: 0.2,
      uptime_seconds: 3 * 86400 + 7 * 3600,
      containers: CONTAINERS.map((c, i) => ({
        service: c.service,
        mem_bytes: (i + 1) * 90 * 2 ** 20,
      })),
      db: {
        db_size_bytes: 38 * 2 ** 20,
        note_count: 47,
        attachment_count: 9,
        attachment_bytes: 21 * 2 ** 20,
      },
      blobs: { file_count: 9, total_bytes: 21 * 2 ** 20 },
    });
  }
  if (path === "/api/ops/status") return json({ containers: CONTAINERS });
  if (path === "/api/ops/restart") return new Response(null, { status: 204 });
  if (path.startsWith("/api/ops/logs/")) {
    const lines = Array.from(
      { length: 40 },
      (_, i) => `${new Date().toISOString()} mock log line ${i + 1}`,
    );
    return new Response(lines.join("\n"), { status: 200 });
  }

  return json({ detail: `mock: no route for ${method} ${path}` }, 404);
};
