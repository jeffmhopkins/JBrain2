import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReviewItem } from "../api/client";
import { ReviewScreen } from "./ReviewScreen";

const ITEMS: ReviewItem[] = [
  {
    id: "rev-1",
    kind: "attribute_collision",
    domain: "general",
    created_at: "2026-06-10T09:45:00Z",
    payload: {
      summary: "two birthdays recorded for Sarah",
      snippet: "Card for Sarah's birthday on <mark>March 14</mark>.",
      outcomes: {
        accept: "the chosen birthday stands; the other is retracted.",
        reject: "both stay pending — nothing publishes until resolved.",
      },
      choices: [
        { action: "keep_fact", label: "March 14, 1988", detail: "from the card note" },
        { action: "split", label: "split into two people", destructive: true },
      ],
    },
  },
  {
    id: "rev-2",
    kind: "merge_proposal",
    domain: "health",
    created_at: "2026-06-09T12:00:00Z",
    payload: {
      summary: "are “Bob” and “Robert Chen” the same person?",
      snippet: "Lunch with <mark>Bob</mark>.",
      outcomes: { accept: "they merge.", reject: "a permanent distinct-from edge is written." },
      reject_destructive: true,
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

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/review?status=open") return jsonResponse({ items: ITEMS });
      if (path.endsWith("/resolve") && init?.method === "POST") {
        return jsonResponse({ ...ITEMS[0], payload: { resolution: "accept" } });
      }
      throw new Error(`Unexpected fetch: ${path}`);
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders one item at a time: dots, kind badge, hero text, outcomes panel", async () => {
    render(<ReviewScreen />);

    expect(await screen.findByText("two birthdays recorded for Sarah")).toBeInTheDocument();
    expect(screen.getByText("attribute collision")).toBeInTheDocument();
    expect(screen.getByText("March 14").closest("mark")).toHaveClass("snip-mark");
    expect(screen.getByText(/the chosen birthday stands/)).toBeInTheDocument();
    expect(screen.getByText(/both stay pending/)).toBeInTheDocument();
    expect(screen.getByLabelText("0 of 2 resolved")).toBeInTheDocument();
    // The second item is queued, not shown.
    expect(screen.queryByText(/Lunch with/)).not.toBeInTheDocument();
  });

  it("skip cycles the queue client-side, with no resolve call", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two birthdays recorded for Sarah");

    fireEvent.click(screen.getByRole("button", { name: "skip" }));
    expect(screen.getByText(/Lunch with/)).toBeInTheDocument();
    expect(screen.queryByText(/birthdays recorded/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "skip" }));
    expect(screen.getByText("two birthdays recorded for Sarah")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("accept resolves the item and advances; inbox zero after the last one", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two birthdays recorded for Sarah");

    fireEvent.click(screen.getByRole("button", { name: "accept" }));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/rev-1/resolve",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    // Optimistic advance: the next card shows immediately, a dot fills.
    expect(screen.getByText(/Lunch with/)).toBeInTheDocument();
    expect(screen.getByLabelText("1 of 2 resolved")).toBeInTheDocument();

    // merge_proposal reject is permanent (distinct_from): it arms first.
    fireEvent.click(screen.getByRole("button", { name: "reject" }));
    expect(screen.queryByText(/Lunch with/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "tap again — permanent" }));

    expect(
      await screen.findByText("inbox zero — new items arrive as notes are analyzed."),
    ).toBeInTheDocument();
  });

  it("destructive choices arm with tap-again and auto-disarm after 3s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      render(<ReviewScreen />);
      expect(await screen.findByText("two birthdays recorded for Sarah")).toBeInTheDocument();

      const split = screen.getByRole("button", { name: /split into two people/ });
      fireEvent.click(split);
      // Armed, not fired.
      expect(screen.getByText("tap again — this is permanent")).toBeInTheDocument();
      expect(fetchMock).toHaveBeenCalledTimes(1);

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

  it("shows the inbox-zero sentence when the queue loads empty", async () => {
    fetchMock.mockImplementation(async () => jsonResponse({ items: [] }));
    render(<ReviewScreen />);
    expect(
      await screen.findByText("inbox zero — new items arrive as notes are analyzed."),
    ).toBeInTheDocument();
  });
});
