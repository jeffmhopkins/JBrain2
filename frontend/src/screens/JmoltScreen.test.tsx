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
    ...over,
  };
}

// The history browser loads three lists on mount when registered; stub them empty by
// default so tests that don't care about history don't hit the network.
function stubHistory() {
  vi.spyOn(api, "getMoltbookNights").mockResolvedValue([]);
  vi.spyOn(api, "getMoltbookActions").mockResolvedValue([]);
  vi.spyOn(api, "getMoltbookFiles").mockResolvedValue([]);
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
    await waitFor(() => expect(screen.getByText("Schedule")).toBeInTheDocument());
    expect(screen.getByText(/currently 03:00/)).toBeInTheDocument();

    // Picking a different hour patches night_hour.
    fireEvent.click(screen.getByRole("button", { name: "Wake at 22:00" }));
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
    expect(screen.queryByText("Schedule")).not.toBeInTheDocument();
  });

  it("lists nights and opens a transcript on tap", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookActions").mockResolvedValue([]);
    vi.spyOn(api, "getMoltbookFiles").mockResolvedValue([]);
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
    vi.spyOn(api, "getMoltbookActions").mockResolvedValue([]);
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
});
