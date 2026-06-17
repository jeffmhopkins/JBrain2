import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReviewItem } from "../api/client";
import { ReviewScreen } from "./ReviewScreen";

// Pending lane: two collisions (accept_a/accept_b choices, before→after diff),
// a merge (accept/reject outcomes), and an ambiguous mention (reject only — the
// case that used to be a dead end). Confidence drives the bulk suggestion.
const PENDING: ReviewItem[] = [
  {
    id: "c1",
    kind: "attribute_collision",
    domain: "general",
    created_at: "2026-06-10T09:45:00Z",
    status: "open",
    resolution: null,
    resolved_at: null,
    payload: {
      fact_a: "fact-old",
      fact_b: "fact-new",
      predicate: "birthDate",
      summary: "two values recorded for Sarah's birthDate",
      rationale: "a card dates Sarah's birthday differently than the wiki.",
      confidence: 0.86,
      snippet: "card for <mark>Sarah's birthday on the 14th</mark>",
      choices: [
        { action: "accept_a", label: "May 2, 1990", detail: "previously recorded" },
        { action: "accept_b", label: "March 14, 1988", detail: "from this note" },
      ],
    },
  },
  {
    id: "c2",
    kind: "fact_conflict",
    domain: "health",
    created_at: "2026-06-10T08:00:00Z",
    status: "open",
    resolution: null,
    resolved_at: null,
    payload: {
      fact_a: "bp-old",
      fact_b: "bp-new",
      summary: "two blood_pressure values disagree for Me",
      confidence: 0.78,
      snippet: "kiosk says <mark>138/92</mark>",
      choices: [
        { action: "accept_a", label: "128/82 mmHg", detail: "previously recorded" },
        { action: "accept_b", label: "138/92 mmHg", detail: "from this note" },
      ],
    },
  },
  {
    id: "m1",
    kind: "merge_proposal",
    domain: "general",
    created_at: "2026-06-09T12:00:00Z",
    status: "open",
    resolution: null,
    resolved_at: null,
    payload: {
      entity_a: "ent-robert",
      entity_b: "ent-bob",
      summary: "are “Bob” and “Robert Chen” the same person?",
      confidence: 0.69,
      snippet: "Lunch with <mark>Bob</mark>.",
      outcomes: { accept: "they merge.", reject: "a permanent distinct-from edge is written." },
      reject_destructive: true,
    },
  },
  {
    id: "a1",
    kind: "ambiguous_mention",
    domain: "general",
    created_at: "2026-06-09T10:30:00Z",
    status: "open",
    resolution: null,
    resolved_at: null,
    payload: {
      name: "Sam",
      summary: "which Sam?",
      confidence: 0.52,
      snippet: "<mark>Sam</mark> said the roof quote covers the flashing.",
      outcomes: { reject: "the mention stays unlinked." },
    },
  },
];

const DEFERRED: ReviewItem[] = [
  {
    id: "d1",
    kind: "merge_proposal",
    domain: "general",
    created_at: "2026-06-08T09:00:00Z",
    status: "deferred",
    resolved_at: "2026-06-09T09:00:00Z",
    resolution: { action: "discuss", payload: {}, effects: [] },
    payload: { summary: "parked for the assistant", outcomes: { accept: "x", reject: "y" } },
  },
];

