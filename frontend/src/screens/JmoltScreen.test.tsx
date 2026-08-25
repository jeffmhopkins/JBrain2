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

afterEach(() => vi.restoreAllMocks());

describe("JmoltScreen", () => {
  it("shows the schedule card with the current wake hour and toggles it", async () => {
    vi.spyOn(api, "getMoltbookSettings").mockResolvedValue(settings());
    vi.spyOn(api, "getMoltbookOutbox").mockResolvedValue([]);
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

    render(<JmoltScreen />);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Register jmolt" })).toBeInTheDocument(),
    );
    // No schedule card before registration.
    expect(screen.queryByText("Schedule")).not.toBeInTheDocument();
  });
});
