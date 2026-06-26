import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { JcodeShareApp } from "./JcodeShareApp";

// The app reads the live location; pin a share link so the boot path runs.
vi.mock("../jcode/share", async (orig) => ({
  ...(await orig<typeof import("../jcode/share")>()),
  parseShareLink: () => ({ sid: "sess-a", token: "tok" }),
}));

// The session screen is heavy (xterm/polling); stub it to assert wiring only.
vi.mock("./JcodeSessionScreen", () => ({
  JcodeSessionScreen: ({ session, shared }: { session: { id: string }; shared?: boolean }) => (
    <div data-testid="session">{`${session.id}:${shared}`}</div>
  ),
}));

const SESSION = {
  id: "sess-a",
  repo: "",
  branch: "main",
  work_branch: "",
  status: "ready",
  title: "",
  archived: false,
  created_at: "",
  last_active_at: "",
};

describe("JcodeShareApp", () => {
  it("redeems the secret, then shows the scoped session in share mode", async () => {
    const redeem = vi.spyOn(api, "jcodeRedeemShare").mockResolvedValue({ session_id: "sess-a" });
    vi.spyOn(api, "jcodeGetSession").mockResolvedValue(SESSION);
    render(<JcodeShareApp />);
    // shows the session, flagged shared, only AFTER the redeem succeeds.
    expect(await screen.findByTestId("session")).toHaveTextContent("sess-a:true");
    expect(redeem).toHaveBeenCalledWith("tok");
  });

  it("shows an invalid-link message when redeem fails", async () => {
    vi.spyOn(api, "jcodeRedeemShare").mockRejectedValue(new Error("401"));
    render(<JcodeShareApp />);
    expect(await screen.findByText(/invalid or has expired/i)).toBeInTheDocument();
    expect(screen.queryByTestId("session")).not.toBeInTheDocument();
  });

  it("strips the secret from the URL up front", async () => {
    const replace = vi.spyOn(window.history, "replaceState");
    vi.spyOn(api, "jcodeRedeemShare").mockResolvedValue({ session_id: "sess-a" });
    vi.spyOn(api, "jcodeGetSession").mockResolvedValue(SESSION);
    render(<JcodeShareApp />);
    // The token is removed from the address bar before any await — never left to linger.
    expect(replace).toHaveBeenCalledWith(null, "", "/jcode/s/sess-a");
    expect(String(replace.mock.calls[0]?.[2])).not.toContain("tok");
  });
});
