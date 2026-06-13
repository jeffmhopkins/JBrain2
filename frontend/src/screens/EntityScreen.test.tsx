import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { EntityOut, FactOut } from "../api/client";
import { EntityScreen } from "./EntityScreen";

const DENVER: FactOut = {
  id: "f-denver",
  entity_id: "ent-sarah",
  entity_name: "Sarah",
  predicate: "address",
  qualifier: "home",
  kind: "state",
  statement: "Sarah's home address is in Denver, CO as of June 2026.",
  value_json: "Denver, CO",
  assertion: "asserted",
  status: "pending_review",
  pinned: false,
  confidence: 0.88,
  valid_from: "2026-06-01T12:00:00Z",
  valid_to: null,
  reported_at: "2026-06-10T09:40:00Z",
  temporal_precision: "month",
  object_entity_id: null,
  object_entity_name: null,
  source_snippet: "she's mostly <mark>moved into the new Denver place</mark> now",
};

const AUSTIN: FactOut = {
  ...DENVER,
  id: "f-austin",
  statement: "Sarah's home address was in Austin, TX from 2023 to 2026.",
  value_json: "Austin, TX",
  status: "superseded",
  valid_from: "2023-03-01T12:00:00Z",
  valid_to: "2026-06-01T12:00:00Z",
  reported_at: "2023-03-12T18:20:00Z",
  source_snippet: "moved into the <mark>Austin apartment</mark>",
};

// A machine extraction error: never true, so it must not appear as a value or
// count toward "earlier" — audit-only (hidden from the value view).
const MISREAD: FactOut = {
  ...AUSTIN,
  id: "f-misread",
  statement: "Sarah's home address was in Ostin, TX.",
  value_json: "Ostin, TX",
  status: "retracted",
};

