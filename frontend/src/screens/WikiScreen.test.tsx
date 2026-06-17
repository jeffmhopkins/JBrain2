import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { WikiArticleOut } from "../api/client";
import { WikiScreen } from "./WikiScreen";

// A trimmed Priya article carrying every structural element the reader must
// render: an infobox field, a lead with an inline [n], a section + nested
// subsection, a bulleted list, a table, and the references the [n]s cite.
const PRIYA: WikiArticleOut = {
  id: "priya-nair",
  title: "Priya Nair",
  subtitle: "Person · pediatrician · machine-written from your notes",
  infobox: {
    title: "Priya Nair",
    photo: true,
    image_url: "/api/wiki/priya-nair/image",
    fields: [
      { label: "Occupation", value: "Pediatrician", citations: [4] },
      { label: "Practice", value: "Nair Pediatrics (2024–)", citations: [9], link: true },
    ],
  },
  lead: [
    {
      kind: "p",
      text: "Priya Nair is a pediatrician and the founder of [Nair Pediatrics](wiki:nair-pediatrics) in [Brookline](wiki:brookline).[9] She is the younger sister of [Jordan Hale](wiki:jordan-hale) and married [Tom](redlink) in 2021.[2]",
    },
  ],
  sections: [
    {
      heading: "Career",
      domain: "general",
      blocks: [
        {
          kind: "p",
          text: "Nair is a pediatrician who trained at a children's clinic before founding her own practice.",
        },
      ],
      subsections: [
        {
          heading: "Training and early career",
          blocks: [
            {
              kind: "p",
              text: "After completing her residency in 2022, Nair worked as a pediatrician at Riverside Children's Clinic.[4]",
            },
          ],
        },
        {
          heading: "Talks and publications",
          blocks: [
            {
              kind: "ul",
              items: ["Co-authored a paper on vaccine hesitancy in JAMA Pediatrics (2023).[16]"],
            },
          ],
        },
      ],
    },
    {
      heading: "Personal life",
      domain: "general",
      blocks: [
        {
          kind: "table",
          header: ["Event", "Year", "Time"],
          rows: [["Boston", "2023", "3:52[5]"]],
        },
      ],
    },
    {
      heading: "Health",
      domain: "health",
      blocks: [{ kind: "p", text: "Nair has a serious peanut allergy and carries an EpiPen.[6]" }],
    },
  ],
  references: [
    {
      n: 2,
      note_id: "note-priya-2",
      meta: "Note · Jun 10, 2019",
      domain: "general",
      snippet: "got into <mark>med school at Johns Hopkins</mark>! my little sister.",
    },
    {
      n: 4,
      note_id: "note-priya-4",
      meta: "Note · Mar 15, 2022",
      domain: "general",
      snippet: "residency, started as a <mark>pediatrician at Riverside</mark>.",
    },
    {
      n: 5,
      note_id: "note-priya-5",
      meta: "Note · Apr 18, 2023",
      domain: "general",
      snippet: "ran the <mark>Boston Marathon — 3:52</mark>!",
    },
    {
      n: 6,
      note_id: "note-priya-6",
      meta: "Note · Nov 20, 2023",
      domain: "health",
      snippet: "carry her EpiPen — <mark>peanut allergy</mark> is serious.",
    },
    {
      n: 9,
      note_id: "note-priya-9",
      meta: "Note · Sep 5, 2024",
      domain: "general",
      snippet: "<mark>left Riverside to open Nair Pediatrics in Brookline</mark>.",
    },
    {
      n: 16,
      note_id: "note-priya-16",
      meta: "Note · Jun 1, 2023",
      domain: "general",
      snippet: "co-authored a <mark>paper on vaccine hesitancy in JAMA Pediatrics</mark>.",
    },
  ],
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("WikiScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();
  const handlers = { onClose: vi.fn() };

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url === "/api/wiki/priya-nair") return jsonResponse(PRIYA);
      if (url === "/api/wiki/priya-nair/corrections" && init?.method === "POST") {
        return jsonResponse({ note_id: "note-x", created: true }, 201);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function setup() {
    render(<WikiScreen articleId="priya-nair" syncStatus="synced" {...handlers} />);
  }

  it("renders the title, the read-only pill, and the prose lead", async () => {
    setup();
    expect(
      await screen.findByRole("heading", { name: "Priya Nair", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Read-only — correct it by discussing/)).toBeInTheDocument();
    expect(screen.getByText(/is a pediatrician and the founder of/)).toBeInTheDocument();
  });

  it("renders the owner profile photo in the infobox when set", async () => {
    setup();
    await screen.findByRole("heading", { name: "Priya Nair", level: 1 });
    const img = screen.getByRole("img", { name: "Priya Nair" });
    expect(img.getAttribute("src")).toBe("/api/wiki/priya-nair/image");
  });

  it("renders in-prose wiki links and red-links", async () => {
    setup();
    await screen.findByRole("heading", { name: "Priya Nair", level: 1 });

    // A live cross-link renders as the link style; a redlink as the muted style.
    const link = screen.getByText("Nair Pediatrics");
    expect(link).toHaveClass("wiki-link");
    const red = screen.getByText("Tom");
    expect(red).toHaveClass("wiki-redlink");
    expect(red).toHaveAttribute("title", "No article yet");
  });

  it("renders type-guided sections, a nested subsection, a list item, and a table cell", async () => {
    setup();
    await screen.findByRole("heading", { name: "Priya Nair", level: 1 });

    // Section (H2) and a nested subsection (H3).
    expect(screen.getByRole("heading", { name: "Career", level: 2 })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Training and early career", level: 3 }),
    ).toBeInTheDocument();

    // The medical section carries its domain label.
    const health = screen.getByRole("heading", { name: /Health/, level: 2 });
    expect(within(health).getByText("medical")).toBeInTheDocument();

    // A bulleted list item and a table cell.
    expect(screen.getByText(/Co-authored a paper on vaccine hesitancy/)).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "Boston" })).toBeInTheDocument();
  });

  it("tapping an inline [n] opens the citation card with its source", async () => {
    setup();
    await screen.findByRole("heading", { name: "Priya Nair", level: 1 });

    // The lead's [9] superscript opens the citation card for reference 9.
    const cites = screen.getAllByRole("button", { name: "Citation 9" });
    fireEvent.click(cites[0] as HTMLElement);

    const card = screen.getByRole("dialog", { name: "Source" });
    expect(within(card).getByText(/Note · Sep 5, 2024/)).toBeInTheDocument();
    expect(within(card).getByText(/open Nair Pediatrics in Brookline/)).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: /Jump to references/ })).toBeInTheDocument();
  });

  it("jumping to references flashes the cited entry, then clears it", async () => {
    vi.useFakeTimers();
    // jsdom has no layout engine, so scrollIntoView is undefined — stub it.
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    try {
      setup();
      // findBy* drives microtasks; advance fake timers so the fetch resolves.
      await vi.waitFor(() =>
        expect(screen.getByRole("heading", { name: "Priya Nair", level: 1 })).toBeInTheDocument(),
      );

      fireEvent.click(screen.getAllByRole("button", { name: "Citation 9" })[0] as HTMLElement);
      fireEvent.click(screen.getByRole("button", { name: /Jump to references/ }));

      // The reference <li> flashes and the matching node is scrolled into view.
      const ref = document.getElementById("wiki-ref-9");
      expect(ref).toHaveClass("wiki-ref-hl");
      expect(scrollIntoView).toHaveBeenCalled();

      // After the flash window the highlight clears (act flushes the timer's
      // state update so React re-renders before we assert).
      act(() => {
        vi.advanceTimersByTime(2200);
      });
      expect(document.getElementById("wiki-ref-9")).not.toHaveClass("wiki-ref-hl");
    } finally {
      vi.useRealTimers();
    }
  });

  it("the References list shows each cited source", async () => {
    setup();
    await screen.findByRole("heading", { name: "Priya Nair", level: 1 });

    const refs = screen.getByRole("heading", { name: "References" }).closest("section");
    expect(within(refs as HTMLElement).getByText(/Note · Apr 18, 2023/)).toBeInTheDocument();
    expect(within(refs as HTMLElement).getByText(/Boston Marathon — 3:52/)).toBeInTheDocument();
  });

  it("the discuss affordance opens the correction sheet", async () => {
    setup();
    await screen.findByRole("heading", { name: "Priya Nair", level: 1 });

    fireEvent.click(screen.getByRole("button", { name: /Discuss this article/ }));
    const sheet = screen.getByRole("dialog", { name: "Discuss this article" });
    expect(within(sheet).getByText(/the wiki stays machine-written/)).toBeInTheDocument();
  });

  it("filing a correction posts it and confirms", async () => {
    setup();
    await screen.findByRole("heading", { name: "Priya Nair", level: 1 });
    fireEvent.click(screen.getByRole("button", { name: /Discuss this article/ }));

    // Submit is disabled until there's text, then it POSTs the correction.
    const submit = screen.getByRole("button", { name: "File correction" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/What's wrong/), {
      target: { value: "She founded Nair Pediatrics, not Riverside." },
    });
    fireEvent.click(screen.getByRole("button", { name: "File correction" }));

    expect(await screen.findByText(/out-argues the conflicting fact/)).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(
      ([u, init]) =>
        String(u) === "/api/wiki/priya-nair/corrections" &&
        (init as RequestInit)?.method === "POST",
    );
    expect(post).toBeTruthy();
    expect(JSON.parse(String((post?.[1] as RequestInit).body)).body).toContain("Nair Pediatrics");
  });

  it("shows the quiet error line when the article fails to load", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 500 }));
    setup();
    expect(
      await screen.findByText("couldn't load this article — reopen to retry."),
    ).toBeInTheDocument();
  });
});