const DECIDED: ReviewItem[] = [
  {
    id: "res1",
    kind: "merge_proposal",
    domain: "general",
    created_at: "2026-06-09T10:00:00Z",
    status: "resolved",
    resolved_at: "2026-06-09T11:18:00Z",
    resolution: {
      action: "accept",
      payload: {},
      effects: [{ action: "merged", entity_id: "ent-dup", into: "ent-keep" }],
    },
    payload: {
      summary: "merge “Dr. Patel” with “Dr. Anita Patel”",
      snippet: "booked with <mark>Dr. Patel</mark>.",
      outcomes: { accept: "they become one person.", reject: "writes a distinct-from edge." },
    },
  },
  {
    id: "res2",
    kind: "low_confidence",
    domain: "finance",
    created_at: "2026-06-08T09:00:00Z",
    status: "dismissed",
    resolved_at: "2026-06-08T10:00:00Z",
    resolution: { action: "dismiss", payload: {}, effects: [] },
    payload: { summary: "low-confidence extraction", outcomes: { accept: "x", reject: "y" } },
  },
];

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ReviewScreen (split inbox)", () => {
  const fetchMock = vi.fn<typeof fetch>();
  // The on-demand predicate-suggestions endpoint's reply (the picker fetches it
  // for cards with no baked suggestions). Tests set it before opening a card.
  let predicateSuggestions: { name: string; score: number }[] = [];

  function serve(pending: ReviewItem[], deferred: ReviewItem[], decided: ReviewItem[]) {
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path.endsWith("/predicate-suggestions")) {
        return jsonResponse({ suggestions: predicateSuggestions });
      }
      if (path === "/api/review?status=open") return jsonResponse({ items: pending });
      if (path === "/api/review?status=deferred") return jsonResponse({ items: deferred });
      if (path === "/api/review?status=resolved") return jsonResponse({ items: decided });
      if (path === "/api/notes" && init?.method === "POST") {
        return jsonResponse({ id: "note-new", ...JSON.parse(String(init.body)) });
      }
      if (path === "/api/review/resolve-batch" && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as {
          decisions: { id: string; action: string }[];
        };
        const items = body.decisions.map((d) => ({
          ...(pending.find((p) => p.id === d.id) as ReviewItem),
          status: "resolved" as const,
          resolution: { action: d.action, payload: {}, effects: [] },
        }));
        return jsonResponse({ items, errors: [] });
      }
      if (path.endsWith("/resolve") && init?.method === "POST") {
        const id = path.split("/")[3] ?? "";
        const action = JSON.parse(String(init.body)).action as string;
        const src = pending.find((p) => p.id === id) as ReviewItem;
        const parked = action === "defer" || action === "discuss";
        return jsonResponse({
          ...src,
          status: parked ? "deferred" : "resolved",
          resolved_at: "2026-06-10T10:00:00Z",
          resolution: { action, payload: {}, effects: [] },
        });
      }
      if (path.endsWith("/reopen") && init?.method === "POST") {
        return jsonResponse({ ...DECIDED[0], status: "open", reopen_note: null });
      }
      throw new Error(`Unexpected fetch: ${path}`);
    });
  }

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    predicateSuggestions = [];
    serve(PENDING, DEFERRED, DECIDED);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows the two filter lanes with counts and a browsable list of all pending items", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    expect(screen.getByRole("tab", { name: "pending 4" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "decided 2" })).toBeInTheDocument();
    // Defer is retired — there is no deferred lane.
    expect(screen.queryByRole("tab", { name: /deferred/ })).not.toBeInTheDocument();
    // Browsable: every pending item is listed, not one-at-a-time.
    expect(screen.getByText("are “Bob” and “Robert Chen” the same person?")).toBeInTheDocument();
    expect(screen.getByText("which Sam?")).toBeInTheDocument();
  });

  it("opens a row into a detail with a before→after diff and proposals; back returns", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    fireEvent.click(screen.getByRole("button", { name: /two values recorded for Sarah/ }));
    const diff = screen.getByLabelText("before and after");
    expect(within(diff).getByText("May 2, 1990")).toBeInTheDocument();
    expect(within(diff).getByText("March 14, 1988")).toBeInTheDocument();
    // The proposals to choose among.
    expect(screen.getByRole("button", { name: /March 14, 1988/ })).toBeInTheDocument();
    expect(screen.getByText("1 of 4")).toBeInTheDocument();
    // A collision shows its diff, not the inference card's proposed-fact panel —
    // even though its payload carries a `predicate`.
    expect(screen.queryByLabelText("proposed fact")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "‹ inbox" }));
    expect(screen.getByRole("tab", { name: "pending 4" })).toBeInTheDocument();
  });

  it("prev/next moves between pending items inside the detail", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("button", { name: /two values recorded for Sarah/ }));

    fireEvent.click(screen.getByRole("button", { name: "next" }));
    expect(screen.getByText("two blood_pressure values disagree for Me")).toBeInTheDocument();
    expect(screen.getByText("2 of 4")).toBeInTheDocument();
  });

  it("swipes left/right to carousel between pending items", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("button", { name: /two values recorded for Sarah/ }));

    const detail = screen.getByText(/two values recorded for Sarah/).closest("section");
    expect(detail).not.toBeNull();
    // Swipe left → next item.
    fireEvent.touchStart(detail as Element, { touches: [{ clientX: 240, clientY: 200 }] });
    fireEvent.touchMove(detail as Element, { touches: [{ clientX: 120, clientY: 210 }] });
    expect(screen.getByText("two blood_pressure values disagree for Me")).toBeInTheDocument();
    expect(screen.getByText("2 of 4")).toBeInTheDocument();

    // Swipe right → back to the previous item.
    const detail2 = screen.getByText(/two blood_pressure values/).closest("section");
    fireEvent.touchStart(detail2 as Element, { touches: [{ clientX: 120, clientY: 200 }] });
    fireEvent.touchMove(detail2 as Element, { touches: [{ clientX: 250, clientY: 205 }] });
    expect(screen.getByText("two values recorded for Sarah's birthDate")).toBeInTheDocument();
    expect(screen.getByText("1 of 4")).toBeInTheDocument();
  });

  it("a vertical drag does not carousel (scroll is preserved)", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("button", { name: /two values recorded for Sarah/ }));

    const detail = screen.getByText(/two values recorded for Sarah/).closest("section");
    fireEvent.touchStart(detail as Element, { touches: [{ clientX: 200, clientY: 100 }] });
    fireEvent.touchMove(detail as Element, { touches: [{ clientX: 205, clientY: 300 }] });
    // Still on the first item — a downward drag is for scrolling, not paging.
    expect(screen.getByText("1 of 4")).toBeInTheDocument();
  });

  it("choosing a proposal resolves with its action and raises an undo snackbar", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("button", { name: /two values recorded for Sarah/ }));
    fireEvent.click(screen.getByRole("button", { name: /March 14, 1988/ }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/c1/resolve",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ action: "accept_b", payload: { choice: "March 14, 1988" } }),
        }),
      ),
    );
    // Back in the list, the item is gone and the undo snackbar offers a reversal.
    expect(screen.getByRole("tab", { name: "pending 3" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "undo" })).toBeInTheDocument();
  });

  it("a new_predicate card ranks candidates, previews the edge, and maps on tap", async () => {
    const newPred: ReviewItem = {
      id: "np1",
      kind: "new_predicate",
      domain: "general",
      created_at: "2026-06-10T07:00:00Z",
      status: "open",
      resolution: null,
      resolved_at: null,
      payload: {
        summary: "unknown predicate “marriedTo” — map it or mint it?",
        predicate: "marriedTo",
        subject: "Jeff",
        value: "Celine",
        suggestions: [
          { name: "spouse", score: 0.78 },
          { name: "partner", score: 0.66 },
        ],
        choices: [
          { action: "map_to_existing", label: "spouse", canonical_name: "spouse" },
          { action: "accept_as_new", label: "keep marriedTo as new" },
        ],
      },
    };
    serve([newPred], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/unknown predicate/);
    fireEvent.click(screen.getByRole("button", { name: /unknown predicate/ }));

    // Match strength is shown as a band, never the raw cosine number.
    expect(screen.getByText("strong match")).toBeInTheDocument();
    expect(screen.queryByText(/0\.78/)).not.toBeInTheDocument();
    // The top candidate is the best match and previews the edge it would write.
    const best = screen.getByRole("button", { name: /spouse.*best match/i });
    expect(within(best).getByText("Jeff.spouse → Celine")).toBeInTheDocument();

    fireEvent.click(best);
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/np1/resolve",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            action: "map_to_existing",
            payload: { choice: "spouse", canonical_name: "spouse" },
          }),
        }),
      ),
    );
  });

  it("an inference card shows the proposed fact as predicate → value", async () => {
    const inference: ReviewItem = {
      id: "inf1",
      kind: "low_confidence_inference",
      domain: "general",
      created_at: "2026-06-15T20:33:00Z",
      status: "open",
      resolution: null,
      resolved_at: null,
      payload: {
        entity_ref: "me",
        predicate: "name.nickname",
        qualifier: "",
        statement: "People call me Jeff.",
        value_json: { name: "Jeff" },
        reasons: ["below_threshold"],
        summary: "hold for review (below_threshold): People call me Jeff.",
        outcomes: { accept: "the fact is recorded and pinned.", reject: "the fact is discarded." },
      },
    };
    serve([inference], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/hold for review/);
    fireEvent.click(screen.getByRole("button", { name: /hold for review/ }));

    // The structured proposal — not only the prose summary — so it's clear what
    // approve records: the concise value (from value_json), not the sentence.
    const proposed = screen.getByLabelText("proposed fact");
    expect(within(proposed).getByText("name.nickname")).toBeInTheDocument();
    expect(within(proposed).getByText("Jeff")).toBeInTheDocument();
    // The header names the subject entity (entity_ref "me" → "Me"), so the card
    // says whose fact this is.
    expect(screen.getByText("Me")).toBeInTheDocument();
  });

  it("fills the predicate picker from on-demand suggestions when none are baked in", async () => {
    // A card with no predicate_suggestions in its payload (e.g. filed before the
    // picker existed) fetches them on demand.
    predicateSuggestions = [
      { name: "name.given", score: 0.82 },
      { name: "name.full", score: 0.6 },
    ];
    const inf: ReviewItem = {
      id: "inf-od",
      kind: "low_confidence_inference",
      domain: "general",
      created_at: "2026-06-15T13:00:00Z",
      status: "open",
      resolution: null,
      resolved_at: null,
      payload: {
        entity_ref: "me",
        predicate: "name.nickname",
        qualifier: "",
        statement: "People call me Jeff.",
        value_json: { name: "Jeff" },
        reasons: ["below_threshold"],
        summary: "hold for review (below_threshold): People call me Jeff.",
        outcomes: { accept: "recorded and pinned.", reject: "the fact is discarded." },
      },
    };
    serve([inf], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/hold for review/);
    fireEvent.click(screen.getByRole("button", { name: /hold for review/ }));

    // Open the relation picker; the on-demand suggestions populate the list.
    fireEvent.click(screen.getByRole("button", { name: /name\.nickname/ }));
    expect(await screen.findByRole("button", { name: /name\.given/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /name\.given/ }));
    fireEvent.click(screen.getByRole("button", { name: "approve correction" }));
    const noteCall = await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => String(u) === "/api/notes");
      if (!call) throw new Error("no note filed yet");
      return call;
    });
    const body = JSON.parse(String((noteCall[1] as RequestInit).body)) as { body: string };
    expect(body.body).toContain("relation should be name.given, not name.nickname");
  });

  // Direction C — correct in place. A typed (closed-enum) inference offers its
  // members as chips; approving unchanged records the inference, picking another
  // member files a correction note.
  const genderInference: ReviewItem = {
    id: "inf-g",
    kind: "low_confidence_inference",
    domain: "general",
    created_at: "2026-06-15T13:00:00Z",
    status: "open",
    resolution: null,
    resolved_at: null,
    payload: {
      entity_ref: "celine",
      predicate: "gender",
      qualifier: "",
      statement: "Celine's gender is female.",
      value_json: { value: "female" },
      enum_values: ["male", "female", "unknown"],
      reasons: ["below_threshold"],
      summary: "hold for review (below_threshold): Celine's gender is female.",
      outcomes: { accept: "recorded and pinned.", reject: "the fact is discarded." },
    },
  };

  it("approves a typed inference unchanged, recording it as accept", async () => {
    serve([genderInference], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/hold for review/);
    fireEvent.click(screen.getByRole("button", { name: /hold for review/ }));

    // The enum members render as chips, the proposed one selected; no edit yet.
    expect(screen.getByRole("button", { name: "female", pressed: true })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "approve" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/inf-g/resolve",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ action: "accept", payload: { choice: "approve" } }),
        }),
      ),
    );
  });

  it("picking a different enum member files a correction note instead of accepting", async () => {
    serve([genderInference], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/hold for review/);
    fireEvent.click(screen.getByRole("button", { name: /hold for review/ }));

    // Correct the held value to another member; the primary becomes a correction.
    fireEvent.click(screen.getByRole("button", { name: "male" }));
    fireEvent.click(screen.getByRole("button", { name: "approve correction" }));

    // The fix is a real note in the item's domain (the #7 channel), then the item
    // resolves as corrected linked to it — never a direct value write.
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/notes",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const noteCall = fetchMock.mock.calls.find(([u]) => String(u) === "/api/notes");
    const body = JSON.parse(String((noteCall?.[1] as RequestInit).body)) as {
      domain: string;
      body: string;
    };
    expect(body.domain).toBe("general");
    expect(body.body).toContain("should be male, not female");
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/inf-g/resolve",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"action":"correct"'),
        }),
      ),
    );
  });

  it("a free-text inference value is editable in place before approving", async () => {
    const nick: ReviewItem = {
      ...genderInference,
      id: "inf-n",
      payload: {
        entity_ref: "celine",
        predicate: "name.nickname",
        qualifier: "",
        statement: "People call her Cel.",
        value_json: { name: "Cel" },
        reasons: ["below_threshold"],
        summary: "hold for review (below_threshold): People call her Cel.",
        outcomes: { accept: "recorded and pinned.", reject: "the fact is discarded." },
      },
    };
    serve([nick], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/hold for review/);
    fireEvent.click(screen.getByRole("button", { name: /hold for review/ }));

    // No enum: the value is a tap-to-edit chip. Editing flips approve to a
    // correction without any separate "correct it" detour.
    expect(screen.queryByRole("button", { name: "correct it" })).not.toBeInTheDocument();
    fireEvent.click(within(screen.getByLabelText("proposed fact")).getByText("Cel"));
    fireEvent.change(screen.getByLabelText("corrected value"), { target: { value: "Celery" } });
    fireEvent.click(screen.getByRole("button", { name: "approve correction" }));

    const noteCall = await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => String(u) === "/api/notes");
      if (!call) throw new Error("no note filed yet");
      return call;
    });
    const body = JSON.parse(String((noteCall[1] as RequestInit).body)) as { body: string };
    expect(body.body).toContain("should be Celery, not Cel");
  });

  // After a decision, triage flows item→item: approve/reject opens the next
  // unresolved card rather than dropping back to the inbox list.
  const nickInference: ReviewItem = {
    id: "inf-a",
    kind: "low_confidence_inference",
    domain: "general",
    created_at: "2026-06-15T13:00:00Z",
    status: "open",
    resolution: null,
    resolved_at: null,
    payload: {
      predicate: "name.nickname",
      qualifier: "",
      statement: "People call me Jeff.",
      value_json: { name: "Jeff" },
      reasons: ["below_threshold"],
      summary: "hold for review (below_threshold): People call me Jeff.",
      outcomes: { accept: "recorded.", reject: "discarded." },
    },
  };

  it("advances to the next pending item after approving, not back to the list", async () => {
    serve([nickInference, genderInference], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/People call me Jeff/);
    fireEvent.click(screen.getByRole("button", { name: /People call me Jeff/ }));
    fireEvent.click(screen.getByRole("button", { name: "approve" }));

    // Lands on the next card's detail (its enum chips), still in the detail view
    // — the lane tabs only render in the list, so their absence proves it.
    expect(
      await screen.findByRole("button", { name: "female", pressed: true }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /pending/ })).toBeNull();
  });

  it("advances to the next pending item after rejecting", async () => {
    serve([nickInference, genderInference], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/People call me Jeff/);
    fireEvent.click(screen.getByRole("button", { name: /People call me Jeff/ }));
    fireEvent.click(screen.getByRole("button", { name: /reject/ }));

    expect(
      await screen.findByRole("button", { name: "female", pressed: true }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /pending/ })).toBeNull();
  });

  it("returns to the list when the last pending item is decided", async () => {
    serve([nickInference], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/People call me Jeff/);
    fireEvent.click(screen.getByRole("button", { name: /People call me Jeff/ }));
    fireEvent.click(screen.getByRole("button", { name: "approve" }));

    // Nothing left to advance to: the inbox list (lane tabs) comes back.
    expect(await screen.findByRole("tab", { name: "pending 0" })).toBeInTheDocument();
  });

  it("a confirm_entity card renders its question with approve/reject", async () => {
    const confirm: ReviewItem = {
      id: "ce1",
      kind: "confirm_entity",
      domain: "general",
      created_at: "2026-06-15T20:33:00Z",
      status: "open",
      resolution: null,
      resolved_at: null,
      payload: {
        entity_id: "ent-zane",
        entity_name: "Zane",
        entity_kind: "Person",
        summary: "is this person “Zane” a single, confirmed entity?",
        outcomes: {
          accept: "the entity is confirmed — it survives note deletion and isn't auto-purged.",
          reject: "left provisional — it stays purge-eligible and is never re-proposed.",
        },
      },
    };
    serve([confirm], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/single, confirmed entity/);
    fireEvent.click(screen.getByRole("button", { name: /single, confirmed entity/ }));
    // Renders via the generic summary + outcomes path: an approve and a reject.
    expect(await screen.findByRole("button", { name: /^approve/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^reject/ })).toBeInTheDocument();
  });

  it("an inference card plays back the three-stage trace, with a copyable console", async () => {
    const writeText = vi.fn();
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const inference: ReviewItem = {
      id: "inf3",
      kind: "low_confidence_inference",
      domain: "general",
      created_at: "2026-06-15T20:33:00Z",
      status: "open",
      resolution: null,
      resolved_at: null,
      payload: {
        predicate: "name.full",
        qualifier: "",
        statement: "My full name is Jeffrey Mark Hopkins.",
        value_json: { value: "Jeffrey Mark Hopkins" },
        reasons: ["below_threshold"],
        summary: "hold for review (below_threshold): My full name is Jeffrey Mark Hopkins.",
        outcomes: { accept: "recorded.", reject: "discarded." },
        trace: {
          stages: [
            {
              key: "extraction",
              name: "Extraction",
              version: "note-extract-v16",
              summary: 'candidate state · "Jeffrey Mark Hopkins"',
              rows: [["value", "Jeffrey Mark Hopkins"]],
            },
            {
              key: "integration",
              name: "Integration",
              version: "integrator-v2 · integrate-v7",
              summary: "resolved Me (existing) · inferred true · self 0.85",
              rows: [["inferred", "true"]],
            },
            {
              key: "arbiter",
              name: "Arbiter",
              version: "weight model · deterministic",
              summary: "ceiling 0.60 · weight 0.60 < 0.80 → pending_review",
              rows: [["weight", "min(self 0.85, ceiling 0.60) = 0.60"]],
            },
          ],
        },
      },
    };
    serve([inference], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/hold for review/);
    fireEvent.click(screen.getByRole("button", { name: /hold for review/ }));

    // The trace is collapsed by default; open it, then expand the arbiter stage.
    fireEvent.click(screen.getByRole("button", { name: /how this was decided/ }));
    expect(screen.getByText(/ceiling 0.60 · weight 0.60/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Arbiter/ }));
    expect(screen.getByText("min(self 0.85, ceiling 0.60) = 0.60")).toBeInTheDocument();

    // Show console swaps in the raw log; copy puts the full trace on the clipboard.
    fireEvent.click(screen.getByRole("button", { name: "‹/› show console" }));
    fireEvent.click(screen.getByRole("button", { name: "copy" }));
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(writeText.mock.calls[0]?.[0]).toContain("ARBITER");
    expect(writeText.mock.calls[0]?.[0]).toContain("name.full → Jeffrey Mark Hopkins");
  });

  it("an inference list row shows the proposed fact without opening it", async () => {
    const inference: ReviewItem = {
      id: "inf2",
      kind: "low_confidence_inference",
      domain: "general",
      created_at: "2026-06-15T20:33:00Z",
      status: "open",
      resolution: null,
      resolved_at: null,
      payload: {
        predicate: "name.nickname",
        qualifier: "",
        statement: "People call me Jeff.",
        value_json: { name: "Jeff" },
        summary: "hold for review (below_threshold): People call me Jeff.",
        outcomes: { accept: "recorded.", reject: "discarded." },
      },
    };
    serve([inference], [], []);
    render(<ReviewScreen />);
    // In the list (not the detail), the row carries predicate → value too.
    const row = await screen.findByRole("button", { name: /hold for review/ });
    expect(within(row).getByText("name.nickname")).toBeInTheDocument();
    expect(within(row).getByText("Jeff")).toBeInTheDocument();
  });

  it("a new_predicate card lets you rename the relation yourself via suggest_better", async () => {
    const newPred: ReviewItem = {
      id: "np2",
      kind: "new_predicate",
      domain: "general",
      created_at: "2026-06-10T07:00:00Z",
      status: "open",
      resolution: null,
      resolved_at: null,
      payload: {
        summary: "unknown predicate “marriedTo” — map it or mint it?",
        predicate: "marriedTo",
        subject: "Jeff",
        value: "Celine",
        suggestions: [{ name: "spouse", score: 0.66 }],
        choices: [{ action: "accept_as_new", label: "keep marriedTo as new" }],
      },
    };
    serve([newPred], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/unknown predicate/);
    fireEvent.click(screen.getByRole("button", { name: /unknown predicate/ }));

    // The rename control is gated behind a non-empty name.
    const box = screen.getByLabelText("rename the relation");
    expect(screen.getByRole("button", { name: "use" })).toBeDisabled();
    fireEvent.change(box, { target: { value: "  partner  " } });
    fireEvent.click(screen.getByRole("button", { name: "use" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/np2/resolve",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            action: "suggest_better",
            payload: { canonical_name: "partner" },
          }),
        }),
      ),
    );
  });

  it("an ambiguous mention is never reject-only: correct-it is the way out", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("button", { name: /which Sam\?/ }));

    // correct it is the non-reject escape; defer / talk it over are retired.
    expect(screen.getByRole("button", { name: /leave unlinked/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "correct it" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "defer" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "talk it over" })).not.toBeInTheDocument();
  });

  it("editing the relation via the weighted picker files a predicate correction", async () => {
    const inf: ReviewItem = {
      id: "inf-p",
      kind: "low_confidence_inference",
      domain: "general",
      created_at: "2026-06-15T13:00:00Z",
      status: "open",
      resolution: null,
      resolved_at: null,
      payload: {
        entity_ref: "me",
        predicate: "name.nickname",
        qualifier: "",
        statement: "People call me Jeff.",
        value_json: { name: "Jeff" },
        reasons: ["below_threshold"],
        summary: "hold for review (below_threshold): People call me Jeff.",
        // The ranked candidates the picker offers — weighted by similarity.
        predicate_suggestions: [
          { name: "name.given", score: 0.82 },
          { name: "name.full", score: 0.6 },
        ],
        outcomes: { accept: "recorded and pinned.", reject: "the fact is discarded." },
      },
    };
    serve([inf], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/hold for review/);
    fireEvent.click(screen.getByRole("button", { name: /hold for review/ }));

    // Open the relation picker from the predicate chip: it marks the current
    // relation and ranks the candidates to swap onto.
    fireEvent.click(screen.getByRole("button", { name: /name\.nickname/ }));
    expect(screen.getByText("current")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /name\.given/ }));

    // Editing the relation flips approve to a correction; the note spells out the
    // relation change (the #7 channel — never a direct predicate write).
    fireEvent.click(screen.getByRole("button", { name: "approve correction" }));
    const noteCall = await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => String(u) === "/api/notes");
      if (!call) throw new Error("no note filed yet");
      return call;
    });
    const body = JSON.parse(String((noteCall[1] as RequestInit).body)) as { body: string };
    expect(body.body).toContain("relation should be name.given, not name.nickname");
  });

  it("editing the modality files a correction note spelling out the stance change", async () => {
    const inf: ReviewItem = {
      id: "inf-m",
      kind: "low_confidence_inference",
      domain: "general",
      created_at: "2026-06-15T13:00:00Z",
      status: "open",
      resolution: null,
      resolved_at: null,
      payload: {
        entity_ref: "me",
        predicate: "employer",
        qualifier: "",
        // the modality the pipeline read — the card lets the owner correct it.
        assertion: "asserted",
        statement: "I might join Acme.",
        value_json: { name: "Acme" },
        reasons: ["below_threshold"],
        summary: "hold for review (below_threshold): I might join Acme.",
        outcomes: { accept: "recorded and pinned.", reject: "the fact is discarded." },
      },
    };
    serve([inf], [], []);
    render(<ReviewScreen />);
    await screen.findByText(/hold for review/);
    fireEvent.click(screen.getByRole("button", { name: /hold for review/ }));

    // The modality control shows the current stance; open it and pick another.
    fireEvent.click(screen.getByRole("button", { name: /asserted/ }));
    fireEvent.click(screen.getByRole("button", { name: "hypothetical" }));

    // The edit flips approve to a correction; the note spells out the modality
    // change (the #7 channel — never a direct assertion write).
    fireEvent.click(screen.getByRole("button", { name: "approve correction" }));
    const noteCall = await waitFor(() => {
      const call = fetchMock.mock.calls.find(([u]) => String(u) === "/api/notes");
      if (!call) throw new Error("no note filed yet");
      return call;
    });
    const body = JSON.parse(String((noteCall[1] as RequestInit).body)) as { body: string };
    expect(body.body).toContain("This is hypothetical, not asserted");
  });

  it("correct it files a correction note, then resolves the item as corrected", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("button", { name: /two values recorded for Sarah/ }));
    fireEvent.click(screen.getByRole("button", { name: "correct it" }));

    const box = screen.getByLabelText("correction note");
    fireEvent.change(box, { target: { value: "Correction — her birthday is March 14, 1988." } });
    fireEvent.click(screen.getByRole("button", { name: "file correction" }));

    // First a real note is filed in the item's domain...
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/notes",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"domain":"general"'),
        }),
      ),
    );
    // ...then the item resolves as corrected, linking that note.
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/c1/resolve",
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining('"action":"correct"'),
        }),
      ),
    );
    const correctCall = fetchMock.mock.calls.find((c) => String(c[0]) === "/api/review/c1/resolve");
    expect(String((correctCall?.[1] as RequestInit).body)).toContain('"note_id":"note-new"');
    expect(screen.getByRole("tab", { name: "pending 3" })).toBeInTheDocument();
  });

  it("the high-confidence suggestion bulk-approves via resolve-batch", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    fireEvent.click(screen.getByRole("button", { name: "approve 2 high-confidence" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/resolve-batch",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            decisions: [
              { id: "c1", action: "accept_b", payload: { choice: "March 14, 1988" } },
              { id: "c2", action: "accept_b", payload: { choice: "138/92 mmHg" } },
            ],
          }),
        }),
      ),
    );
    expect(screen.getByRole("tab", { name: "pending 2" })).toBeInTheDocument();
  });

  it("select mode offers bulk approve but no defer", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    fireEvent.click(screen.getByRole("button", { name: "select" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /which Sam/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Bob.*Robert Chen/ }));

    // Defer is retired; bulk approve remains the only batch action.
    expect(screen.queryByRole("button", { name: "defer all" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "approve all" })).toBeInTheDocument();
  });

  it("the decided lane lists decisions and reopen unwinds one", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("tab", { name: "decided 2" }));

    fireEvent.click(screen.getByRole("button", { name: /merge “Dr. Patel”/ }));
    expect(screen.getByText("what was decided")).toBeInTheDocument();
    const chosen = screen.getByText(/become one person/).closest(".offered-row");
    expect(chosen).toHaveClass("chosen");

    // Reopen is armed tap-again.
    fireEvent.click(screen.getByRole("button", { name: /reopen — unwind/ }));
    fireEvent.click(screen.getByRole("button", { name: /tap again — decision unwound/ }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/res1/reopen",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("a decided new_predicate card shows the before→after of the decision", async () => {
    const decided: ReviewItem = {
      id: "np-decided",
      kind: "new_predicate",
      domain: "general",
      created_at: "2026-06-09T10:00:00Z",
      status: "resolved",
      resolved_at: "2026-06-09T11:00:00Z",
      resolution: {
        action: "map_to_existing",
        payload: { canonical_name: "spouse" },
        effects: [{ action: "predicate_remapped", raw: "marriedTo", canonical: "spouse" }],
      },
      payload: {
        summary: "new predicate marriedTo",
        predicate: "marriedTo",
        subject: "Pat",
        value: "Dana",
        suggestions: [{ name: "spouse", score: 0.78 }],
        choices: [{ action: "accept_as_new", label: "keep marriedTo" }],
      },
    };
    serve([], [], [decided]);
    render(<ReviewScreen />);
    await screen.findByText("pending is clear — new items arrive as notes are analyzed.");
    fireEvent.click(screen.getByRole("tab", { name: "decided 1" }));
    fireEvent.click(screen.getByRole("button", { name: /new predicate marriedTo/ }));

    // The outcome is a before→after diff, not a list of re-ticked options.
    expect(screen.getByText("was")).toBeInTheDocument();
    expect(screen.getByText("Mapped to spouse")).toBeInTheDocument();
    expect(screen.getByText("Pat.spouse → Dana")).toBeInTheDocument();
    expect(screen.queryByText("what was decided")).not.toBeInTheDocument();
  });

  it("groups pending items by entity by default, subjectless ones under Other", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    // The ambiguous "Sam" mention names a subject; the collisions and the merge
    // don't, so they collect under Other (which always sorts last).
    expect(screen.getByRole("button", { name: /^Sam 1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Other 3/ })).toBeInTheDocument();
    // Groups start expanded, so every row is still reachable.
    expect(screen.getByText("which Sam?")).toBeInTheDocument();
    expect(screen.getByText("two values recorded for Sarah's birthDate")).toBeInTheDocument();
  });

  it("collapsing an entity group hides only its own rows", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    fireEvent.click(screen.getByRole("button", { name: /^Other 3/ }));
    expect(screen.queryByText("two values recorded for Sarah's birthDate")).toBeNull();
    // Sam's group is independent — still open.
    expect(screen.getByText("which Sam?")).toBeInTheDocument();
  });

  it("group-by time falls back to the flat chronological list", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    fireEvent.click(screen.getByRole("button", { name: "time" }));
    expect(screen.queryByRole("button", { name: /^Other 3/ })).toBeNull();
    expect(screen.getByText("which Sam?")).toBeInTheDocument();
    expect(screen.getByText("two values recorded for Sarah's birthDate")).toBeInTheDocument();
  });

  it("shows per-lane empty states", async () => {
    serve([], [], []);
    render(<ReviewScreen />);
    expect(
      await screen.findByText("pending is clear — new items arrive as notes are analyzed."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "decided 0" }));
    expect(screen.getByText("no decisions yet — resolved items collect here.")).toBeInTheDocument();
  });
});
