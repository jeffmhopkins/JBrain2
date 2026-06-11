import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReviewItem } from "../api/client";
import { ReviewScreen } from "./ReviewScreen";

// Payload shapes mirror what the backend writes: collisions advertise
// accept_a/accept_b choices and no footer verbs; merges advertise the
// accept/reject outcomes. The destructive choice exercises the arming path
// a future split_proposal will advertise.
const ITEMS: ReviewItem[] = [
  {
    id: "rev-1",
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
      snippet: "Card for Sarah's birthday on <mark>March 14</mark>.",
      choices: [
        { action: "accept_a", label: "May 2, 1990", detail: "previously recorded" },
        { action: "accept_b", label: "March 14, 1988", detail: "from this note" },
        { action: "split", label: "split into two people", destructive: true },
      ],
    },
  },
  {
    id: "rev-2",
    kind: "merge_proposal",
    domain: "health",
    created_at: "2026-06-09T12:00:00Z",
    status: "open",
    resolution: null,
    resolved_at: null,
    payload: {
      summary: "are “Bob” and “Robert Chen” the same person?",
      snippet: "Lunch with <mark>Bob</mark>.",
      outcomes: { accept: "they merge.", reject: "a permanent distinct-from edge is written." },
      reject_destructive: true,
    },
  },
];

