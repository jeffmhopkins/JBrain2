import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { JlaunchSpec } from "../jlaunch/types";
import { JlaunchScreen } from "./JlaunchScreen";

const SPEC = {
  name: "growbot_look",
  title: "GrowBot look",
  est_hours: "2-4 h",
  all_cores: true,
  disk_gb: 3,
  notes: "Stand in place and turn.",
} as unknown as JlaunchSpec;

afterEach(() => vi.restoreAllMocks());

describe("JlaunchScreen", () => {
  it("is called Jobs and starts a run only from the Start button", async () => {
    vi.spyOn(api, "jlaunchSpecs").mockResolvedValue([SPEC]);
    vi.spyOn(api, "jlaunchJobs").mockResolvedValue([]);
    const start = vi.spyOn(api, "jlaunchStart").mockRejectedValue(new Error("stop here"));
    vi.spyOn(window, "alert").mockImplementation(() => {});
    render(<JlaunchScreen onClose={() => {}} />);
    expect(screen.getByRole("heading", { name: /Jobs/ })).toBeTruthy();
    const title = await screen.findByText("GrowBot look");
    fireEvent.click(title);
    fireEvent.click(screen.getByText("Stand in place and turn."));
    expect(start).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Start GrowBot look" }));
    expect(start).toHaveBeenCalledWith("growbot_look");
  });
});
