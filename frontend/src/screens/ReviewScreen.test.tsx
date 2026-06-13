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

  function serve(pending: ReviewItem[], deferred: ReviewItem[], decided: ReviewItem[]) {
    fetchMock.mockImplementation(async (input, init) => {
      const path = String(input);
      if (path === "/api/review?status=open") return jsonResponse({ items: pending });
      if (path === "/api/review?status=deferred") return jsonResponse({ items: deferred });
      if (path === "/api/review?status=resolved") return jsonResponse({ items: decided });
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
    serve(PENDING, DEFERRED, DECIDED);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows three filter lanes with counts and a browsable list of all pending items", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    expect(screen.getByRole("tab", { name: "pending 4" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "deferred 1" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "decided 2" })).toBeInTheDocument();
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

  it("an ambiguous mention is never reject-only: defer and talk-it-over are offered", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("button", { name: /which Sam\?/ }));

    expect(screen.getByRole("button", { name: /leave unlinked/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "defer" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "talk it over" })).toBeInTheDocument();
  });

  it("talk-it-over parks the item with the discuss action", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");
    fireEvent.click(screen.getByRole("button", { name: /which Sam\?/ }));
    fireEvent.click(screen.getByRole("button", { name: "talk it over" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/a1/resolve",
        expect.objectContaining({ body: JSON.stringify({ action: "discuss", payload: {} }) }),
      ),
    );
    expect(screen.getByRole("tab", { name: "pending 3" })).toBeInTheDocument();
    // The parked item now rides in the deferred lane, tagged for the assistant.
    fireEvent.click(screen.getByRole("tab", { name: "deferred 2" }));
    expect(screen.getByText("which Sam?")).toBeInTheDocument();
    expect(screen.getAllByText("with assistant").length).toBeGreaterThanOrEqual(1);
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

  it("select mode lets you defer several at once", async () => {
    render(<ReviewScreen />);
    await screen.findByText("two values recorded for Sarah's birthDate");

    fireEvent.click(screen.getByRole("button", { name: "select" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /which Sam/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Bob.*Robert Chen/ }));
    fireEvent.click(screen.getByRole("button", { name: "defer all" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/review/resolve-batch",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(screen.getByRole("tab", { name: "pending 2" })).toBeInTheDocument();
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

  it("shows per-lane empty states", async () => {
    serve([], [], []);
    render(<ReviewScreen />);
    expect(
      await screen.findByText("pending is clear — new items arrive as notes are analyzed."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "deferred 0" }));
    expect(
      screen.getByText("nothing parked — items you defer or talk over collect here."),
    ).toBeInTheDocument();
  });
});
