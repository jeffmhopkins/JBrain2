import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import type { MoltbookSettings } from "../api/client";
import { JmoltScreen } from "./JmoltScreen";

function settings(over: Partial<MoltbookSettings> = {}): MoltbookSettings {
  return {
    key_set: true,
    handle: "jmolt",
    autonomy: false,
    killed: false,
    disclosure: "Autonomous experiment; one hour a night.",
    account_state: "ok",
    verify_fail_streak: 0,
    night_enabled: true,
    night_hour: 3,
    advisory_note: "",
    night_next_run: null,
    night_last_run: null,
    night_running_until: null,
    drip_last_swept: null,
    ...over,
  };
}

// The history browser loads four lists on mount when registered; stub them empty by
// default so tests that don't care about history don't hit the network.
function stubHistory() {
  vi.spyOn(api, "getMoltbookNights").mockResolvedValue([]);
  vi.spyOn(api, "getMoltbookActivity").mockResolvedValue([]);
  vi.spyOn(api, "getMoltbookActivityStats").mockResolvedValue([]);
  vi.spyOn(api, "getMoltbookFiles").mockResolvedValue([]);
  vi.spyOn(api, "getMoltbookJournal").mockResolvedValue([]);
}

afterEach(() => vi.restoreAllMocks());

describe("JmoltScreen", () => {
  it("shows the schedule card with the current wake hour and toggles it", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    stubHistory();
    const update = vi
      .spyOn(api, "updateMoltbookSettings")
      .mockResolvedValue(settings({ night_hour: 22 }));

    render(<JmoltScreen />);

    // The account + schedule cards render once settings load.
    await waitFor(() => expect(screen.getByText("Schedule & drip")).toBeInTheDocument());
    expect(screen.getByText("03:00")).toBeInTheDocument();

    // Picking a different time via the native input patches night_hour; minutes
    // are dropped (night_hour is hour-only).
    fireEvent.change(screen.getByLabelText("Nightly wake hour"), {
      target: { value: "22:30" },
    });
    expect(update).toHaveBeenCalledWith({ night_hour: 22 });
  });

  it("toggles the nightly run off", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    stubHistory();
    const update = vi
      .spyOn(api, "updateMoltbookSettings")
      .mockResolvedValue(settings({ night_enabled: false }));

    render(<JmoltScreen />);
    await waitFor(() => expect(screen.getByLabelText("Nightly run")).toBeInTheDocument());

    fireEvent.click(screen.getByLabelText("Nightly run"));
    expect(update).toHaveBeenCalledWith({ night_enabled: false });
  });

  it("offers the register form when unregistered", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(
      settings({ key_set: false, handle: "" }),
    );
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    stubHistory();

    render(<JmoltScreen />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Register jmolt" })).toBeInTheDocument(),
    );
    // No schedule card before registration.
    expect(screen.queryByText("Schedule & drip")).not.toBeInTheDocument();
  });

  it("lists nights and opens a transcript on tap", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookActivity").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookFiles").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookJournal").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookNights").mockResolvedValue([
      {
        session_id: "s1",
        title: "night",
        at: "2026-08-25T07:00:00Z",
        status: "done",
        stop_reason: null,
        steps: 14,
        cost_tokens: 46125,
        sittings: 2,
      },
    ]);
    const transcript = vi.spyOn(api, "getMoltbookTranscript").mockResolvedValue([
      {
        role: "assistant",
        content: "lurked the general submolt",
        reasoning: "mostly noise so far",
        tools: [],
        at: "2026-08-25T07:04:00Z",
      },
    ]);

    render(<JmoltScreen />);
    // The night row renders with its aggregated run outcome across sittings.
    await waitFor(() => expect(screen.getByText("14 steps")).toBeInTheDocument());
    expect(screen.getByText("2 sittings")).toBeInTheDocument();
    expect(screen.getByText("46.1k tok")).toBeInTheDocument();

    // Tapping the night loads and shows its transcript.
    fireEvent.click(screen.getByRole("button", { name: /14 steps/ }));
    await waitFor(() => expect(screen.getByText("lurked the general submolt")).toBeInTheDocument());
    expect(screen.getByText("mostly noise so far")).toBeInTheDocument();
    expect(transcript).toHaveBeenCalledWith("s1");
  });

  it("reads a notebook file on tap", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookNights").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookActivity").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookJournal").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookFiles").mockResolvedValue([
      { filename: "intro.md", bytes: 1848, updated_at: "2026-08-25T07:04:00Z" },
    ]);
    const readFile = vi
      .spyOn(api, "getMoltbookFileContent")
      .mockResolvedValue({ filename: "intro.md", content: "who I am: a naturalist among agents" });

    render(<JmoltScreen />);
    await waitFor(() => expect(screen.getByText("Notebook")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /intro\.md/ }));
    await waitFor(() => expect(screen.getByText(/naturalist among agents/)).toBeInTheDocument());
    expect(readFile).toHaveBeenCalledWith("intro.md");
  });

  it("shows jmolt's journal entries as inert text", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookNights").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookActivity").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookFiles").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookJournal").mockResolvedValue([
      { content: "quiet night, mostly read", at: "2026-08-25T07:05:00Z" },
    ]);

    render(<JmoltScreen />);
    await waitFor(() => expect(screen.getByText("Journal")).toBeInTheDocument());
    expect(screen.getByText("quiet night, mostly read")).toBeInTheDocument();
  });

  it("edits and saves the advisory note to jmolt", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    stubHistory();
    const update = vi
      .spyOn(api, "updateMoltbookSettings")
      .mockResolvedValue(settings({ advisory_note: "look at the tide-pool submol" }));

    render(<JmoltScreen />);
    await waitFor(() => expect(screen.getByText("Notes to jmolt")).toBeInTheDocument());

    const box = screen.getByRole("textbox", { name: "Note to jmolt" });
    fireEvent.change(box, { target: { value: "look at the tide-pool submol" } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));
    expect(update).toHaveBeenCalledWith({ advisory_note: "look at the tide-pool submol" });
    await waitFor(() => expect(screen.getByText(/Saved/)).toBeInTheDocument());
  });

  it("renders a compact activity row with a status badge and a Moltbook link", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookNights").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookFiles").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookJournal").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookActivityStats").mockResolvedValue([{ kind: "comment", count: 1 }]);
    const list = vi.spyOn(api, "getMoltbookActivity").mockResolvedValue([
      {
        id: "row-1",
        seq: 42,
        kind: "comment",
        state: "published",
        verb: "commented",
        subject: "a sharp reply",
        body: "a sharp reply about continuity",
        link: "https://www.moltbook.com/post/post-abc",
        error: null,
        at: "2026-08-26T07:47:00Z",
      },
    ]);

    render(<JmoltScreen />);
    await waitFor(() => expect(screen.getByText("a sharp reply")).toBeInTheDocument());

    // The row carries its own status badge (distinct from the "Published" filter button) and
    // a real link to the item on Moltbook.
    expect(screen.getByText("Published", { selector: "span.molt-badge" })).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /View on Moltbook/ });
    expect(link).toHaveAttribute("href", "https://www.moltbook.com/post/post-abc");

    // The status segment filters server-side: picking Published refetches with that slice.
    fireEvent.click(screen.getByRole("button", { name: "Published" }));
    await waitFor(() =>
      expect(list).toHaveBeenCalledWith(expect.objectContaining({ status: "published" })),
    );
    // Under the Published segment the per-row badge is redundant (every row is published) and
    // is dropped — so the green "Published" badge no longer shows on the row.
    await waitFor(() =>
      expect(screen.queryByText("Published", { selector: "span.molt-badge" })).toBeNull(),
    );
  });

  it("formats a night's token cost with an M tier", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookActivity").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookActivityStats").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookFiles").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookJournal").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookNights").mockResolvedValue([
      {
        session_id: "s1",
        title: "night",
        at: "2026-08-26T07:00:00Z",
        status: "done",
        stop_reason: null,
        steps: 390,
        cost_tokens: 4_537_300,
        sittings: 12,
      },
    ]);

    render(<JmoltScreen />);
    // 4,537,300 tokens reads as "4.5M tok", not "4537.3k tok".
    await waitFor(() => expect(screen.getByText("4.5M tok")).toBeInTheDocument());
  });

  it("flags a stalled drip when the heartbeat is old, and stays calm when fresh", async () => {
    // The sweep stamps drip_last_swept every ~60s, so an old value = the loop is dead.
    const stale = new Date(Date.now() - 30 * 60_000).toISOString(); // 30 min ago
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings({ drip_last_swept: stale }));
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    stubHistory();

    const { unmount } = render(<JmoltScreen />);
    await waitFor(() => expect(screen.getByText("Schedule & drip")).toBeInTheDocument());
    expect(screen.getByText("Drip stalled")).toBeInTheDocument(); // the status pill
    expect(screen.getByText(/Not sweeping/)).toBeInTheDocument(); // the drip row
    unmount();

    // A fresh heartbeat reads as healthy — no stall.
    const fresh = new Date(Date.now() - 30_000).toISOString(); // 30s ago
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings({ drip_last_swept: fresh }));
    render(<JmoltScreen />);
    await waitFor(() => expect(screen.getByText("Publishing every minute")).toBeInTheDocument());
    expect(screen.queryByText("Drip stalled")).toBeNull();
  });

  it("clamps a long journal entry behind a Show more toggle", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookNights").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookActivity").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookActivityStats").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookFiles").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookJournal").mockResolvedValue([
      { content: `A long reflection. ${"more thinking ".repeat(30)}`, at: "2026-08-25T07:05:00Z" },
    ]);

    render(<JmoltScreen />);
    await waitFor(() => expect(screen.getByText("Journal")).toBeInTheDocument());
    // A long entry starts collapsed with a "Show more" affordance that toggles to "Show less".
    fireEvent.click(screen.getByRole("button", { name: "Show more" }));
    expect(screen.getByRole("button", { name: "Show less" })).toBeInTheDocument();
  });
});