const SARAH: EntityOut = {
  id: "ent-sarah",
  kind: "Person",
  canonical_name: "Sarah Hopkins",
  status: "provisional",
  aliases: ["Sarah", "sis"],
  domain: "general",
  predicates: [
    // History is newest-first per the contract; the sheet keeps it so. The
    // retracted MISREAD is in the chain but must never surface in the value view.
    {
      predicate: "address",
      qualifier: "home",
      current: DENVER,
      history: [DENVER, AUSTIN, MISREAD],
    },
    {
      predicate: "worksFor",
      qualifier: null,
      current: {
        ...DENVER,
        id: "f-job",
        value_json: "Ridgeline Architects",
        status: "active",
        object_entity_id: "ent-ridgeline",
        object_entity_name: "Ridgeline Architects",
      },
      history: [
        {
          ...DENVER,
          id: "f-job",
          value_json: "Ridgeline Architects",
          status: "active",
          object_entity_id: "ent-ridgeline",
          object_entity_name: "Ridgeline Architects",
        },
      ],
    },
  ],
  inbound: [
    { entity_id: "ent-me", name: "Me", predicate: "sibling", statement: "Sarah is Jeff's sister." },
  ],
  mentions: [
    {
      note_id: "n1",
      snippet: "<mark>Sarah</mark> drove me over.",
      created_at: "2026-06-10T09:40:00Z",
    },
  ],
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("EntityScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();
  const handlers = {
    onClose: vi.fn(),
    onOpenEntity: vi.fn(),
    onOpenNote: vi.fn(),
  };

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/entities/ent-sarah") return jsonResponse(SARAH);
      throw new Error(`Unexpected fetch: ${String(input)}`);
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function setup() {
    render(<EntityScreen entityId="ent-sarah" syncStatus="synced" {...handlers} />);
  }

  it("renders the hub: name, kind, provisional chip, aliases, domain", async () => {
    setup();
    expect(await screen.findByRole("heading", { name: "Sarah Hopkins" })).toBeInTheDocument();
    expect(screen.getByText("person")).toBeInTheDocument();
    expect(screen.getByText("provisional")).toBeInTheDocument();
    expect(screen.getByText("also “Sarah”, “sis”")).toBeInTheDocument();
    expect(screen.getByText("general")).toBeInTheDocument();
  });

  it("page is current-only: history collapses behind a disclosure, no inline rail", async () => {
    setup();
    await screen.findByRole("heading", { name: "Sarah Hopkins" });

    // The current value dominates; prior values are NOT in the default footprint.
    expect(screen.getByText("Denver, CO")).toBeInTheDocument();
    expect(screen.queryByText("Austin, TX")).not.toBeInTheDocument();
    expect(screen.queryByRole("list")).not.toBeInTheDocument(); // no inline rail
    // A pending_review current value stays visible — it needs the owner.
    expect(screen.getByText("pending review")).toBeInTheDocument();

    // One prior once-true value (Austin); the retracted MISREAD is not counted.
    const disclosure = screen.getByRole("button", { name: "1 earlier →" });
    // worksFor has a single active fact -> no disclosure for it.
    expect(screen.getAllByRole("button", { name: /earlier →/ })).toHaveLength(1);

    // worksFor's object renders as a link to the org node, not the statement.
    fireEvent.click(screen.getByRole("button", { name: "Ridgeline Architects" }));
    expect(handlers.onOpenEntity).toHaveBeenCalledWith("ent-ridgeline");

    fireEvent.click(disclosure);
  });

  it("history sheet: superseded timeline, retracted excluded, dots cite their note", async () => {
    setup();
    await screen.findByRole("heading", { name: "Sarah Hopkins" });
    fireEvent.click(screen.getByRole("button", { name: "1 earlier →" }));

    const sheet = screen.getByRole("dialog");
    const rail = within(sheet).getByRole("list");
    const dots = within(rail).getAllByRole("listitem");
    // Current + superseded, newest-first; the retracted MISREAD is filtered out.
    expect(dots).toHaveLength(2);
    expect(dots[0]).toHaveTextContent("Denver, CO");
    expect(dots[1]).toHaveTextContent("Austin, TX");
    expect(dots[1]).toHaveTextContent("superseded");
    expect(dots[1]).toHaveClass("fact-superseded");
    // Superseded facts stay true about their interval — the span shows it.
    expect(dots[1]).toHaveTextContent("Mar 2023 → Jun 2026");
    expect(within(sheet).queryByText("Ostin, TX")).not.toBeInTheDocument(); // retracted hidden

    // Each dot cites its note snippet on tap.
    const head = within(rail).getAllByRole("button")[0];
    if (!head) throw new Error("rail fact missing");
    fireEvent.click(head);
    expect(screen.getByText("moved into the new Denver place").closest("mark")).toHaveClass(
      "snip-mark",
    );
  });

  it("inbound edges link to their entity; mentions open the note", async () => {
    setup();
    await screen.findByRole("heading", { name: "Sarah Hopkins" });

    expect(screen.getByText("Sarah is Jeff's sister.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Me" }));
    expect(handlers.onOpenEntity).toHaveBeenCalledWith("ent-me");

    fireEvent.click(screen.getByRole("button", { name: /Sarah drove me over/ }));
    expect(handlers.onOpenNote).toHaveBeenCalledWith("n1");
  });

  it("set-valued relationship: every child renders as its own live edge, none 'earlier'", async () => {
    // children is non-functional: the backend emits one predicate block per
    // kid, each a current edge with its own one-fact history. The page must
    // show all of them — never collapse to one + a misleading "N earlier".
    const child = (id: string, name: string): FactOut => ({
      ...DENVER,
      id,
      predicate: "children",
      qualifier: null,
      kind: "relationship",
      status: "active",
      value_json: null,
      object_entity_id: id,
      object_entity_name: name,
    });
    const summer = child("ent-summer", "Summer Hopkins");
    const harmony = child("ent-harmony", "Harmony Hopkins");
    const dad: EntityOut = {
      ...SARAH,
      predicates: [
        { predicate: "children", qualifier: null, current: summer, history: [summer] },
        { predicate: "children", qualifier: null, current: harmony, history: [harmony] },
      ],
      inbound: [],
    };
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/entities/ent-sarah") return jsonResponse(dad);
      throw new Error(`Unexpected fetch: ${String(input)}`);
    });
    setup();
    await screen.findByRole("heading", { name: "Sarah Hopkins" });

    expect(screen.getByRole("button", { name: "Summer Hopkins" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Harmony Hopkins" })).toBeInTheDocument();
    // No child is demoted to a history disclosure.
    expect(screen.queryByRole("button", { name: /earlier →/ })).not.toBeInTheDocument();
  });

  it("shows the quiet error line when the entity fails to load", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 500 }));
    setup();
    expect(
      await screen.findByText("couldn't load this entity — reopen to retry."),
    ).toBeInTheDocument();
  });
});