// The decision log: an accepted merge and a muted dismissal.
const RESOLVED: ReviewItem[] = [
  {
    id: "res-1",
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
      snippet: "follow-up booked with <mark>Dr. Patel</mark> for the 24th.",
      outcomes: {
        accept: "they become one person.",
        reject: "writes a permanent distinct-from edge.",
      },
    },
  },
  {
    id: "res-2",
    kind: "low_confidence",
    domain: "finance",
    created_at: "2026-06-08T09:00:00Z",
    status: "dismissed",
    resolved_at: "2026-06-08T10:00:00Z",
    resolution: { action: "dismiss", payload: {}, effects: [] },
    payload: {
      summary: "low-confidence extraction: “Roth contribution maxed”",
      snippet: "Think the <mark>Roth is maxed</mark> for the year?",
      outcomes: { accept: "the fact stands.", reject: "the extraction is dropped." },
    },
  },
];

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("ReviewScreen", () => {
  const fetchMock = vi.fn<typeof fetch>();

  function serve(open: ReviewItem[], resolved: ReviewItem[]) {
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/review?status=open") return jsonResponse({ items: open });
      if (path === "/api/review?status=resolved") return jsonResponse({ items: resolved });
      if (path.endsWith("/resolve") && init?.method === "POST") {
        return jsonResponse({
          ...ITEMS[0],
          status: "resolved",
          resolved_at: "2026-06-10T10:00:00Z",
          resolution: { action: "accept_b", payload: {}, effects: [] },
        });
      }
      if (path.endsWith("/reopen") && init?.method === "POST") {
        return jsonResponse({
          ...RESOLVED[0],
          status: "open",
          resolved_at: null,
          resolution: { ...RESOLVED[0]?.resolution, reopened_at: "2026-06-10T11:00:00Z" },
          reopen_note: null,
        });
      }
      throw new Error(`Unexpected fetch: ${path}`);
    });
  }

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    serve(ITEMS, RESOLVED);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders one item at a time: dots, kind badge, hero text, choice buttons", async () => {
    render(<ReviewScreen />);

    expect(
      await screen.findByText("two values recorded for Sarah's birthDate"),
    ).toBeInTheDocument();
    expect(screen.getByText("attribute collision")).toBeInTheDocument();
    expect(screen.getByText("March 14").closest("mark")).toHaveClass("snip-mark");
    expect(screen.getByRole("button", { name: /May 2, 1990/ })).toBeInTheDocument();
    // No outcomes advertised: the footer shows no generic accept/reject —
    // they would 400 on this kind; the choices ARE the resolution.
    expect(screen.queryByRole("button", { name: "accept" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "reject" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("0 of 2 resolved")).toBeInTheDocument();
    // The second item is queued, not shown.
    expect(screen.queryByText(/Lunch with/)).not.toBeInTheDocument();
  });

  it("shows live count pills on the open | resolved segments", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    expect(screen.getByRole("tab", { name: "open 2" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "resolved 2" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it("skip cycles the queue client-side, with no resolve call", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    const loads = fetchMock.mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: "skip" }));
    expect(screen.getByText(/Lunch with/)).toBeInTheDocument();
    expect(screen.queryByText(/values recorded/)).not.toBeInTheDocument();
    // The merge card's footer verbs come from its advertised outcomes.
    expect(screen.getByText(/they merge/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "accept" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "skip" }));
    expect(screen.getByText("two values recorded for Sarah's birthDate")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(loads);
  });

  it("a choice resolves with its advertised action; inbox zero counts the log", async () => {
    serve(ITEMS, []);
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    fireEvent.click(screen.getByRole("button", { name: /March 14, 1988/ }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/rev-1/resolve",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            action: "accept_b",
            payload: { choice: "March 14, 1988" },
          }),
        }),
      ),
    );
    // Optimistic advance: the next card shows immediately, a dot fills.
    expect(screen.getByText(/Lunch with/)).toBeInTheDocument();
    expect(screen.getByLabelText("1 of 2 resolved")).toBeInTheDocument();

    // merge_proposal reject is permanent (distinct_from): it arms first.
    fireEvent.click(screen.getByRole("button", { name: "reject" }));
    expect(screen.queryByText(/Lunch with/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "tap again — permanent" }));

    // Inbox zero counts the session's decisions in the resolved segment.
    expect(await screen.findByText(/inbox zero — 2 past decisions in/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "resolved" }));
    expect(screen.getByRole("tab", { name: /resolved/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("two values recorded for Sarah's birthDate")).toBeInTheDocument();
  });

  it("destructive choices arm with tap-again and auto-disarm after 3s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<ReviewScreen />);
      expect(
        await screen.findByText("two values recorded for Sarah's birthDate"),
      ).toBeInTheDocument();

      const split = screen.getByRole("button", { name: /split into two people/ });
      fireEvent.click(split);
      // Armed, not fired.
      expect(screen.getByText("tap again — this is permanent")).toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledTimes(2);

      // The 3s timeout disarms it again.
      await act(() => vi.advanceTimersByTimeAsync(3000));
      expect(screen.queryByText("tap again — this is permanent")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: /split into two people/ })).toBeInTheDocument();

      // Arm + confirm fires the resolve with the choice's action.
      fireEvent.click(screen.getByRole("button", { name: /split into two people/ }));
      fireEvent.click(screen.getByRole("button", { name: "tap again — this is permanent" }));
      await waitFor(() =>
        expect(fetchMock).toHaveBeenCalledWith(
          "/api/review/rev-1/resolve",
          expect.objectContaining({
            method: "POST",
            body: JSON.stringify({
              action: "split",
              payload: { choice: "split into two people" },
            }),
          }),
        ),
      );
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows the bare inbox-zero sentence when there is no history at all", async () => {
    serve([], []);
    render(<ReviewScreen />);
    expect(
      await screen.findByText("inbox zero — new items arrive as notes are analyzed."),
    ).toBeInTheDocument();
  });

  it("the resolved segment lists decisions with dismissals muted", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    fireEvent.click(screen.getByRole("tab", { name: /resolved/ }));
    expect(screen.getByText("merge “Dr. Patel” with “Dr. Anita Patel”")).toBeInTheDocument();
    // Decided lines read as plain language from the chosen option's copy.
    expect(screen.getByText("accept — they become one person.")).toBeInTheDocument();
    expect(screen.getByText("dismissed — skipped without a decision")).toBeInTheDocument();
    const dismissedRow = screen
      .getByText("low-confidence extraction: “Roth contribution maxed”")
      .closest(".rrow");
    expect(dismissedRow).toHaveClass("rrow-dismissed");
    expect(screen.getByText("dismissed")).toHaveClass("chip-dismissed");
  });

  it("a row expands inline into the decision record: evidence + chosen-vs-offered", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("tab", { name: /resolved/ }));

    const row = screen.getByRole("button", { name: /merge “Dr. Patel”/ });
    expect(row).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(row);
    expect(row).toHaveAttribute("aria-expanded", "true");

    expect(screen.getByText("cited evidence")).toBeInTheDocument();
    expect(screen.getByText("Dr. Patel").closest("mark")).toHaveClass("snip-mark");
    expect(screen.getByText("choices offered")).toBeInTheDocument();
    const chosen = screen.getByText("accept — they become one person.", {
      selector: ".offered-row span",
    });
    expect(chosen.closest(".offered-row")).toHaveClass("chosen");
    const offered = screen.getByText("reject — writes a permanent distinct-from edge.");
    expect(offered.closest(".offered-row")).not.toHaveClass("chosen");

    // Collapse on a second tap.
    fireEvent.click(row);
    expect(screen.queryByText("cited evidence")).not.toBeInTheDocument();
  });

  it("reopen is armed tap-again: it re-queues the item and tombstones the row", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("tab", { name: /resolved/ }));
    fireEvent.click(screen.getByRole("button", { name: /merge “Dr. Patel”/ }));

    // The consequence text names the unwind before arming.
    expect(screen.getByText(/unwinds the merge — both entities/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^reopen/ }));
    // Armed, not fired.
    expect(screen.getByText("tap again — back in the queue, decision unwound")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);

    fireEvent.click(
      screen.getByRole("button", { name: "tap again — back in the queue, decision unwound" }),
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/res-1/reopen",
        expect.objectContaining({ method: "POST" }),
      ),
    );

    // The struck-through tombstone stays in the log...
    const row = screen.getByText("merge “Dr. Patel” with “Dr. Anita Patel”").closest(".rrow");
    expect(row).toHaveClass("rrow-reopened");
    expect(screen.getByText("reopened")).toHaveClass("chip-reopened");
    expect(screen.getByText("reopened — waiting in the open queue.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^reopen$/ })).not.toBeInTheDocument();

    // ...and the item is back in the open queue: count pill updates.
    expect(screen.getByRole("tab", { name: "open 3" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "open 3" }));
    fireEvent.click(screen.getByRole("button", { name: "skip" }));
    fireEvent.click(screen.getByRole("button", { name: "skip" }));
    expect(screen.getByText("merge “Dr. Patel” with “Dr. Anita Patel”")).toBeInTheDocument();
  });

  it("the inbox-zero line links into the resolved segment", async () => {
    serve([], RESOLVED);
    render(<ReviewScreen />);
    expect(await screen.findByText(/inbox zero — 2 past decisions in/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "resolved" }));
    expect(screen.getByText("merge “Dr. Patel” with “Dr. Anita Patel”")).toBeInTheDocument();
  });
});
