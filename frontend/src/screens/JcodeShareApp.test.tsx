import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { JcodeShareApp } from "./JcodeShareApp";

// The app reads the live location; pin the share path (+ a secret, by default) so the
// boot path runs. Individual tests override these to model a reload with no secret.
const share = vi.hoisted(() => ({
  path: "sess-a" as string | null,
  link: { sid: "sess-a", token: "tok" } as { sid: string; token: string } | null,
}));
vi.mock("../jcode/share", async (orig) => ({
  ...(await orig<typeof import("../jcode/share")>()),
  parseSharePath: () => share.path,
  parseShareLink: () => share.link,
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
  beforeEach(() => {
    share.path = "sess-a";
    share.link = { sid: "sess-a", token: "tok" };
  });

  it("redeems the secret, then shows the scoped session in share mode", async () => {
    const redeem = vi.spyOn(api, "jcodeRedeemShare").mockResolvedValue({ session_id: "sess-a" });
    vi.spyOn(api, "jcodeGetSession").mockResolvedValue(SESSION);
    render(<JcodeShareApp />);
    // shows the session, flagged shared, only AFTER the redeem succeeds.
    expect(await screen.findByTestId("session")).toHaveTextContent("sess-a:true");
    expect(redeem).toHaveBeenCalledWith("tok");
  });

  it("shows an invalid-link message when redeem AND the cookie both fail", async () => {
    vi.spyOn(api, "jcodeRedeemShare").mockRejectedValue(new Error("401"));
    vi.spyOn(api, "jcodeGetSession").mockRejectedValue(new Error("401"));
    render(<JcodeShareApp />);
    expect(await screen.findByText(/invalid or has expired/i)).toBeInTheDocument();
    expect(screen.queryByTestId("session")).not.toBeInTheDocument();
  });

  it("on reload with no secret, opens via the existing scoped cookie (no redeem)", async () => {
    // The bug fix: after the secret is stripped from the URL, a reload has the path but no
    // token — it must open the session via the redeemed cookie, not redeem (and never drop
    // to the owner login).
    share.link = null;
    const redeem = vi.spyOn(api, "jcodeRedeemShare");
    vi.spyOn(api, "jcodeGetSession").mockResolvedValue(SESSION);
    render(<JcodeShareApp />);
    expect(await screen.findByTestId("session")).toHaveTextContent("sess-a:true");
    expect(redeem).not.toHaveBeenCalled();
  });

  it("an already-claimed link (redeem 401) still opens via the bound cookie", async () => {
    // Single-use: reopening the original link 401s the redeem, but the browser already
    // bound to it keeps access through its cookie.
    vi.spyOn(api, "jcodeRedeemShare").mockRejectedValue(new Error("401"));
    vi.spyOn(api, "jcodeGetSession").mockResolvedValue(SESSION);
    render(<JcodeShareApp />);
    expect(await screen.findByTestId("session")).toHaveTextContent("sess-a:true");
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
