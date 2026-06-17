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

// A person whose only worksFor values are FORMER ("I used to work…"): the head
// is a closed interval (valid_to set, valid_from unknown), with no current
// replacement — the case that must not read as present.
const US_ARMY: FactOut = {
  ...DENVER,
  id: "f-army",
  predicate: "worksFor",
  qualifier: null,
  value_json: "US Army",
  status: "active",
  valid_from: null,
  valid_to: "2026-06-01T12:00:00Z",
  temporal_precision: "year",
  object_entity_id: "ent-army",
  object_entity_name: "US Army",
};
const OREGON: FactOut = {
  ...US_ARMY,
  id: "f-oregon",
  value_json: "Oregon Lithoprint",
  object_entity_id: "ent-oregon",
  object_entity_name: "Oregon Lithoprint",
};
const NAME_FULL: FactOut = {
  ...DENVER,
  id: "f-name",
  predicate: "name.full",
  qualifier: null,
  value_json: "Jeff Hopkins",
  status: "active",
  valid_from: null,
  valid_to: null,
  object_entity_id: null,
  object_entity_name: null,
};
const FORMER_ME: EntityOut = {
  id: "ent-me",
  kind: "Person",
  canonical_name: "Me",
  status: "active",
  aliases: ["Jeff Hopkins"],
  domain: "general",
  predicates: [
    { predicate: "name.full", qualifier: null, current: NAME_FULL, history: [NAME_FULL] },
    { predicate: "worksFor", qualifier: null, current: null, history: [US_ARMY, OREGON] },
  ],
  inbound: [],
  mentions: [],
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

  it("a former relationship drops into a collapsed 'previously' group with a vague span", async () => {
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/entities/ent-me") return jsonResponse(FORMER_ME);
      throw new Error(`Unexpected fetch: ${String(input)}`);
    });
    render(<EntityScreen entityId="ent-me" syncStatus="synced" {...handlers} />);
    await screen.findByRole("heading", { name: "Me" });

    // The current value (an open name) stays under Current.
    const current = screen.getByRole("heading", { name: "Current" }).closest("section");
    expect(within(current as HTMLElement).getByText("Jeff Hopkins")).toBeInTheDocument();

    // worksFor's live value has ended → it sits under a "previously" group,
    // collapsed by default, so a job you've left never reads as current.
    const prev = screen.getByRole("button", { name: /previously/ });
    expect(prev).toHaveAttribute("aria-expanded", "false");
    expect(prev).toHaveTextContent("1 former");
    // worksFor is NOT in the Current section.
    expect(within(current as HTMLElement).queryByText("US Army")).not.toBeInTheDocument();
    // The former employer shows a vague tenure span (unknown start), never "— → 2026".
    expect(screen.getByText("US Army")).toBeInTheDocument();
    expect(screen.getByText("until 2026")).toBeInTheDocument();
    expect(screen.queryByText(/—\s*→/)).not.toBeInTheDocument();

    fireEvent.click(prev);
    expect(prev).toHaveAttribute("aria-expanded", "true");
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

    // The inbound row shows the source entity + predicate path, not the prose
    // statement (the source name IS the value for an inbound edge).
    expect(screen.getByText(/sibling/)).toBeInTheDocument();
    expect(screen.queryByText("Sarah is Jeff's sister.")).not.toBeInTheDocument();
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

  it("a negated current head stays visible, flagged 'not currently'", async () => {
    // A live retraction with nothing positive replacing it ("no longer allergic
    // to penicillin") must read as the current state — surfaced explicitly so it
    // never looks like the value is still true (Wave 1, slice 2).
    const notAllergic: FactOut = {
      ...DENVER,
      id: "f-neg",
      predicate: "allergy",
      qualifier: null,
      value_json: "penicillin",
      statement: "no longer allergic to penicillin",
      assertion: "negated",
      status: "active",
      valid_from: null,
      valid_to: null,
      object_entity_id: null,
      object_entity_name: null,
    };
    const me: EntityOut = {
      ...SARAH,
      predicates: [
        { predicate: "allergy", qualifier: null, current: notAllergic, history: [notAllergic] },
      ],
      inbound: [],
      mentions: [],
    };
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/entities/ent-sarah") return jsonResponse(me);
      throw new Error(`Unexpected fetch: ${String(input)}`);
    });
    setup();
    await screen.findByRole("heading", { name: "Sarah Hopkins" });
    const current = screen.getByRole("heading", { name: "Current" }).closest("section");
    expect(within(current as HTMLElement).getByText("penicillin")).toBeInTheDocument();
    expect(within(current as HTMLElement).getByText("not currently")).toBeInTheDocument();
  });

  it("hides an irrealis-only slot from the value view", async () => {
    // A hypothetical "maybe I'll switch to Acme" is not a claim about the
    // present, so it never floors as current and is dropped from the page.
    const maybe: FactOut = {
      ...DENVER,
      id: "f-maybe",
      predicate: "employer",
      qualifier: null,
      value_json: "Acme (maybe)",
      assertion: "hypothetical",
      status: "active",
      valid_from: null,
      valid_to: null,
      object_entity_id: null,
      object_entity_name: null,
    };
    const dreamer: EntityOut = {
      ...SARAH,
      predicates: [{ predicate: "employer", qualifier: null, current: null, history: [maybe] }],
      inbound: [],
      mentions: [],
    };
    fetchMock.mockImplementation(async (input) => {
      if (String(input) === "/api/entities/ent-sarah") return jsonResponse(dreamer);
      throw new Error(`Unexpected fetch: ${String(input)}`);
    });
    setup();
    await screen.findByRole("heading", { name: "Sarah Hopkins" });
    expect(screen.queryByText("Acme (maybe)")).not.toBeInTheDocument();
    // The only slot is irrealis → no current (or former) value section at all.
    expect(screen.queryByRole("heading", { name: "Current" })).not.toBeInTheDocument();
  });

  it("uploads a profile photo and re-renders the image", async () => {
    let uploaded = false;
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/entities/ent-sarah") {
        // After the PUT, the refetched entity carries the new sha → the img renders.
        return jsonResponse(uploaded ? { ...SARAH, image_sha: "img-1" } : SARAH);
      }
      if (url === "/api/entities/ent-sarah/image" && init?.method === "PUT") {
        uploaded = true;
        return jsonResponse({ image_sha: "img-1", media_type: "image/png" });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    setup();
    await screen.findByRole("heading", { name: "Sarah Hopkins" });
    // No image yet: the type icon stands in (no <img>).
    expect(screen.queryByRole("img")).not.toBeInTheDocument();

    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File([new Uint8Array([1, 2, 3])], "p.png", { type: "image/png" });
    fireEvent.change(input, { target: { files: [file] } });

    const img = await screen.findByRole("img");
    expect(img.getAttribute("src")).toContain("/api/entities/ent-sarah/image?v=img-1");
    const put = fetchMock.mock.calls.find(
      ([u, init]) =>
        String(u) === "/api/entities/ent-sarah/image" && (init as RequestInit)?.method === "PUT",
    );
    expect(put).toBeTruthy();
  });

  it("shows the quiet error line when the entity fails to load", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 500 }));
    setup();
    expect(
      await screen.findByText("couldn't load this entity — reopen to retry."),
    ).toBeInTheDocument();
  });
});
