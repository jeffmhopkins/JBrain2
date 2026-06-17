import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { WikiLandingOut } from "../api/client";
import { WikiLandingScreen } from "./WikiLandingScreen";

const LANDING: WikiLandingOut = {
  recent: [
    {
      id: "priya-nair",
      title: "Priya Nair",
      kind: "Person",
      domain: "general",
      blurb: "Pediatrician; founder of Nair Pediatrics.",
      when: "updated 2h ago",
    },
  ],
  hubs: [
    {
      id: "celine-hopkins",
      title: "Celine Hopkins",
      kind: "Person",
      domain: "general",
      blurb: "Software engineer at Globex.",
      links: 12,
    },
  ],
  groups: [
    {
      type: "People",
      entries: [
        {
          id: "priya-nair",
          title: "Priya Nair",
          kind: "Person",
          domain: "general",
          blurb: "Pediatrician; founder of Nair Pediatrics.",
        },
      ],
    },
    {
      type: "Places",
      entries: [
        {
          id: "brookline",
          title: "Brookline",
          kind: "Place",
          domain: "general",
          blurb: "Massachusetts town; where Priya lives.",
        },
      ],
    },
  ],
};

function setup(over?: { load?: () => Promise<WikiLandingOut> }) {
  const load = over?.load ?? vi.fn(async () => LANDING);
  const onOpenArticle = vi.fn();
  render(<WikiLandingScreen onOpenArticle={onOpenArticle} load={load} />);
  return { load, onOpenArticle };
}

describe("WikiLandingScreen", () => {
  it("renders the three rails: recently updated, most connected, browse by type", async () => {
    setup();
    await screen.findByText("Recently updated");
    expect(screen.getByText("Most connected")).toBeInTheDocument();
    expect(screen.getByText("Browse by type")).toBeInTheDocument();

    // The recent card carries the "when" line.
    expect(screen.getByText("updated 2h ago")).toBeInTheDocument();
    // The hub row carries the inbound link count, labelled for a11y.
    expect(screen.getByLabelText("12 links")).toBeInTheDocument();
    // The type groups head with their entry counts.
    expect(screen.getByText("People")).toBeInTheDocument();
    expect(screen.getByText("Places")).toBeInTheDocument();
  });

  it("opens an article when a row is tapped", async () => {
    const { onOpenArticle } = setup();
    await screen.findByText("Most connected");
    fireEvent.click(screen.getByText("Celine Hopkins").closest("button") as HTMLElement);
    expect(onOpenArticle).toHaveBeenCalledWith("celine-hopkins");
  });

  it("filters every rail as you type, and shows the empty line when nothing matches", async () => {
    setup();
    await screen.findByText("Browse by type");

    fireEvent.change(screen.getByLabelText("Search the wiki"), {
      target: { value: "brookline" },
    });
    // Only Brookline survives; the Places group remains, People drops out.
    expect(screen.getByText("Brookline")).toBeInTheDocument();
    expect(screen.queryByText("Celine Hopkins")).not.toBeInTheDocument();
    expect(screen.queryByText("People")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Search the wiki"), {
      target: { value: "zzz no such article" },
    });
    expect(screen.getByText(/nothing matched/)).toBeInTheDocument();
  });

  it("collapses and expands a type group", async () => {
    setup();
    await screen.findByText("Browse by type");

    const peopleHead = screen.getByRole("button", { name: /People/ });
    expect(peopleHead).toHaveAttribute("aria-expanded", "true");
    // Priya appears in both the index and (here) only the People group.
    const group = peopleHead.closest("section") as HTMLElement;
    expect(within(group).getByText("Priya Nair")).toBeInTheDocument();

    fireEvent.click(peopleHead);
    expect(peopleHead).toHaveAttribute("aria-expanded", "false");
    expect(within(group).queryByText("Priya Nair")).not.toBeInTheDocument();
  });

  it("shows the quiet error line when the landing fails to load", async () => {
    setup({ load: vi.fn(async () => Promise.reject(new Error("boom"))) });
    await waitFor(() =>
      expect(screen.getByText("couldn't load the wiki — reopen to retry.")).toBeInTheDocument(),
    );
  });
});
