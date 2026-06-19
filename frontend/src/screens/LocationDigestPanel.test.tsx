import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { DayTrack, LocationDigest } from "../api/client";
import { LocationDigestPanel } from "./LocationDigestPanel";

function track(day: string, segs: Partial<DayTrack["segments"][number]>[], home = false): DayTrack {
  return {
    day,
    home,
    // Real data means at least one NAMED segment (a gap-only day is "no data").
    has_data: segs.some((s) => (s.place_name ?? null) !== null),
    segments: segs.map((s) => ({
      place_name: s.place_name ?? null,
      start: s.start ?? 0,
      width: s.width ?? 1,
      entered_at: s.entered_at ?? `${day}T00:00:00Z`,
      exited_at: s.exited_at ?? `${day}T12:00:00Z`,
    })),
  };
}

function digest(over: Partial<LocationDigest> = {}): LocationDigest {
  return {
    period: "week",
    since: "2026-06-11T12:00:00Z",
    until: "2026-06-18T12:00:00Z",
    timezone: "UTC",
    days: [
      track(
        "2026-06-16",
        [
          { place_name: "Home", width: 0.5 },
          { place_name: null, width: 0.5 },
        ],
        true,
      ),
      track("2026-06-17", [{ place_name: "Office", width: 1 }]),
    ],
    nights_home: 1,
    nights_total: 7,
    places_visited: 1,
    longest_trip: {
      place_name: "Office",
      day: "2026-06-17",
      entered_at: "2026-06-17T09:00:00Z",
      exited_at: "2026-06-17T17:00:00Z",
      seconds: 8 * 3600,
    },
    seen: [
      {
        place_name: "Office",
        first_seen: "2026-06-17T09:00:00Z",
        last_seen: "2026-06-17T17:00:00Z",
      },
    ],
    computed_at: "2026-06-18T12:00:00Z",
    ...over,
  };
}

describe("LocationDigestPanel", () => {
  it("defaults to the weekly period and renders per-day tracks", async () => {
    const loadDigest = vi.fn().mockResolvedValue(digest());
    render(<LocationDigestPanel deps={{ loadDigest }} />);
    await waitFor(() => expect(screen.getByText(/nights home/i)).toBeInTheDocument());
    // The first call is the weekly default.
    expect(loadDigest).toHaveBeenCalledWith("week");
    // The week pill is selected.
    expect(screen.getByRole("tab", { name: "This week" })).toHaveAttribute("aria-selected", "true");
    // A per-day track row exists per day (by its weekday label + a track aria-label).
    expect(screen.getByLabelText(/Office/)).toBeInTheDocument();
  });

  it("toggles to the nightly period and refetches", async () => {
    const loadDigest = vi.fn().mockResolvedValue(digest());
    render(<LocationDigestPanel deps={{ loadDigest }} />);
    await waitFor(() => expect(loadDigest).toHaveBeenCalledWith("week"));
    fireEvent.click(screen.getByRole("tab", { name: "Last night" }));
    await waitFor(() => expect(loadDigest).toHaveBeenCalledWith("night"));
  });

  it("recomputes on the ↻ affordance (compute-on-read)", async () => {
    const loadDigest = vi.fn().mockResolvedValue(digest());
    render(<LocationDigestPanel deps={{ loadDigest }} />);
    await waitFor(() => expect(loadDigest).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText(/computed just now/i));
    await waitFor(() => expect(loadDigest).toHaveBeenCalledTimes(2));
  });

  it("shows an empty state when no day carried data", async () => {
    const empty = digest({
      days: [track("2026-06-17", [{ place_name: null, width: 1 }])],
      nights_home: 0,
      places_visited: 0,
      longest_trip: null,
      seen: [],
    });
    const loadDigest = vi.fn().mockResolvedValue(empty);
    render(<LocationDigestPanel deps={{ loadDigest }} />);
    await waitFor(() => expect(screen.getByText(/no place activity/i)).toBeInTheDocument());
  });

  it("collapses and expands from the header", async () => {
    const loadDigest = vi.fn().mockResolvedValue(digest());
    render(<LocationDigestPanel deps={{ loadDigest }} />);
    const head = await screen.findByRole("button", { expanded: true });
    fireEvent.click(head);
    expect(screen.getByRole("button", { expanded: false })).toBeInTheDocument();
    // Collapsed → the period toggle is gone.
    expect(screen.queryByRole("tab", { name: "This week" })).not.toBeInTheDocument();
  });

  it("renders names, never coordinates", async () => {
    const withCoordishName = digest({
      days: [track("2026-06-17", [{ place_name: "Office", width: 1 }])],
    });
    const loadDigest = vi.fn().mockResolvedValue(withCoordishName);
    const { container } = render(<LocationDigestPanel deps={{ loadDigest }} />);
    await waitFor(() => expect(screen.getByLabelText(/Office/)).toBeInTheDocument());
    // No latitude/longitude numbers leak into the rendered text.
    expect(container.textContent).not.toMatch(/-?\d{1,3}\.\d{3,}/);
  });
});
