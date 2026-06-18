import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LocationPresence } from "../api/client";
import { PresenceToast } from "./PresenceToast";

function presence(over: Partial<LocationPresence> = {}): LocationPresence {
  return {
    present: true,
    place_name: "Home",
    last_seen: new Date(Date.now() - 4 * 60_000).toISOString(),
    age_seconds: 240,
    stale: false,
    ...over,
  };
}

describe("PresenceToast", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("shows a fresh teal toast 'currently at'", async () => {
    const loadPresence = vi.fn().mockResolvedValue(presence());
    render(<PresenceToast deps={{ loadPresence }} />);
    const toast = await screen.findByRole("status");
    expect(toast).toHaveTextContent(/Currently at: Home/);
    expect(toast.className).toContain("fresh");
    expect(toast.className).not.toContain("stale");
  });

  it("shows a stale amber toast 'last known', never 'here now'", async () => {
    const loadPresence = vi
      .fn()
      .mockResolvedValue(presence({ place_name: "Office", stale: true, age_seconds: 3 * 3600 }));
    render(<PresenceToast deps={{ loadPresence }} />);
    const toast = await screen.findByRole("status");
    expect(toast).toHaveTextContent(/Last known: Office/);
    expect(toast.className).toContain("stale");
    expect(toast).not.toHaveTextContent(/here now/i);
    expect(toast).not.toHaveTextContent(/Currently at/);
  });

  it("renders nothing when there is no usable fix", async () => {
    const loadPresence = vi
      .fn()
      .mockResolvedValue(
        presence({ present: false, place_name: null, last_seen: null, age_seconds: null }),
      );
    render(<PresenceToast deps={{ loadPresence }} />);
    await waitFor(() => expect(loadPresence).toHaveBeenCalled());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("is dismissible via the open action and fires onOpen", async () => {
    const loadPresence = vi.fn().mockResolvedValue(presence());
    const onOpen = vi.fn();
    render(<PresenceToast deps={{ loadPresence }} onOpen={onOpen} />);
    await screen.findByRole("status");
    fireEvent.click(screen.getByRole("button", { name: "open" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("auto-dismisses after a few seconds", async () => {
    const loadPresence = vi.fn().mockResolvedValue(presence());
    render(<PresenceToast deps={{ loadPresence }} />);
    await screen.findByRole("status");
    act(() => vi.advanceTimersByTime(5000));
    await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
  });

  it("carries no coordinate", async () => {
    const loadPresence = vi.fn().mockResolvedValue(presence({ place_name: "Home" }));
    const { container } = render(<PresenceToast deps={{ loadPresence }} />);
    await screen.findByRole("status");
    expect(container.textContent).not.toMatch(/-?\d{1,3}\.\d{3,}/);
  });
});
