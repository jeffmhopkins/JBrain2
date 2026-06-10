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

  it("skip cycles the queue client-side, with no resolve call", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    fireEvent.click(screen.getByRole("button", { name: "skip" }));
    expect(screen.getByText(/Lunch with/)).toBeInTheDocument();
    expect(screen.queryByText(/values recorded/)).not.toBeInTheDocument();
    // The merge card's footer verbs come from its advertised outcomes.
    expect(screen.getByText(/they merge/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "accept" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "skip" }));
    expect(screen.getByText("two values recorded for Sarah's birthDate")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("a choice resolves with its advertised action; inbox zero after the last one", async () => {
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

    expect(
      await screen.findByText("inbox zero — new items arrive as notes are analyzed."),
    ).toBeInTheDocument();
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
