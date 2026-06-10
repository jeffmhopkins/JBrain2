import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { SearchOut, SearchResult } from "../api/client";
import { SearchScreen } from "./SearchScreen";

let seq = 0;
function result(overrides: Partial<SearchResult> = {}): SearchResult {
  seq += 1;
  return {
    note_id: `n-${seq}`,
    chunk_id: `c-${seq}`,
    snippet: `plain snippet ${seq}`,
    match: "semantic",
    score: 0.9,
    domain: "general",
    destination: null,
    created_at: new Date().toISOString(),
    body_preview: `preview ${seq}`,
    attachment_count: 0,
    source_kind: "note",
    source_anchor: null,
    ...overrides,
  };
}

function setup(out: SearchOut) {
  const search = vi.fn(async () => out);
  const onOpenResult = vi.fn();
  render(<SearchScreen onOpenResult={onOpenResult} search={search} />);
  return { search, onOpenResult };
}

async function submit(query: string) {
  fireEvent.change(screen.getByLabelText("Search query"), { target: { value: query } });
  fireEvent.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() => expect(screen.queryByText("searching…")).not.toBeInTheDocument());
}

describe("SearchScreen", () => {
  it("shows the pre-query empty state until an explicit submit", () => {
    const { search } = setup({ degraded: false, results: [] });
    expect(screen.getByText("search by meaning or keywords")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search query"), { target: { value: "roof" } });
    expect(search).not.toHaveBeenCalled();
  });

  it("submits with the selected domain filter", async () => {
    const { search } = setup({ degraded: false, results: [] });
    fireEvent.click(screen.getByRole("button", { name: "Medical" }));
    await submit("vitamin");
    expect(search).toHaveBeenCalledWith("vitamin", "health");
    expect(screen.getByText(/nothing matched “vitamin”/)).toBeInTheDocument();
  });

  it("renders mark spans by splitting — markup never reaches innerHTML", async () => {
    setup({
      degraded: false,
      results: [result({ snippet: 'evil <script>alert("x")</script> and <mark>vitamin</mark> D' })],
    });
    await submit("vitamin");

    const marked = screen.getByText("vitamin", { selector: "mark" });
    expect(marked).toHaveClass("snip-mark");
    // The script tag is inert text, not a DOM node.
    expect(
      screen.getByText(/<script>alert\("x"\)<\/script>/, { selector: "span" }),
    ).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
  });

  it("maps match badges: semantic/both ride the steel tint, keyword stays neutral", async () => {
    setup({
      degraded: false,
      results: [
        result({ match: "semantic" }),
        result({ match: "keyword" }),
        result({ match: "both" }),
      ],
    });
    await submit("anything");

    expect(screen.getByText("semantic")).toHaveClass("match-badge", "match-steel");
    expect(screen.getByText("keyword")).toHaveClass("match-badge");
    expect(screen.getByText("keyword")).not.toHaveClass("match-steel");
    const both = screen.getByText("both");
    expect(both).toHaveClass("match-badge", "match-steel");
  });

  it("shows the amber degraded banner on degraded responses", async () => {
    setup({ degraded: true, results: [result({ match: "keyword" })] });
    await submit("anything");
    expect(
      screen.getByText("keyword-only results — semantic search recovering…"),
    ).toBeInTheDocument();
  });

  it("renders the context row: preview, attachment count, source anchor", async () => {
    setup({
      degraded: false,
      results: [
        result({
          body_preview: "the full note preview",
          attachment_count: 2,
          source_anchor: "brokerage-q2.pdf · p.1",
        }),
      ],
    });
    await submit("statement");
    expect(screen.getByText("the full note preview")).toBeInTheDocument();
    expect(document.querySelector(".result-attachments")?.textContent).toContain("2");
    expect(screen.getByText("brokerage-q2.pdf · p.1")).toBeInTheDocument();
  });

  it("opens the note view for a tapped result", async () => {
    const hit = result({ snippet: "tap this result" });
    const { onOpenResult } = setup({ degraded: false, results: [hit] });
    await submit("tap");
    fireEvent.click(
      screen.getByText("tap this result", { selector: "span" }).closest("button") as HTMLElement,
    );
    expect(onOpenResult).toHaveBeenCalledWith(hit);
  });
});
